#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx>=0.27",
#     "duckdb>=1.1",
#     "beautifulsoup4>=4.12",
# ]
# ///
"""Fetch Neuralwatt model metadata and per-request energy data, store in DuckDB.

The script:
  1. Fetches GET https://api.neuralwatt.com/v1/models (JSON, public, no auth) and
     writes the response into Neuralwatt's `models` table — one row per model id.
  2. Scrapes https://portal.neuralwatt.com/energy-pricing, locating the table
     under the heading "Average energy per request, by model and request size".
     The table has 7 prompt-size bands as columns (0–256, 256–1k, 1k–4k, 4k–16k,
     16k–64k, 64k–256k, 256k–1M) and one row per model. Each cell exposes, via
     the title attribute on the energy div, the average cache-hit rate that was
     measured for that model in that request-size band; cells with insufficient
     measurements render an em-dash with title "Gathering data…" instead.
  3. Normalizes energy values to milliWatt-hours (mWh) — values in the source
     table are typically mWh but switch to Wh once they exceed 1 Wh; those are
     multiplied by 1000.
  4. Loads into two DuckDB tables in data/pareto.duckdb (shared with AA/models.dev):
       - neuralwatt_models  — one row per API model, with pricing/capabilities/limits
       - neuralwatt_energy  — LONG format: one row per (model_display_name, band), exposing
                              energy_mwh, request_pct, cache_hit_rate_pct, title_text

Both endpoints are cached for 24h. There are no auth or rate-limit concerns for
either endpoint, but caching is still appropriate to keep the HTML report fast.

Usage
-----
  ./fetch_neuralwatt.py                 # full pipeline: fetch API + scrape table
  ./fetch_neuralwatt.py --force-refresh # ignore cache TTL, re-fetch both
  ./fetch_neuralwatt.py --no-scrape     # skip the energy table scrape
  ./fetch_neuralwatt.py --print-table   # pretty-print the parsed energy table
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import httpx
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "pareto.duckdb"

NW_API_URL = "https://api.neuralwatt.com/v1/models"
NW_PORTAL_URL = "https://portal.neuralwatt.com/energy-pricing"

CACHE_TTL_SECONDS = 24 * 3600
REQUEST_TIMEOUT = 30.0

TABLE_HEADING = "Average energy per request, by model and request size"


# --------------------------------------------------------------------------- #
# HTTP fetch with cache
# --------------------------------------------------------------------------- #

def _cache_age_hours(path: Path) -> float | None:
    if not path.exists():
        return None
    return (time.time() - path.stat().st_mtime) / 3600


def _fetch_cached_json(url: str, cache_name: str, *, force: bool = False) -> dict[str, Any]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / cache_name
    if not force and (age := _cache_age_hours(cache_path)) is not None and age * 3600 < CACHE_TTL_SECONDS:
        print(f"Using cached {url} (age {age:.1f}h): {cache_path}")
        return json.loads(cache_path.read_text())
    print(f"Fetching {url} ...")
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers={"accept": "application/json"}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        payload = resp.json()
    cache_path.write_text(json.dumps(payload, indent=2))
    print(f"Wrote cache: {cache_path}")
    return payload


def _fetch_cached_html(url: str, cache_name: str, *, force: bool = False) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / cache_name
    if not force and (age := _cache_age_hours(cache_path)) is not None and age * 3600 < CACHE_TTL_SECONDS:
        print(f"Using cached {url} (age {age:.1f}h): {cache_path}")
        return cache_path.read_text()
    print(f"Fetching {url} ...")
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers={"accept": "text/html"}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        html = resp.text
    cache_path.write_text(html, encoding="utf-8")
    print(f"Wrote cache: {cache_path} ({len(html)} chars)")
    return html


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def parse_energy_value(text: str) -> tuple[float | None, str | None]:
    """Return (mWh, unit_text). Normalizes Wh -> mWh by *1000. None if no number."""
    if not text:
        return None, None
    text = text.strip().replace(",", "")
    m = re.search(r"(-?[\d.]+)\s*(mWh|Wh)", text, re.IGNORECASE)
    if not m:
        return None, None
    value = float(m.group(1))
    unit = m.group(2).lower()
    if unit == "wh":
        return value * 1000.0, "Wh"
    return value, "mWh"


def parse_cache_hit_rate(title: str) -> float | None:
    """Extract the average cache-hit rate percentage from a title attribute."""
    if not title:
        return None
    # "Measured at a 12% average cache-hit rate in this size band."
    m = re.search(r"Measured at a\s+(\d+(?:\.\d+)?)%\s+average cache-hit rate", title)
    if m:
        return float(m.group(1))
    return None


def parse_request_pct(text: str) -> float | None:
    """Extract the percentage number from a "X% of reqs" cell."""
    if not text:
        return None
    m = re.search(r"(-?[\d.]+)%\s*of\s*reqs", text, re.IGNORECASE)
    return float(m.group(1)) if m else None


def parse_energy_table(html: str) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Parse the energy table from the Neuralwatt portal HTML.

    Returns (bands, rows) where each row is:
      {model_display_name, cells: [{band_label, band_order, energy_mwh,
        unit_text, request_pct, cache_hit_rate_pct, title_text, has_data}]}
    """
    soup = BeautifulSoup(html, "html.parser")

    # Find the table by locating the heading first, then walking forward to <table>.
    headings = soup.find_all(string=re.compile(re.escape(TABLE_HEADING)))
    if not headings:
        sys.exit(f"Could not find heading {TABLE_HEADING!r} in the Neuralwatt portal page.")
    heading_tag = headings[0].parent
    table_tag = heading_tag.find_next("table")
    if table_tag is None:
        sys.exit("Found the heading but no <table> after it on the page.")

    # Parse bands from the header row.
    header_cells = table_tag.select("thead th")
    bands: list[dict[str, str]] = []
    for i, th in enumerate(header_cells):
        if i == 0:
            continue  # first column is the model name
        label = th.get_text(strip=True)
        bands.append({"label": label, "order": i})

    # Parse body rows.
    body_rows_raw = table_tag.select("tbody > tr")
    if not body_rows_raw:
        # Some pages render rows without a wrapping tbody; fall back.
        body_rows_raw = table_tag.find_all("tr")[1:]

    rows: list[dict[str, Any]] = []
    for tr in body_rows_raw:
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue  # skip malformed empty rows
        # First cell = model display name.
        model_cell = cells[0]
        model_name = model_cell.get_text(strip=True)
        parsed_cells: list[dict[str, Any]] = []
        for band_idx, band in enumerate(bands):
            cell_idx = band_idx + 1
            if cell_idx >= len(cells):
                parsed_cells.append({
                    "band_label": band["label"], "band_order": band["order"],
                    "energy_mwh": None, "unit_text": None,
                    "request_pct": None, "cache_hit_rate_pct": None,
                    "title_text": None, "has_data": False,
                })
                continue
            cell = cells[cell_idx]
            # The energy div has a title attribute and the "num text-nw-terracotta" class.
            energy_div = cell.select_one("div[title]")
            if energy_div is None:
                # Cell contains no measurement.
                parsed_cells.append({
                    "band_label": band["label"], "band_order": band["order"],
                    "energy_mwh": None, "unit_text": None,
                    "request_pct": None, "cache_hit_rate_pct": None,
                    "title_text": None, "has_data": False,
                })
                continue
            title = energy_div.get("title", "")
            energy_text = energy_div.get_text(strip=True)
            energy_mwh, unit_text = parse_energy_value(energy_text)
            cache_pct = parse_cache_hit_rate(title)
            # The "X% of reqs" line lives in the next sibling text/div.
            cell_text = cell.get_text(" ", strip=True)
            request_pct = parse_request_pct(cell_text)
            has_data = energy_mwh is not None
            parsed_cells.append({
                "band_label": band["label"], "band_order": band["order"],
                "energy_mwh": energy_mwh, "unit_text": unit_text,
                "request_pct": request_pct, "cache_hit_rate_pct": cache_pct,
                "title_text": title, "has_data": has_data,
            })
        rows.append({"model_display_name": model_name, "cells": parsed_cells})

    if not rows:
        sys.exit("Parsed zero rows from the energy table.")
    return bands, rows


