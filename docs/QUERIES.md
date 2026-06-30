# DuckDB Queries

All queries run against `data/pareto.duckdb`. Use `duckdb` CLI, `uv run --with duckdb python3`, or any DuckDB client.

```sh
# Interactive CLI
duckdb data/pareto.duckdb

# One-off query via uv
uv run --with duckdb python3 -c "
import duckdb
con = duckdb.connect('data/pareto.duckdb')
for r in con.execute('SELECT name, aa_agentic_index FROM aa_models ORDER BY aa_agentic_index DESC LIMIT 5').fetchall():
    print(r)
"
```

Parquet exports of all tables are in `data/parquet/`.

## Schema reference

### `aa_models` (511 rows)

One row per model from Artificial Analysis Free tier.

| Column | Type | Description |
|--------|------|-------------|
| `id` | VARCHAR PK | AA UUID |
| `name` | VARCHAR | Display name |
| `slug` | VARCHAR | URL-safe slug |
| `release_date` | DATE | ISO date |
| `model_creator_id` / `model_creator_name` | VARCHAR | Lab/creator |
| `aa_intelligence_index` | DOUBLE | Composite intelligence score |
| `aa_coding_index` | DOUBLE | Coding sub-index |
| `aa_agentic_index` | DOUBLE | Agentic sub-index |
| `ii_total_cost` | DOUBLE | Total USD to run the Intelligence Index |
| `ii_cost_per_task_total` | DOUBLE | Average USD per task |
| `price_1m_input_tokens` | DOUBLE | $/1M input |
| `price_1m_output_tokens` | DOUBLE | $/1M output |
| `price_1m_cache_hit_tokens` | DOUBLE | $/1M cache hit (nullable) |
| `median_output_tokens_per_second` | DOUBLE | Throughput |
| `median_time_to_first_token_seconds` | DOUBLE | TTFT |
| `median_end_to_end_response_time_seconds` | DOUBLE | E2E latency |

### `models_enriched` (511 rows, view)

Joins `aa_models` + models.dev + Neuralwatt + OpenRouter. Start here for most queries.

Key added columns: `open_weights`, `modelsdev_provider_id`, `weights_urls`, `input_modalities`, `output_modalities`, `tool_call`, `reasoning`, `context_window`, `nw_model_id`, `nw_blended_cost` (not in view — computed in Python), `nw_energy_16k_64k_mwh`, `openrouter_hq` (via subquery in scatter SQL).

## Sample queries

### Q1: Top 10 open-weight models by Agentic Index

```sql
SELECT name, model_creator_name,
       ROUND(aa_agentic_index, 2) AS agentic,
       ROUND(aa_coding_index, 1) AS coding
FROM models_enriched
WHERE aa_agentic_index IS NOT NULL AND open_weights IS TRUE
ORDER BY aa_agentic_index DESC
LIMIT 10;
```

| name | creator | agentic | coding |
|------|---------|--------:|-------:|
| 43.10 | GLM-5.2 (max) | Z AI | 68.8 |
| 36.40 | DeepSeek V4 Pro (Reasoning, Max Effort) | DeepSeek | 59.4 |
| 35.40 | MiniMax-M3 | MiniMax | 58.6 |
| 31.10 | DeepSeek V4 Flash (Reasoning, Max Effort) | DeepSeek | 56.2 |
| 30.30 | Kimi K2.6 | Kimi | 56.0 |
| 29.90 | GLM-5.1 (Reasoning) | Z AI | 55.8 |
| 29.60 | Kimi K2.7 Code | Kimi | 60.8 |
| 29.10 | MiMo-V2.5-Pro | Xiaomi | 60.2 |
| 27.40 | Nemotron 3 Ultra 550B A55B (Reasoning) | NVIDIA | 49.3 |
| 27.00 | Qwen3.6 27B (Reasoning) | Alibaba | 53.7 |

### Q2: Cheapest open-weight models by blended cost

