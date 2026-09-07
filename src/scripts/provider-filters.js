// Faithful port of the third inline IIFE: provider filter chips.
function applyProviderFilters() {
  var filters = {};
  document.querySelectorAll('#provider-filters .filter-chip[data-ploc]').forEach(function (c) {
    filters[c.dataset.ploc] = c.dataset.active === 'true';
  });
  var ptypeFilters = {};
  document.querySelectorAll('#provider-filters .filter-chip[data-ptype]').forEach(function (c) {
    ptypeFilters[c.dataset.ptype] = c.dataset.active === 'true';
  });
  var cachePriced = document.querySelector('#provider-filters .filter-chip[data-cache=priced]').dataset.active === 'true';
  var cacheAll = document.querySelector('#provider-filters .filter-chip[data-cache=all]').dataset.active === 'true';
  document.querySelectorAll('#providers-grid .provider-card').forEach(function (card) {
    var hq = card.dataset.hq || 'unknown';
    var match;
    if (hq === 'us') match = filters.us;
    else if (hq === 'cn') match = filters.cn;
    else if (hq === 'unknown') match = filters.unknown;
    else match = filters.other;
    if (match) {
      var cache = card.dataset.cache;
      if (cache === 'priced' && !cachePriced) match = false;
      if (cache === 'all' && !cacheAll) match = false;
    }
    if (match) {
      var ptype = card.dataset.ptype || 'other';
      if (!ptypeFilters[ptype]) match = false;
    }
    card.style.display = match ? '' : 'none';
  });
}
document.querySelectorAll('#provider-filters .filter-chip').forEach(function (chip) {
  function toggle() {
    var isActive = chip.dataset.active === 'true';
    chip.dataset.active = isActive ? 'false' : 'true';
    applyProviderFilters();
  }
  chip.addEventListener('click', toggle);
  chip.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
  });
});
