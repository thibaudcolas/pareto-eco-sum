#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx>=0.27",
#     "duckdb>=1.1",
#     "python-dotenv>=1.0",
#     "pyyaml>=6.0",
# ]
# ///
"""Fetch Artificial Analysis language model data, enrich with models.dev, store in DuckDB.

The script:
  1. Fetches /api/v2/language/models/free from Artificial Analysis (cached 24h, paginated)
  2. Fetches the models.dev catalog (cached 24h)
  3. Loads both into a DuckDB file at data/pareto.duckdb
  4. Joins AA models to models.dev models on a normalized slug
  5. Prints the top 10 models by Artificial Analysis Agentic Index
  6. Exports JSON data snapshots to src/data/ for the Astro site —
     scatter data, model cards, provider directory, and colors.

Caching and API budget
-----------------------
The Free tier allows 100 requests/day. Each page of the AA endpoint counts as one
request. The script writes a merged JSON cache to data/cache/language_models_free_latest.json

Usage
  ./fetch_models.py                 # full pipeline: fetch AA, fetch models.dev, export site JSON
  ./fetch_models.py --no-fetch      # skip AA API; use cached data if present (even if
                                    # stale), else sample.json
"""

from __future__ import annotations

import argparse
import json
import math

import yaml
import os
import sys
import time
from datetime import date, datetime, timezone
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
SITE_DATA_DIR = SCRIPT_DIR / "src" / "data"

AA_BASE_URL = "https://artificialanalysis.ai/api/v2"
AA_ENDPOINT = "/language/models/free"

MODELSDEV_CATALOG_URL = "https://models.dev/catalog.json"
MODELSDEV_LOGOS_BASE = "https://models.dev/logos"

OPENROUTER_PROVIDERS_URL = "https://openrouter.ai/api/v1/providers"


# --------------------------------------------------------------------------- #
# Provider overrides
# --------------------------------------------------------------------------- #
#
# A generic mechanism for adding providers not covered by models.dev /
# OpenRouter, or overriding fields (headquarters, datacenters, ...) on
# providers that are. Each entry is a dict:
#
#   id            provider slug (matches models.dev / OpenRouter if existing)
#   name          display name
#   headquarters  ISO 3166-1 alpha-2 country code (optional, overrides OpenRouter)
#   datacenters   comma-separated ISO codes (optional, overrides OpenRouter)
#   domain        site domain used for favicon (optional)
#   doc_url       documentation URL (optional)
#   api_base      API base URL (optional; defaults to doc_url)
#   models        list of {slug, input, cache_read, output} in $/1M tokens
#                 (optional; when present the model links are injected into the
#                 models.dev provider↔model junction table)
#
# Existing provider rows are left untouched except for the fields listed above
# (which are updated only when set, via ON CONFLICT … DO UPDATE).

PROVIDER_OVERRIDES_PATH = SCRIPT_DIR / "provider_overrides.yaml"
with PROVIDER_OVERRIDES_PATH.open() as _f:
    PROVIDER_OVERRIDES: list[dict[str, Any]] = yaml.safe_load(_f)

MODEL_OVERRIDES_PATH = SCRIPT_DIR / "model_overrides.yaml"
with MODEL_OVERRIDES_PATH.open() as _f:
    MODEL_OVERRIDES: list[dict[str, Any]] = yaml.safe_load(_f)

CACHE_TTL_SECONDS = 24 * 3600
REQUEST_TIMEOUT = 30.0
SAMPLE_FILE = SCRIPT_DIR / "sample.json"

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
    "#5b8def",
    "#f97316",
    "#10b981",
    "#a855f7",
    "#ec4899",
    "#14b8a6",
    "#eab308",
    "#6366f1",
    "#84cc16",
    "#f43f5e",
    "#06b6d4",
    "#8b5cf6",
    "#fb7185",
    "#22d3ee",
    "#facc15",
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


