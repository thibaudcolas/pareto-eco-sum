# DuckDB Queries

All queries run against `data/pareto.duckdb`. Use `duckdb` CLI, `uv run --with duckdb python3`, or any DuckDB client.

```sh
# Interactive CLI
duckdb data/pareto.duckdb

# One-off query via uv
uv run --with duckdb python3 -c "
import duckdb
con = duckdb.connect('data/pareto.duckdb')
for r in con.execute('SELECT name, aa_intelligence_index FROM aa_models ORDER BY aa_intelligence_index DESC LIMIT 5').fetchall():
    print(r)
"
```

Parquet exports of all tables are in `data/parquet/`.

## Schema reference

### `aa_models` (734 rows)

One row per model from Artificial Analysis Free tier.

| Column | Type | Description |
|--------|------|-------------|
| `id` | VARCHAR PK | AA UUID |
| `name` | VARCHAR | Display name |
| `slug` | VARCHAR | URL-safe slug |
| `release_date` | DATE | ISO date |
| `model_creator_id` / `model_creator_name` | VARCHAR | Lab/creator |
| `aa_intelligence_index` | DOUBLE | Composite Intelligence Index score (v5) |
| `ii_total_cost` | DOUBLE | Total USD to run the Intelligence Index |
| `ii_cost_per_task_total` | DOUBLE | Average USD per task |
| `price_1m_input_tokens` | DOUBLE | $/1M input |
| `price_1m_output_tokens` | DOUBLE | $/1M output |
| `price_1m_cache_hit_tokens` | DOUBLE | $/1M cache hit (nullable) |
| `median_output_tokens_per_second` | DOUBLE | Throughput |
| `median_time_to_first_token_seconds` | DOUBLE | TTFT |
| `median_end_to_end_response_time_seconds` | DOUBLE | E2E latency |

### `models_enriched` (734 rows, view)

Joins `aa_models` + models.dev + Neuralwatt + OpenRouter. Start here for most queries.

Key added columns: `open_weights`, `modelsdev_provider_id`, `weights_urls`, `input_modalities`, `output_modalities`, `tool_call`, `reasoning`, `context_window`, `nw_model_id`, `nw_blended_cost` (not in view — computed in Python), `nw_energy_16k_64k_mwh`, `openrouter_hq` (via subquery in scatter SQL).

## Sample queries

### Q1: Top 10 open-weight models by Intelligence Index

```sql
SELECT name, model_creator_name,
       ROUND(aa_intelligence_index, 2) AS intel
FROM models_enriched
WHERE aa_intelligence_index IS NOT NULL AND open_weights IS TRUE
ORDER BY aa_intelligence_index DESC
LIMIT 10;
```

| name | creator | intel |
|------|---------|------:|
| Kimi K3.1 Pro | Kimi | 46.5 |
| DeepSeek V4.1 Flash 1017 (Max) | DeepSeek | 32.6 |
| GLM-5.3 (Max) | Z AI | 31.7 |
| MiMo-V2.6-Pro | Xiaomi | 29.9 |
| MiniMax-M3.1-Flash | MiniMax | 29.1 |
| Step 5 Preview | StepFun | 28.5 |
| GLM-5.3-Flash | Z AI | 27.8 |
| MiMo-V2.6-Flash | Xiaomi | 26.7 |
| Muse Spark Open (Xhigh) | Meta | 24.9 |
| DeepSeek V4 Pro 0813 (Max) | DeepSeek | 23.1 |

### Q2: Cheapest open-weight models by blended cost

```sql
SELECT name, model_creator_name,
  ROUND((7 * COALESCE(price_1m_cache_hit_tokens, price_1m_input_tokens)
        + 2 * price_1m_input_tokens
        + price_1m_output_tokens) / 10.0, 4) AS blended_cost,
  ROUND(aa_intelligence_index, 1) AS intel
FROM models_enriched
WHERE open_weights IS TRUE AND aa_intelligence_index IS NOT NULL
  AND price_1m_input_tokens IS NOT NULL AND price_1m_output_tokens IS NOT NULL
ORDER BY blended_cost ASC
LIMIT 10;
```

