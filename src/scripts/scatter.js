// Main scatter: open-weight models, benchmarks vs cost/energy.
// Data comes from build-time JSON imports, Plot from npm.
import scatterData from "../data/scatter.json";
import colorMap from "../data/colors.json";
import * as Plot from "@observablehq/plot";
import {
  escapeHtml, fmtMoney, fmtNum, shortName, createTooltip, bindCircleTooltips,
} from "./plot-common.js";

const data = scatterData;

if (data.length) {
  const colorFor = (pidOrName) => colorMap[pidOrName] || "#5b8def";

  // Metric registries: keys match the radio input values and the JSON fields.
  const METRICS = {
    agentic: { field: "agentic", label: "Agentic Index (Artificial Analysis)" },
    coding:  { field: "coding",  label: "Coding Index (Artificial Analysis)" },
    intel:   { field: "intel",   label: "Intelligence Index (Artificial Analysis)" },
  };
  const X_METRICS = {
    energy_per_req:  { field: "energy_per_req",  label: "Energy per request (mWh) — 16k–64k band", fmt: (d) => d + " mWh" },
    blended_cost:    { field: "blended_cost",  label: "Blended cost / 1M tokens (USD) — 7 cache · 2 input · 1 output", fmt: (d) => "$" + d.toFixed(2) },
    cost_per_task:   { field: "cost_per_task", label: "Cost per Intelligence Index task (USD)", fmt: (d) => "$" + d.toFixed(2) },
  };

  // Build a small HTML tooltip body, highlighting the active Y-metric row.
  const tipHtml = (d, activeMetric) => {
    const rows = [
      ["Agentic", fmtNum(d.agentic, 2), "agentic"],
      ["Coding", fmtNum(d.coding, 1), "coding"],
      ["Intelligence", fmtNum(d.intel, 1), "intel"],
      ["Energy @ 16k–64k", d.energy_per_req != null ? fmtNum(d.energy_per_req, 2) + " mWh" : "n/a"],
      ["  · cache hit", fmtMoney(d.cache_hit_price), null],
      ["  · input", fmtMoney(d.input_price), null],
      ["  · output", fmtMoney(d.output_price), null],
      ["Tokens/s", d.tokens_per_second != null ? Math.round(d.tokens_per_second) : "n/a", null],
      ["TTFT (s)", fmtNum(d.ttft, 2), null],
      ["E2E (s)", fmtNum(d.e2e, 2), null],
      ["Context", d.context_window ? (d.context_window >= 1000 ? Math.round(d.context_window / 1000) + "k" : d.context_window) : "n/a", null],
      ["Modalities", (d.input_modalities || "—") + " → " + (d.output_modalities || "—"), null],
      ["Capabilities", [d.reasoning && "reasoning", d.tool_call && "tools"].filter(Boolean).join(", ") || "—", null],
      ["Released", d.release_label || "?", null],
    ];
    const rowsHtml = rows.map(([k, v, key]) => {
      const isActive = key === activeMetric;
      const style = isActive
        ? "display:flex;justify-content:space-between;gap:1em;color:var(--text);font-weight:600"
        : "display:flex;justify-content:space-between;gap:1em";
      const keyStyle = isActive ? "color:var(--text)" : "color:var(--muted)";
      return "<div style=\"" + style + "\"><span style=\"" + keyStyle + "\">" +
        escapeHtml(k) + "</span><span style=\"font-variant-numeric:tabular-nums\">" +
        escapeHtml(String(v)) + "</span></div>";
    }).join("");
    const linkHtml = d.weights_url
      ? "<a style=\"display:block;margin-top:8px;color:var(--accent);text-decoration:none;pointer-events:auto;font-size:0.85rem\" "
        + "href=\"" + escapeHtml(d.weights_url) + "\" target=\"_blank\" rel=\"noopener\">"
        + "View weights on Hugging Face →</a>"
      : "";
    // Neuralwatt section — only when an AA model matched a Neuralwatt model.
    // Energy source line distinguishes measured (NW-matched) vs estimated
    // (k × blended cost) energy values.
    const energySourceLine = d.energy_source === "measured" ? "measured (Neuralwatt)"
      : d.energy_source === "estimated" ? "estimated (k × blended cost)" : null;
    const nwRows = d.nw_model_id ? [
      ["Energy source", energySourceLine || "n/a"],
      ["NW blended cost / 1M", fmtMoney(d.nw_blended_cost)],
      ["  · input", fmtMoney(d.nw_input_per_million)],
      ["  · output", fmtMoney(d.nw_output_per_million)],
      ["  · cached input", fmtMoney(d.nw_cached_input_per_million)],
      ["NW energy @ 16k–64k", d.nw_energy_mwh_16k_64k != null ? fmtNum(d.nw_energy_mwh_16k_64k, 2) + " mWh" : "n/a"],
      ["  · cache-hit rate", d.nw_cache_hit_rate_16k_64k != null ? fmtNum(d.nw_cache_hit_rate_16k_64k, 0) + "%" : "n/a"],
      ["  · share of reqs", d.nw_request_share_16k_64k != null ? fmtNum(d.nw_request_share_16k_64k, 1) + "%" : "n/a"],
    ] : (energySourceLine ? [
      ["Energy source", energySourceLine],
    ] : []);
    const nwHtml = nwRows.length
      ? "<div style=\"margin-top:8px;padding-top:6px;border-top:1px solid var(--border)\">" +
        "<div style=\"color:var(--accent);font-size:0.7rem;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:3px\">Neuralwatt</div>" +
        nwRows.map(([k, v]) =>
          "<div style=\"display:flex;justify-content:space-between;gap:1em\">" +
          "<span style=\"color:var(--muted)\">" + escapeHtml(k) + "</span>" +
          "<span style=\"font-variant-numeric:tabular-nums\">" + escapeHtml(String(v)) + "</span></div>"
        ).join("") + "</div>"
      : "";
    return "<div style=\"font-weight:600;margin-bottom:4px\">" + escapeHtml(d.name) + "</div>" +
      "<div style=\"color:var(--muted);margin-bottom:6px\">" + escapeHtml(d.provider_name) + "</div>" +
      rowsHtml + nwHtml + linkHtml;
  };

  const tooltip = createTooltip();

  function render(xMetricKey, yMetricKey) {
    const yMetric = METRICS[yMetricKey];
    const xMetric = X_METRICS[xMetricKey];
    if (!yMetric || !xMetric) return;
    // Filter by active location filters.
    const locFilters = {
      US: document.querySelector('.filter-chip[data-loc="US"]').dataset.active === "true",
      CN: document.querySelector('.filter-chip[data-loc="CN"]').dataset.active === "true",
      other: document.querySelector('.filter-chip[data-loc="other"]').dataset.active === "true",
      unknown: document.querySelector('.filter-chip[data-loc="unknown"]').dataset.active === "true"
    };
    const locMatch = (d) => {
      // KV cache filter: if "Priced" is off and this model has cache-priced
      // providers, hide it. If "Not priced" is off and this model has NO cache-priced
      // providers, hide it.
      const kvPriced = document.querySelector('.filter-chip[data-kv="priced"]').dataset.active === "true";
      const kvAll = document.querySelector('.filter-chip[data-kv="all"]').dataset.active === "true";
      if (d.has_cache_priced_provider && !kvPriced) return false;
      if (!d.has_cache_priced_provider && !kvAll) return false;
      // Provider type filter.
      const ptypeFilters = {
        "model-maker": document.querySelector('.filter-chip[data-ptype="model-maker"]').dataset.active === "true",
        proxy: document.querySelector('.filter-chip[data-ptype="proxy"]').dataset.active === "true",
        other: document.querySelector('.filter-chip[data-ptype="other"]').dataset.active === "true",
      };
      if (!ptypeFilters[d.provider_type]) return false;
      // Location filter.
      const hqs = (d.provider_hqs || "").split(",").filter(Boolean);
      if (hqs.length === 0) return locFilters.unknown;
      return hqs.some((hq) => {
        if (hq === "US") return locFilters.US;
        if (hq === "CN") return locFilters.CN;
        if (hq === "unknown") return locFilters.unknown;
        return locFilters.other;
      });
    };
    // Filter to models that have a value for BOTH metrics AND pass loc filter.
    const plotData = data.filter((d) => d[yMetric.field] != null && d[xMetric.field] != null && locMatch(d));

    // Pareto frontier: points that are not dominated (no other point is both
    // further left AND higher). Sort by X ascending, keep points where Y is
    // strictly greater than the max Y seen so far.
    const paretoFrontier = plotData
      .slice()
      .sort((a, b) => a[xMetric.field] - b[xMetric.field])
      .filter((d, i, arr) => {
        if (i === 0) return true;
        let maxY = -Infinity;
        for (let j = 0; j < i; j++) {
          if (arr[j][yMetric.field] > maxY) maxY = arr[j][yMetric.field];
        }
        return d[yMetric.field] > maxY;
      });

    const plot = Plot.plot({
      marginTop: 24, marginRight: 60, marginBottom: 64, marginLeft: 50,
      height: 560,
      x: {
        type: "linear",
        tickFormat: xMetric.fmt,
        label: xMetric.label,
        labelAnchor: "right", labelOffset: 40,
        grid: true,
      },
      y: {
        label: yMetric.label,
        labelAnchor: "top", labelOffset: 16,
        grid: true,
      },
      marks: [
      // Measured vs estimated energy: solid = measured (NW-matched),
      // hollow (fill transparent, provider-colored ring) = estimated
      // (k × blended cost).
      Plot.dot(plotData, {
        x: xMetric.field, y: yMetric.field,
        fill: (d) => d.energy_source === "estimated" ? "none" : colorFor(d.provider_id || d.provider_name),
        fillOpacity: (d) => d.energy_source === "estimated" ? 0 : 0.95,
        stroke: (d) => colorFor(d.provider_id || d.provider_name),
        strokeWidth: (d) => d.energy_source === "estimated" ? 1.8 : 1.2,
        r: (d) => d.energy_source === "estimated" ? 6.5 : 8,
      }),
        // Pareto frontier line.
        ...(paretoFrontier.length >= 2 ? [Plot.line(paretoFrontier, {
          x: xMetric.field, y: yMetric.field,
          stroke: "#f0b429", strokeWidth: 2, strokeDasharray: "6,3",
          opacity: 0.7,
        })] : []),
        // Pareto frontier dots (highlighted).
        ...(paretoFrontier.length >= 2 ? [Plot.dot(paretoFrontier, {
          x: xMetric.field, y: yMetric.field,
          fill: "#f0b429", stroke: "var(--bg)", strokeWidth: 1.5,
          r: 5, opacity: 0.9,
        })] : []),
        Plot.text(plotData, {
          x: xMetric.field, y: yMetric.field,
          text: (d) => shortName(d.name),
          fontSize: 9.5, dx: 12, dy: -8, textAnchor: "start",
          fill: "var(--text)", fillOpacity: 0.78, fontWeight: 500,
          pointerEvents: "none",
        }),
      ],
    });

    const target = document.getElementById("scatter-plot");
    target.innerHTML = "";
    target.appendChild(plot);

    bindCircleTooltips(plot, tooltip, {
      points: () => plotData.map((d) => ({ x: d[xMetric.field], y: d[yMetric.field], datum: d })),
    }, (d) => tipHtml(d, yMetricKey));

  }

  // Track current selections.
  let currentX = "energy_per_req";
  let currentY = "agentic";

  // Initial render.
  render(currentX, currentY);

  // Wire up Y-axis metric radio buttons.
  document.querySelectorAll('input[name="metric"]').forEach((input) => {
    input.addEventListener("change", (e) => {
      currentY = e.target.value;
      document.querySelectorAll('.metric-radio:not(.x-metric)').forEach((label) => {
        label.dataset.active = label.dataset.metric === currentY ? "true" : "false";
      });
      render(currentX, currentY);
    });
  });

  // Wire up X-axis metric radio buttons.
  document.querySelectorAll('input[name="xmetric"]').forEach((input) => {
    input.addEventListener("change", (e) => {
      currentX = e.target.value;
      document.querySelectorAll('.metric-radio.x-metric').forEach((label) => {
        label.dataset.active = label.dataset.xmetric === currentX ? "true" : "false";
      });
      render(currentX, currentY);
    });
  });

  // Wire up scatter location filter chips.
  document.querySelectorAll('.scatter-controls .filter-chip').forEach((chip) => {
    chip.addEventListener("click", (e) => {
      e.preventDefault();
      const isActive = chip.dataset.active === "true";
      chip.dataset.active = isActive ? "false" : "true";
      chip.querySelector("input").checked = !isActive;
      render(currentX, currentY);
    });
  });
}
