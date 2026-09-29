// Shared loader for the Wagtail front-end scoring dataset. Parses the raw
// full-run CSV (one row per model) into the shape used by the page and the
// scatter script: name, provider, family, accuracy, and median-per-task
// energy / cost / tokens / speed. CSV file lives in src/data/.
export function parseWagtailCsv(raw) {
  const lines = raw.trim().split("\n");
  const headers = lines[0].split(",");
  return lines.slice(1).map((line) => {
    const cells = line.split(",");
    const row = {};
    headers.forEach((h, i) => {
      row[h] = (cells[i] ?? "").trim();
    });
    return row;
  });
}

// Provider lookups: match on the model_id path prefix.
const PROVIDER_MATCHERS = [
  [
    /^openai-api\/tensorx\//,
    { provider_id: "tensorx", provider_name: "TensorX" },
  ],
  [
    /^openai-api\/neuralwatt\//,
    { provider_id: "neuralwatt", provider_name: "Neuralwatt" },
  ],
  [/^anthropic\//, { provider_id: "anthropic", provider_name: "Anthropic" }],
  [/^openai\//, { provider_id: "openai", provider_name: "OpenAI" }],
  [/^google\//, { provider_id: "google", provider_name: "Google" }],
];

// Model families for dot grouping / tooltips. Family colors match the main
// page palette (src/data/colors.json), keyed by that page's provider ids —
// TensorX and Neuralwatt are inference providers, not model families.
function familyFor(name) {
  if (name.startsWith("GPT-6")) return { id: "openai", name: "GPT-6" };
  if (name.startsWith("Claude")) return { id: "anthropic", name: "Claude" };
  if (name.startsWith("Qwen")) return { id: "alibaba", name: "Qwen" };
  if (name.startsWith("DeepSeek")) return { id: "deepseek", name: "DeepSeek" };
  if (name.startsWith("GLM")) return { id: "zhipuai", name: "GLM" };
  if (name.startsWith("Kimi")) return { id: "moonshotai", name: "Kimi" };
  if (name.startsWith("Gemma")) return { id: "google", name: "Gemma" };
  return { id: "", name: name.split(" ")[0] };
}

// Logo sources per family, mirroring the main page (index.astro): the id maps
// to a models.dev SVG logo, and domain to a Google favicon fallback. These are
// model-maker domains, not the inference providers.
const FAMILY_DOMAINS = {
  openai: "openai.com",
  anthropic: "anthropic.com",
  alibaba: "qwen.ai",
  deepseek: "deepseek.com",
  zhipuai: "z.ai",
  moonshotai: "moonshot.ai",
  google: "google.com",
};

const num = (v) => (v === "" ? null : Number(v));

function speedLabel(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.min(59, Math.round(seconds % 60));
  return m + ":" + String(s).padStart(2, "0");
}

export function csvRowsToModels(rows) {
  return rows.map((row) => {
    const name = row.model;
    const provider = (PROVIDER_MATCHERS.find(([re]) => re.test(row.model_id)) ||
      [])[1] || { provider_id: "other", provider_name: name };
    const family = familyFor(name);
    const speedSeconds = Math.round(num(row.speed_seconds_median_20) || 0);
    const speedTotal = num(row.speed_seconds_total_20);
    const round = (v, places) =>
      v == null ? null : Math.round(v * 10 ** places) / 10 ** places;
    return {
      name,
      provider_id: provider.provider_id,
      provider_name: provider.provider_name,
      family: family.name,
      family_id: family.id,
      family_domain: FAMILY_DOMAINS[family.id] || null,
      accuracy_successful: Number(row.development_passes) || 0,
      accuracy_total: Number(row.development_tasks) || 20,
      accuracy_pct: Math.round(num(row.accuracy_percent) || 0),
      speed_seconds: speedSeconds,
      speed_label: speedLabel(speedSeconds),
      tokens: Math.round(num(row.output_tokens_median_20) || 0),
      cost_usd:
        Math.round((num(row.estimated_cost_usd_median_20) || 0) * 10000) /
        10000,
      // Unmeasured energy (blank cells) becomes null, shown as “—”.
      energy_wh:
        row.energy_wh_median_20 === ""
          ? null
          : Math.round(num(row.energy_wh_median_20) * 1000) / 1000,
      // Totals across all 20 tasks, for the median/total toggle. Same
      // blank-to-null convention as the medians.
      energy_wh_total: round(num(row.energy_wh_total_20), 3),
      cost_usd_total: round(num(row.estimated_cost_usd_total_20), 4),
      tokens_total: num(row.output_tokens_total_20),
      speed_seconds_total: speedTotal == null ? null : Math.round(speedTotal),
    };
  });
}