```sql
SELECT name, model_creator_name,
  ROUND((7 * COALESCE(price_1m_cache_hit_tokens, price_1m_input_tokens)
        + 2 * price_1m_input_tokens
        + price_1m_output_tokens) / 10.0, 4) AS blended_cost,
  ROUND(aa_agentic_index, 1) AS agentic
FROM models_enriched
WHERE open_weights IS TRUE AND aa_agentic_index IS NOT NULL
  AND price_1m_input_tokens IS NOT NULL AND price_1m_output_tokens IS NOT NULL
ORDER BY blended_cost ASC
LIMIT 10;
```

| blended_cost | name | agentic |
|-------------:|------|--------:|
| $0.0000 | Gemma 4 31B (Reasoning) | 14.4 |
| $0.0560 | DeepSeek V4 Flash (Reasoning, Max Effort) | 31.1 |
| $0.1360 | Gemma 4 26B A4B (Reasoning) | 11.0 |
| $0.1730 | MiMo-V2.5-Pro | 29.1 |
| $0.1730 | DeepSeek V4 Pro (Reasoning, Max Effort) | 36.4 |
| $0.1830 | Step 3.7 Flash | 21.5 |
| $0.2190 | Llama 4 Scout | 1.1 |
| $0.2220 | MiniMax-M2.7 | 25.6 |
| $0.2220 | MiniMax-M3 | 35.4 |
| $0.2750 | NVIDIA Nemotron 3 Super 120B A12B (Reasoning) | 8.7 |

### Q3: Providers by count of open-weight models offered

```sql
SELECT p.name,
       COUNT(DISTINCT pm.slug_normalized) AS open_weight_models
FROM modelsdev_provider_models pm
INNER JOIN modelsdev_providers p ON p.id = pm.provider_id
WHERE pm.slug_normalized IN (
  SELECT slug FROM models_enriched
  WHERE open_weights IS TRUE AND aa_agentic_index IS NOT NULL
    AND price_1m_input_tokens IS NOT NULL
)
GROUP BY p.name
ORDER BY open_weight_models DESC, p.name
LIMIT 10;
```

| open_weight_models | provider |
|-------------------:|---------|
| 16 | OpenRouter |
| 13 | NanoGPT |
| 13 | Vercel AI Gateway |
| 12 | Kilo Gateway |
| 11 | Cortecs |
| 11 | Hugging Face |
| 11 | LLM Gateway |
| 10 | ZenMux |
| 9 | CrofAI |
| 9 | NovitaAI |

### Q4: Energy efficiency — agentic score per Watt-hour

```sql
SELECT name, model_creator_name,
       ROUND(aa_agentic_index, 2) AS agentic,
       ROUND(nw_energy_16k_64k_mwh, 1) AS energy_mwh,
       ROUND(aa_agentic_index / nw_energy_16k_64k_mwh * 1000, 4) AS score_per_wh
FROM models_enriched
WHERE nw_energy_16k_64k_mwh IS NOT NULL
  AND aa_agentic_index IS NOT NULL
ORDER BY score_per_wh DESC;
```

| score_per_wh | name | agentic | energy_mwh |
|-------------:|------|--------:|-----------:|
| 191.6704 | Qwen3.6 35B A3B (Reasoning) | 21.4 | 111.7 |
| 75.4890 | Qwen3.5 397B A17B (Reasoning) | 19.8 | 262.3 |
| 50.9526 | Kimi K2.6 | 30.3 | 594.7 |
| 38.6105 | Kimi K2.7 Code | 29.6 | 766.6 |
| 29.1216 | GLM-5.2 (max) | 43.1 | 1480.0 |

**Insight**: Qwen3.6 35B is the most energy-efficient model — it delivers 191 agentic-score-points per Watt-hour, 2.5× more than the next best (Qwen3.5 397B). GLM-5.2 has the highest absolute agentic score but is the least efficient because it consumes 13× more energy per request than Qwen3.6 35B.

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
