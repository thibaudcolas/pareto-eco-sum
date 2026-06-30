# Contributing / Maintenance Guide

This document flags things to watch for when maintaining the Pareto eco sum project. Most of the code is in two self-contained Python scripts with inline PEP 723 metadata — no build system, no package.json.

## When to update

### Artificial Analysis

- **Intelligence Index version bumps**: the AA response carries `intelligence_index_version` (currently v4.1). When a major version bump occurs, scores are not directly comparable across versions. The `fetch_runs` table records the version per fetch.
- **Rate limit**: Free tier = 100 requests/day. Each paginated page counts as one request. With ~511 models at 200/page, that's 3 calls per full fetch. The cache TTL is 24h. Monitor `X-RateLimit-Remaining` in the script output.
- **New model fields**: if AA adds fields to the Free tier response (e.g. blended pricing, percentiles), extend the `aa_models` schema in `init_db()` and the `load_aa_models()` INSERT. The OpenAPI spec is in `artificial-analysis-openapi.yaml`.

### models.dev

- **New providers**: automatically picked up — `load_modelsdev()` iterates the full `providers` dict. The `modelsdev_provider_models` junction table (5,335 rows) is rebuilt on each run.
- **Logo CDN**: logos are at `https://models.dev/logos/{provider_id}.svg`. If a provider has no logo, the HTML falls back to the first letter of the provider name. Google Favicons (`https://www.google.com/s2/favicons?sz=64&domain=`) are the primary logo source in the providers section, with models.dev SVGs as fallback.
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
- AA → models.dev: 129/511 matched, 60 confirmed open-weight.
- AA → Neuralwatt: 8/511 matched.

### HTML report JS

The HTML report has two inline `<script>` blocks:
1. **AA scatter plot** (Observable Plot): X/Y axis switchers, location filter, custom DOM tooltips. The `render()` function filters data on both metrics and location. If you add metrics, extend `METRICS` / `X_METRICS` registries in the JS.
2. **NW scatter plot**: regression line, variant vs base dots, custom tooltips.

Both use a shared `.tooltip-popup` CSS class. There are two tooltip elements in the DOM (one per script block) — they're independently selected by `document.querySelectorAll('.tooltip-popup')[0]` and `[1]`.

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
- F-strings with double braces `{{` / `}}` for literal braces inside the JS embedded in Python f-strings.
- The HTML template is a single large f-string — be careful with `escape()` calls and CSS `{{ }}`.
- JS uses `(function () { ... })();` IIFE pattern — don't forget the trailing `()` to invoke it.
