# Contributing / Maintenance Guide

This document flags things to watch for when maintaining the Pareto eco sum project. Most of the code is in two self-contained Python scripts with inline PEP 723 metadata — no build system, no package.json.

## When to update

### Artificial Analysis

- **Intelligence Index version bumps**: the AA response carries `intelligence_index_version` (currently v4.1). When a major version bump occurs, scores are not directly comparable across versions. The `fetch_runs` table records the version per fetch.
- **Rate limit**: Free tier = 100 requests/day. Each paginated page counts as one request. With ~511 models at 200/page, that's 3 calls per full fetch. The cache TTL is 24h. Monitor `X-RateLimit-Remaining` in the script output.
- **New model fields**: if AA adds fields to the Free tier response (e.g. blended pricing, percentiles), extend the `aa_models` schema in `init_db()` and the `load_aa_models()` INSERT. The OpenAPI spec is in `artificial-analysis-openapi.yaml`.

### models.dev

- **New providers**: automatically picked up — `load_modelsdev()` iterates the full `providers` dict. The `modelsdev_provider_models` junction table (5,335 rows) is rebuilt on each run.
- **Logo CDN**: logos are at `https://models.dev/logos/{provider_id}.svg`. If a provider has no logo, the site falls back to the first letter of the provider name. Google Favicons (`https://www.google.com/s2/favicons?sz=64&domain=`) are the primary logo source in the providers section, with models.dev SVGs as fallback (domain is exported in `providers.json`).
- **Slug normalization**: the `normalize_slug()` function replaces dots with dashes (models.dev uses `gemini-3.5-flash`, AA uses `gemini-3-5-flash`). If either source changes their slug format, this function needs updating.

### Neuralwatt

- **Energy table HTML structure**: `parse_energy_table()` uses BeautifulSoup to find the table after the heading "Average energy per request, by model and request size". If Neuralwatt changes their page structure (class names, heading text, table layout), this will break. Look for:
  - The heading text — currently matched by exact string.
  - The `<div title="...">` pattern on energy cells — the `title` attribute is parsed for cache-hit rate via regex: `Measured at a (\d+)% average cache-hit rate`.
  - The `mWh` / `Wh` unit — values above 1 Wh switch to Wh and are multiplied by 1000.
  - Empty cells with `title="Gathering data..."` — stored with `has_data = false`.
- **New models**: automatically picked up by the API fetch. The energy table scraping handles any number of rows.
- **Base vs variant classification**: `compute_aa_neuralwatt_matches()` prefers "base" models (IDs not ending in `-fast`, `-short`, `-short-fast`) when multiple NW models share a display name. If Neuralwatt introduces new variant suffixes, update the `variant_suffixes` list.

### OpenRouter

- **New providers**: automatically picked up — 86 providers at time of writing. The `domain` field is derived from the first available URL field (`terms_of_service_url`, `privacy_policy_url`, `status_page_url`) by extracting the netloc.
- **HQ/datacenter codes**: ISO 3166-1 alpha-2. The `_country_flag()` function converts these to emoji regional indicator symbols. Any new country codes will work as long as they're valid 2-letter codes.

## Things that can break

### Slug matching chain

The matching from AA models → models.dev → Neuralwatt is a multi-step process:

1. `compute_aa_modelsdev_matches()`: 3-tier match (exact slug → creator-prefix-strip → AA-slug-prefix). Change `normalize_slug()` or any of the 3 strategies and the match rate changes.
2. `compute_aa_neuralwatt_matches()`: 2-tier match (exact name → AA-starts-with-NW). Dependent on NW `display_name` matching AA `name`.
3. `create_enriched_view()`: LEFT JOINs all three sources. If any table is missing, the view still works but columns are NULL.

Match rates as of last run:
- AA → models.dev: 184/643 matched, 92 confirmed open-weight.
- AA → Neuralwatt: 14/643 matched.

### Site data export

`fetch_models.py` no longer renders HTML. It exports eight JSON files to
`src/data/` — this is the contract with the Astro front-end:

1. `scatter.json` — one entry per open-weight model with agentic score + pricing, ordered by Agentic index DESC. Includes NW pricing/energy fields, the precomputed `release_label`, and the energy outputs: `energy_per_req` (measured NW energy when the model matches Neuralwatt, else `k × AA blended cost`) and `energy_source` (`"measured"` | `"estimated"` | null when there is no energy value, e.g. free models).
2. `nw-scatter.json` — Neuralwatt models with energy at the 16k–64k band and NW pricing (`is_variant` flags `-fast`/`-short` model ids).
3. `calibration.json` — proportional cost→energy calibration (`{kind: "proportional", k, r, r_squared, n, band}`), fitted through the origin on NW base models only; k null when fewer than 3 base points.
4. `colors.json` — provider pid/name → hex color map (PROVIDER_COLORS exact → lowercase → FALLBACK_PALETTE by scatter order; NW providers appended with continuing indices).
5. `legend.json` — legend entries in scatter-data provider order (id, name, color, logo_url, letter).
6. `models.json` — model cards data (same rows, agentic DESC): resolved color, precomputed relative release label, blended cost, capabilities, and per-model provider list (doc_url=null for providers without docs).
7. `providers.json` — provider directory from `provider_section_rows` (count, HQ + flag, deduped datacenters excluding HQ, favicon domain, has_cache_read, `ptype_key` (machine value: `model-maker` | `proxy` | `other`), color). `scatter.json` uses the same machine key in its `provider_type` field.
8. `meta.json` — `{generated_at}` build timestamp (`YYYY-MM-DD HH:MM UTC`).

Every object key is always present (null for missing values) — the front-end relies on
the schema being stable. If you add or rename a key here, update the Astro site in the
same commit. Do not move data analysis to the front-end: all computation (blended-cost
formulas, energy calibration, energy estimation, provider typing, colors, relative-date labels)
stays in `fetch_models.py`. These files are committed; re-export after data refreshes.

### Cached data

All cached API responses live in `data/cache/` with 24h TTL. If you need fresh data, use `--force-refresh`. The cache files:
- `language_models_free_latest.json` — merged AA response (all pages)
- `modelsdev_catalog_latest.json` — full models.dev catalog
- `neuralwatt_models_api_latest.json` — NW API response
- `neuralwatt_energy_pricing_latest.html` — raw HTML of the energy pricing page
- `openrouter_providers_latest.json` — OR providers response

### Parquet exports

`data/parquet/` contains one Parquet file per table + `models_enriched.parquet` (the view). These are snapshots — re-export after each data refresh by running the export script. DuckDB handles this natively: `COPY table TO 'file.parquet' (FORMAT PARQUET)`.

## Code style

- No comments in the Python source (per project convention).
- The site front-end is Astro at the repo root — no JS-in-f-strings anymore; edit
  `src/` components directly (another workstream's domain).
