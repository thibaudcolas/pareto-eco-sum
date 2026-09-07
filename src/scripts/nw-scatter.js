// Neuralwatt energy vs cost scatter. Data comes from build-time JSON imports,
// Plot from npm.
import nwData from "../data/nw-scatter.json";
import colorMap from "../data/colors.json";
import regression from "../data/regression.json";
import * as Plot from "@observablehq/plot";
import {
  escapeHtml, fmtMoney, fmtNum, shortName, createTooltip, bindCircleTooltips,
} from "./plot-common.js";

if (nwData.length) {
  const colorFor = (name) => colorMap[name] || "#5b8def";

  const tipHtml = (d) => {
    const rows = [
      ["Blended cost / 1M", fmtMoney(d.blended_cost)],
      ["  · input", fmtMoney(d.input_price)],
      ["  · output", fmtMoney(d.output_price)],
      ["  · cached input", fmtMoney(d.cached_input_price)],
      ["Energy @ 16k–64k", fmtNum(d.energy_mwh, 2) + " mWh"],
      ["  · cache-hit rate", d.cache_hit_rate != null ? fmtNum(d.cache_hit_rate, 0) + "%" : "n/a"],
      ["  · share of reqs", d.request_pct != null ? fmtNum(d.request_pct, 1) + "%" : "n/a"],
    ];
    const variantTag = d.is_variant ? " <span style=\"color:#8b97a8;font-weight:400\">(variant)</span>" : "";
    return "<div style=\"font-weight:600;margin-bottom:4px\">" + escapeHtml(d.name) + variantTag + "</div>" +
      "<div style=\"color:#8b97a8;margin-bottom:6px\">" + escapeHtml(d.provider || "?") + "</div>" +
      "<div style=\"color:#5b8def;font-size:0.7rem;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:3px\">Neuralwatt</div>" +
      rows.map(([k, v]) =>
        "<div style=\"display:flex;justify-content:space-between;gap:1em\">" +
        "<span style=\"color:#8b97a8\">" + escapeHtml(k) + "</span>" +
        "<span style=\"font-variant-numeric:tabular-nums\">" + escapeHtml(String(v)) + "</span></div>"
      ).join("");
  };

  const tooltip = createTooltip();

  // Build regression line data points for the dashed line mark. The line spans
  // the observed cost range (plus 10% headroom) rather than a hardcoded domain.
  const regressionLine = [];
  if (regression.slope != null) {
    const xMax = Math.max(0.1, Math.max(...nwData.map((d) => d.blended_cost)) * 1.1);
    regressionLine.push({ x: 0, y: Math.max(0, regression.intercept) });
    regressionLine.push({ x: xMax, y: Math.max(0, regression.slope * xMax + regression.intercept) });
  }

  const plot = Plot.plot({
    marginTop: 24, marginRight: 40, marginBottom: 64, marginLeft: 80,
    height: 480,
    x: {
      type: "linear",
      tickFormat: (d) => "$" + d.toFixed(2),
      label: "NW blended cost per 1M tokens (USD)  —  7 cache · 2 input · 1 output",
      labelAnchor: "right", labelOffset: 40,
      grid: true,
    },
    y: {
      type: "linear",
      tickFormat: (d) => d + " mWh",
      label: "Energy per request (mWh)  —  16k–64k band",
      labelAnchor: "top", labelOffset: 16,
      grid: true,
    },
    marks: [
      // All model dots: base models are solid/large, variants are hollow/smaller.
      Plot.dot(nwData, {
        x: "blended_cost", y: "energy_mwh",
        fill: (d) => colorFor(d.provider || d.name),
        fillOpacity: (d) => d.is_variant ? 0.3 : 0.95,
        stroke: (d) => colorFor(d.provider || d.name),
        strokeWidth: (d) => d.is_variant ? 1.5 : 1.2,
        r: (d) => d.is_variant ? 6 : 8,
      }),
      // Regression line.
      ...(regressionLine.length ? [Plot.line(regressionLine, {
        x: "x", y: "y",
        stroke: "#5b8def", strokeWidth: 1.5, strokeDasharray: "5,4",
        opacity: 0.6,
      })] : []),
      // Labels.
      Plot.text(nwData, {
        x: "blended_cost", y: "energy_mwh",
        text: (d) => shortName(d.name, 24),
        fontSize: 9.5, dx: 12, dy: -8, textAnchor: "start",
        fill: "#e6edf3", fillOpacity: 0.78, fontWeight: 500,
        pointerEvents: "none",
      }),
    ],
  });

  const target = document.getElementById("nw-scatter-plot");
  target.innerHTML = "";
  target.appendChild(plot);

  // Append an annotation with the regression equation.
  if (regression.slope != null) {
    const annotation = document.createElement("div");
    annotation.style.cssText = "font-size:0.8rem;color:var(--muted);margin-top:0.6rem;font-family:ui-monospace,monospace";
    annotation.innerHTML = "y = " + regression.slope + " × cost + (" + regression.intercept + ")  ·  "
      + "r = " + regression.r + "  ·  r² = " + regression.r_squared + "  ·  n = " + regression.n
      + " (base models only)";
    target.appendChild(annotation);
  }

  // Bind tooltips; the circle↔datum matching (Plot batches circles by channel
  // value, so DOM order ≠ data order) lives in the shared module.
  bindCircleTooltips(plot, tooltip, {
    points: () => nwData.map((d) => ({ x: d.blended_cost, y: d.energy_mwh, datum: d })),
  }, tipHtml);
}
