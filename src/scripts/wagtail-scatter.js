// Wagtail front-end scoring scatter: Top 10 Accuracy/success vs
// energy / cost / tokens / speed. Dataset comes from the full-run CSV
// (src/data/wagtail-evals-scores.csv), parsed with the shared loader,
// keeping the provider-colored-dot + tooltip convention of the main page.
import csvRaw from "../data/wagtail-evals-scores.csv?raw";
import colors from "../data/colors.json";
import { parseWagtailCsv, csvRowsToModels } from "./load-wagtail-data.js";
import * as Plot from "@observablehq/plot";
import {
  escapeHtml,
  fmtNum,
  shortName,
  createTooltip,
  bindCircleTooltips,
} from "./plot-common.js";

// Family colors reuse the shared provider palette (src/data/colors.json).
// TensorX and Neuralwatt are inference providers, not model families, so dots
// are colored by model family (GPT-6, Claude, Qwen, ...) instead.
const data = csvRowsToModels(parseWagtailCsv(csvRaw));

const FALLBACK_COLOR = "#5b8def";
const colorFor = (d) => colors[d.family_id] || FALLBACK_COLOR;

// Shared value formatters: cost to the cent, energy to a tenth of a Wh.
const fmtWh = (v) => v.toFixed(1) + " Wh";
const fmtUsd = (v) => "$" + v.toFixed(2);

// X metrics, swappable. Keys match the radio input values. Each metric maps
// to a median field and a total field; the median/total toggle picks one.
const X_METRICS = {
  energy_wh: {
    medianField: "energy_wh",
    totalField: "energy_wh_total",
    label: (mode) =>
      mode === "total"
        ? "Energy use — total for all 20 tasks (Wh)"
        : "Energy use — median per task (Wh)",
    title: (mode) =>
      mode === "total"
        ? "Accuracy vs. energy — each model's score against the energy used to run all 20 tasks."
        : "Accuracy vs. energy — each model's score against the energy it uses for a typical task.",
    fmt: fmtWh,
  },
  cost_usd: {
    medianField: "cost_usd",
    totalField: "cost_usd_total",
    label: (mode) =>
      mode === "total"
        ? "Cost — total for all 20 tasks (USD)"
        : "Cost — median per task (USD)",
    title: (mode) =>
      mode === "total"
        ? "Accuracy vs. cost — each model's score against the cost of running all 20 tasks."
        : "Accuracy vs. cost — each model's score against what a typical task costs.",
    fmt: fmtUsd,
  },
  tokens: {
    medianField: "tokens",
    totalField: "tokens_total",
    label: (mode) =>
      mode === "total"
        ? "Output tokens — total for all 20 tasks"
        : "Output tokens — median per task",
    title: (mode) =>
      mode === "total"
        ? "Accuracy vs. output tokens — each model's score against the tokens it writes across all 20 tasks."
        : "Accuracy vs. output tokens — each model's score against the tokens it writes for a typical task.",
    fmt: (d) => Math.round(d).toLocaleString(),
  },
  speed_seconds: {
    medianField: "speed_seconds",
    totalField: "speed_seconds_total",
    label: (mode) =>
      mode === "total"
        ? "Speed — total task time for all 20 tasks (seconds)"
        : "Speed — median task time (seconds)",
    title: (mode) =>
      mode === "total"
        ? "Accuracy vs. speed — each model's score against the time to run all 20 tasks."
        : "Accuracy vs. speed — each model's score against how long a typical task takes.",
    fmt: (d) => d + "s",
  },
};

const Y_FIELD = "accuracy_pct";

const tooltip = createTooltip();

// Current aggregate mode: "median" (default) or "total". Swapped by the
// toolbar radio inputs, shared via dataset attributes and a mode listener.
let currentMode = "median";
const modeListeners = [];
function setMode(mode) {
  if (mode === currentMode) return;
  currentMode = mode;
  for (const fn of modeListeners) fn(mode);
}

// Field accessor honoring the current mode.
const fieldFor = (metric, d) =>
  d[currentMode === "total" ? metric.totalField : metric.medianField];

// Tooltip: one block per untruncated column plus the restated accuracy. Shows
// the values for the currently selected mode.
const tipHtml = (d) => {
  const metric = X_METRICS[currentX] || X_METRICS.energy_wh;
  const xValue = fieldFor(metric, d);
  const rows = [
    [
      "Accuracy",
      d.accuracy_successful +
        "/" +
        d.accuracy_total +
        " (" +
        fmtNum(d.accuracy_pct, 1) +
        "%)",
    ],
    [
      currentMode === "total" ? "Speed (total)" : "Speed (median)",
      currentMode === "total"
        ? d.speed_seconds_total == null
          ? "n/a"
          : d.speed_seconds_total.toLocaleString() + "s"
        : d.speed_label + " (" + d.speed_seconds + "s)",
    ],
    [
      currentMode === "total"
        ? "Output tokens (total)"
        : "Output tokens (median)",
      currentMode === "total"
        ? d.tokens_total == null
          ? "n/a"
          : d.tokens_total.toLocaleString()
        : d.tokens.toLocaleString(),
    ],
    [
      currentMode === "total" ? "Cost (total)" : "Cost (median)",
      currentMode === "total"
        ? d.cost_usd_total == null
          ? "n/a"
          : fmtUsd(d.cost_usd_total)
        : fmtUsd(d.cost_usd),
    ],
    [
      currentMode === "total" ? "Energy (total)" : "Energy (median)",
      currentMode === "total"
        ? d.energy_wh_total == null
          ? "n/a"
          : fmtWh(d.energy_wh_total)
        : d.energy_wh == null
          ? "n/a"
          : fmtWh(d.energy_wh),
    ],
    ["X axis", xValue == null ? "n/a" : metric.fmt(xValue)],
    ["Provider", d.provider_name],
    ["Family", d.family],
  ];
  const rowsHtml = rows
    .map(
      ([k, v]) =>
        '<div style="display:flex;justify-content:space-between;gap:1em">' +
        '<span style="color:var(--muted)">' +
        escapeHtml(k) +
        "</span>" +
        '<span style="font-variant-numeric:tabular-nums">' +
        escapeHtml(String(v)) +
        "</span></div>",
    )
    .join("");
  return (
    '<div style="font-weight:600;margin-bottom:4px">' +
    escapeHtml(d.name) +
    "</div>" +
    rowsHtml
  );
};

