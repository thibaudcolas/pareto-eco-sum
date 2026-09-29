// Client-side sorting for the wagtail model table. Click a header to sort by
// that column; click again to flip the direction. Text columns sort A→Z/ Z→A,
// numeric columns sort largest or smallest first by default. Empty
// data-sort-value cells (unmeasured energy) always sort last.
function initTableSort() {
  const table = document.getElementById("wagtail-table");
  if (!table) return;
  const tbody = table.tBodies[0];
  if (!tbody) return;
  const buttons = Array.from(table.querySelectorAll("th .sort-btn"));

  const compare = (cellA, cellB) => {
    const rawA = cellA?.dataset.sortValue ?? "";
    const rawB = cellB?.dataset.sortValue ?? "";
    const emptyA = rawA === "";
    const emptyB = rawB === "";
    if (emptyA !== emptyB) return emptyA ? 1 : -1; // missing values last
    const numA = Number(rawA);
    const numB = Number(rawB);
    if (!Number.isNaN(numA) && !Number.isNaN(numB)) return numA - numB;
    return String(rawA).localeCompare(String(rawB), undefined, {
      numeric: true,
    });
  };

  function applySort(btn) {
    const key = btn.getAttribute("data-sort-key");
    const columnIndex = buttons.indexOf(btn);
    const current = btn.hasAttribute("dir") ? btn.getAttribute("dir") : null;
    const nextIsDesc =
      current === null
        ? // Defaults: keep the page’s “highest accuracy first” feel for numeric
          // stats, but list names and providers alphabetically.
          key === "name" || key === "provider_name"
          ? false
          : true
        : current === "asc";
    const dir = nextIsDesc ? "desc" : "asc";

    const rows = Array.from(tbody.rows);
    rows.sort((rowA, rowB) => {
      const result = compare(rowA.cells[columnIndex], rowB.cells[columnIndex]);
      return dir === "desc" ? -result : result;
    });
    for (const row of rows) tbody.appendChild(row);

    buttons.forEach((b) => {
      b.classList.toggle("is-active", b === btn);
      b.removeAttribute("dir");
      const ind = b.querySelector(".sort-ind");
      if (ind) ind.textContent = "";
      const th = b.closest("th");
      if (th) th.removeAttribute("aria-sort");
    });
    btn.classList.add("is-active");
    btn.setAttribute("dir", dir);
    const indicator = btn.querySelector(".sort-ind");
    if (indicator) indicator.textContent = dir === "desc" ? "▼" : "▲";
    const th = btn.closest("th");
    if (th)
      th.setAttribute("aria-sort", dir === "desc" ? "descending" : "ascending");
  }

  buttons.forEach((btn) => {
    btn.addEventListener("click", () => applySort(btn));
  });
}

initTableSort();
