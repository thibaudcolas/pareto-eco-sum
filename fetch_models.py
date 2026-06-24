#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx>=0.27",
#     "duckdb>=1.1",
#     "python-dotenv>=1.0",
# ]
# ///
"""Fetch Artificial Analysis language model data, enrich with models.dev, store in DuckDB.

The script:
  1. Fetches /api/v2/language/models/free from Artificial Analysis (cached 24h, paginated)
  2. Fetches the models.dev catalog (cached 24h)
  3. Loads both into a DuckDB file at data/pareto.duckdb
  4. Joins AA models to models.dev models on a normalized slug
  5. Prints the top 10 models by Artificial Analysis Agentic Index
  6. Renders data/index.html — an interactive report with scatter plots,
     model cards, and a provider directory for open-weight LLMs.

Caching and API budget
-----------------------
The Free tier allows 100 requests/day. Each page of the AA endpoint counts as one
request. The script writes a merged JSON cache to data/cache/language_models_free_latest.json
and reuses it for 24 hours unless --force-refresh is passed. Use --no-fetch to skip the
network entirely and fall back to sample.json.

Usage
-----
  ./fetch_models.py                 # full pipeline: fetch AA, fetch models.dev, HTML
  ./fetch_models.py --no-fetch      # use cached or sample.json, skip AA API
  ./fetch_models.py --force-refresh # ignore cache TTL, re-fetch from both APIs
  ./fetch_models.py --no-modelsdev  # skip models.dev fetch and enrichment
  ./fetch_models.py --no-html       # skip HTML report
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

import duckdb
import httpx
from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "pareto.duckdb"
HTML_PATH = DATA_DIR / "index.html"

AA_BASE_URL = "https://artificialanalysis.ai/api/v2"
AA_ENDPOINT = "/language/models/free"

MODELSDEV_CATALOG_URL = "https://models.dev/catalog.json"
MODELSDEV_LOGOS_BASE = "https://models.dev/logos"

OPENROUTER_PROVIDERS_URL = "https://openrouter.ai/api/v1/providers"
GOOGLE_FAVICONS_URL = "https://www.google.com/s2/favicons?sz=64&domain="

CACHE_TTL_SECONDS = 24 * 3600
REQUEST_TIMEOUT = 30.0
SAMPLE_FILE = SCRIPT_DIR / "sample.json"

OBSERVABLE_PLOT_VERSION = "0.6.16"

# Brand-aligned colors per provider id (from models.dev). Providers not in
# this map fall back to a position in FALLBACK_PALETTE below.
PROVIDER_COLORS: dict[str, str] = {
    "openai": "#10a37f",
    "anthropic": "#d97757",
    "google": "#4285f4",
    "alibaba": "#615ced",
    "alibaba-cn": "#615ced",
    "deepseek": "#4d6bfe",
    "mistral": "#ff7000",
    "meta": "#0866ff",
    "xai": "#e2b85a",
    "nvidia": "#76b900",
    "zhipuai": "#3155d6",
    "xiaomi": "#ff7e3f",
    "moonshotai": "#9b6dff",
    "minimax": "#ff4d4f",
    "cohere": "#39c5bb",
    "amazon-bedrock": "#ff9900",
    "togetherai": "#94a3b8",
    "huggingface": "#ff9d00",
    "ibm": "#0f62fe",
    "databricks": "#ff3621",
    "perplexity": "#20808d",
    "inception": "#a78bfa",
    "liquidai": "#3b82f6",
    "microsoft": "#00a4ef",
}

FALLBACK_PALETTE = [
    "#5b8def", "#f97316", "#10b981", "#a855f7", "#ec4899",
    "#14b8a6", "#eab308", "#6366f1", "#84cc16", "#f43f5e",
    "#06b6d4", "#8b5cf6", "#fb7185", "#22d3ee", "#facc15",
]


# --------------------------------------------------------------------------- #
# API key
# --------------------------------------------------------------------------- #

def load_api_key() -> str:
    load_dotenv(SCRIPT_DIR / ".env")
    key = os.environ.get("AA_API_KEY")
    if not key:
        sys.exit("AA_API_KEY missing. Add it to .env as AA_API_KEY=...")
    return key


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def _cache_age_hours(path: Path) -> float | None:
    if not path.exists():
        return None
    return (time.time() - path.stat().st_mtime) / 3600


def fetch_aa_models(api_key: str, *, force: bool = False) -> tuple[dict[str, Any], Path]:
    """Fetch every page of /language/models/free and cache the merged payload."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / "language_models_free_latest.json"

    if not force and (age := _cache_age_hours(cache_path)) is not None and age * 3600 < CACHE_TTL_SECONDS:
        print(f"Using cached AA response (age {age:.1f}h): {cache_path}")
        return json.loads(cache_path.read_text()), cache_path

    headers = {"x-api-key": api_key, "accept": "application/json"}
    merged_data: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    page = 1
    total_pages = 1

    with httpx.Client(base_url=AA_BASE_URL, timeout=REQUEST_TIMEOUT, headers=headers) as client:
        while page <= total_pages:
            print(f"Fetching AA page {page}/{total_pages if meta else '?'} ...")
            resp = client.get(AA_ENDPOINT, params={"page": page})

            remaining = resp.headers.get("X-RateLimit-Remaining")
            limit = resp.headers.get("X-RateLimit-Limit")
            if remaining is not None:
                print(f"  rate limit: {remaining}/{limit} left today (tier={resp.headers.get('X-AA-Tier')})")

            if resp.status_code == 429:
                retry = resp.headers.get("Retry-After")
                sys.exit(f"AA rate limited (HTTP 429). Retry-After={retry}s.")
            resp.raise_for_status()

            payload = resp.json()
            (CACHE_DIR / f"aa_language_models_free_page{page}_{datetime.now():%Y%m%d_%H%M%S}.json").write_text(
                json.dumps(payload, indent=2)
            )

            if not meta:
                meta = {k: payload.get(k) for k in ("tier", "intelligence_index_version")}
                pagination = payload.get("pagination", {})
                total_pages = pagination.get("total_pages", 1)

            merged_data.extend(payload.get("data", []))
            if not payload.get("pagination", {}).get("has_more"):
                break
            page += 1

    merged = {
        "tier": meta.get("tier"),
        "intelligence_index_version": meta.get("intelligence_index_version"),
        "pagination": {"page": 1, "page_size": len(merged_data), "total_pages": 1, "has_more": False},
        "data": merged_data,
        "_fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_path.write_text(json.dumps(merged, indent=2))
    print(f"Wrote merged AA cache: {cache_path} ({len(merged_data)} models)")
    return merged, cache_path


def fetch_modelsdev_catalog(*, force: bool = False) -> dict[str, Any]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / "modelsdev_catalog_latest.json"

    if not force and (age := _cache_age_hours(cache_path)) is not None and age * 3600 < CACHE_TTL_SECONDS:
        print(f"Using cached models.dev catalog (age {age:.1f}h): {cache_path}")
        return json.loads(cache_path.read_text())

    print("Fetching models.dev catalog...")
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        resp = client.get(MODELSDEV_CATALOG_URL)
        resp.raise_for_status()
        payload = resp.json()

    cache_path.write_text(json.dumps(payload, indent=2))
    counts = {
        "providers": len(payload.get("providers", {})),
        "models": len(payload.get("models", {})),
    }
    print(f"Wrote models.dev cache: {cache_path} ({counts['providers']} providers, {counts['models']} models)")
    return payload


def load_sample() -> tuple[dict[str, Any], Path]:
    if not SAMPLE_FILE.exists():
        sys.exit("No AA cache and no sample.json to fall back on. Run without --no-fetch first.")
    print(f"Loading sample data (no API calls): {SAMPLE_FILE}")
    return json.loads(SAMPLE_FILE.read_text()), SAMPLE_FILE


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def _relative_date(d: date | None) -> str:
    """Return a human label like '3 months ago · 2026-04-22' or '?'."""
    if not d:
        return "?"
    today = date.today()
    delta = (today - d).days
    if delta < 0:
        rel = "upcoming"
    elif delta == 0:
        rel = "today"
    elif delta < 7:
        rel = f"{delta}d ago"
    elif delta < 30:
        rel = f"{delta // 7}w ago"
    elif delta < 365:
        rel = f"{delta // 30}mo ago"
    else:
        rel = f"{delta // 365}y ago"
    return f"{rel} · {d.isoformat()}"


# ISO 3166-1 alpha-2 → emoji flag (regional indicator symbols).
def _country_flag(code: str | None) -> str:
    if not code or len(code) != 2:
        return ""
    code = code.upper()
    if not code.isalpha():
        return ""
    # Regional indicator symbol range: U+1F1E6 for 'A' .. U+1F1FF for 'Z'
    return chr(0x1F1E6 + (ord(code[0]) - ord("A"))) + chr(0x1F1E6 + (ord(code[1]) - ord("A")))


def normalize_slug(slug: str | None) -> str | None:
    """Normalize a models.dev model slug to match AA slugs.

    models.dev uses dots between version digits (``gemini-3.5-flash``), while
    Artificial Analysis uses dashes (``gemini-3-5-flash``). Both are already
    lowercased; we just replace dots with dashes.
    """
    if not slug:
        return None
    return slug.lower().replace(".", "-")


# --------------------------------------------------------------------------- #
# DuckDB schema
# --------------------------------------------------------------------------- #

def init_db(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS fetch_runs (
            fetched_at              TIMESTAMP,
            source                  VARCHAR,
            tier                    VARCHAR,
            intelligence_index_version DOUBLE,
            model_count             INTEGER,
            raw_path                VARCHAR
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS aa_models (
            id                                         VARCHAR PRIMARY KEY,
            name                                       VARCHAR,
            slug                                       VARCHAR,
            release_date                               DATE,
            model_creator_id                           VARCHAR,
            model_creator_name                         VARCHAR,
            intelligence_index_version                 DOUBLE,
            tier                                       VARCHAR,
            aa_intelligence_index                      DOUBLE,
            aa_coding_index                            DOUBLE,
            aa_agentic_index                           DOUBLE,
            ii_total_cost                              DOUBLE,
            ii_cost_per_task_total                     DOUBLE,
            price_1m_input_tokens                      DOUBLE,
            price_1m_output_tokens                     DOUBLE,
            price_1m_cache_hit_tokens                  DOUBLE,
            price_1m_cache_write_tokens                DOUBLE,
            median_output_tokens_per_second            DOUBLE,
            median_time_to_first_token_seconds         DOUBLE,
            median_time_to_first_answer_token_seconds  DOUBLE,
            median_end_to_end_response_time_seconds     DOUBLE,
            fetched_at                                 TIMESTAMP
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS modelsdev_providers (
            id          VARCHAR PRIMARY KEY,
            name        VARCHAR,
            api_base    VARCHAR,
            doc_url     VARCHAR,
            npm_package VARCHAR,
            env_vars    VARCHAR,
            fetched_at  TIMESTAMP
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS modelsdev_models (
            id                  VARCHAR PRIMARY KEY,
            provider_id         VARCHAR,
            slug_normalized     VARCHAR,
            name                VARCHAR,
            family              VARCHAR,
            release_date        DATE,
            last_updated        DATE,
            open_weights        BOOLEAN,
            reasoning           BOOLEAN,
            tool_call           BOOLEAN,
            structured_output   BOOLEAN,
            attachment          BOOLEAN,
            temperature         BOOLEAN,
            knowledge           VARCHAR,
            context_window      BIGINT,
            output_limit        BIGINT,
            weights_urls        VARCHAR,
            input_modalities    VARCHAR,
            output_modalities   VARCHAR,
            fetched_at          TIMESTAMP
        );
        """
    )
    con.execute("DROP TABLE IF EXISTS modelsdev_provider_models")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS modelsdev_provider_models (
            provider_id         VARCHAR,
            model_id            VARCHAR,
            slug_normalized     VARCHAR,
            model_name          VARCHAR,
            cache_read          DOUBLE,
            fetched_at          TIMESTAMP
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS openrouter_providers (
            slug                VARCHAR PRIMARY KEY,
            name                VARCHAR,
            headquarters        VARCHAR,
            datacenters         VARCHAR,
            privacy_policy_url  VARCHAR,
            terms_of_service_url VARCHAR,
            status_page_url     VARCHAR,
            domain              VARCHAR,
            fetched_at          TIMESTAMP
        );
        """
    )


def load_aa_models(
    con: duckdb.DuckDBPyConnection, payload: dict[str, Any], raw_path: Path
) -> None:
    fetched_at = datetime.now(timezone.utc)
    version = payload.get("intelligence_index_version")
    tier = payload.get("tier")

    rows: list[tuple] = []
    for m in payload.get("data", []):
        creator = m.get("model_creator") or {}
        evals = m.get("evaluations") or {}
        cost = m.get("artificial_analysis_intelligence_index_cost") or {}
        cpt = cost.get("cost_per_task") or {}
        pricing = m.get("pricing") or {}
        perf = m.get("performance") or {}
        rows.append(
            (
                m.get("id"),
                m.get("name"),
                m.get("slug"),
                _parse_date(m.get("release_date")),
                creator.get("id"),
                creator.get("name"),
                version,
                tier,
                evals.get("artificial_analysis_intelligence_index"),
                evals.get("artificial_analysis_coding_index"),
                evals.get("artificial_analysis_agentic_index"),
                cost.get("total_cost"),
                cpt.get("total_cost"),
                pricing.get("price_1m_input_tokens"),
                pricing.get("price_1m_output_tokens"),
                pricing.get("price_1m_cache_hit_tokens"),
                pricing.get("price_1m_cache_write_tokens"),
                perf.get("median_output_tokens_per_second"),
                perf.get("median_time_to_first_token_seconds"),
                perf.get("median_time_to_first_answer_token_seconds"),
                perf.get("median_end_to_end_response_time_seconds"),
                fetched_at,
            )
        )

    con.execute("DELETE FROM aa_models")
    if rows:
        con.executemany(
            "INSERT INTO aa_models VALUES (" + ", ".join(["?"] * 22) + ")",
            rows,
        )
    con.execute(
        "INSERT INTO fetch_runs VALUES (?, ?, ?, ?, ?, ?)",
        (fetched_at, AA_BASE_URL + AA_ENDPOINT, tier, version, len(rows), str(raw_path)),
    )
    print(f"Loaded {len(rows)} AA models into DuckDB (version {version}, tier {tier})")


def load_modelsdev(con: duckdb.DuckDBPyConnection, catalog: dict[str, Any]) -> None:
    fetched_at = datetime.now(timezone.utc)
    providers = catalog.get("providers", {}) or {}
    models = catalog.get("models", {}) or {}

    prov_rows: list[tuple] = []
    for pid, p in providers.items():
        prov_rows.append(
            (
                pid,
                p.get("name"),
                p.get("api"),
                p.get("doc"),
                p.get("npm"),
                ",".join(p.get("env", []) or []),
                fetched_at,
            )
        )
    con.execute("DELETE FROM modelsdev_providers")
    if prov_rows:
        con.executemany(
            "INSERT INTO modelsdev_providers VALUES (" + ", ".join(["?"] * 7) + ")",
            prov_rows,
        )

    md_rows: list[tuple] = []
    for mid, m in models.items():
        provider_id = mid.split("/", 1)[0] if "/" in mid else None
        slug_norm = normalize_slug(mid.split("/", 1)[-1] if "/" in mid else mid)
        limit = m.get("limit") or {}
        weights = m.get("weights") or []
        weights_urls = ",".join(w.get("url", "") for w in weights if w.get("url"))
        modalities = m.get("modalities") or {}
        md_rows.append(
            (
                mid,
                provider_id,
                slug_norm,
                m.get("name"),
                m.get("family"),
                _parse_date(m.get("release_date")),
                _parse_date(m.get("last_updated")),
                bool(m.get("open_weights")),
                m.get("reasoning"),
                m.get("tool_call"),
                m.get("structured_output"),
                m.get("attachment"),
                m.get("temperature"),
                m.get("knowledge"),
                limit.get("context"),
                limit.get("output"),
                weights_urls or None,
                ",".join(modalities.get("input", []) or []) or None,
                ",".join(modalities.get("output", []) or []) or None,
                fetched_at,
            )
        )
    con.execute("DELETE FROM modelsdev_models")
    if md_rows:
        con.executemany(
            "INSERT INTO modelsdev_models VALUES (" + ", ".join(["?"] * 20) + ")",
            md_rows,
        )

    # Provider → model junction: iterate every provider's `models` dict to
    # capture ALL provider/model pairs (the `models` section above only has
    # one canonical entry per model).
    junction_rows: list[tuple] = []
    for pid, p in providers.items():
        for mid, m in (p.get("models") or {}).items():
            slug_norm = normalize_slug(mid.split("/", 1)[-1] if "/" in mid else mid)
            cost = m.get("cost") or {}
            cache_read = cost.get("cache_read")
            junction_rows.append((pid, mid, slug_norm, m.get("name"), cache_read, fetched_at))
    con.execute("DELETE FROM modelsdev_provider_models")
    if junction_rows:
        con.executemany(
            "INSERT INTO modelsdev_provider_models VALUES (" + ", ".join(["?"] * 6) + ")",
            junction_rows,
        )

    print(f"Loaded {len(prov_rows)} providers, {len(md_rows)} models, and {len(junction_rows)} provider→model links from models.dev")


def fetch_openrouter_providers(*, force: bool = False) -> dict[str, Any]:
    """Fetch OpenRouter providers and cache for 24h."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / "openrouter_providers_latest.json"
    if not force and (age := _cache_age_hours(cache_path)) is not None and age * 3600 < CACHE_TTL_SECONDS:
        print(f"Using cached OpenRouter providers (age {age:.1f}h): {cache_path}")
        return json.loads(cache_path.read_text())
    print("Fetching OpenRouter providers...")
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers={"accept": "application/json"}) as client:
        resp = client.get(OPENROUTER_PROVIDERS_URL)
        resp.raise_for_status()
        payload = resp.json()
    cache_path.write_text(json.dumps(payload, indent=2))
    count = len(payload.get("data", []))
    print(f"Wrote cache: {cache_path} ({count} providers)")
    return payload


def load_openrouter_providers(con: duckdb.DuckDBPyConnection, payload: dict[str, Any]) -> None:
    """Load OpenRouter providers into DuckDB, deriving a domain from URL fields."""
    fetched_at = datetime.now(timezone.utc)
    items = payload.get("data", [])
    rows: list[tuple] = []
    for p in items:
        domain = None
        for field in ("terms_of_service_url", "privacy_policy_url", "status_page_url"):
            url = p.get(field)
            if url:
                # Extract netloc, strip leading www.
                domain = url.split("//", 1)[-1].split("/", 1)[0].replace("www.", "")
                break
        rows.append((
            p.get("slug"),
            p.get("name"),
            p.get("headquarters"),
            ",".join(p.get("datacenters") or []),
            p.get("privacy_policy_url"),
            p.get("terms_of_service_url"),
            p.get("status_page_url"),
            domain,
            fetched_at,
        ))
    con.execute("DELETE FROM openrouter_providers")
    if rows:
        con.executemany(
            "INSERT INTO openrouter_providers VALUES (" + ", ".join(["?"] * 9) + ")",
            rows,
        )
    print(f"Loaded {len(rows)} providers from OpenRouter")


def compute_aa_modelsdev_matches(con: duckdb.DuckDBPyConnection) -> None:
    """Compute the best models.dev match for each AA model, persisted to a table.

    Matching strategy (in order):
      1. Exact normalized slug match: AA slug == models.dev slug_normalized
      2. Creator-prefix fallback: ``nvidia-nemotron-...`` -> ``nemotron-...``
         (strip a single leading ``"<word>-"`` to handle AA slugs that include
         a brand prefix that models.dev omits).
      3. AA slug is a prefix of a models.dev slug (handles AA omitting
         ``-reasoning`` / ``-non-reasoning`` suffixes).

    When a slug matches multiple providers, prefer the one whose provider name
    matches the AA model creator (case-insensitive).
    """
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS aa_modelsdev_matches (
            aa_model_id            VARCHAR PRIMARY KEY,
            modelsdev_id           VARCHAR,
            modelsdev_provider_id  VARCHAR,
            match_type             VARCHAR
        );
        """
    )

    aa_rows = con.execute(
        "SELECT id, slug, model_creator_name FROM aa_models"
    ).fetchall()

    md_rows = con.execute(
        "SELECT id, provider_id, slug_normalized FROM modelsdev_models"
    ).fetchall()
    provider_names = dict(
        con.execute("SELECT id, name FROM modelsdev_providers").fetchall()
    )

    slug_index: dict[str, list[tuple[str, str]]] = {}
    for mid, prov_id, slug_norm in md_rows:
        if not slug_norm:
            continue
        slug_index.setdefault(slug_norm, []).append((mid, prov_id))

    rows: list[tuple[str, str | None, str | None, str | None]] = []
    matched = 0
    for aa_id, slug, creator in aa_rows:
        if not slug:
            rows.append((aa_id, None, None, None))
            continue

        aa_slug = slug.lower()
        creator_lower = (creator or "").lower()

        def pick_best(candidates: list[tuple[str, str]]) -> tuple[str, str, str]:
            # Best match = provider name matches AA creator; otherwise lowest id (stable).
            for mid, prov_id in candidates:
                pname = (provider_names.get(prov_id) or "").lower()
                if pname and pname == creator_lower:
                    return mid, prov_id, "exact"
            return candidates[0][0], candidates[0][1], "exact"

        chosen: tuple[str | None, str | None, str | None] = (None, None, None)

        # 1. Exact normalized slug match.
        candidates = slug_index.get(aa_slug)
        if candidates:
            chosen = pick_best(candidates)

        # 2. Creator-prefix fallback: try stripping a leading "<word>-" prefix.
        if chosen[0] is None:
            dash = aa_slug.find("-")
            if dash > 0:
                stripped = aa_slug[dash + 1:]
                candidates = slug_index.get(stripped)
                if candidates:
                    chosen = pick_best(candidates)
                    chosen = (chosen[0], chosen[1], "creator_prefix_strip")

        # 3. AA slug is a prefix of a models.dev slug (e.g. AA omits "-reasoning").
        if chosen[0] is None:
            for md_slug, candidates in slug_index.items():
                if md_slug.startswith(aa_slug + "-"):
                    chosen = pick_best(candidates)
                    match_type = "aa_slug_prefix"
                    chosen = (chosen[0], chosen[1], match_type)
                    break

        if chosen[0]:
            matched += 1
        rows.append((aa_id, chosen[0], chosen[1], chosen[2]))

    con.execute("DELETE FROM aa_modelsdev_matches")
    if rows:
        con.executemany(
            "INSERT INTO aa_modelsdev_matches VALUES (" + ", ".join(["?"] * 4) + ")",
            rows,
        )
    print(f"Computed AA <-> models.dev matches: {matched}/{len(rows)} models matched")


def compute_aa_neuralwatt_matches(con: duckdb.DuckDBPyConnection) -> None:
    """Match AA models → Neuralwatt models by display_name and persist to a table.

    Matching strategy (in order, case-insensitive throughout):
      1. Exact: AA `name` == NW `display_name`.
      2. AA starts-with NW `display_name` (e.g. AA "GLM-5.2 (max)" → NW "GLM-5.2"; AA
         "Qwen3.5 397B A17B (Reasoning)" → NW "Qwen3.5 397B").

    When several NW models have the same `display_name`, prefer the "base" variant
    — the entry whose `id` does NOT end in a known variant suffix like `-fast`,
    `-short`, `-short-fast` — so that "GLM-5.2 (max)" matches "GLM-5.2" rather than
    "GLM-5.2 (fast)" / "GLM-5.2 (short)".
    """
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS aa_neuralwatt_matches (
            aa_model_id              VARCHAR PRIMARY KEY,
            neuralwatt_model_id      VARCHAR,
            neuralwatt_display_name  VARCHAR,
            match_type               VARCHAR
        );
        """
    )

    # Skip silently if Neuralwatt tables don't exist yet (separate script not run).
    has_nw = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE lower(table_name) = 'neuralwatt_models'"
    ).fetchone()[0]
    if not has_nw:
        # Create an empty matches table so the enriched view joins cleanly.
        con.execute("DELETE FROM aa_neuralwatt_matches")
        print("Skipped AA <-> Neuralwatt matches: neuralwatt_models table not loaded yet")
        return

    aa_rows = con.execute("SELECT id, name FROM aa_models").fetchall()
    nw_rows = con.execute(
        "SELECT id, display_name FROM neuralwatt_models WHERE display_name IS NOT NULL"
    ).fetchall()

    # Index by lowercased display name. Mark base variants so we can prefer them.
    by_name: dict[str, list[tuple[str, bool]]] = {}
    variant_suffixes = ("-fast", "-short", "-short-fast")
    for nw_id, display_name in nw_rows:
        key = display_name.lower()
        is_base = not any(nw_id.lower().endswith(s) for s in variant_suffixes)
        by_name.setdefault(key, []).append((nw_id, is_base))

    out_rows: list[tuple[str, str | None, str | None, str | None]] = []
    matched = 0
    for aa_id, aa_name in aa_rows:
        if not aa_name:
            out_rows.append((aa_id, None, None, None))
            continue
        aa_name_l = aa_name.lower()
        chosen: tuple[str | None, str | None, str | None] = (None, None, None)
        # Prefer exact, then starts-with (longest display_name wins for specificity).
        def pick(candidates: list[tuple[str, bool]]) -> tuple[str, str, str]:
            base = [c for c in candidates if c[1]]
            chosen_list = base if base else candidates
            return chosen_list[0][0], None, None  # display_name not available here; just return id.

        # Strategy 1: exact name match.
        candidates = by_name.get(aa_name_l)
        if candidates:
            nw_id = sorted(candidates, key=lambda c: (0 if c[1] else 1, c[0]))[0][0]
            display_name = next(n[1] for n in nw_rows if n[0] == nw_id)
            chosen = (nw_id, display_name, "exact_name")

        # Strategy 2: AA name starts with NW display_name (longest first).
        if chosen[0] is None:
            matching = [(key, cands) for key, cands in by_name.items() if aa_name_l.startswith(key + " ") or aa_name_l.startswith(key + "(")]
            if matching:
                matching.sort(key=lambda kv: len(kv[0]), reverse=True)
                key, cands = matching[0]
                nw_id = sorted(cands, key=lambda c: (0 if c[1] else 1, c[0]))[0][0]
                display_name = next(n[1] for n in nw_rows if n[0] == nw_id)
                chosen = (nw_id, display_name, "aa_starts_with_nw")

        if chosen[0]:
            matched += 1
        out_rows.append((aa_id, chosen[0], chosen[1], chosen[2]))

    con.execute("DELETE FROM aa_neuralwatt_matches")
    if out_rows:
        con.executemany(
            "INSERT INTO aa_neuralwatt_matches VALUES (" + ", ".join(["?"] * 4) + ")",
            out_rows,
        )
    print(f"Computed AA <-> Neuralwatt matches: {matched}/{len(out_rows)} models matched")


def create_enriched_view(con: duckdb.DuckDBPyConnection) -> None:
    """A view joining AA models to models.dev AND Neuralwatt, via precomputed match tables."""
    con.execute(
        """
        CREATE OR REPLACE VIEW models_enriched AS
        SELECT
            a.*,
            m.id AS modelsdev_id,
            m.provider_id AS modelsdev_provider_id,
            m.open_weights,
            m.context_window AS modelsdev_context_window,
            m.weights_urls,
            m.input_modalities AS modelsdev_input_modalities,
            m.output_modalities AS modelsdev_output_modalities,
            m.tool_call AS modelsdev_tool_call,
            m.reasoning AS modelsdev_reasoning,
            p.name AS modelsdev_provider_name,
            mdvmatch.match_type AS modelsdev_match_type,
            nw.id AS nw_model_id,
            nw.display_name AS nw_display_name,
            nw.provider AS nw_provider,
            nw.huggingface_id AS nw_huggingface_id,
            nw.pricing_input_per_million AS nw_input_per_million,
            nw.pricing_output_per_million AS nw_output_per_million,
            nw.pricing_cached_input_per_million AS nw_cached_input_per_million,
            nwe.energy_mwh AS nw_energy_16k_64k_mwh,
            nwe.cache_hit_rate_pct AS nw_cache_hit_rate_16k_64k,
            nwe.request_pct AS nw_request_share_16k_64k
        FROM aa_models a
        LEFT JOIN aa_modelsdev_matches mdvmatch ON mdvmatch.aa_model_id = a.id
        LEFT JOIN modelsdev_models m ON m.id = mdvmatch.modelsdev_id
        LEFT JOIN modelsdev_providers p ON p.id = m.provider_id
        LEFT JOIN aa_neuralwatt_matches nwmatch ON nwmatch.aa_model_id = a.id
        LEFT JOIN neuralwatt_models nw ON nw.id = nwmatch.neuralwatt_model_id
        LEFT JOIN neuralwatt_energy nwe
               ON nwe.model_display_name = nw.display_name
              AND nwe.band_label = '16k–64k'
        """
    )


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

def print_top10_agentic(con: duckdb.DuckDBPyConnection) -> None:
    print("\nTop 10 models by Artificial Analysis Agentic Index:")
    rows = con.execute(
        """
        SELECT name, model_creator_name, aa_agentic_index, aa_coding_index, aa_intelligence_index
        FROM aa_models
        WHERE aa_agentic_index IS NOT NULL
        ORDER BY aa_agentic_index DESC
        LIMIT 10
        """
    ).fetchall()
    if not rows:
        print("  (no models with an agentic index score)")
        return
    print(f"  {'#':>2}  {'Agentic':>7}  {'Coding':>7}  {'Intel':>7}  Model (creator)")
    print(f"  {'-' * 70}")
    for i, (name, creator, agentic, coding, intel) in enumerate(rows, 1):
        print(f"  {i:>2}  {agentic or 0:>7.2f}  {coding or 0:>7.2f}  {intel or 0:>7.2f}  {name} ({creator})")


def render_top10_open_html(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        """
        SELECT
            a.name,
            a.slug,
            a.model_creator_name,
            a.release_date,
            a.aa_agentic_index,
            a.aa_coding_index,
            a.aa_intelligence_index,
            a.price_1m_input_tokens,
            a.price_1m_output_tokens,
            a.price_1m_cache_hit_tokens,
            a.median_output_tokens_per_second,
            COALESCE(p.id, '') AS provider_id,
            COALESCE(p.name, a.model_creator_name) AS provider_name,
            a.weights_urls,
            a.nw_model_id,
            a.nw_input_per_million,
            a.nw_output_per_million,
            a.nw_energy_16k_64k_mwh,
            a.modelsdev_input_modalities,
            a.modelsdev_output_modalities,
            a.modelsdev_tool_call,
            a.modelsdev_reasoning,
            a.modelsdev_context_window,
            -- Aggregate all providers offering this model slug via models.dev junction.
            (
                SELECT STRING_AGG(pm.provider_id || '\t' || COALESCE(p2.name, pm.provider_id) || '\t' || COALESCE(p2.doc_url, ''), '\n')
                FROM modelsdev_provider_models pm
                LEFT JOIN modelsdev_providers p2 ON p2.id = pm.provider_id
                WHERE pm.slug_normalized = a.slug
            ) AS providers_offering
        FROM models_enriched a
        LEFT JOIN modelsdev_providers p ON p.id = a.modelsdev_provider_id
        WHERE a.aa_agentic_index IS NOT NULL
          AND a.open_weights IS TRUE
          AND a.price_1m_input_tokens IS NOT NULL
          AND a.price_1m_output_tokens IS NOT NULL
        ORDER BY a.aa_agentic_index DESC
        """
    ).fetchall()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not rows:
        print("Skipping HTML report: no open-weight models with an agentic index score matched models.dev.")
        return

    # Scatter data: all open-weight models that have an agentic score AND
    # input + output pricing. The 7:2:1 blend weights cache hits heavily; when a
    # model has no published cache-hit price, we fall back to the input price
    # (an upper bound: cache hits are at most as expensive as regular input,
    # never more). This avoids dropping models simply because they don't report
    # cache pricing.
    scatter_rows = con.execute(
        """
        SELECT
            a.name,
            a.slug,
            a.model_creator_name,
            a.aa_agentic_index,
            a.aa_coding_index,
            a.aa_intelligence_index,
            a.price_1m_input_tokens,
            a.price_1m_output_tokens,
            a.price_1m_cache_hit_tokens,
            (7 * COALESCE(a.price_1m_cache_hit_tokens, a.price_1m_input_tokens)
                  + 2 * a.price_1m_input_tokens
                  + 1 * a.price_1m_output_tokens) / 10.0 AS blended_cost_721,
            a.ii_cost_per_task_total,
            a.median_output_tokens_per_second,
            a.median_time_to_first_token_seconds,
            a.median_end_to_end_response_time_seconds,
            a.release_date,
            COALESCE(p.id, '') AS provider_id,
            COALESCE(p.name, a.model_creator_name) AS provider_name,
            a.weights_urls,
            a.nw_model_id,
            a.nw_display_name,
            a.nw_input_per_million,
            a.nw_output_per_million,
            a.nw_cached_input_per_million,
            (7 * COALESCE(a.nw_cached_input_per_million, a.nw_input_per_million)
                  + 2 * a.nw_input_per_million
                  + 1 * a.nw_output_per_million) / 10.0 AS nw_blended_cost_721,
            a.nw_energy_16k_64k_mwh,
            a.nw_cache_hit_rate_16k_64k,
            a.nw_request_share_16k_64k,
            a.modelsdev_input_modalities,
            a.modelsdev_output_modalities,
            a.modelsdev_tool_call,
            a.modelsdev_reasoning,
            a.modelsdev_context_window,
            (
                SELECT STRING_AGG(DISTINCT COALESCE(orp.headquarters, 'unknown'), ',')
                FROM modelsdev_provider_models pm2
                LEFT JOIN openrouter_providers orp ON orp.slug = pm2.provider_id
                WHERE pm2.slug_normalized = a.slug
                  AND pm2.cache_read IS NOT NULL
                  AND pm2.cache_read != 0
                  AND orp.headquarters IS NOT NULL
            ) AS provider_hqs,
            (
                SELECT STRING_AGG(DISTINCT COALESCE(orp.headquarters, 'unknown'), ',')
                FROM modelsdev_provider_models pm3
                LEFT JOIN openrouter_providers orp ON orp.slug = pm3.provider_id
                WHERE pm3.slug_normalized = a.slug
                  AND pm3.cache_read IS NOT NULL
                  AND pm3.cache_read != 0
            ) AS provider_hqs_incl_unknown
        FROM models_enriched a
        LEFT JOIN modelsdev_providers p ON p.id = a.modelsdev_provider_id
        WHERE a.aa_agentic_index IS NOT NULL
          AND a.open_weights IS TRUE
          AND a.price_1m_input_tokens IS NOT NULL
          AND a.price_1m_output_tokens IS NOT NULL
        ORDER BY a.aa_agentic_index DESC
        """
    ).fetchall()

    scatter_data = [
        {
            "name": r[0],
            "slug": r[1],
            "creator": r[2],
            "agentic": r[3],
            "coding": r[4],
            "intel": r[5],
            "input_price": r[6],
            "output_price": r[7],
            "cache_hit_price": r[8],
            "blended_cost": r[9],
            "cost_per_task": r[10],
            "tokens_per_second": r[11],
            "ttft": r[12],
            "e2e": r[13],
            "release_date": str(r[14]) if r[14] else None,
            "provider_id": r[15] or "",
            "provider_name": r[16] or r[2],
            "weights_url": (r[17].split(",", 1)[0] if r[17] else None),
            "nw_model_id": r[18],
            "nw_display_name": r[19],
            "nw_input_per_million": r[20],
            "nw_output_per_million": r[21],
            "nw_cached_input_per_million": r[22],
            "nw_blended_cost": r[23],
            "nw_energy_mwh_16k_64k": r[24],
            "nw_cache_hit_rate_16k_64k": r[25],
            "nw_request_share_16k_64k": r[26],
            "input_modalities": r[27] or "",
            "output_modalities": r[28] or "",
            "tool_call": r[29],
            "reasoning": r[30],
            "context_window": r[31],
            "provider_hqs": r[32] or "",
            "provider_hqs_incl_unknown": r[33] or "",
        }
        for r in scatter_rows
    ]

    # Neuralwatt-only scatter data: all NW models that have energy at the
    # 16k–64k band, with NW pricing for the X-axis blended cost.
    nw_scatter_rows = con.execute(
        """
        SELECT
            nw.display_name,
            nw.id,
            nw.provider,
            nw.pricing_input_per_million,
            nw.pricing_output_per_million,
            nw.pricing_cached_input_per_million,
            (7 * COALESCE(nw.pricing_cached_input_per_million, nw.pricing_input_per_million)
                  + 2 * nw.pricing_input_per_million
                  + 1 * nw.pricing_output_per_million) / 10.0 AS nw_blended_cost_721,
            nwe.energy_mwh,
            nwe.cache_hit_rate_pct,
            nwe.request_pct
        FROM neuralwatt_models nw
        INNER JOIN neuralwatt_energy nwe
            ON nwe.model_display_name = nw.display_name
           AND nwe.band_label = '16k–64k'
           AND nwe.has_data IS TRUE
        WHERE nw.pricing_input_per_million IS NOT NULL
          AND nw.pricing_output_per_million IS NOT NULL
        ORDER BY nwe.energy_mwh DESC
        """
    ).fetchall()

    nw_scatter_data = [
        {
            "name": r[0],
            "id": r[1],
            "provider": r[2],
            "input_price": r[3],
            "output_price": r[4],
            "cached_input_price": r[5],
            "blended_cost": r[6],
            "energy_mwh": r[7],
            "cache_hit_rate": r[8],
            "request_pct": r[9],
            "is_variant": "-fast" in (r[1] or "").lower() or "-short" in (r[1] or "").lower(),
        }
        for r in nw_scatter_rows
    ]

    # --- Linear regression: energy_mwh ~ slope * blended_cost + intercept ---
    # Computed on NW base models only (excluding -fast / -short variants) so the
    # relationship reflects the "canonical" model, not tuned variants.
    regression: dict[str, Any] = {"n": 0, "slope": None, "intercept": None, "r": None, "r_squared": None}
    base_points = [(d["blended_cost"], d["energy_mwh"]) for d in nw_scatter_data if not d["is_variant"]]
    if len(base_points) >= 3:
        n = len(base_points)
        xs = [p[0] for p in base_points]
        ys = [p[1] for p in base_points]
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        ss_xx = sum((x - mean_x) ** 2 for x in xs)
        ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in base_points)
        ss_yy = sum((y - mean_y) ** 2 for y in ys)
        if ss_xx > 0:
            slope = ss_xy / ss_xx
            intercept = mean_y - slope * mean_x
            r = ss_xy / math.sqrt(ss_xx * ss_yy) if ss_yy > 0 else 0.0
            regression = {
                "n": n,
                "slope": round(slope, 2),
                "intercept": round(intercept, 2),
                "r": round(r, 4),
                "r_squared": round(r * r, 4),
            }
            print(f"Neuralwatt regression (n={n}): energy_mWh = {regression['slope']} × cost + {regression['intercept']} (r={regression['r']}, r²={regression['r_squared']})")

    # Ratio between NW blended cost and AA blended cost for each matched model.
    # This lets us estimate NW blended cost for unmatched models from their AA
    # blended cost.
    nw_aa_cost_ratios: list[float] = []
    for entry in scatter_data:
        nw_c = entry.get("nw_blended_cost")
        aa_c = entry.get("blended_cost")
        if nw_c and aa_c and aa_c > 0:
            nw_aa_cost_ratios.append(nw_c / aa_c)
    avg_nw_aa_ratio = (sum(nw_aa_cost_ratios) / len(nw_aa_cost_ratios)) if nw_aa_cost_ratios else 1.0

    # Apply estimated energy to AA scatter entries.
    # Use NW blended cost when available; for unmatched models, estimate NW
    # blended cost from AA blended cost × avg ratio so the regression input
    # matches what the model would cost on Neuralwatt.
    if regression["slope"] is not None:
        for entry in scatter_data:
            measured = entry.get("nw_energy_mwh_16k_64k")
            if measured is not None:
                entry["energy_per_req"] = measured
                continue  # has measured NW energy — no estimation needed
            # Estimate NW blended cost: use actual NW blended cost if the model
            # has NW pricing; otherwise scale the AA blended cost by the average
            # NW/AA cost ratio.
            nw_blended = entry.get("nw_blended_cost")
            if nw_blended and nw_blended > 0:
                est_cost = nw_blended
            else:
                aa_blended = entry.get("blended_cost")
                if aa_blended and aa_blended > 0:
                    est_cost = aa_blended * avg_nw_aa_ratio
                else:
                    continue
            predicted = regression["slope"] * est_cost + regression["intercept"]
            # Clamp at the minimum observed energy from the regression data so
            # very cheap models don't get estimated at 0 mWh (which is physically
            # implausible). Use the lowest measured energy from base models.
            min_energy = min((p[1] for p in base_points), default=0)
            entry["nw_energy_estimated_mwh"] = round(max(min_energy * 0.5, predicted), 2)
            entry["energy_per_req"] = entry["nw_energy_estimated_mwh"]

            # Energy per task: estimate from cost_per_task via regression,
            # also accounting for the NW/AA cost ratio.
            cpt = entry.get("cost_per_task")
            if cpt and cpt > 0:
                est_cpt_nw = cpt * avg_nw_aa_ratio
                predicted_task = regression["slope"] * est_cpt_nw + regression["intercept"]
                entry["energy_per_task_estimated_mwh"] = round(max(min_energy * 0.5, predicted_task), 2)
                entry["energy_per_task"] = entry["energy_per_task_estimated_mwh"]

    # Provider color map: brand color if known, else derive from the provider's
    # position in the list so each provider gets a stable, distinct color.
    providers_in_data: list[dict[str, str]] = []
    seen_pids: set[str] = set()
    for entry in scatter_data:
        pid = entry["provider_id"] or entry["provider_name"]
        if pid in seen_pids:
            continue
        seen_pids.add(pid)
        providers_in_data.append({"id": entry["provider_id"], "name": entry["provider_name"], "pid": pid})

    color_map: dict[str, str] = {}
    for idx, prov in enumerate(providers_in_data):
        pid = prov["id"] or prov["name"]
        # Try the exact id/name first, then a lowercase creator-derived key
        # (e.g. provider_id="" but creator="Meta" → try "meta").
        resolved = (
            PROVIDER_COLORS.get(pid)
            or PROVIDER_COLORS.get(pid.lower())
            or FALLBACK_PALETTE[idx % len(FALLBACK_PALETTE)]
        )
        color_map[pid] = resolved

    # Extend color map with NW providers (e.g. "Z AI", "MoonshotAI", "Alibaba").
    nw_provider_idx = 0
    for entry in nw_scatter_data:
        pname = entry.get("provider") or entry.get("name", "?")
        if pname in color_map:
            continue
        resolved = (
            PROVIDER_COLORS.get(pname)
            or PROVIDER_COLORS.get(pname.lower())
            or FALLBACK_PALETTE[(len(providers_in_data) + nw_provider_idx) % len(FALLBACK_PALETTE)]
        )
        color_map[pname] = resolved
        nw_provider_idx += 1

    # Each row becomes a card.
    cards: list[str] = []
    for i, r in enumerate(rows, 1):
        (
            name, slug, creator, release, agentic, coding, intel,
            p_in, p_out, p_cache, tps, provider_id, provider_name, weights_urls,
            nw_model_id, nw_in, nw_out, nw_energy,
            md_input_mods, md_output_mods, md_tool_call, md_reasoning, md_context,
            providers_offering,
        ) = r

        # Resolve provider color for the logo border.
        border_color = color_map.get(provider_id) or color_map.get(provider_name) or "#5b8def"

        if provider_id:
            logo = (
                f'<img class="logo" src="{MODELSDEV_LOGOS_BASE}/{escape(provider_id)}.svg" '
                f'alt="{escape(provider_name)} logo" '
                f'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'inline\'">'
                f'<span class="logo-fallback" style="display:none">{escape(provider_name[0])}</span>'
            )
        else:
            logo = f'<span class="logo-fallback">{escape((provider_name or "?")[0])}</span>'

        weights_link = ""
        if weights_urls:
            first_url = weights_urls.split(",", 1)[0]
            weights_link = f'<a class="weights" href="{escape(first_url)}" target="_blank" rel="noopener">weights</a>'

        # Neuralwatt energy chip.
        nw_chip = ""
        if nw_model_id:
            energy_str = f"{nw_energy:.2f} mWh" if nw_energy is not None else "—"
            nw_chip = (
                f'<span class="nw-chip" title="Neuralwatt · energy per request at the 16k–64k band">'
                f'⚡ {escape(energy_str)}'
                f'</span>'
            )

        # Capability chips.
        caps: list[str] = []
        if md_reasoning:
            caps.append('<span class="cap-chip cap-reasoning" title="Reasoning">R</span>')
        if md_tool_call:
            caps.append('<span class="cap-chip cap-tools" title="Tool calling">T</span>')
        if md_input_mods:
            for m in md_input_mods.split(","):
                m = m.strip()
                if m:
                    caps.append(f'<span class="cap-chip cap-mod" title="Input: {escape(m)}">{escape(m[:3])}</span>')
        ctx_str = f'<span class="cap-chip cap-ctx" title="Context window">{md_context // 1000 if md_context else "?"}k</span>' if md_context else ""

        # Provider list: parse the tab-separated \n-delimited aggregate.
        provider_links: list[str] = []
        if providers_offering:
            for line in providers_offering.split("\n"):
                parts = line.split("\t")
                if len(parts) >= 3 and parts[2]:
                    pid, pname, doc = parts[0], parts[1], parts[2]
                    provider_links.append(
                        f'<a class="provider-link" href="{escape(doc)}" target="_blank" rel="noopener" title="{escape(pname)} docs">{escape(pname)}</a>'
                    )
                elif len(parts) >= 2 and parts[1]:
                    provider_links.append(f'<span class="provider-link">{escape(parts[1])}</span>')
        providers_html = ""
        if provider_links:
            providers_html = (
                f'<div class="card-providers">'
                f'<span class="providers-label">{len(provider_links)} providers:</span>'
                f'{"".join(provider_links)}'
                f'</div>'
            )

        cards.append(f"""
        <article class="card" style="--rank:{i}">
          <div class="rank">#{i}</div>
          <div class="logo-wrap" style="border: 2px solid {border_color}; box-shadow: 0 0 0 1px {border_color}33;">{logo}</div>
          <div class="info">
            <h2>{escape(name)}</h2>
            <div class="creator">{escape(provider_name)}{weights_link and f" · {weights_link}"}</div>
            <div class="meta">
              <span>released {_relative_date(_parse_date(str(release) if release else None))}</span>
              <span>slug: <code>{escape(slug)}</code></span>
              {ctx_str}
              {''.join(caps)}
              {nw_chip}
            </div>
            {providers_html}
          </div>
          <div class="score-wrap">
            <div class="score agentic" title="Artificial Analysis Agentic Index">
              <div class="score-value">{agentic:.2f}</div>
              <div class="score-label">agentic</div>
            </div>
            <div class="secondary-scores">
              <div><span>coding</span><b>{coding:.1f}</b></div>
              <div><span>intel.</span><b>{intel:.1f}</b></div>
            </div>
          </div>
          <div class="stats">
            <div><span>blended $/1M</span><b>{f"${(7 * (p_cache or p_in) + 2 * p_in + p_out) / 10:.2f}" if p_in is not None else "—"}</b></div>
            <div><span>tokens/s</span><b>{f"{tps:.0f}" if tps is not None else "—"}</b></div>
          </div>
        </article>""")

    # Custom HTML legend: provider logo + colored swatch + name.
    legend_items: list[str] = []
    for prov in providers_in_data:
        pid = prov["id"]
        pname = prov["name"]
        color = color_map[prov["pid"]]
        if pid:
            logo_html = (
                f'<img src="{MODELSDEV_LOGOS_BASE}/{escape(pid)}.svg" alt="{escape(pname)}" '
                f'title="{escape(pname)}" loading="lazy" '
                f'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'inline-block\'">'
                f'<span class="logo-letter" style="display:none">{escape(pname[0])}</span>'
            )
        else:
            logo_html = f'<span class="logo-letter">{escape(pname[0])}</span>'
        legend_items.append(
            f'<span class="legend-item" data-provider="{escape(pid or pname)}">'
            f'<span class="swatch" style="background:{color}"></span>'
            f'<span class="legend-logo" style="background:{color}20">{logo_html}</span>'
            f'<span class="legend-name">{escape(pname)}</span>'
            f'</span>'
        )

    json_blob = json.dumps(scatter_data, separators=(",", ":"))
    color_map_json = json.dumps(color_map, separators=(",", ":"))
    providers_json = json.dumps(
        [{"id": p["id"] or p["name"], "name": p["name"], "color": color_map[p["pid"]]} for p in providers_in_data],
        separators=(",", ":"),
    )
    plot_build_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Provider section: all providers that offer at least one open-weight model
    # in our scatter dataset, with count of such models. LEFT JOIN OpenRouter
    # for headquarters/datacenter locations.
    provider_section_rows = con.execute(
        """
        SELECT
            pm.provider_id,
            p.name,
            p.doc_url,
            COUNT(DISTINCT pm.slug_normalized) AS open_weight_model_count,
            orp.headquarters,
            orp.datacenters,
            orp.domain AS or_domain
        FROM modelsdev_provider_models pm
        INNER JOIN modelsdev_providers p ON p.id = pm.provider_id
        LEFT JOIN openrouter_providers orp ON orp.slug = pm.provider_id
        WHERE pm.slug_normalized IN (
            SELECT a.slug
            FROM models_enriched a
            WHERE a.aa_agentic_index IS NOT NULL
              AND a.open_weights IS TRUE
              AND a.price_1m_input_tokens IS NOT NULL
              AND a.price_1m_output_tokens IS NOT NULL
        )
        AND pm.cache_read IS NOT NULL
        AND pm.cache_read != 0
        GROUP BY pm.provider_id, p.name, p.doc_url, orp.headquarters, orp.datacenters, orp.domain
        ORDER BY open_weight_model_count DESC, p.name
        """
    ).fetchall()
    provider_cards: list[str] = []
    for pid, pname, doc_url, count, hq, dcs, or_domain in provider_section_rows:
        color = color_map.get(pid) or color_map.get(pname) or "#5b8def"

        # Derive a domain for the favicon: prefer OpenRouter-derived domain,
        # then extract from the models.dev doc_url.
        domain = or_domain
        if not domain and doc_url:
            domain = doc_url.split("//", 1)[-1].split("/", 1)[0].replace("www.", "")

        # Favicon from Google's service, with models.dev logo as fallback.
        if domain:
            logo_html = (
                f'<img src="{GOOGLE_FAVICONS_URL}{escape(domain)}" alt="{escape(pname)}" loading="lazy" '
                f'onerror="this.onerror=null;this.src=\'{MODELSDEV_LOGOS_BASE}/{escape(pid)}.svg\';'
                f'this.nextElementSibling.style.display=\'inline-block\'">'
                f'<span class="logo-letter" style="display:none">{escape(pname[0])}</span>'
            ) if pid else (
                f'<img src="{GOOGLE_FAVICONS_URL}{escape(domain)}" alt="{escape(pname)}" loading="lazy" '
                f'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'inline-block\'">'
                f'<span class="logo-letter" style="display:none">{escape(pname[0])}</span>'
            )
        elif pid:
            logo_html = (
                f'<img src="{MODELSDEV_LOGOS_BASE}/{escape(pid)}.svg" alt="{escape(pname)}" loading="lazy" '
                f'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'inline-block\'">'
                f'<span class="logo-letter" style="display:none">{escape(pname[0])}</span>'
            )
        else:
            logo_html = f'<span class="logo-letter">{escape(pname[0])}</span>'

        # Location badges: HQ flag + datacenter flags.
        loc_badges = ""
        if hq:
            loc_badges += f'<span class="loc-badge" title="Headquarters: {escape(hq)}">{_country_flag(hq)} {escape(hq)}</span>'
        if dcs:
            dc_list = [d.strip() for d in dcs.split(",") if d.strip()]
            # Deduplicate, exclude HQ (already shown).
            seen = {hq.upper()} if hq else set()
            dc_flags = []
            for dc in dc_list:
                if dc.upper() not in seen:
                    dc_flags.append(f'<span class="loc-badge loc-dc" title="Datacenter: {escape(dc)}">{_country_flag(dc)} {escape(dc)}</span>')
                    seen.add(dc.upper())
            if dc_flags:
                loc_badges += "".join(dc_flags)

        link = f'href="{escape(doc_url)}"' if doc_url else ""
        tag = "a" if doc_url else "span"
        hq_attr = hq or "unknown"
        provider_cards.append(
            f'<{tag} class="provider-card" data-hq="{escape(hq_attr.lower())}" {link} target="_blank" rel="noopener" '
            f'style="border-color: {color}33;">'
            f'<span class="provider-card-logo" style="background: {color}20;">{logo_html}</span>'
            f'<span class="provider-card-info">'
            f'<span class="provider-card-name">{escape(pname)}</span>'
            f'{loc_badges}'
            f'</span>'
            f'<span class="provider-card-count" style="color: {color};">{count}</span>'
            f'</{tag}>'
        )

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Open-weight LLM landscape — benchmarks, pricing & energy</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{
    --bg: #0c1018; --surface: #141b27; --surface-2: #1b2434;
    --text: #e6edf3; --muted: #8b97a8; --accent: #5b8def;
    --agentic: #2dd4bf; --coding: #60a5fa; --intel: #c084fc;
    --border: #243044;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg); color: var(--text); font-family: ui-sans-serif, system-ui, -apple-system,
      "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0; line-height: 1.4; padding: 2rem 1rem;
  }}
  .container {{ max-width: 1180px; margin: 0 auto; }}
  header {{ margin-bottom: 2rem; }}
  h1 {{ margin: 0 0 0.4rem; font-size: 1.7rem; }}
  h2.section {{ font-size: 1.15rem; margin: 2.5rem 0 0.8rem; letter-spacing: 0.02em;
    scroll-margin-top: 1rem; }}
  .anchor {{ color: var(--muted); text-decoration: none; margin-right: 0.4rem; font-weight: 400; }}
  .anchor:hover {{ color: var(--accent); }}
  header p {{ color: var(--muted); margin: 0.3rem 0; font-size: 0.95rem; }}
  header a {{ color: var(--accent); }}
  .legend {{ display: flex; gap: 1rem; flex-wrap: wrap; font-size: 0.85rem; color: var(--muted); margin-top: 0.6rem; }}
  .legend .swatch {{ display: inline-block; width: 0.8em; height: 0.8em; border-radius: 3px; margin-right: 0.3em; vertical-align: middle; }}
  .scatter-wrap {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 1rem 1.2rem 1.4rem;
    margin-top: 0.5rem;
  }}
  #scatter-plot {{ width: 100%; overflow: visible; }}
  #scatter-plot svg {{ width: 100%; height: auto; font-family: inherit; }}
  .plot-note {{ color: var(--muted); font-size: 0.8rem; margin-top: 0.6rem; }}
  /* Custom provider legend for the scatter plot. */
  .provider-legend {{
    display: flex; flex-wrap: wrap; gap: 0.5rem 0.9rem; margin: 0.8rem 0 1.2rem; font-size: 0.85rem;
  }}
  .legend-item {{ display: inline-flex; align-items: center; gap: 0.35rem; cursor: default;
    padding: 0.15rem 0.5rem 0.15rem 0.35rem; border-radius: 999px;
    border: 1px solid var(--border); background: var(--surface-2); }}
  .legend-item:hover {{ border-color: var(--accent); }}
  .legend-item .swatch {{ width: 0.7em; height: 0.7em; border-radius: 999px; flex: none; }}
  .legend-logo {{
    width: 20px; height: 20px; border-radius: 4px; overflow: hidden;
    display: inline-flex; align-items: center; justify-content: center; flex: none;
  }}
  .legend-logo img {{ width: 16px; height: 16px; object-fit: contain; }}
  .logo-letter {{ font-weight: 700; font-size: 0.8rem; color: var(--text); }}
  .legend-name {{ color: var(--text); }}
  /* Top 10 cards. */
  .top10 {{ display: grid; gap: 0.8rem; }}
  .card {{
    display: grid; grid-template-columns: auto 64px 1fr auto; gap: 1rem; align-items: center;
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
    padding: 1rem 1.2rem; transition: transform 0.1s ease;
  }}
  .card:hover {{ transform: translateY(-1px); border-color: var(--accent); }}
  .rank {{ font-weight: 700; color: var(--muted); font-size: 1.1rem; min-width: 2rem; }}
  .logo-wrap {{ display: flex; align-items: center; justify-content: center; width: 64px; height: 64px;
    background: var(--surface-2); border-radius: 10px; overflow: hidden; }}
  .logo {{ width: 40px; height: 40px; object-fit: contain; }}
  .logo-fallback {{ width: 100%; text-align: center; font-weight: 700; font-size: 1.5rem; color: var(--accent); }}
  .info {{ min-width: 0; }}
  .info h2 {{ margin: 0; font-size: 1.05rem; }}
  .creator {{ color: var(--muted); font-size: 0.85rem; margin-top: 0.15rem; }}
  .creator .weights {{ margin-left: 0.4rem; }}
  .meta {{ font-size: 0.75rem; color: var(--muted); margin-top: 0.3rem; display: flex; gap: 0.8rem; flex-wrap: wrap; }}
  .meta code {{ background: var(--surface-2); padding: 0 0.3em; border-radius: 3px; font-size: 0.85em; }}
  .nw-chip {{ background: color-mix(in srgb, var(--accent) 22%, transparent); color: var(--accent);
    padding: 0.05rem 0.4rem; border-radius: 999px; font-size: 0.72rem; font-weight: 600;
    border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent); display: inline-block; }}
  .cap-chip {{ padding: 0.05rem 0.35rem; border-radius: 4px; font-size: 0.68rem; font-weight: 700;
    display: inline-block; border: 1px solid var(--border); background: var(--surface-2); color: var(--muted); }}
  .cap-reasoning {{ color: var(--intel); border-color: color-mix(in srgb, var(--intel) 40%, transparent); }}
  .cap-tools {{ color: var(--coding); border-color: color-mix(in srgb, var(--coding) 40%, transparent); }}
  .cap-mod {{ text-transform: capitalize; }}
  .cap-ctx {{ font-weight: 600; color: var(--accent); border-color: color-mix(in srgb, var(--accent) 40%, transparent); }}
  .card-providers {{ margin-top: 0.4rem; display: flex; flex-wrap: wrap; gap: 0.25rem 0.5rem; align-items: center; }}
  .providers-label {{ font-size: 0.7rem; color: var(--muted); margin-right: 0.2rem; }}
  .provider-link {{ font-size: 0.72rem; color: var(--accent); text-decoration: none; padding: 0.05rem 0.35rem;
    border: 1px solid var(--border); border-radius: 4px; background: var(--surface-2); display: inline-block; }}
  .provider-link:hover {{ border-color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, transparent); }}
  /* Providers section */
  .providers-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 0.6rem; }}
  .provider-card {{ display: flex; align-items: center; gap: 0.5rem; padding: 0.6rem 0.8rem;
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    text-decoration: none; color: var(--text); transition: border-color 0.1s, transform 0.1s; }}
  .provider-card:hover {{ transform: translateY(-1px); border-color: var(--accent); }}
  .provider-card-logo {{ width: 28px; height: 28px; border-radius: 6px; overflow: hidden;
    display: inline-flex; align-items: center; justify-content: center; flex: none; }}
  .provider-card-logo img {{ width: 20px; height: 20px; object-fit: contain; }}
  .provider-card-info {{ flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 0.15rem; }}
  .provider-card-name {{ font-size: 0.85rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .provider-card-count {{ font-size: 1.2rem; font-weight: 700; font-variant-numeric: tabular-nums; flex: none; }}
  .loc-badge {{ font-size: 0.65rem; font-weight: 600; padding: 0.05rem 0.3rem; border-radius: 3px;
    background: var(--surface-2); color: var(--muted); display: inline-flex; align-items: center; gap: 0.15rem; }}
  .loc-dc {{ opacity: 0.7; }}
  .score-wrap {{ display: flex; align-items: center; gap: 0.8rem; }}
  .score.agentic {{ text-align: center; min-width: 80px; }}
  .score-value {{ font-size: 1.6rem; font-weight: 700; color: var(--agentic); }}
  .score-label {{ font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }}
  .secondary-scores {{ display: flex; flex-direction: column; gap: 0.15rem; font-size: 0.75rem; }}
  .secondary-scores div {{ display: flex; justify-content: space-between; gap: 0.5rem; color: var(--muted); }}
  .secondary-scores b {{ color: var(--text); }}
  .stats {{
    display: none; grid-template-columns: repeat(2, minmax(80px, auto)); gap: 0.4rem 1rem;
    border-left: 1px solid var(--border); padding-left: 1rem;
  }}
  .stats div {{ display: flex; flex-direction: column; font-size: 0.75rem; color: var(--muted); }}
  .stats b {{ color: var(--text); font-size: 0.9rem; }}
  @media (min-width: 820px) {{ .stats {{ display: grid; }} }}
  footer {{ color: var(--muted); font-size: 0.8rem; margin-top: 2rem; text-align: center; }}
  footer a {{ color: var(--accent); }}
  /* Floating custom tooltip bound to scatter dots. */
  .tooltip-popup {{
    position: fixed; z-index: 100; display: none;
    background: var(--surface-2); border: 1px solid var(--accent);
    border-radius: 8px; padding: 0.65rem 0.85rem; font-size: 0.8rem;
    min-width: 240px; max-width: 340px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.55);
    pointer-events: none; color: var(--text); line-height: 1.35;
  }}
  .tooltip-popup .tip-title {{ font-weight: 700; margin-bottom: 2px; }}
  .tooltip-popup .tip-provider {{ color: var(--muted); margin-bottom: 6px; }}
  .tooltip-popup .tip-row {{ display: flex; justify-content: space-between; gap: 1.2em; }}
  .tooltip-popup .tip-key {{ color: var(--muted); }}
  .tooltip-popup .tip-val {{ font-variant-numeric: tabular-nums; }}
  .tooltip-popup .tip-indent {{ padding-left: 0.8em; color: var(--muted); }}
  /* Segmented control for selecting the Y-axis metric. */
  .scatter-controls {{
    display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap;
    margin-bottom: 0.6rem;
  }}
  .scatter-controls .controls-label {{
    color: var(--muted); font-size: 0.8rem; text-transform: uppercase;
    letter-spacing: 0.05em; margin-right: 0.2rem;
  }}
  .metric-radio {{ display: inline-flex; align-items: center; cursor: pointer;
    padding: 0.25rem 0.7rem; border: 1px solid var(--border); border-radius: 999px;
    background: var(--surface-2); font-size: 0.85rem; user-select: none;
    transition: border-color 0.1s, background 0.1s; }}
  .metric-radio:hover {{ border-color: var(--accent); }}
  .metric-radio input {{ position: absolute; opacity: 0; pointer-events: none; }}
  .metric-radio .swatch {{
    display: inline-block; width: 0.6em; height: 0.6em; border-radius: 999px;
    margin-right: 0.35em; background: currentColor;
  }}
  .metric-radio[data-active="true"] {{ border-color: currentColor; background: color-mix(in srgb, currentColor 18%, transparent); }}
  .metric-radio[data-metric="agentic"] {{ color: var(--agentic); }}
  .metric-radio[data-metric="coding"] {{ color: var(--coding); }}
  .metric-radio[data-metric="intel"] {{ color: var(--intel); }}
  .metric-radio.x-metric {{ color: var(--accent); }}
  .filter-chip {{ display: inline-flex; align-items: center; cursor: pointer;
    padding: 0.25rem 0.7rem; border: 1px solid var(--border); border-radius: 999px;
    background: var(--surface-2); font-size: 0.85rem; user-select: none;
    transition: border-color 0.1s, background 0.1s; }}
  .filter-chip:hover {{ border-color: var(--accent); }}
  .filter-chip input {{ position: absolute; opacity: 0; pointer-events: none; }}
  .filter-chip[data-active="true"] {{ border-color: var(--accent); background: color-mix(in srgb, var(--accent) 18%, transparent); }}
  .filter-chip[data-active="false"] {{ opacity: 0.4; }}
  .provider-filters {{ display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap;
    margin-bottom: 0.6rem; }}
  /* Observable Plot theme tweaks to match the dark UI. */
  #scatter-plot svg .plot text {{ fill: var(--text); }}
  #scatter-plot svg .plot .axis text {{ fill: var(--muted); }}
  #scatter-plot svg .plot .axis line, #scatter-plot svg .plot .grid line {{ stroke: var(--border); }}
  #scatter-plot svg .plot .axis title {{ fill: var(--muted); }}
  #scatter-plot svg .plot .tip {{ fill: var(--surface-2); stroke: var(--border); }}
  #scatter-plot svg .plot .tip text {{ fill: var(--text); font-size: 0.8rem; }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1 id="top">Open-weight LLM landscape</h1>
    <p>Comparing benchmark scores, pricing, and energy use across open-weight language models and their providers.</p>
    <p>Built with <a href="https://artificialanalysis.ai/models/glm-5-2" target="_blank" rel="noopener">GLM-5.2</a> on
       <a href="https://neuralwatt.com" target="_blank" rel="noopener">Neuralwatt</a> ·
       <span title="Total energy and carbon cost of building this report">1.60 kWh · 193.7 g CO₂</span> ·
       Data: <a href="https://artificialanalysis.ai/api/v2/language/models/free">Artificial Analysis</a> ·
       <a href="https://models.dev/catalog.json">models.dev</a> ·
       <a href="https://portal.neuralwatt.com/energy-pricing">Neuralwatt</a> ·
       <a href="https://openrouter.ai/api/v1/providers">OpenRouter</a> · generated {plot_build_ts}</p>
   </header>

  <h2 class="section" id="scatter"><a href="#scatter" class="anchor">§</a> Model comparison — {len(scatter_data)} open-weight models</h2>
  <div class="scatter-wrap">
    <div class="provider-legend">{''.join(legend_items)}</div>
    <div class="scatter-controls">
      <span class="controls-label">X axis</span>
      <label class="metric-radio x-metric" data-xmetric="blended_cost" data-active="true">
        <input type="radio" name="xmetric" value="blended_cost" checked>
        <span>Cost / 1M tok</span>
      </label>
      <label class="metric-radio x-metric" data-xmetric="cost_per_task">
        <input type="radio" name="xmetric" value="cost_per_task">
        <span>Cost / task</span>
      </label>
      <label class="metric-radio x-metric" data-xmetric="energy_per_req">
        <input type="radio" name="xmetric" value="energy_per_req">
        <span>Energy / req</span>
      </label>
    </div>
    <div class="scatter-controls">
      <span class="controls-label">Y axis</span>
      <label class="metric-radio" data-metric="agentic" data-active="true">
        <input type="radio" name="metric" value="agentic" checked>
        <span class="swatch"></span><span>Agentic Index</span>
      </label>
      <label class="metric-radio" data-metric="coding">
        <input type="radio" name="metric" value="coding">
        <span class="swatch"></span><span>Coding Index</span>
      </label>
      <label class="metric-radio" data-metric="intel">
        <input type="radio" name="metric" value="intel">
        <span class="swatch"></span><span>Intelligence Index</span>
      </label>
    </div>
    <div class="scatter-controls">
      <span class="controls-label">Inference provider availability</span>
      <label class="filter-chip" data-loc="US" data-active="true">
        <input type="checkbox" checked> 🇺🇸 US
      </label>
      <label class="filter-chip" data-loc="CN" data-active="true">
        <input type="checkbox" checked> 🇨🇳 China
      </label>
      <label class="filter-chip" data-loc="other" data-active="true">
        <input type="checkbox" checked> 🌍 Other
      </label>
      <label class="filter-chip" data-loc="unknown" data-active="true">
        <input type="checkbox" checked> ❓ Unknown
      </label>
    </div>
    <div id="scatter-plot"></div>
    <p class="plot-note">Blended cost = <code>(7·cache + 2·input + 1·output)/10</code>. When a provider omits cache-hit pricing, the input price is used as an upper bound (cache hits are never more expensive than a regular input token). <strong>{len(scatter_data)} models shown</strong>: open-weight (via models.dev), with an Agentic Index score AND input + output pricing. Models without input or output pricing are excluded. Neuralwatt energy values in tooltips are measured when an AA model matches a NW model; otherwise the tooltip shows an <strong>estimated</strong> energy derived from the NW cost ↔ energy regression (see the Neuralwatt scatter below).</p>
  </div>

  <h2 class="section" id="neuralwatt"><a href="#neuralwatt" class="anchor">§</a> Neuralwatt — Energy use vs. cost ({len(nw_scatter_data)} models, 16k–64k band)</h2>
  <div class="scatter-wrap">
    <div id="nw-scatter-plot"></div>
    <p class="plot-note">X: NW blended cost per 1M tokens = <code>(7·cache + 2·input + 1·output)/10</code> (USD, from Neuralwatt pricing).
       Y: energy per request at the 16k–64k prompt-size band (mWh, scraped from the
       <a href="https://portal.neuralwatt.com/energy-pricing">Neuralwatt portal</a>).
       Dashed line: linear regression on base models only (excluding -fast / -short variants).
       Hover any dot for details.</p>
  </div>

  <h2 class="section" id="models"><a href="#models" class="anchor">§</a> Open-weight models — {len(rows)} models by Agentic Index</h2>
  <section class="top10">{''.join(cards)}
  </section>

  <h2 class="section" id="providers"><a href="#providers" class="anchor">§</a> Providers ({len(provider_section_rows)})</h2>
  <p style="color:var(--muted);font-size:0.85rem;margin:0 0 0.8rem;">Providers offering at least one of the {len(rows)} open-weight models above, with count of those models. Click for docs.</p>
  <div class="provider-filters" id="provider-filters">
    <span class="controls-label">HQ location</span>
    <span class="filter-chip" data-ploc="us" data-active="true" tabindex="0">🇺🇸 US</span>
    <span class="filter-chip" data-ploc="cn" data-active="true" tabindex="0">🇨🇳 China</span>
    <span class="filter-chip" data-ploc="sg" data-active="true" tabindex="0">🇸🇬 Singapore</span>
    <span class="filter-chip" data-ploc="other" data-active="true" tabindex="0">🌍 Other</span>
    <span class="filter-chip" data-ploc="unknown" data-active="true" tabindex="0">❓ Unknown</span>
  </div>
  <div class="providers-grid" id="providers-grid">{''.join(provider_cards)}
  </div>

  <footer>Logos from models.dev. Scores subject to Intelligence Index version in the AA response; see
    <a href="https://artificialanalysis.ai/methodology/intelligence-benchmarking">methodology</a>.</footer>
</div>
<script type="application/json" id="scatter-data">{json_blob}</script>
<script type="application/json" id="nw-scatter-data">{json.dumps(nw_scatter_data, separators=(",", ":"))}</script>
<script type="application/json" id="nw-regression">{json.dumps(regression, separators=(",", ":"))}</script>
<script type="application/json" id="color-map">{color_map_json}</script>
<script type="application/json" id="providers-data">{providers_json}</script>
<script src="https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@observablehq/plot@{OBSERVABLE_PLOT_VERSION}/dist/plot.umd.min.js"></script>
<script>
  (function () {{
    const data = JSON.parse(document.getElementById("scatter-data").textContent);
    const colorMap = JSON.parse(document.getElementById("color-map").textContent);
    const providers = JSON.parse(document.getElementById("providers-data").textContent);
    if (!data.length || typeof Plot === "undefined") {{
      document.getElementById("scatter-plot").innerHTML =
        '<p style="color:var(--muted)">No plot data available' +
        (typeof Plot === "undefined" ? " (Observable Plot failed to load)" : "") + ".</p>";
      return;
    }}
    const colorFor = (pidOrName) => colorMap[pidOrName] || "#5b8def";

    // Metric registries: keys match the radio input values and the JSON fields.
    const METRICS = {{
      agentic: {{ field: "agentic", label: "Agentic Index (Artificial Analysis)" }},
      coding:  {{ field: "coding",  label: "Coding Index (Artificial Analysis)" }},
      intel:   {{ field: "intel",   label: "Intelligence Index (Artificial Analysis)" }},
    }};
    const X_METRICS = {{
      blended_cost:    {{ field: "blended_cost",  label: "Blended cost / 1M tokens (USD) — 7 cache · 2 input · 1 output", fmt: (d) => "$" + d.toFixed(2) }},
      cost_per_task:   {{ field: "cost_per_task", label: "Cost per Intelligence Index task (USD)", fmt: (d) => "$" + d.toFixed(2) }},
      energy_per_req:  {{ field: "energy_per_req",  label: "Energy per request (mWh) — 16k–64k band", fmt: (d) => d + " mWh" }},
    }};

    const shortName = (name) => {{
      if (!name) return "";
      const trimmed = name.replace(/\\s*\\([^)]*\\)\\s*/g, "").trim();
      const max = 28;
      return trimmed.length <= max ? trimmed : trimmed.slice(0, max - 1) + "…";
    }};

    const fmtMoney = (n) => (n == null ? "n/a" : "$" + Number(n).toFixed(2));
    const fmtNum = (n, d = 2) => (n == null ? "n/a" : Number(n).toFixed(d));

    const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => (
      {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c]
    ));

    const relativeDate = (dateStr) => {{
      if (!dateStr) return "?";
      const d = new Date(dateStr);
      if (isNaN(d)) return dateStr;
      const now = new Date();
      const days = Math.round((now - d) / 86400000);
      let rel;
      if (days < 0) rel = "upcoming";
      else if (days === 0) rel = "today";
      else if (days < 7) rel = days + "d ago";
      else if (days < 30) rel = Math.floor(days / 7) + "w ago";
      else if (days < 365) rel = Math.floor(days / 30) + "mo ago";
      else rel = Math.floor(days / 365) + "y ago";
      return rel + " · " + dateStr.slice(0, 10);
    }};

    // Build a small HTML tooltip body, highlighting the active Y-metric row.
    const tipHtml = (d, activeMetric) => {{
      const rows = [
        ["Agentic", fmtNum(d.agentic, 2), "agentic"],
        ["Coding", fmtNum(d.coding, 1), "coding"],
        ["Intelligence", fmtNum(d.intel, 1), "intel"],
        ["Blended cost / 1M", fmtMoney(d.blended_cost), null],
        ["Cost / II task", fmtMoney(d.cost_per_task), null],
        ["  · cache hit", fmtMoney(d.cache_hit_price), null],
        ["  · input", fmtMoney(d.input_price), null],
        ["  · output", fmtMoney(d.output_price), null],
        ["Tokens/s", d.tokens_per_second != null ? Math.round(d.tokens_per_second) : "n/a", null],
        ["TTFT (s)", fmtNum(d.ttft, 2), null],
        ["E2E (s)", fmtNum(d.e2e, 2), null],
        ["Context", d.context_window ? (d.context_window >= 1000 ? Math.round(d.context_window / 1000) + "k" : d.context_window) : "n/a", null],
        ["Modalities", (d.input_modalities || "—") + " → " + (d.output_modalities || "—"), null],
        ["Capabilities", [d.reasoning && "reasoning", d.tool_call && "tools"].filter(Boolean).join(", ") || "—", null],
        ["Released", relativeDate(d.release_date), null],
      ];
      const rowsHtml = rows.map(([k, v, key]) => {{
        const isActive = key === activeMetric;
        const style = isActive
          ? "display:flex;justify-content:space-between;gap:1em;color:var(--text);font-weight:600"
          : "display:flex;justify-content:space-between;gap:1em";
        const keyStyle = isActive ? "color:var(--text)" : "color:#8b97a8";
        return "<div style=\\"" + style + "\\"><span style=\\"" + keyStyle + "\\">" +
               escapeHtml(k) + "</span><span style=\\"font-variant-numeric:tabular-nums\\">" +
               escapeHtml(String(v)) + "</span></div>";
      }}).join("");
      const linkHtml = d.weights_url
        ? "<a style=\\"display:block;margin-top:8px;color:var(--accent);text-decoration:none;pointer-events:auto;font-size:0.85rem\\" "
          + "href=\\"" + escapeHtml(d.weights_url) + "\\" target=\\"_blank\\" rel=\\"noopener\\">"
          + "View weights on Hugging Face →</a>"
        : "";
      // Neuralwatt section — only when an AA model matched a Neuralwatt model.
      const nwRows = d.nw_model_id ? [
        ["NW blended cost / 1M", fmtMoney(d.nw_blended_cost)],
        ["  · input", fmtMoney(d.nw_input_per_million)],
        ["  · output", fmtMoney(d.nw_output_per_million)],
        ["  · cached input", fmtMoney(d.nw_cached_input_per_million)],
        ["Energy @ 16k–64k", d.nw_energy_mwh_16k_64k != null ? fmtNum(d.nw_energy_mwh_16k_64k, 2) + " mWh" : "n/a"],
        ["  · cache-hit rate", d.nw_cache_hit_rate_16k_64k != null ? fmtNum(d.nw_cache_hit_rate_16k_64k, 0) + "%" : "n/a"],
        ["  · share of reqs", d.nw_request_share_16k_64k != null ? fmtNum(d.nw_request_share_16k_64k, 1) + "%" : "n/a"],
      ] : (d.nw_energy_estimated_mwh != null ? [
        ["Energy @ 16k–64k", "≈ " + fmtNum(d.nw_energy_estimated_mwh, 0) + " mWh (est.)"],
        ["  · derived from", "NW cost ↔ energy regression"],
      ] : []);
      const nwHtml = nwRows.length
        ? "<div style=\\"margin-top:8px;padding-top:6px;border-top:1px solid #243044\\">" +
          "<div style=\\"color:#5b8def;font-size:0.7rem;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:3px\\">Neuralwatt</div>" +
          nwRows.map(([k, v]) =>
            "<div style=\\"display:flex;justify-content:space-between;gap:1em\\">" +
            "<span style=\\"color:#8b97a8\\">" + escapeHtml(k) + "</span>" +
            "<span style=\\"font-variant-numeric:tabular-nums\\">" + escapeHtml(String(v)) + "</span></div>"
          ).join("") + "</div>"
        : "";
      return "<div style=\\"font-weight:600;margin-bottom:4px\\">" + escapeHtml(d.name) + "</div>" +
        "<div style=\\"color:#8b97a8;margin-bottom:6px\\">" + escapeHtml(d.provider_name) + "</div>" +
        rowsHtml + nwHtml + linkHtml;
    }};

    // Shared floating tooltip element reused across renders.
    const tooltip = document.createElement("div");
    tooltip.className = "tooltip-popup";
    document.body.appendChild(tooltip);

    function render(xMetricKey, yMetricKey) {{
      const yMetric = METRICS[yMetricKey];
      const xMetric = X_METRICS[xMetricKey];
      if (!yMetric || !xMetric) return;
      // Filter by active location filters.
      const locFilters = {{
        US: document.querySelector('.filter-chip[data-loc="US"]').dataset.active === "true",
        CN: document.querySelector('.filter-chip[data-loc="CN"]').dataset.active === "true",
        other: document.querySelector('.filter-chip[data-loc="other"]').dataset.active === "true",
        unknown: document.querySelector('.filter-chip[data-loc="unknown"]').dataset.active === "true"
      }};
      const locMatch = (d) => {{
        // provider_hqs is a comma-separated list of HQ country codes from
        // providers that offer this model WITH non-zero cache_read pricing.
        // "unknown" means no OpenRouter HQ data for any cache-read provider.
        const hqs = (d.provider_hqs || "").split(",").filter(Boolean);
        if (hqs.length === 0) return locFilters.unknown;
        return hqs.some((hq) => {{
          if (hq === "US") return locFilters.US;
          if (hq === "CN") return locFilters.CN;
          if (hq === "unknown") return locFilters.unknown;
          return locFilters.other;
        }});
      }};
      // Filter to models that have a value for BOTH metrics AND pass loc filter.
      const plotData = data.filter((d) => d[yMetric.field] != null && d[xMetric.field] != null && locMatch(d));

      const plot = Plot.plot({{
        marginTop: 24, marginRight: 40, marginBottom: 64, marginLeft: 70,
        height: 560,
        x: {{
          type: "linear",
          tickFormat: xMetric.fmt,
          label: xMetric.label,
          labelAnchor: "right", labelOffset: 40,
          grid: true,
        }},
        y: {{
          label: yMetric.label,
          labelAnchor: "top", labelOffset: 16,
          grid: true,
        }},
        marks: [
          Plot.dot(plotData, {{
            x: xMetric.field, y: yMetric.field,
            fill: (d) => colorFor(d.provider_id || d.provider_name),
            stroke: "#0c1018", strokeWidth: 1.2,
            r: 8, opacity: 0.95,
          }}),
          Plot.text(plotData, {{
            x: xMetric.field, y: yMetric.field,
            text: (d) => shortName(d.name),
            fontSize: 9.5, dx: 12, dy: -8, textAnchor: "start",
            fill: "#e6edf3", fillOpacity: 0.78, fontWeight: 500,
            pointerEvents: "none",
          }}),
        ],
      }});

      const target = document.getElementById("scatter-plot");
      target.innerHTML = "";
      target.appendChild(plot);

      // Re-bind tooltips (circles get replaced on each render).
      const circles = plot.querySelectorAll("circle");
      circles.forEach((c, i) => {{
        const d = plotData[i];
        if (!d) return;
        c.style.cursor = "pointer";
        c.addEventListener("mouseenter", () => {{
          tooltip.innerHTML = tipHtml(d, yMetricKey);
          tooltip.style.display = "block";
        }});
        c.addEventListener("mousemove", (e) => {{
          const padX = 16, padY = 16;
          const rect = tooltip.getBoundingClientRect();
          let x = e.clientX + padX;
          let y = e.clientY + padY;
          if (x + rect.width > window.innerWidth - 8) x = e.clientX - rect.width - padX;
          if (y + rect.height > window.innerHeight - 8) y = e.clientY - rect.height - padY;
          tooltip.style.left = x + "px";
          tooltip.style.top = y + "px";
        }});
        c.addEventListener("mouseleave", () => {{ tooltip.style.display = "none"; }});
      }});
    }}

    // Track current selections.
    let currentX = "blended_cost";
    let currentY = "agentic";

    // Initial render.
    render(currentX, currentY);

    // Wire up Y-axis metric radio buttons.
    document.querySelectorAll('input[name="metric"]').forEach((input) => {{
      input.addEventListener("change", (e) => {{
        currentY = e.target.value;
        document.querySelectorAll('.metric-radio:not(.x-metric)').forEach((label) => {{
          label.dataset.active = label.dataset.metric === currentY ? "true" : "false";
        }});
        render(currentX, currentY);
      }});
    }});

    // Wire up X-axis metric radio buttons.
    document.querySelectorAll('input[name="xmetric"]').forEach((input) => {{
      input.addEventListener("change", (e) => {{
        currentX = e.target.value;
        document.querySelectorAll('.metric-radio.x-metric').forEach((label) => {{
          label.dataset.active = label.dataset.xmetric === currentX ? "true" : "false";
        }});
        render(currentX, currentY);
      }});
    }});

    // Wire up scatter location filter chips.
    document.querySelectorAll('.scatter-controls .filter-chip').forEach((chip) => {{
      chip.addEventListener("click", (e) => {{
        e.preventDefault();
        const loc = chip.dataset.loc;
        const isActive = chip.dataset.active === "true";
        chip.dataset.active = isActive ? "false" : "true";
        chip.querySelector("input").checked = !isActive;
        render(currentX, currentY);
      }});
    }});
  }})();
</script>
<script>
  (function () {{
    const nwData = JSON.parse(document.getElementById("nw-scatter-data").textContent);
    const colorMap = JSON.parse(document.getElementById("color-map").textContent);
    const regression = JSON.parse(document.getElementById("nw-regression").textContent);
    if (!nwData.length || typeof Plot === "undefined") return;

    const colorFor = (name) => colorMap[name] || "#5b8def";
    const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => (
      {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c]
    ));
    const fmtMoney = (n) => (n == null ? "n/a" : "$" + Number(n).toFixed(2));
    const fmtNum = (n, d = 2) => (n == null ? "n/a" : Number(n).toFixed(d));

    const shortName = (name) => {{
      if (!name) return "";
      const trimmed = name.replace(/\\s*\\([^)]*\\)\\s*/g, "").trim();
      const max = 24;
      return trimmed.length <= max ? trimmed : trimmed.slice(0, max - 1) + "…";
    }};

    const tipHtml = (d) => {{
      const rows = [
        ["Blended cost / 1M", fmtMoney(d.blended_cost)],
        ["  · input", fmtMoney(d.input_price)],
        ["  · output", fmtMoney(d.output_price)],
        ["  · cached input", fmtMoney(d.cached_input_price)],
        ["Energy @ 16k–64k", fmtNum(d.energy_mwh, 2) + " mWh"],
        ["  · cache-hit rate", d.cache_hit_rate != null ? fmtNum(d.cache_hit_rate, 0) + "%" : "n/a"],
        ["  · share of reqs", d.request_pct != null ? fmtNum(d.request_pct, 1) + "%" : "n/a"],
      ];
      const variantTag = d.is_variant ? " <span style=\\"color:#8b97a8;font-weight:400\\">(variant)</span>" : "";
      return "<div style=\\"font-weight:600;margin-bottom:4px\\">" + escapeHtml(d.name) + variantTag + "</div>" +
        "<div style=\\"color:#8b97a8;margin-bottom:6px\\">" + escapeHtml(d.provider || "?") + "</div>" +
        "<div style=\\"color:#5b8def;font-size:0.7rem;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:3px\\">Neuralwatt</div>" +
        rows.map(([k, v]) =>
          "<div style=\\"display:flex;justify-content:space-between;gap:1em\\">" +
          "<span style=\\"color:#8b97a8\\">" + escapeHtml(k) + "</span>" +
          "<span style=\\"font-variant-numeric:tabular-nums\\">" + escapeHtml(String(v)) + "</span></div>"
        ).join("");
    }};

    const tooltip = document.createElement("div");
    tooltip.className = "tooltip-popup";
    document.body.appendChild(tooltip);

    // Build regression line data points for the dashed line mark.
    const regressionLine = [];
    if (regression.slope != null) {{
      const xMin = 0, xMax = 1.1;
      regressionLine.push({{x: xMin, y: Math.max(0, regression.slope * xMin + regression.intercept)}});
      regressionLine.push({{x: xMax, y: Math.max(0, regression.slope * xMax + regression.intercept)}});
    }}

    const plot = Plot.plot({{
      marginTop: 24, marginRight: 40, marginBottom: 64, marginLeft: 80,
      height: 480,
      x: {{
        type: "linear",
        tickFormat: (d) => "$" + d.toFixed(2),
        label: "NW blended cost per 1M tokens (USD)  —  7 cache · 2 input · 1 output",
        labelAnchor: "right", labelOffset: 40,
        grid: true,
      }},
      y: {{
        type: "linear",
        tickFormat: (d) => d + " mWh",
        label: "Energy per request (mWh)  —  16k–64k band",
        labelAnchor: "top", labelOffset: 16,
        grid: true,
      }},
      marks: [
        // Base model dots (solid fill).
        Plot.dot(nwData.filter(d => !d.is_variant), {{
          x: "blended_cost", y: "energy_mwh",
          fill: (d) => colorFor(d.provider || d.name),
          stroke: "#0c1018", strokeWidth: 1.2,
          r: 8, opacity: 0.95,
        }}),
        // Variant dots (hollow / lighter).
        Plot.dot(nwData.filter(d => d.is_variant), {{
          x: "blended_cost", y: "energy_mwh",
          fill: (d) => colorFor(d.provider || d.name),
          fillOpacity: 0.3,
          stroke: (d) => colorFor(d.provider || d.name), strokeWidth: 1.5,
          r: 6,
        }}),
        // Regression line.
        ...(regressionLine.length ? [Plot.line(regressionLine, {{
          x: "x", y: "y",
          stroke: "#5b8def", strokeWidth: 1.5, strokeDasharray: "5,4",
          opacity: 0.6,
        }})] : []),
        // Labels.
        Plot.text(nwData, {{
          x: "blended_cost", y: "energy_mwh",
          text: (d) => shortName(d.name),
          fontSize: 9.5, dx: 12, dy: -8, textAnchor: "start",
          fill: "#e6edf3", fillOpacity: 0.78, fontWeight: 500,
          pointerEvents: "none",
        }}),
      ],
    }});

    const target = document.getElementById("nw-scatter-plot");
    target.innerHTML = "";
    target.appendChild(plot);

    // Append an annotation with the regression equation.
    if (regression.slope != null) {{
      const annotation = document.createElement("div");
      annotation.style.cssText = "font-size:0.8rem;color:var(--muted);margin-top:0.6rem;font-family:ui-monospace,monospace";
      annotation.innerHTML = "y = " + regression.slope + " × cost + (" + regression.intercept + ")  ·  "
        + "r = " + regression.r + "  ·  r² = " + regression.r_squared + "  ·  n = " + regression.n
        + " (base models only)";
      target.appendChild(annotation);
    }}

    const circles = plot.querySelectorAll("circle");
    circles.forEach((c, i) => {{
      const d = nwData[i];
      if (!d) return;
      c.style.cursor = "pointer";
      c.addEventListener("mouseenter", () => {{
        tooltip.innerHTML = tipHtml(d);
        tooltip.style.display = "block";
      }});
      c.addEventListener("mousemove", (e) => {{
        const padX = 16, padY = 16;
        const rect = tooltip.getBoundingClientRect();
        let x = e.clientX + padX;
        let y = e.clientY + padY;
        if (x + rect.width > window.innerWidth - 8) x = e.clientX - rect.width - padX;
        if (y + rect.height > window.innerHeight - 8) y = e.clientY - rect.height - padY;
        tooltip.style.left = x + "px";
        tooltip.style.top = y + "px";
      }});
      c.addEventListener("mouseleave", () => {{ tooltip.style.display = "none"; }});
    }});
  }})();
</script>
<script>
  (function () {{
    function applyProviderFilters() {{
      var filters = {{}};
      document.querySelectorAll('#provider-filters .filter-chip').forEach(function (c) {{
        filters[c.dataset.ploc] = c.dataset.active === 'true';
      }});
      document.querySelectorAll('#providers-grid .provider-card').forEach(function (card) {{
        var hq = card.dataset.hq || 'unknown';
        var match;
        if (hq === 'us') match = filters.us;
        else if (hq === 'cn') match = filters.cn;
        else if (hq === 'sg') match = filters.sg;
        else if (hq === 'unknown') match = filters.unknown;
        else match = filters.other;
        card.style.display = match ? '' : 'none';
      }});
    }}
    document.querySelectorAll('#provider-filters .filter-chip').forEach(function (chip) {{
      function toggle() {{
        var isActive = chip.dataset.active === 'true';
        chip.dataset.active = isActive ? 'false' : 'true';
        applyProviderFilters();
      }}
      chip.addEventListener('click', toggle);
      chip.addEventListener('keydown', function (e) {{
        if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); toggle(); }}
      }});
    }});
  }})();
</script>
</body>
</html>
"""
    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"\nWrote HTML report: {HTML_PATH}")
    print(f"Open with: file://{HTML_PATH}")


def print_match_summary(con: duckdb.DuckDBPyConnection) -> None:
    row = con.execute(
        """
        SELECT
            COUNT(*) AS aa_total,
            COUNT(r.modelsdev_id) AS matched,
            COUNT(*) FILTER (WHERE r.open_weights IS TRUE) AS open_weight
        FROM aa_models a
        LEFT JOIN models_enriched r ON r.id = a.id
        """
    ).fetchone()
    if not row:
        return
    aa_total, matched, open_w = row
    print(
        f"\nmodels.dev match summary: {matched}/{aa_total} AA models matched a models.dev slug, "
        f"{open_w or 0} confirmed open-weight."
    )
    if (matched or 0) == 0:
        print("  (no matches — check that models.dev data was loaded.)")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-fetch", action="store_true", help="Skip AA API; use cache or sample.json")
    parser.add_argument("--force-refresh", action="store_true", help="Ignore cache TTL; re-fetch both APIs")
    parser.add_argument("--no-modelsdev", action="store_true", help="Skip models.dev fetch and enrichment")
    parser.add_argument("--no-html", action="store_true", help="Skip HTML report")
    args = parser.parse_args()

    # 1. Artificial Analysis source
    if args.no_fetch:
        aa_payload, raw_path = load_sample()
    else:
        api_key = load_api_key()
        aa_payload, raw_path = fetch_aa_models(api_key, force=args.force_refresh)

    # 2. DuckDB init + load AA
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    init_db(con)
    load_aa_models(con, aa_payload, raw_path)

    # 3. models.dev enrichment
    if not args.no_modelsdev:
        try:
            catalog = fetch_modelsdev_catalog(force=args.force_refresh)
            load_modelsdev(con, catalog)
            compute_aa_modelsdev_matches(con)
            create_enriched_view(con)
            print_match_summary(con)
        except Exception as exc:
            print(f"WARN: models.dev step failed: {exc}", file=sys.stderr)
            print("Proceeding without models.dev enrichment. Use --no-modelsdev next time to silence.", file=sys.stderr)

    # 3b. Neuralwatt enrichment — runs whenever its DuckDB tables exist.
    if not args.no_modelsdev:
        try:
            compute_aa_neuralwatt_matches(con)
            create_enriched_view(con)
        except Exception as exc:
            print(f"WARN: Neuralwatt match step failed: {exc}", file=sys.stderr)

    # 3c. OpenRouter providers — fetch + load (cached, public API).
    if not args.no_modelsdev:
        try:
            or_payload = fetch_openrouter_providers(force=args.force_refresh)
            load_openrouter_providers(con, or_payload)
        except Exception as exc:
            print(f"WARN: OpenRouter providers step failed: {exc}", file=sys.stderr)

    # 4. Top 10 demo (console + HTML)
    print_top10_agentic(con)
    if not args.no_html:
        render_top10_open_html(con)

    # 5. Export all tables to Parquet
    parquet_dir = DATA_DIR / "parquet"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    tables_to_export = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = 'BASE TABLE' ORDER BY table_name"
    ).fetchall()
    for (t,) in tables_to_export:
        con.execute(f"COPY {t} TO '{parquet_dir / (t + '.parquet')}' (FORMAT PARQUET)")
    con.execute("CREATE OR REPLACE TEMP TABLE _enriched_export AS SELECT * FROM models_enriched")
    con.execute(f"COPY _enriched_export TO '{parquet_dir / 'models_enriched.parquet'}' (FORMAT PARQUET)")
    print(f"Exported {len(tables_to_export) + 1} tables to {parquet_dir}")

    con.close()
    print(f"\nDuckDB file: {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
