// Shared helpers for the two Observable Plot scatters: formatting, escaping,
// the floating tooltip element, and the circle↔datum tooltip binding.
// (Observable Plot batches circles by channel value, so DOM order does not
// match data order; bindCircleTooltips matches by rendered (cx, cy).)

export const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

export const fmtMoney = (n) => (n == null ? "n/a" : "$" + Number(n).toFixed(2));
export const fmtNum = (n, d = 2) => (n == null ? "n/a" : Number(n).toFixed(d));

export const shortName = (name, max = 28) => {
  if (!name) return "";
  const trimmed = name.replace(/\s*\([^)]*\)\s*/g, "").trim();
  return trimmed.length <= max ? trimmed : trimmed.slice(0, max - 1) + "…";
};

export const createTooltip = () => {
  const tooltip = document.createElement("div");
  tooltip.className = "tooltip-popup";
  document.body.appendChild(tooltip);
  return tooltip;
};

export const bindCircleTooltips = (plot, tooltip, getDatum, getTipHtml) => {
  const sx = plot.scale("x"), sy = plot.scale("y");
  const apply = (sc, v) => (sc && typeof sc.apply === "function" ? sc.apply(v) : null);
  const dedup = new Map(); // "cx|cy" → datum (last one wins on tie)
  for (const d of getDatum.points()) {
    const cx = apply(sx, d.x), cy = apply(sy, d.y);
    if (cx == null || cy == null) continue;
    dedup.set(Math.round(cx * 100) + "|" + Math.round(cy * 100), d.datum);
  }
  const circles = plot.querySelectorAll("circle");
  circles.forEach((c) => {
    const cx = parseFloat(c.getAttribute("cx"));
    const cy = parseFloat(c.getAttribute("cy"));
    const datum = (Number.isFinite(cx) && Number.isFinite(cy))
      ? dedup.get(Math.round(cx * 100) + "|" + Math.round(cy * 100))
      : null;
    if (!datum) return;
    c.style.cursor = "pointer";
    c.addEventListener("mouseenter", () => {
      tooltip.innerHTML = getTipHtml(datum);
      tooltip.style.display = "block";
    });
    c.addEventListener("mousemove", (e) => {
      const padX = 16, padY = 16;
      const rect = tooltip.getBoundingClientRect();
      let x = e.clientX + padX;
      let y = e.clientY + padY;
      if (x + rect.width > window.innerWidth - 8) x = e.clientX - rect.width - padX;
      if (y + rect.height > window.innerHeight - 8) y = e.clientY - rect.height - padY;
      tooltip.style.left = x + "px";
      tooltip.style.top = y + "px";
    });
    c.addEventListener("mouseleave", () => { tooltip.style.display = "none"; });
  });
};
