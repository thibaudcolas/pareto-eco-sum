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

This is conservative: cache hits are never more expensive than a regular input token, so using the input price as a proxy slightly overestimates cost (and thus overestimates energy via the cost-proportionality estimate). Models with $0 cache pricing (e.g. DeepSeek V4 Flash) use 0 directly.

## Cost-per-task

Artificial Analysis provides `artificial_analysis_intelligence_index_cost.cost_per_task.total_cost` — the weighted average cost in USD to run one Intelligence Index evaluation task. This is available even on the Free tier and provides a per-task cost that already accounts for the full token mix (input + reasoning + answer tokens at the model's actual pricing).

This is distinct from the blended cost per 1M tokens: cost-per-task includes the actual token counts consumed by the benchmark, not just the pricing rates.

## Energy estimation via cost proportionality

### Motivation

Neuralwatt provides measured energy per request for a small set of models at the 16k–64k prompt-size band. To estimate energy for open-weight models that don't have a direct Neuralwatt match, we use the owner's modeling assumption: **energy use is proportional to cost on Neuralwatt** (`energy = k × blended_cost`, no intercept). Cost scales with compute delivered per request, and compute is what consumes energy — so the model is as simple as possible while fitting the measured data.

### Data selection

The calibration is computed on **base models only** — excluding Neuralwatt variants with `-fast`, `-short`, or `-short-fast` suffixes. This ensures the relationship reflects the "canonical" model, not tuned variants that may have different energy/cost tradeoffs.

Base models used (n=6):

| Model             | NW blended cost | Energy (mWh) |
| ----------------- | ---------------: | -----------: |
| Qwen3.6 35B       |          $0.1933 |        64.54 |
| Gemma 4 31B       |         $0.08088 |       295.63 |
| DeepSeek V4 Flash |          $0.0756 |       302.16 |
| Kimi K2.7 Code    |          $0.6565 |       586.93 |
| GLM-5.2           |          $0.8415 |      1460.00 |
| Kimi K3           |           $2.3100 |      1900.00 |

### Calibration results

Least squares through the origin:

```
k = Σ(cost × energy) / Σ(cost²)
```

```
energy_mWh = 929.09 × blended_cost
Pearson r = 0.9097
r² = 0.8275
n = 6
```

The correlation is strong (r² ≈ 0.83): ~83% of the variance in energy consumption is explained by blended cost. The remaining 17% depends on model architecture, quantization, and serving infrastructure — factors not captured by price alone. The fitted k (929 mWh per blended $) sits between the per-model energy/cost ratios (334–3997 mWh/$), weighted toward the most expensive models, which contribute most to Σ(cost²).

### What was removed, and why

The previous model was an affine regression (`energy = slope × cost + intercept`, fitted at 1527 × cost − 302 mWh). Its negative intercept implied zero cost at ~200 mWh of energy — physically implausible — and forced two workarounds that are gone with the proportional model:

- **NW/AA cost-ratio correction**: the affine regression was fit against NW blended cost, so AA costs had to be rescaled by an average NW/AA ratio (~1.06) before prediction. Under proportionality, energy is estimated directly from the AA blended cost — `k × AA_blended_cost` — with no rescaling.
- **Energy floor**: the intercept predicted negative energy below ~$0.20/1M, so estimates were clamped at `min_measured_energy × 0.5` (≈ 55.8 mWh). With no intercept, predictions are non-negative everywhere, so no floor is needed.

### Inference rule

For every open-weight model in the scatter data:

- **Measured wins**: if the model matches a Neuralwatt model, `energy_per_req` is the measured NW energy (16k–64k band) and `energy_source` is `"measured"`.
- Otherwise, if the model has a positive AA blended cost: `energy_per_req = k × AA_blended_cost` and `energy_source` is `"estimated"`.
- Models with no cost (free models, blended cost 0 or null): `energy_per_req` and `energy_source` are null — cost 0 would imply 0 energy, which is not a meaningful estimate.

The fitted constant and fit statistics are exported in `src/data/calibration.json` (`{kind: "proportional", k, r, r_squared, n, band}`); k is in mWh per blended dollar.

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
