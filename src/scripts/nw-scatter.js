// Neuralwatt calibration view: measured energy vs NW cost with the
// proportional fit energy = k × cost through the origin. Data comes from
// build-time JSON imports, Plot from npm.
import nwData from "../data/nw-scatter.json";
import colorMap from "../data/colors.json";
import calibration from "../data/calibration.json";
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
    const variantTag = d.is_variant ? " <span style=\"color:var(--muted);font-weight:400\">(variant)</span>" : "";
    return "<div style=\"font-weight:600;margin-bottom:4px\">" + escapeHtml(d.name) + variantTag + "</div>" +
      "<div style=\"color:var(--muted);margin-bottom:6px\">" + escapeHtml(d.provider || "?") + "</div>" +
      "<div style=\"color:var(--accent);font-size:0.7rem;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:3px\">Neuralwatt</div>" +
      rows.map(([k, v]) =>
        "<div style=\"display:flex;justify-content:space-between;gap:1em\">" +
        "<span style=\"color:var(--muted)\">" + escapeHtml(k) + "</span>" +
        "<span style=\"font-variant-numeric:tabular-nums\">" + escapeHtml(String(v)) + "</span></div>"
      ).join("");
  };

  const tooltip = createTooltip();

  // Proportional fit line: energy = k × cost through the origin. Extends to
  // the observed cost range plus 10% headroom rather than a hardcoded domain.
  const fitLine = [];
  if (calibration.k != null) {
    const xMax = Math.max(0.1, Math.max(...nwData.map((d) => d.blended_cost)) * 1.1);
    fitLine.push({ x: 0, y: 0 });
    fitLine.push({ x: xMax, y: calibration.k * xMax });
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
      // Proportional fit line through the origin.
      ...(fitLine.length ? [Plot.line(fitLine, {
        x: "x", y: "y",
        stroke: "#5b8def", strokeWidth: 1.5, strokeDasharray: "5,4",
        opacity: 0.6,
      })] : []),
      // Labels.
      Plot.text(nwData, {
        x: "blended_cost", y: "energy_mwh",
        text: (d) => shortName(d.name, 24),
        fontSize: 9.5, dx: 12, dy: -8, textAnchor: "start",
        fill: "var(--text)", fillOpacity: 0.78, fontWeight: 500,
        pointerEvents: "none",
      }),
    ],
  });

  const target = document.getElementById("nw-scatter-plot");
  target.innerHTML = "";
  target.appendChild(plot);

  // Append an annotation with the proportional fit.
  if (calibration.k != null) {
    const annotation = document.createElement("div");
    annotation.style.cssText = "font-size:0.8rem;color:var(--muted);margin-top:0.6rem;font-family:ui-monospace,monospace";
    annotation.textContent = "energy = k × cost"
      + "  ·  k = " + calibration.k + " mWh/$"
      + (calibration.r != null ? "  ·  r = " + calibration.r : "")
      + (calibration.r_squared != null ? "  ·  r² = " + calibration.r_squared : "")
      + "  ·  n = " + calibration.n
      + " (base models)";
    target.appendChild(annotation);
  }

  // Bind tooltips; the circle↔datum matching (Plot batches circles by channel
  // value, so DOM order ≠ data order) lives in the shared module.
  bindCircleTooltips(plot, tooltip, {
    points: () => nwData.map((d) => ({ x: d.blended_cost, y: d.energy_mwh, datum: d })),
  }, tipHtml);
}