def fetch_aa_models(
    api_key: str, *, force: bool = False
) -> tuple[dict[str, Any], Path]:
    """Fetch every page of /language/models/free and cache the merged payload."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / "language_models_free_latest.json"

    if (
        not force
        and (age := _cache_age_hours(cache_path)) is not None
        and age * 3600 < CACHE_TTL_SECONDS
    ):
        print(f"Using cached AA response (age {age:.1f}h): {cache_path}")
        return json.loads(cache_path.read_text()), cache_path

    headers = {"x-api-key": api_key, "accept": "application/json"}
    merged_data: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    page = 1
    total_pages = 1

    with httpx.Client(
        base_url=AA_BASE_URL, timeout=REQUEST_TIMEOUT, headers=headers
    ) as client:
        while page <= total_pages:
            print(f"Fetching AA page {page}/{total_pages if meta else '?'} ...")
            resp = client.get(AA_ENDPOINT, params={"page": page})

            remaining = resp.headers.get("X-RateLimit-Remaining")
            limit = resp.headers.get("X-RateLimit-Limit")
            if remaining is not None:
                print(
                    f"  rate limit: {remaining}/{limit} left today (tier={resp.headers.get('X-AA-Tier')})"
                )

            if resp.status_code == 429:
                retry = resp.headers.get("Retry-After")
                sys.exit(f"AA rate limited (HTTP 429). Retry-After={retry}s.")
            resp.raise_for_status()

            payload = resp.json()

            if not meta:
                meta = {
                    k: payload.get(k) for k in ("tier", "intelligence_index_version")
                }
                pagination = payload.get("pagination", {})
                total_pages = pagination.get("total_pages", 1)

            merged_data.extend(payload.get("data", []))
            if not payload.get("pagination", {}).get("has_more"):
                break
            page += 1

    merged = {
        "tier": meta.get("tier"),
        "intelligence_index_version": meta.get("intelligence_index_version"),
        "pagination": {
            "page": 1,
            "page_size": len(merged_data),
            "total_pages": 1,
            "has_more": False,
        },
        "data": merged_data,
        "_fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_path.write_text(json.dumps(merged, indent=2))
    print(f"Wrote merged AA cache: {cache_path} ({len(merged_data)} models)")
    return merged, cache_path


def fetch_modelsdev_catalog(*, force: bool = False) -> dict[str, Any]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / "modelsdev_catalog_latest.json"

    if (
        not force
        and (age := _cache_age_hours(cache_path)) is not None
        and age * 3600 < CACHE_TTL_SECONDS
    ):
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
    print(
        f"Wrote models.dev cache: {cache_path} ({counts['providers']} providers, {counts['models']} models)"
    )
    return payload


def load_sample() -> tuple[dict[str, Any], Path]:
    if not SAMPLE_FILE.exists():
        sys.exit(
            "No AA cache and no sample.json to fall back on. Run without --no-fetch first."
        )
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
    return chr(0x1F1E6 + (ord(code[0]) - ord("A"))) + chr(
        0x1F1E6 + (ord(code[1]) - ord("A"))
    )


def normalize_slug(slug: str | None) -> str | None:
    """Normalize a models.dev model slug to match AA slugs.

    models.dev uses dots between version digits (``gemini-3.5-flash``), while
    Artificial Analysis uses dashes (``gemini-3-5-flash``). Both are already
    lowercased; we just replace dots with dashes.
    """
    if not slug:
        return None
    return slug.lower().replace(".", "-")


def _ptype_key(ptype: str) -> str:
    """Map a display provider type to its machine key for filtering."""
    return {"Proxy": "proxy", "Model maker": "model-maker"}.get(ptype, "other")


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
    # Legacy table from before the generic overrides refactor — drop if present.
    con.execute("DROP TABLE IF EXISTS tensorx_models")
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
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS model_overrides (
            slug VARCHAR PRIMARY KEY,
            open_weights BOOLEAN,
            fetched_at TIMESTAMP
        );
        """
    )