| blended_cost | name | intel |
|-------------:|------|------:|
| $0.0000 | North Mini Code | 3.4 |
| $0.0000 | Gemma 4 31B (Reasoning) | 4.4 |
| $0.0000 | Command A+ | 6.2 |
| $0.0475 | Granite 4.2 8B | 5.1 |
| $0.0580 | MiMo-V2.6-Flash | 26.7 |
| $0.0600 | Nemotron 3.5 Lightning | 5.5 |
| $0.1005 | MiniMax-M3.1-Flash | 29.1 |
| $0.1010 | GLM-5.3-Flash | 27.8 |
| $0.1200 | Nemotron 3.5 Super VL | 13.6 |
| $0.1600 | gpt-oss-120b (High) | 3.8 |

### Q3: Providers by count of open-weight models offered

```sql
SELECT p.name,
       COUNT(DISTINCT pm.slug_normalized) AS open_weight_models
FROM modelsdev_provider_models pm
INNER JOIN modelsdev_providers p ON p.id = pm.provider_id
WHERE pm.slug_normalized IN (
  SELECT slug FROM models_enriched
  WHERE open_weights IS TRUE AND aa_intelligence_index IS NOT NULL
    AND price_1m_input_tokens IS NOT NULL
)
GROUP BY p.name
ORDER BY open_weight_models DESC, p.name
LIMIT 10;
```

| open_weight_models | provider |
|-------------------:|---------|
| 19 | Kilo Gateway |
| 19 | OpenRouter |
| 18 | NanoGPT |
| 17 | Vercel AI Gateway |
| 13 | DevPass (LLM Gateway) |
| 13 | LLM Gateway |
| 12 | Deep Infra |
| 12 | Requesty |
| 11 | Merge Gateway |
| 11 | Novita AI |

### Q4: Energy efficiency — Intelligence Index points per Watt-hour

```sql
SELECT name, model_creator_name,
       ROUND(aa_intelligence_index, 2) AS intel,
       ROUND(nw_energy_16k_64k_mwh, 1) AS energy_mwh,
       ROUND(aa_intelligence_index / nw_energy_16k_64k_mwh * 1000, 4) AS score_per_wh
FROM models_enriched
WHERE nw_energy_16k_64k_mwh IS NOT NULL
  AND aa_intelligence_index IS NOT NULL
ORDER BY score_per_wh DESC;
```

| score_per_wh | name | intel | energy_mwh |
|-------------:|------|------:|-----------:|
| 334.5305 | DeepSeek V4.1 Flash 1017 (Max) | 32.6 | 97.5 |
| 293.4838 | DeepSeek V4.1 Flash 1017 (High) | 28.6 | 97.5 |
| 189.8409 | DeepSeek V4.1 Flash 1017 (Non-reasoning) | 18.5 | 97.5 |
| 89.5183 | MiMo-V2.6-Pro | 29.9 | 334.0 |
| 23.4542 | Gemma 4 31B (Reasoning) | 4.4 | 187.6 |
| 12.5180 | Kimi K3 (Low) | 17.4 | 1390.0 |

**Insight**: DeepSeek V4.1 Flash is by far the most energy-efficient family — its Max variant delivers 335 Intelligence Index points per Watt-hour, 3.7× more than MiMo-V2.6-Pro, because it runs the same ~98 mWh per request as its siblings. Kimi K3 (Low) scores higher than Gemma 4 31B but costs 7× the energy, so it lands last on efficiency.

### Q5: Non-US/CN/SG providers offering target models

```sql
SELECT pm.provider_id, p.name, orp.headquarters, pm.model_name
FROM modelsdev_provider_models pm
INNER JOIN modelsdev_providers p ON p.id = pm.provider_id
LEFT JOIN openrouter_providers orp ON orp.slug = pm.provider_id
WHERE (pm.slug_normalized IN ('glm-5-2','glm-5-1','kimi-k2-6','kimi-k2-5',
                            'minimax-m3','minimax-m2-7')
       OR pm.slug_normalized LIKE 'gemma-4%')
  AND orp.headquarters IS NOT NULL
  AND orp.headquarters NOT IN ('US','CN','SG')
ORDER BY orp.headquarters, pm.provider_id;
```

| provider_id | name | HQ | model |
|-------------|------|----|-------|
| nebius | Nebius Token Factory | NL | Kimi-K2.5 |
| inceptron | Inceptron | SE | Kimi K2.6 |

**Insight**: Only 2 providers outside the US/China/Singapore hub offer the target open-weight models. Nebius (Netherlands, HQ of Yandex's cloud spinoff) offers Kimi K2.5, and Inceptron (Sweden) offers Kimi K2.6.