# --------------------------------------------------------------------------- #
# DuckDB
# --------------------------------------------------------------------------- #

def init_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS neuralwatt_models (
            id                          VARCHAR PRIMARY KEY,
            object                      VARCHAR,
            created                     TIMESTAMP,
            owned_by                    VARCHAR,
            root                       VARCHAR,
            parent                     VARCHAR,
            max_model_len               BIGINT,
            display_name                VARCHAR,
            description                 VARCHAR,
            provider                   VARCHAR,
            huggingface_id              VARCHAR,
            pricing_input_per_million       DOUBLE,
            pricing_output_per_million      DOUBLE,
            pricing_cached_input_per_million  DOUBLE,
            pricing_cached_output_per_million DOUBLE,
            pricing_currency            VARCHAR,
            pricing_tbd                 BOOLEAN,
            capability_tools            BOOLEAN,
            capability_json_mode        BOOLEAN,
            capability_vision           BOOLEAN,
            capability_reasoning        BOOLEAN,
            capability_reasoning_effort BOOLEAN,
            capability_streaming        BOOLEAN,
            capability_system_role      BOOLEAN,
            capability_developer_role   BOOLEAN,
            limit_max_context_length    BIGINT,
            limit_max_output_tokens     BIGINT,
            limit_max_images            BIGINT,
            deprecated                  BOOLEAN,
            deprecated_message          VARCHAR,
            fetched_at                  TIMESTAMP
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS neuralwatt_energy (
            model_display_name  VARCHAR,
            band_label          VARCHAR,
            band_order          INTEGER,
            energy_mwh          DOUBLE,
            unit_text           VARCHAR,
            request_pct         DOUBLE,
            cache_hit_rate_pct  DOUBLE,
            title_text          VARCHAR,
            has_data            BOOLEAN,
            fetched_at          TIMESTAMP
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS fetch_runs_neuralwatt (
            fetched_at   TIMESTAMP,
            source       VARCHAR,
            items        INTEGER,
            note         VARCHAR
        );
        """
    )


def load_models(con: duckdb.DuckDBPyConnection, payload: dict[str, Any]) -> None:
    fetched_at = datetime.now(timezone.utc)
    items = payload.get("data", [])
    rows: list[tuple] = []
    for m in items:
        md = m.get("metadata") or {}
        pricing = md.get("pricing") or {}
        caps = md.get("capabilities") or {}
        limits = md.get("limits") or {}
        created_val = m.get("created")
        created = datetime.fromtimestamp(created_val, tz=timezone.utc) if isinstance(created_val, (int, float)) else None
        rows.append((
            m.get("id"),
            m.get("object"),
            created,
            m.get("owned_by"),
            m.get("root"),
            m.get("parent"),
            m.get("max_model_len"),
            md.get("display_name"),
            md.get("description"),
            md.get("provider"),
            md.get("huggingface_id"),
            pricing.get("input_per_million"),
            pricing.get("output_per_million"),
            pricing.get("cached_input_per_million"),
            pricing.get("cached_output_per_million"),
            pricing.get("currency"),
            pricing.get("pricing_tbd"),
            caps.get("tools"),
            caps.get("json_mode"),
            caps.get("vision"),
            caps.get("reasoning"),
            caps.get("reasoning_effort"),
            caps.get("streaming"),
            caps.get("system_role"),
            caps.get("developer_role"),
            limits.get("max_context_length"),
            limits.get("max_output_tokens"),
            limits.get("max_images"),
            md.get("deprecated"),
            md.get("deprecated_message"),
            fetched_at,
        ))
    con.execute("DELETE FROM neuralwatt_models")
    if rows:
        con.executemany(
            "INSERT INTO neuralwatt_models VALUES (" + ", ".join(["?"] * 31) + ")",
            rows,
        )
    con.execute(
        "INSERT INTO fetch_runs_neuralwatt VALUES (?, ?, ?, ?)",
        (fetched_at, NW_API_URL, len(rows), "models API"),
    )
    print(f"Loaded {len(rows)} models from Neuralwatt API")


def load_energy(con: duckdb.DuckDBPyConnection, bands: list[dict[str, str]], rows: list[dict[str, Any]]) -> None:
    fetched_at = datetime.now(timezone.utc)
    flat: list[tuple] = []
    populated_cells = 0
    total_cells = 0
    for row in rows:
        for cell in row["cells"]:
            total_cells += 1
            if cell["has_data"]:
                populated_cells += 1
    if populated_cells == 0:
        sys.exit(
            f"Neuralwatt energy table parsed {len(rows)} model rows / {total_cells} cells "
            "but ZERO populated cells — the portal markup likely changed. "
            "Inspect data/cache/neuralwatt_energy_pricing_latest.html and update parse_energy_table()."
        )
    for row in rows:
        for cell in row["cells"]:
            flat.append((
                row["model_display_name"],
                cell["band_label"],
                cell["band_order"],
                cell["energy_mwh"],
                cell["unit_text"],
                cell["request_pct"],
                cell["cache_hit_rate_pct"],
                cell["title_text"],
                cell["has_data"],
                fetched_at,
            ))
    con.execute("DELETE FROM neuralwatt_energy")
    if flat:
        con.executemany(
            "INSERT INTO neuralwatt_energy VALUES (" + ", ".join(["?"] * 10) + ")",
            flat,
        )
    con.execute(
        "INSERT INTO fetch_runs_neuralwatt VALUES (?, ?, ?, ?)",
        (fetched_at, NW_PORTAL_URL, populated_cells, f"energy table ({populated_cells}/{total_cells} cells populated, {len(rows)} models, {len(bands)} bands)"),
    )
    print(f"Loaded {populated_cells}/{total_cells} populated cells across {len(rows)} model rows into neuralwatt_energy")


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def print_energy_table(bands: list[dict[str, str]], rows: list[dict[str, Any]]) -> None:
    print()
    print("Energy table (mWh, cache-hit%, % of reqs):")
    header_cells = ["Model".ljust(24)] + [b["label"].rjust(24) for b in bands]
    print("  " + " ".join(header_cells))
    print("  " + "-" * (24 + 25 * len(bands)))
    for row in rows:
        name_col = row["model_display_name"][:24].ljust(24)
        cells_out = []
        for cell in row["cells"]:
            if not cell["has_data"]:
                cells_out.append("—".rjust(24))
                continue
            energy = f"{cell['energy_mwh']:.1f}mWh"
            ch = f"{cell['cache_hit_rate_pct']}%ch"
            rq = f"{cell['request_pct']}%rq"
            cells_out.append(f"{energy},{ch},{rq}".rjust(24))
        print("  " + name_col + " " + " ".join(cells_out))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force-refresh", action="store_true", help="Ignore cache TTL; re-fetch both endpoints")
    parser.add_argument("--no-scrape", action="store_true", help="Skip the energy-pricing table scrape")
    parser.add_argument("--print-table", action="store_true", help="Pretty-print the parsed energy table")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    init_tables(con)

    # 1. Model metadata API
    api_payload = _fetch_cached_json(NW_API_URL, "neuralwatt_models_api_latest.json", force=args.force_refresh)
    load_models(con, api_payload)

    # 2. Energy-pricing table scrape
    if not args.no_scrape:
        portal_html = _fetch_cached_html(NW_PORTAL_URL, "neuralwatt_energy_pricing_latest.html", force=args.force_refresh)
        bands, rows = parse_energy_table(portal_html)
        print(f"Parsed {len(bands)} bands and {len(rows)} model rows from energy table")
        load_energy(con, bands, rows)
        if args.print_table:
            print_energy_table(bands, rows)

    con.close()
    print(f"\nDuckDB file: {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