def load_provider_overrides(con: duckdb.DuckDBPyConnection) -> None:
    """Apply provider overrides: add custom providers or override fields.

    Each entry in ``PROVIDER_OVERRIDES`` is upserted into:
      - ``modelsdev_providers`` (name / api_base / doc_url),
      - ``openrouter_providers`` (name / headquarters / datacenters / domain),
    so it appears in the providers section and model card provider lists.
    When ``models`` is present, the override's model links replace the
    provider's rows in the ``modelsdev_provider_models`` junction.

    Upserts use ``ON CONFLICT DO UPDATE SET … = COALESCE(excluded, row)`` so
    existing fields (e.g. Scaleway's privacy/terms URLs) are preserved when
    the override does not specify them.
    """
    fetched_at = datetime.now(timezone.utc)
    for prov in PROVIDER_OVERRIDES:
        pid = prov["id"]
        pname = prov.get("name")
        hq = prov.get("headquarters")
        dcs = prov.get("datacenters")
        domain = prov.get("domain")
        doc_url = prov.get("doc_url")
        api_base = prov.get("api_base") or doc_url
        models = prov.get("models") or []

        # modelsdev_providers upsert (id PK).
        con.execute(
            """
            INSERT INTO modelsdev_providers VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name     = COALESCE(excluded.name, modelsdev_providers.name),
                api_base = COALESCE(excluded.api_base, modelsdev_providers.api_base),
                doc_url  = COALESCE(excluded.doc_url, modelsdev_providers.doc_url),
                npm_package = COALESCE(excluded.npm_package, modelsdev_providers.npm_package)
            """,
            (pid, pname, api_base, doc_url, None, "", fetched_at),
        )
        # openrouter_providers upsert (slug PK) — override HQ/DCs/name/domain.
        con.execute(
            """
            INSERT INTO openrouter_providers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (slug) DO UPDATE SET
                name          = COALESCE(excluded.name, openrouter_providers.name),
                headquarters  = COALESCE(excluded.headquarters, openrouter_providers.headquarters),
                datacenters   = COALESCE(excluded.datacenters, openrouter_providers.datacenters),
                domain        = COALESCE(excluded.domain, openrouter_providers.domain)
            """,
            (pid, pname, hq, dcs, None, None, None, domain, fetched_at),
        )
        # Replace the override's model links in the junction table.
        if models:
            con.execute(
                "DELETE FROM modelsdev_provider_models WHERE provider_id = ?",
                (pid,),
            )
            con.executemany(
                "INSERT INTO modelsdev_provider_models VALUES ("
                + ", ".join(["?"] * 6)
                + ")",
                [
                    (
                        pid,
                        m["slug"],
                        m["slug"],
                        m["slug"],
                        m.get("cache_read"),
                        fetched_at,
                    )
                    for m in models
                ],
            )

        models_hint = f", {len(models)} models" if models else ""
        print(
            f"Applied provider override: {pname} ({pid}) HQ={hq} DCs={dcs}{models_hint}"
        )


def load_model_overrides(con: duckdb.DuckDBPyConnection) -> None:
    """Apply model-level overrides (open_weights) from model_overrides.yaml.

    Each entry in ``MODEL_OVERRIDES`` is upserted into the ``model_overrides``
    table keyed by AA slug. The enriched view then uses ``COALESCE(mo.open_weights,
    m.open_weights, FALSE)`` so overrides take precedence over models.dev data.

    When an entry has ``providers``, a row is injected into
    ``modelsdev_provider_models`` for each provider so the model appears as
    offered by that provider in the report even when the provider's slug
    differs from the AA slug (e.g. includes a parameter count).
    """
    fetched_at = datetime.now(timezone.utc)
    # --- model_overrides table ---
    rows = [(m["slug"], m.get("open_weights"), fetched_at) for m in MODEL_OVERRIDES]
    con.execute("DELETE FROM model_overrides")
    if rows:
        con.executemany(
            "INSERT INTO model_overrides VALUES (" + ", ".join(["?"] * 3) + ")",
            rows,
        )
    print(f"Loaded {len(rows)} model overrides from {MODEL_OVERRIDES_PATH.name}")

    # --- Inject provider→model links ---
    junction_rows: list[tuple[str, str, str, str, float | None, Any]] = []
    for m in MODEL_OVERRIDES:
        providers = m.get("providers") or []
        slug = m["slug"]
        for prov_id in providers:
            junction_rows.append((prov_id, slug, slug, slug, None, fetched_at))
    if junction_rows:
        con.executemany(
            "INSERT INTO modelsdev_provider_models VALUES ("
            + ", ".join(["?"] * 6)
            + ")",
            junction_rows,
        )
        print(
            f"  Injected {len(junction_rows)} provider→model links for {MODEL_OVERRIDES_PATH.name}"
        )