function render(xMetricKey) {
  const xMetric = X_METRICS[xMetricKey];
  if (!xMetric) return;
  const xField =
    currentMode === "total" ? xMetric.totalField : xMetric.medianField;

  // Energy is only available for the measured subset; the other X metrics
  // cover every model.
  const plotData = data.filter((d) => d[Y_FIELD] != null && d[xField] != null);

  // Labels: Plot's dx/dy are scalar mark options (a function is silently
  // dropped and no transform is emitted), so all labels share one constant
  // offset via a single text mark. Every label sits at the same top-right
  // position relative to its dot: right of the dot's radius, above it.
  const labelOffset = { dx: 11, dy: -11 };

  // Pareto frontier on this axis pair: cheaper (or fewer tokens / faster) AND
  // at least as accurate. Sort by X ascending, keep strictly-better Y.
  const paretoFrontier = plotData
    .slice()
    .sort((a, b) => a[xField] - b[xField])
    .filter((d, i, arr) => {
      if (i === 0) return true;
      let maxY = -Infinity;
      for (let j = 0; j < i; j++) {
        if (arr[j][Y_FIELD] > maxY) maxY = arr[j][Y_FIELD];
      }
      return d[Y_FIELD] > maxY;
    });

  // Guarantee at least one tick beyond the right-most model: the axis keeps
  // Plot's default ticks (which continue past the data) and the domain is
  // extended by one tick step (20%) plus room for the label that sits to the
  // right of the outermost dot.
  const xDomainMax = plotData.length
    ? Math.max(...plotData.map((d) => d[xField]))
    : 1;

  const plot = Plot.plot({
    marginTop: 24,
    marginRight: 60,
    marginBottom: 64,
    marginLeft: 50,
    width: 1024,
    height: 560,
    x: {
      type: "linear",
      tickFormat: xMetric.fmt,
      label: xMetric.label(currentMode),
      labelAnchor: "right",
      labelOffset: 40,
      grid: true,
      domain: [0, xDomainMax * 1.12],
    },
    y: {
      label: "Accuracy (% of 20 tasks)",
      labelAnchor: "top",
      labelOffset: 16,
      domain: [0, 100],
      tickFormat: (d) => d + "%",
      grid: true,
    },
    marks: [
      Plot.dot(plotData, {
        x: xField,
        y: Y_FIELD,
        fill: (d) => colorFor(d),
        fillOpacity: 0.95,
        stroke: (d) => colorFor(d),
        strokeWidth: 1.2,
        r: 8,
      }),
      ...(paretoFrontier.length >= 2
        ? [
            Plot.line(paretoFrontier, {
              x: xField,
              y: Y_FIELD,
              stroke: "#f0b429",
              strokeWidth: 2,
              strokeDasharray: "6,3",
              opacity: 0.7,
            }),
          ]
        : []),
      ...(paretoFrontier.length >= 2
        ? [
            Plot.dot(paretoFrontier, {
              x: xField,
              y: Y_FIELD,
              fill: "#f0b429",
              stroke: "var(--bg)",
              strokeWidth: 1.5,
              r: 5,
              opacity: 0.9,
            }),
          ]
        : []),
      // Labels: always top-right of the dot, same offset for every model.
      Plot.text(plotData, {
        x: xField,
        y: Y_FIELD,
        text: (d) => shortName(d.name),
        fontSize: 14,
        textAnchor: "start",
        dx: labelOffset.dx,
        dy: labelOffset.dy,
        fill: "var(--text)",
        fillOpacity: 0.85,
        fontWeight: 500,
        pointerEvents: "none",
      }),
    ],
  });

  const target = document.getElementById("wagtail-scatter-plot");
  target.innerHTML = "";
  target.appendChild(plot);

  bindCircleTooltips(
    plot,
    tooltip,
    {
      points: () =>
        plotData.map((d) => ({ x: d[xField], y: d[Y_FIELD], datum: d })),
    },
    tipHtml,
  );

  // Plain-language one-liner for the current metric + aggregation, plus how
  // many models are actually plotted on this axis (energy is a subset).
  const titleEl = document.getElementById("wagtail-chart-title");
  if (titleEl) {
    titleEl.textContent =
      xMetric.title(currentMode) +
      " " +
      plotData.length +
      " of " +
      data.length +
      " models shown.";
  }
}

let currentX = "cost_usd";
render(currentX);
modeListeners.push(() => render(currentX));

document.querySelectorAll('input[name="wxmetric"]').forEach((input) => {
  input.addEventListener("change", (e) => {
    currentX = e.target.value;
    document.querySelectorAll(".metric-radio.x-metric").forEach((label) => {
      label.dataset.active =
        label.dataset.xmetric === currentX ? "true" : "false";
    });
    render(currentX);
  });
});

// Median/total toggle, shared with the table via the "wagtail-mode" inputs.
document.querySelectorAll('input[name="wmode"]').forEach((input) => {
  input.addEventListener("change", (e) => {
    setMode(e.target.value === "total" ? "total" : "median");
  });
});
