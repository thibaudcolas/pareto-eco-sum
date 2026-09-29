// Median/total value toggle for the wagtail table. The mode radio inputs
// ("wmode") are shared with the scatter chart, which listens to them too.
// Each switchable table cell carries data-median / data-total attributes; the
// sort value of a cell always follows its displayed value.
(() => {
  const cells = document.querySelectorAll("#wagtail-table td[data-field]");
  const radios = document.querySelectorAll('input[name="wmode"]');
  if (!cells.length || !radios.length) return;

  function applyMode(mode) {
    const attr = mode === "total" ? "data-total" : "data-median";
    cells.forEach((cell) => {
      const value = cell.getAttribute(attr) ?? "";
      cell.textContent = value === "" ? "—" : value;
      cell.classList.toggle("cell-na", value === "");
      cell.dataset.sortValue = value;
    });
  }

  radios.forEach((input) => {
    input.addEventListener("change", (e) => {
      const mode = e.target.value === "total" ? "total" : "median";
      applyMode(mode);
      document
        .querySelectorAll(".mode-toggle .metric-radio")
        .forEach((label) => {
          label.dataset.active = label.dataset.mode === mode ? "true" : "false";
        });
    });
  });
})();