def load_aa_models(
    con: duckdb.DuckDBPyConnection, payload: dict[str, Any], raw_path: Path
) -> None:
    fetched_at = datetime.now(timezone.utc)
    version = payload.get("intelligence_index_version")
    tier = payload.get("tier")

    rows: list[tuple] = []
    seen_ids: set[str] = set()
    for m in payload.get("data", []):
        # The AA API occasionally returns duplicate entries for the same model
        # id (same fields, differing performance snapshots). Keep the first.
        mid = m.get("id")
        if mid in seen_ids:
            print(
                f"  Warning: duplicate AA model id {mid} ({m.get('slug')}) in "
                "payload; keeping first occurrence"
            )
            continue
        seen_ids.add(mid)
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
        (
            fetched_at,
            AA_BASE_URL + AA_ENDPOINT,
            tier,
            version,
            len(rows),
            str(raw_path),
        ),
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
            junction_rows.append(
                (pid, mid, slug_norm, m.get("name"), cache_read, fetched_at)
            )
    con.execute("DELETE FROM modelsdev_provider_models")
    if junction_rows:
        con.executemany(
            "INSERT INTO modelsdev_provider_models VALUES ("
            + ", ".join(["?"] * 6)
            + ")",
            junction_rows,
        )

    print(
        f"Loaded {len(prov_rows)} providers, {len(md_rows)} models, and {len(junction_rows)} provider→model links from models.dev"
    )


