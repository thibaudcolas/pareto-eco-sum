# Methodology

## Blended cost formula

The blended cost per 1M tokens is a weighted average designed to reflect a realistic token mix in production:

```
blended_cost = (7 × cache_hit + 2 × input + 1 × output) / 10
```

This 7:2:1 ratio assumes that for every 10 tokens billed:
- 7 are cache hits (read from prompt cache, typically the cheapest rate),
- 2 are regular input tokens (new, uncached prompt content),
- 1 is an output token (model generation).

This weighting reflects a production scenario where prompt caching is well-utilised (70%+ of input tokens are cache hits). The same ratio is used by both Artificial Analysis (in their `price_1m_blended_7_to_2_to_1` field, available on the Pro tier) and Neuralwatt (in their blended price, derived from the same formula).

### Cache-hit fallback

When a model has no published cache-hit price (AA `price_1m_cache_hit_tokens` is null), the input price is used as an upper bound:

```
effective_cache_hit = COALESCE(cache_hit_price, input_price)
```

This is conservative: cache hits are never more expensive than a regular input token, so using the input price as a proxy slightly overestimates cost (and thus overestimates energy via the regression). Models with $0 cache pricing (e.g. DeepSeek V4 Flash) use 0 directly.

## Cost-per-task

Artificial Analysis provides `artificial_analysis_intelligence_index_cost.cost_per_task.total_cost` — the weighted average cost in USD to run one Intelligence Index evaluation task. This is available even on the Free tier and provides a per-task cost that already accounts for the full token mix (input + reasoning + answer tokens at the model's actual pricing).

This is distinct from the blended cost per 1M tokens: cost-per-task includes the actual token counts consumed by the benchmark, not just the pricing rates.

## Energy estimation via cost regression

### Motivation

Neuralwatt provides measured energy per request for a small set of models at the 16k–64k prompt-size band. To estimate energy for open-weight models that don't have a direct Neuralwatt match, we derive a linear regression from Neuralwatt's own data.

### Data selection

The regression is computed on **base models only** — excluding Neuralwatt variants with `-fast`, `-short`, or `-short-fast` suffixes. This ensures the relationship reflects the "canonical" model, not tuned variants that may have different energy/cost tradeoffs.

Base models used (n=6):

| Model            | NW blended cost | Energy (mWh) |
| ---------------- | ---------------: | -----------: |
| Qwen3.6 35B      |          $0.2237 |       111.65 |
| Kimi K2.6 (alt)  |          $0.4540 |       594.67 |
| Kimi K2.6        |          $0.5807 |       594.67 |
| Qwen3.5 397B     |          $0.6727 |       262.29 |
| Kimi K2.7 Code   |          $0.7562 |       766.63 |
| GLM-5.2          |          $0.9938 |      1480.00 |

### Regression results

```
energy_mWh = 1527.11 × blended_cost + (−301.96)
Pearson r = 0.8397
r² = 0.7050
n = 6
```

The correlation is moderate (r² ≈ 0.71). This means ~70% of the variance in energy consumption is explained by blended cost. The remaining 30% depends on model architecture, quantization, and serving infrastructure — factors not captured by price alone.

### NW/AA cost ratio correction

The same model can have different pricing on Neuralwatt vs. other providers. For example, GLM-5.2 costs $0.90/1M (blended) on Artificial Analysis but $0.99/1M on Neuralwatt. To ensure the regression input matches what the model would cost on Neuralwatt:

- For models with a Neuralwatt match: use the **NW blended cost** directly.
- For models without a NW match: estimate NW blended cost as `AA_blended_cost × avg_nw_aa_ratio`, where `avg_nw_aa_ratio` is the average (NW blended / AA blended) ratio across all matched models (currently ~1.06).

### Energy floor

The linear model predicts negative energy for costs below ~$0.20/1M (intercept = −302 mWh), which is physically implausible. Instead of clamping to 0, we use a floor of `min_measured_energy × 0.5` (≈ 55.83 mWh, half of the cheapest measured base model). This ensures even the cheapest models have a non-zero energy estimate while acknowledging the regression's limitations at the low end.

### Energy per task

For energy per Intelligence Index task, the same regression is applied to the `cost_per_task` value (after the NW/AA ratio correction). This gives an estimate of how much energy a full benchmark evaluation task would consume.

## Neuralwatt energy bands

Neuralwatt reports energy per request at 7 prompt-size bands. The site data export (and the site's charts) use the **16k–64k** band as the representative value because:

1. It has data for all 12 Neuralwatt models (the 256k–1M band is missing for 10/12 models).
2. It represents a mid-range request size typical of production workloads.
3. The cache-hit rate at this band (60–96%) reflects mature prompt caching.

Other bands remain in the `neuralwatt_energy` table for future analysis.

## Location classification

The scatter plot location filter classifies models by their **model creator's headquarters** (from OpenRouter), not the provider that serves them:

- **US**: creator HQ = United States
- **China**: creator HQ = China
- **Other**: creator HQ is any other country, or HQ is unknown (no OpenRouter match)

This is a model-level classification, not a provider-level one. A model created by a Chinese lab (e.g. ZhipuAI/GLM) but served by a US provider (e.g. NovitaAI) is classified as "China". The providers section shows the provider's own HQ/datacenter locations separately.
