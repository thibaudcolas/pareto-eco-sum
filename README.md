# Pareto eco sum

Working title. Data-driven exploration of open weight AI models and providers.

## Overview

This project fetches, cross-references, and visualizes data about open-weight language models from four sources:

1. **[Artificial Analysis](https://artificialanalysis.ai)** — benchmark scores (Intelligence, Coding, Agentic indices), pricing, and performance metrics for 511 LLMs.
2. **[models.dev](https://models.dev)** — provider-agnostic model metadata (open weights, modalities, context window), provider catalog, and which providers offer which models (5,335 provider→model links).
3. **[Neuralwatt](https://portal.neuralwatt.com)** — per-request energy consumption (mWh) measured from real traffic, plus Neuralwatt's own pricing, for 12 models across 7 prompt-size bands.
4. **[OpenRouter](https://openrouter.ai)** — provider metadata (86 providers): headquarters location (ISO 3166-1 alpha-2), datacenter locations, and documentation URLs.

All data is stored in a single DuckDB file (`data/pareto.duckdb`) and rendered into an interactive HTML report (`data/index.html`) with:

- **Scatter plot** of 20 open-weight models (X: blended cost or energy, Y: Agentic/Coding/Intelligence index) with Observable Plot, provider-colored dots, hover tooltips, and US/China/Other location filters.
- **Neuralwatt scatter plot** of 12 models (X: NW blended cost, Y: energy per request at 16k–64k band) with a linear regression line and correlation statistics.
- **Model cards** for all 20 open-weight models with provider logos, capability chips (reasoning, tool calling, modalities, context window), Neuralwatt energy badges, and links to every provider offering the model.
- **Providers section** listing 73 providers that offer open-weight models, with Google favicon logos, headquarters/datacenter flags, and model counts.

## Tech stack

- **[uv](https://docs.astral.sh/uv/)** with PEP 723 inline script metadata — no virtualenv setup required; `uv run --script` resolves dependencies automatically.
- **[DuckDB](https://duckdb.org/)** — single-file analytical database, ideal for joining multiple data sources locally.
- **[httpx](https://www.python-httpx.org/)** — HTTP client for API fetching.
- **[BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/)** — HTML parsing for the Neuralwatt energy-pricing table (server-rendered, no JSON API).
- **[Observable Plot](https://observablehq.com/plot/)** (via CDN) — declarative SVG charting in the browser.
- **[Google Favicons](https://www.google.com/s2/favicons)** — provider favicon lookup by domain.
- **Python 3.11+** — runs as standalone scripts with inline metadata, no package management needed.

## Quickstart

```sh
# 1. Add your Artificial Analysis API key
echo "AA_API_KEY=aa_your_key_here" > .env

# 2. Fetch Neuralwatt data (API + energy table scrape)
./fetch_neuralwatt.py

# 3. Fetch Artificial Analysis + models.dev + OpenRouter data, render HTML report
./fetch_models.py

# 4. Open the report
open data/index.html
```

## Scripts

### `fetch_models.py` (PEP 723, `uv run --script`)

Fetches Artificial Analysis models, models.dev catalog, OpenRouter providers, loads them into DuckDB, computes cross-source matches, and renders the HTML report.

```sh
./fetch_models.py                  # full pipeline: fetch all, refresh cache if stale, render HTML
./fetch_models.py --no-fetch       # use cache or sample.json; no AA API calls
./fetch_models.py --force-refresh  # ignore cache TTL; re-fetch everything
./fetch_models.py --no-modelsdev   # skip models.dev + OpenRouter + HTML
./fetch_models.py --no-html        # skip HTML report
```

### `fetch_neuralwatt.py` (PEP 723, `uv run --script`)

Fetches the Neuralwatt API (`/v1/models`) and scrapes the energy-pricing table from the portal. Run before `fetch_models.py` for the full demo.

```sh
./fetch_neuralwatt.py              # fetch API + scrape energy table
./fetch_neuralwatt.py --force-refresh   # ignore cache TTL
./fetch_neuralwatt.py --no-scrape       # API only, skip energy table
./fetch_neuralwatt.py --print-table     # pretty-print parsed energy table
```

## DuckDB schema

| Table                                | Source             | Rows  | Notes                                                              |
| ------------------------------------ | ------------------ | ----: | ------------------------------------------------------------------ |
| `aa_models`                          | Artificial Analysis |   511 | Evaluations, pricing, performance per model                        |
| `modelsdev_providers`                | models.dev         |   144 | Provider metadata (id, name, api base, doc url)                    |
| `modelsdev_models`                   | models.dev         |   220 | Canonical model entries; `open_weights`, `context_window`, modalities |
| `modelsdev_provider_models`          | models.dev         | 5,335 | Junction: every (provider, model) pair from the catalog            |
| `aa_modelsdev_matches`               | computed           |   511 | Best models.dev match per AA model                                 |
| `neuralwatt_models`                  | Neuralwatt API     |    12 | NW pricing, capabilities, limits                                   |
| `neuralwatt_energy`                  | Neuralwatt portal |    84 | LONG format: (model, band) → energy_mwh, cache_hit_rate, req_pct   |
| `aa_neuralwatt_matches`              | computed           |   511 | Best Neuralwatt match per AA model                                 |
| `openrouter_providers`               | OpenRouter API     |    86 | HQ, datacenters, domain per provider                               |
| `models_enriched` (view)             | join               |   511 | AA + models.dev + Neuralwatt + OpenRouter all joined               |
| `fetch_runs`                         | internal           |    31 | AA fetch audit log                                                 |
| `fetch_runs_neuralwatt`              | internal           |     2 | Neuralwatt fetch audit log                                          |

All tables are exported to Parquet in `data/parquet/`.

## Data sources

### Artificial Analysis

- Endpoint: `GET https://artificialanalysis.ai/api/v2/language/models/free`
- Auth: `x-api-key` header (Free / Pro / Commercial tiers).
- Free tier: 100 requests/day, ~200 models/page (3 pages = 3 API calls for 511 models).
- Response: `tier`, `intelligence_index_version` (currently v4.1), `pagination`, `data[]`. Each model has `evaluations` (intelligence/coding/agentic indices), `pricing` (input/output/cache-hit/cache-write per 1M tokens), `performance` (median tokens/s, TTFT, E2E), `artificial_analysis_intelligence_index_cost` (total + cost_per_task).
- Schema: `artificial-analysis-openapi.yaml` (`#/components/schemas/LLMModelsFreeResponse`).

### models.dev

- Endpoint: `GET https://models.dev/catalog.json` — combined `{providers, models}` payload.
- The `providers` section maps provider IDs to their metadata + the models they offer (used for the `modelsdev_provider_models` junction).
- The `models` section has one canonical entry per model with `open_weights`, `weights[]` (Hugging Face URLs), `limit.context`, `modalities`, `family`, `tool_call`, `reasoning`.
- Provider logos: `https://models.dev/logos/{provider}.svg`.

### Neuralwatt

- **API**: `GET https://api.neuralwatt.com/v1/models` (public, no auth). OpenAI-compatible `{object, data[]}` with `metadata.{display_name, pricing, capabilities, limits}`.
- **Energy table**: `GET https://portal.neuralwatt.com/energy-pricing` (server-rendered HTML, scraped with BeautifulSoup).
  - 7 prompt-size bands as columns: `0–256`, `256–1k`, `1k–4k`, `4k–16k`, `16k–64k`, `64k–256k`, `256k–1M`.
  - Each cell: energy (mWh or Wh, normalized to mWh), cache-hit rate (parsed from `title` attribute), request share %. Cells with insufficient data render "—" with `title="Gathering data..."`.

### OpenRouter

- Endpoint: `GET https://openrouter.ai/api/v1/providers` (public, no auth). Returns `{data[]}` with `slug`, `name`, `headquarters` (ISO 3166-1 alpha-2), `datacenters[]`, and URL fields (privacy/terms/status).
- Domain derived from URL fields for Google Favicon lookup.

## Slug matching

AA slugs use dashes between version digits (`gemini-3-5-flash`), models.dev IDs use dots (`google/gemini-3.5-flash`). The script normalizes by replacing dots with dashes, then matches in order:

1. Exact normalized slug match.
2. Creator-prefix fallback: AA `nvidia-nemotron-3-ultra-550b-a55b` strips `nvidia-` to match models.dev's `nemotron-3-ultra-550b-a55b`.
3. AA-slug-prefix fallback: AA omits a `-reasoning` / `-non-reasoning` suffix.

For Neuralwatt, matching is by display name: exact match first, then "AA name starts with NW display_name". Base variants preferred over `-fast`/`-short` suffixed ones.

## Documentation

- **[docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)** — maintenance guide, things to watch for, and update cadence.
- **[docs/METHODOLOGY.md](docs/METHODOLOGY.md)** — methodological details: blended cost formula, energy estimation, location classification.
- **[docs/QUERIES.md](docs/QUERIES.md)** — DuckDB schema reference and sample queries with current results.

## License

Data from Artificial Analysis requires attribution. See their [Terms of Use](https://artificialanalysis.ai/docs/legal/Terms-of-Use.pdf). Models.dev data is under their respective license. OpenRouter API data is public. Neuralwatt data is public.