def fetch_openrouter_providers(*, force: bool = False) -> dict[str, Any]:
    """Fetch OpenRouter providers and cache for 24h."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / "openrouter_providers_latest.json"
    if (
        not force
        and (age := _cache_age_hours(cache_path)) is not None
        and age * 3600 < CACHE_TTL_SECONDS
    ):
        print(f"Using cached OpenRouter providers (age {age:.1f}h): {cache_path}")
        return json.loads(cache_path.read_text())
    print("Fetching OpenRouter providers...")
    with httpx.Client(
        timeout=REQUEST_TIMEOUT, headers={"accept": "application/json"}
    ) as client:
        resp = client.get(OPENROUTER_PROVIDERS_URL)
        resp.raise_for_status()
        payload = resp.json()
    cache_path.write_text(json.dumps(payload, indent=2))
    count = len(payload.get("data", []))
    print(f"Wrote cache: {cache_path} ({count} providers)")
    return payload


def load_openrouter_providers(
    con: duckdb.DuckDBPyConnection, payload: dict[str, Any]
) -> None:
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
        rows.append(
            (
                p.get("slug"),
                p.get("name"),
                p.get("headquarters"),
                ",".join(p.get("datacenters") or []),
                p.get("privacy_policy_url"),
                p.get("terms_of_service_url"),
                p.get("status_page_url"),
                domain,
                fetched_at,
            )
        )
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
                stripped = aa_slug[dash + 1 :]
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
        print(
            "Skipped AA <-> Neuralwatt matches: neuralwatt_models table not loaded yet"
        )
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

        # Strategy 1: exact name match.
        candidates = by_name.get(aa_name_l)
        if candidates:
            nw_id = sorted(candidates, key=lambda c: (0 if c[1] else 1, c[0]))[0][0]
            display_name = next(n[1] for n in nw_rows if n[0] == nw_id)
            chosen = (nw_id, display_name, "exact_name")

        # Strategy 2: AA name starts with NW display_name (longest first).
        if chosen[0] is None:
            matching = [
                (key, cands)
                for key, cands in by_name.items()
                if aa_name_l.startswith(key + " ") or aa_name_l.startswith(key + "(")
            ]
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
    print(
        f"Computed AA <-> Neuralwatt matches: {matched}/{len(out_rows)} models matched"
    )


def create_enriched_view(con: duckdb.DuckDBPyConnection) -> None:
    """A view joining AA models to models.dev AND Neuralwatt, via precomputed match tables."""
    con.execute(
        """
        CREATE OR REPLACE VIEW models_enriched AS
        SELECT
            a.*,
            m.id AS modelsdev_id,
            m.provider_id AS modelsdev_provider_id,
            COALESCE(mo.open_weights, m.open_weights, FALSE) AS open_weights,
            m.context_window AS modelsdev_context_window,
            m.weights_urls,
            m.input_modalities AS modelsdev_input_modalities,
            m.output_modalities AS modelsdev_output_modalities,
            m.tool_call AS modelsdev_tool_call,
            m.reasoning AS modelsdev_reasoning,
            p.name AS modelsdev_provider_name,
            mdvmatch.match_type AS modelsdev_match_type,
            nw.id AS nw_model_id,
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
        LEFT JOIN model_overrides mo ON mo.slug = a.slug
        LEFT JOIN aa_neuralwatt_matches nwmatch ON nwmatch.aa_model_id = a.id
        LEFT JOIN neuralwatt_models nw ON nw.id = nwmatch.neuralwatt_model_id
        LEFT JOIN neuralwatt_energy nwe
               ON nwe.model_display_name = nw.display_name
              AND nwe.band_label = '16k–64k'
        """
    )


def build_provider_type_map(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """Map provider id -> display type: Proxy, Model maker, or Other.

    Proxies are providers whose name/id contains "router"/"routing"/"gateway",
    or are known proxy services (NanoGPT, OpenCode Go, Ollama Cloud).
    Model makers are providers that appear as the creator of at least one model
    in the models.dev catalog.
    Exports use _ptype_key() machine keys (proxy / model-maker / other).
    """
    providers = dict(con.execute("SELECT id, name FROM modelsdev_providers").fetchall())
    model_maker_ids = set(
        r[0]
        for r in con.execute(
            "SELECT DISTINCT provider_id FROM modelsdev_models"
        ).fetchall()
    )
    model_maker_ids.update(
        {
            "zai",
            "alibaba-cn",
            "minimax-cn",
            "moonshotai",
            "minimax-cn-coding-plan",
            "minimax-coding-plan",
            "zhipuai-coding-plan",
            "alibaba-coding-plan",
            "alibaba-coding-plan-cn",
        }
    )
    proxy_keywords = ["router", "routing", "gateway"]
    proxy_names = {
        "nanogpt",
        "opencode go",
        "opencode zen",
        "ollama cloud",
        "hugging face",
    }
    proxy_ids = {"nanogpt", "opencode-go", "opencode-zenollama-cloud", "huggingface"}

    type_map: dict[str, str] = {}
    for pid, pname in providers.items():
        name_lower = (pname or "").lower()
        id_lower = pid.lower()
        if any(kw in name_lower or kw in id_lower for kw in proxy_keywords):
            type_map[pid] = "Proxy"
        elif name_lower in proxy_names or id_lower in proxy_ids:
            type_map[pid] = "Proxy"
        elif pid in model_maker_ids:
            type_map[pid] = "Model maker"
        else:
            type_map[pid] = "Other"
    return type_map


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
        print(
            f"  {i:>2}  {agentic or 0:>7.2f}  {coding or 0:>7.2f}  {intel or 0:>7.2f}  {name} ({creator})"
        )



def _write_json(path: Path, obj: Any) -> None:
    path.write_text(
        json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def export_site_data(con: duckdb.DuckDBPyConnection) -> None:
    """Export JSON data snapshots for the Astro site into src/data/.

    These files are the contract between the Python pipeline and the
    front-end: every object key is always present (null for missing values)
    so the site can rely on a stable schema.
    """
    SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # All rows the model cards loop uses (ALL open-weight models with agentic
    # score AND input + output pricing), ordered by agentic index DESC.
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
            a.nw_input_per_million,
            a.nw_output_per_million,
            a.nw_cached_input_per_million,
            (7 * COALESCE(a.nw_cached_input_per_million, a.nw_input_per_million)
                  + 2 * a.nw_input_per_million
                  + 1 * a.nw_output_per_million) / 10.0 AS nw_blended_cost_721,
            a.nw_energy_16k_64k_mwh,
            a.nw_cache_hit_rate_16k_64k,
            a.nw_request_share_16k_64k,
            a.modelsdev_tool_call,
            a.modelsdev_reasoning,
            a.modelsdev_context_window,
            a.modelsdev_input_modalities,
            a.modelsdev_output_modalities,
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
                SELECT COUNT(*) > 0
                FROM modelsdev_provider_models pm4
                WHERE pm4.slug_normalized = a.slug
                  AND pm4.cache_read IS NOT NULL
                  AND pm4.cache_read != 0
            ) AS has_cache_priced_provider
        FROM models_enriched a
        LEFT JOIN modelsdev_providers p ON p.id = a.modelsdev_provider_id
        WHERE a.aa_agentic_index IS NOT NULL
          AND a.open_weights IS TRUE
          AND a.price_1m_input_tokens IS NOT NULL
          AND a.price_1m_output_tokens IS NOT NULL
        ORDER BY a.aa_agentic_index DESC
        """
    ).fetchall()

    provider_type_map = build_provider_type_map(con)

    scatter_data = [
        {
            "name": r[0],
            "agentic": r[2],
            "coding": r[3],
            "intel": r[4],
            "input_price": r[5],
            "output_price": r[6],
            "cache_hit_price": r[7],
            "blended_cost": r[8],
            "cost_per_task": r[9],
            "tokens_per_second": r[10],
            "ttft": r[11],
            "e2e": r[12],
            "release_date": str(r[13]) if r[13] else None,
            "release_label": _relative_date(_parse_date(str(r[13]) if r[13] else None)),
            "provider_id": r[14] or "",
            "provider_name": r[15] or r[1],
            "provider_type": _ptype_key(provider_type_map.get(r[14] or r[1], "Other")),
            "weights_url": (r[16].split(",", 1)[0] if r[16] else None),
            "nw_model_id": r[17],
            "nw_input_per_million": r[18],
            "nw_output_per_million": r[19],
            "nw_cached_input_per_million": r[20],
            "nw_blended_cost": r[21],
            "nw_energy_mwh_16k_64k": r[22],
            "nw_cache_hit_rate_16k_64k": r[23],
            "nw_request_share_16k_64k": r[24],
            "tool_call": r[25],
            "reasoning": r[26],
            "context_window": r[27],
            "input_modalities": r[28] or "",
            "output_modalities": r[29] or "",
            "provider_hqs": r[30] or "",
            "has_cache_priced_provider": bool(r[31]) if r[31] is not None else False,
            "energy_per_req": None,
            "energy_source": None,
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
            "provider": r[2],
            "input_price": r[3],
            "output_price": r[4],
            "cached_input_price": r[5],
            "blended_cost": r[6],
            "energy_mwh": r[7],
            "cache_hit_rate": r[8],
            "request_pct": r[9],
            "is_variant": "-fast" in (r[1] or "").lower()
            or "-short" in (r[1] or "").lower(),
        }
        for r in nw_scatter_rows
    ]

    # --- Proportional calibration: energy_mwh = k × blended_cost ---
    # Owner's modeling assumption: energy use is proportional to cost on
    # Neuralwatt (no intercept). Least squares through the origin, on NW base
    # models only (excluding -fast / -short variants) so the relationship
    # reflects the "canonical" model, not tuned variants.
    calibration: dict[str, Any] = {
        "kind": "proportional",
        "k": None,
        "r": None,
        "r_squared": None,
        "n": 0,
        "band": "16k–64k",
    }
    base_points = [
        (d["blended_cost"], d["energy_mwh"])
        for d in nw_scatter_data
        if not d["is_variant"]
    ]
    if len(base_points) >= 3:
        n = len(base_points)
        xs = [p[0] for p in base_points]
        ys = [p[1] for p in base_points]
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        ss_xx = sum((x - mean_x) ** 2 for x in xs)
        ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in base_points)
        ss_yy = sum((y - mean_y) ** 2 for y in ys)
        sum_x2 = sum(x * x for x in xs)
        sum_xy = sum(x * y for x, y in base_points)
        if sum_x2 > 0:
            k = sum_xy / sum_x2
            r = ss_xy / math.sqrt(ss_xx * ss_yy) if ss_xx > 0 and ss_yy > 0 else 0.0
            calibration.update(
                {
                    "k": round(k, 2),
                    "r": round(r, 4),
                    "r_squared": round(r * r, 4),
                    "n": n,
                }
            )
            print(
                f"Neuralwatt calibration (n={n}): energy_mWh = {calibration['k']} × blended_cost (r={calibration['r']}, r²={calibration['r_squared']})"
            )

    # Inference: for ANY open-weight model with an AA blended cost, estimated
    # energy = k × AA blended cost. Measured NW energy wins when a NW match
    # exists. Models without a positive blended cost (free / unpriced) get no
    # energy value — cost 0 implies energy 0 under proportionality, which is
    # not a meaningful estimate.
    if calibration["k"] is not None:
        for entry in scatter_data:
            measured = entry.get("nw_energy_mwh_16k_64k")
            if measured is not None:
                entry["energy_per_req"] = measured
                entry["energy_source"] = "measured"
                continue
            aa_blended = entry.get("blended_cost")
            if aa_blended and aa_blended > 0:
                entry["energy_per_req"] = round(calibration["k"] * aa_blended, 2)
                entry["energy_source"] = "estimated"

    # Provider color map: brand color if known, else derive from the provider's
    # position in the list so each provider gets a stable, distinct color.
    providers_in_data: list[dict[str, str]] = []
    seen_pids: set[str] = set()
    for entry in scatter_data:
        pid = entry["provider_id"] or entry["provider_name"]
        if pid in seen_pids:
            continue
        seen_pids.add(pid)
        providers_in_data.append(
            {"id": entry["provider_id"], "name": entry["provider_name"], "pid": pid}
        )

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
            or FALLBACK_PALETTE[
                (len(providers_in_data) + nw_provider_idx) % len(FALLBACK_PALETTE)
            ]
        )
        color_map[pname] = resolved
        nw_provider_idx += 1

    legend = [
        {
            "id": prov["id"],
            "name": prov["name"],
            "color": color_map[prov["pid"]],
            "logo_url": f"{MODELSDEV_LOGOS_BASE}/{prov['id']}.svg" if prov["id"] else None,
            "letter": prov["name"][0],
        }
        for prov in providers_in_data
    ]

    # Model cards data: every row the HTML cards loop used, with the resolved
    # provider color and the relative release label precomputed.
    model_entries = []
    for r in rows:
        (
            name,
            slug,
            creator,
            release,
            agentic,
            coding,
            intel,
            p_in,
            p_out,
            p_cache,
            tps,
            provider_id,
            provider_name,
            weights_urls,
            nw_model_id,
            nw_in,
            nw_out,
            nw_energy,
            md_input_mods,
            md_tool_call,
            md_reasoning,
            md_context,
            providers_offering,
        ) = r
        border_color = (
            color_map.get(provider_id) or color_map.get(provider_name) or "#5b8def"
        )

        # Provider list: parse the tab-separated \n-delimited aggregate. Entries
        # without a doc_url keep doc_url=null (the old HTML rendered plain spans).
        providers_list = []
        if providers_offering:
            for line in providers_offering.split("\n"):
                parts = line.split("\t")
                if len(parts) >= 2 and parts[1]:
                    providers_list.append(
                        {
                            "id": parts[0],
                            "name": parts[1],
                            "doc_url": parts[2] if len(parts) >= 3 and parts[2] else None,
                        }
                    )

        model_entries.append(
            {
                "name": name,
                "slug": slug,
                "provider_name": provider_name,
                "provider_id": provider_id,
                "color": border_color,
                "release_label": _relative_date(
                    _parse_date(str(release) if release else None)
                ),
                "agentic": agentic,
                "coding": coding,
                "intel": intel,
                "blended_cost": (
                    7 * (p_cache if p_cache is not None else p_in) + 2 * p_in + p_out
                )
                / 10
                if p_in is not None
                else None,
                "tokens_per_second": tps,
                "weights_url": (weights_urls.split(",", 1)[0] if weights_urls else None),
                "nw_model_id": nw_model_id,
                "nw_energy_mwh": nw_energy if nw_model_id else None,
                "input_modalities": md_input_mods or "",
                "tool_call": md_tool_call,
                "reasoning": md_reasoning,
                "context_window": md_context,
                "providers": providers_list,
            }
        )

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
            orp.domain AS or_domain,
            MAX(CASE WHEN pm.cache_read IS NOT NULL AND pm.cache_read != 0 THEN 1 ELSE 0 END) AS has_cache_read
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
        GROUP BY pm.provider_id, p.name, p.doc_url, orp.headquarters, orp.datacenters, orp.domain
        ORDER BY open_weight_model_count DESC, p.name
        """
    ).fetchall()

    provider_entries = []
    for (
        pid,
        pname,
        doc_url,
        count,
        hq,
        dcs,
        or_domain,
        has_cache_read,
    ) in provider_section_rows:
        ptype_key = _ptype_key(provider_type_map.get(pid, "Other"))
        color = color_map.get(pid) or color_map.get(pname) or "#5b8def"

        # Derive a domain for the favicon: prefer OpenRouter-derived domain,
        # then extract from the models.dev doc_url.
        domain = or_domain
        if not domain and doc_url:
            domain = doc_url.split("//", 1)[-1].split("/", 1)[0].replace("www.", "")

        # Datacenters: deduplicate case-insensitively, exclude HQ (already shown).
        datacenters = []
        if dcs:
            seen = {hq.upper()} if hq else set()
            for dc in (d.strip() for d in dcs.split(",")):
                if dc and dc.upper() not in seen:
                    seen.add(dc.upper())
                    datacenters.append({"code": dc, "flag": _country_flag(dc)})

        provider_entries.append(
            {
                "id": pid,
                "name": pname,
                "doc_url": doc_url,
                "count": count,
                "hq": hq,
                "hq_flag": _country_flag(hq) if hq else None,
                "datacenters": datacenters,
                "domain": domain,
                "has_cache_read": bool(has_cache_read),
                "ptype_key": ptype_key,
                "color": color,
            }
        )

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    _write_json(SITE_DATA_DIR / "scatter.json", scatter_data)
    _write_json(SITE_DATA_DIR / "nw-scatter.json", nw_scatter_data)
    _write_json(SITE_DATA_DIR / "calibration.json", calibration)
    _write_json(SITE_DATA_DIR / "colors.json", color_map)
    _write_json(SITE_DATA_DIR / "legend.json", legend)
    _write_json(SITE_DATA_DIR / "models.json", model_entries)
    _write_json(SITE_DATA_DIR / "providers.json", provider_entries)
    _write_json(SITE_DATA_DIR / "meta.json", {"generated_at": generated_at})

    print(f"\nWrote site data to {SITE_DATA_DIR}:")
    print(f"  scatter.json:    {len(scatter_data)} models")
    print(f"  nw-scatter.json: {len(nw_scatter_data)} models")
    print(f"  calibration.json: n={calibration['n']} k={calibration['k']}")
    print(f"  colors.json:     {len(color_map)} providers")
    print(f"  legend.json:     {len(legend)} providers")
    print(f"  models.json:     {len(model_entries)} models")
    print(f"  providers.json:  {len(provider_entries)} providers")
    print(f"  meta.json:       generated_at={generated_at}")


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
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip AA API; use cached data if present (even if stale), else sample.json",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore cache TTL; re-fetch both APIs",
    )
    parser.add_argument(
        "--no-modelsdev",
        action="store_true",
        help="Skip models.dev fetch and enrichment",
    )
    args = parser.parse_args()

    # 1. Artificial Analysis source
    if args.no_fetch:
        cache_path = CACHE_DIR / "language_models_free_latest.json"
        if cache_path.exists():
            print(f"--no-fetch: using cached AA data (any age): {cache_path}")
            aa_payload, raw_path = json.loads(cache_path.read_text()), cache_path
        else:
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
            print(
                "Proceeding without models.dev enrichment. Use --no-modelsdev next time to silence.",
                file=sys.stderr,
            )

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

    # 3d. Provider overrides — Scaleway HQ/DC, TensorX, ArgyllDev, …
    if not args.no_modelsdev:
        try:
            load_provider_overrides(con)
        except Exception as exc:
            print(f"WARN: Provider overrides load failed: {exc}", file=sys.stderr)

    # 3e. Model overrides — open_weights corrections for models whose models.dev
    #     metadata is missing or stale (e.g. Mistral Medium 3.5).
    if not args.no_modelsdev:
        try:
            load_model_overrides(con)
            create_enriched_view(con)
        except Exception as exc:
            print(f"WARN: Model overrides load failed: {exc}", file=sys.stderr)

    # 4. Top 10 demo (console) + site data export
    print_top10_agentic(con)
    export_site_data(con)

    # 5. Export all tables to Parquet
    parquet_dir = DATA_DIR / "parquet"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    tables_to_export = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = 'BASE TABLE' ORDER BY table_name"
    ).fetchall()
    for (t,) in tables_to_export:
        con.execute(f"COPY {t} TO '{parquet_dir / (t + '.parquet')}' (FORMAT PARQUET)")
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _enriched_export AS SELECT * FROM models_enriched"
    )
    con.execute(
        f"COPY _enriched_export TO '{parquet_dir / 'models_enriched.parquet'}' (FORMAT PARQUET)"
    )
    print(f"Exported {len(tables_to_export) + 1} tables to {parquet_dir}")

    con.close()
    print(f"\nDuckDB file: {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
