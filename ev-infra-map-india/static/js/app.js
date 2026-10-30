(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const NOT_RECORDED = 'Not recorded';
  const BAND_LABEL = { slow: 'Slow (<10 kW)', fast: 'Fast (10–30 kW)', rapid: 'Rapid (>30 kW)', unknown: 'Power not supplied' };
  const BAND_COLOR = { slow: '#e69f00', fast: '#0072b2', rapid: '#009e73', unknown: '#7a8699' };
  const PAGE_SIZE = 15;

  const esc = (v) => String(v == null || v === '' ? '' : v).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (v) => (v == null ? '—' : Number(v).toLocaleString('en-IN'));
  const band = (kw) => (kw == null ? 'unknown' : kw < 10 ? 'slow' : kw <= 30 ? 'fast' : 'rapid');

  let state = (window.EV_CONFIG && window.EV_CONFIG.state) || 'All India';
  let stations = [];
  let filtered = [];
  let page = 0;
  const activeBands = new Set(['slow', 'fast', 'rapid', 'unknown']);
  const markers = new Map();

  /* ------------------------------------------------------------------ tabs */
  function showTab(name) {
    const valid = ['overview', 'states', 'districts', 'data', 'method'];
    if (!valid.includes(name)) name = 'overview';
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
    document.querySelectorAll('.panel').forEach((p) => p.classList.toggle('active', p.id === 'tab-' + name));
    if (history.replaceState) history.replaceState(null, '', '#' + name);
    if (name === 'overview' && map) setTimeout(() => map.invalidateSize(), 50);
  }
  document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => showTab(t.dataset.tab)));

  /* ------------------------------------------------------------------ map */
  let map = null, cluster = null;
  const mapMsg = $('map-message');
  if (typeof L === 'undefined') {
    mapMsg.textContent = 'The map library did not load. Check your internet connection and reload.';
  } else {
    map = L.map('station-map', { zoomControl: true }).setView([22.5, 79], 5);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
      { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(map);
    cluster = (L.markerClusterGroup ? L.markerClusterGroup({ showCoverageOnHover: false, maxClusterRadius: 45 }) : L.layerGroup());
    map.addLayer(cluster);
    const legend = L.control({ position: 'bottomright' });
    legend.onAdd = function () {
      const d = L.DomUtil.create('div', 'map-legend');
      d.innerHTML = Object.keys(BAND_LABEL).map((k) =>
        `<div><i class="sw ${k}"></i> ${BAND_LABEL[k]}</div>`).join('');
      return d;
    };
    legend.addTo(map);
  }

  function popupHtml(s) {
    const row = (k, v) => `<dt>${k}</dt><dd>${v ? esc(v) : '<span class="muted">Not supplied</span>'}</dd>`;
    const district = s.district && s.district !== NOT_RECORDED ? s.district : '';
    return `<strong>${esc(s.station_name)}</strong>
      <div class="pop-meta">${esc([s.city, district, s.state].filter(Boolean).join(', '))}</div>
      <dl class="pop-grid">
        ${row('Operator', s.operator)}${row('Connectors', s.charger_type)}
        ${row('Max power', s.power_kw != null ? s.power_kw + ' kW' : '')}
        ${row('Ports', s.connectors)}${row('Access', s.usage_type)}${row('Source', s.source)}
      </dl>`;
  }

  /* ------------------------------------------------------------------ data load */
  async function load() {
    $('map-summary').textContent = 'Loading stations…';
    try {
      const res = await fetch('/api/stations?state=' + encodeURIComponent(state));
      stations = await res.json();
    } catch (e) {
      stations = [];
      $('map-summary').textContent = 'Could not load stations from the server.';
    }
    buildFilterOptions();
    applyFilters(true);
  }

  function buildFilterOptions() {
    const fill = (id, values, label) => {
      const sel = $(id), prev = sel.value;
      sel.innerHTML = `<option value="">${label}</option>` +
        values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
      if (values.includes(prev)) sel.value = prev;
    };
    const uniq = (arr) => [...new Set(arr.filter(Boolean))].sort((a, b) => a.localeCompare(b));
    fill('f-operator', uniq(stations.map((s) => s.operator)), 'All operators');
    fill('f-usage', uniq(stations.map((s) => s.usage_type)), 'All access types');
    fill('f-connector', uniq(stations.flatMap((s) => (s.charger_type || '').split(' / ').map((x) => x.trim()))), 'All connector types');
  }

  /* ------------------------------------------------------------------ filtering */
  function applyFilters(fit) {
    const q = $('f-search').value.trim().toLowerCase();
    const op = $('f-operator').value, cn = $('f-connector').value, us = $('f-usage').value;
    filtered = stations.filter((s) => {
      if (!activeBands.has(band(s.power_kw))) return false;
      if (op && s.operator !== op) return false;
      if (us && s.usage_type !== us) return false;
      if (cn && !(s.charger_type || '').split(' / ').map((x) => x.trim()).includes(cn)) return false;
      if (q && !((s.station_name || '') + ' ' + (s.city || '')).toLowerCase().includes(q)) return false;
      return true;
    });
    page = 0;
    drawMap(fit);
    drawKpis();
    drawCharts();
    drawTable();
  }

  function drawMap(fit) {
    if (!map) return;
    cluster.clearLayers();
    markers.clear();
    const pts = [];
    filtered.forEach((s) => {
      const lat = Number(s.latitude), lon = Number(s.longitude);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
      const m = L.circleMarker([lat, lon], {
        radius: 7, color: '#ffffff', weight: 1.5, fillColor: BAND_COLOR[band(s.power_kw)], fillOpacity: 0.92,
      });
      m.bindPopup(popupHtml(s));
      cluster.addLayer(m);
      markers.set(s.station_id, m);
      pts.push([lat, lon]);
    });
    const scope = state === 'All India' ? 'India' : state;
    $('map-summary').textContent = `${fmt(filtered.length)} of ${fmt(stations.length)} stations shown for ${scope}`;
    if (pts.length) {
      if (fit) map.fitBounds(pts, { padding: [28, 28], maxZoom: 10 });
      mapMsg.textContent = 'Source dates are not recorded for these stations. Verify status before relying on a location. No marker does not mean no station exists.';
    } else {
      mapMsg.textContent = stations.length
        ? 'No stations match the current filters.'
        : 'No station records are loaded for this region. Upload a station CSV under Data & import.';
    }
  }

  /* ------------------------------------------------------------------ KPIs and charts */
  function drawKpis() {
    const n = filtered.length;
    const connectors = filtered.reduce((a, s) => a + (s.connectors || 1), 0);
    const ops = new Set(filtered.map((s) => s.operator).filter(Boolean));
    const rapid = filtered.filter((s) => band(s.power_kw) === 'rapid').length;
    $('kpi-stations').textContent = fmt(n);
    $('kpi-connectors').textContent = fmt(connectors);
    $('kpi-operators').textContent = fmt(ops.size);
    $('kpi-rapid').textContent = fmt(rapid);
    $('kpi-rapid-sub').textContent = n ? `${Math.round((rapid / n) * 100)}% of stations shown` : 'No stations shown';
  }

  function barChart(title, entries) {
    const max = Math.max(1, ...entries.map((e) => e[1]));
    const rows = entries.length ? entries.map(([name, v]) =>
      `<div class="bar-row"><span class="name" title="${esc(name)}">${esc(name)}</span><span class="val">${fmt(v)}</span>
       <div class="bar"><i style="width:${(v / max) * 100}%"></i></div></div>`).join('')
      : '<p class="muted small">No data</p>';
    return `<div class="chart"><h3>${esc(title)}</h3>${rows}</div>`;
  }

  function drawCharts() {
    const count = (keyFn) => {
      const m = new Map();
      filtered.forEach((s) => [].concat(keyFn(s)).forEach((k) => { if (k) m.set(k, (m.get(k) || 0) + 1); }));
      return [...m.entries()].sort((a, b) => b[1] - a[1]);
    };
    const ops = count((s) => s.operator).slice(0, 6);
    const conns = count((s) => (s.charger_type || '').split(' / ').map((x) => x.trim())).slice(0, 5);
    const bands = ['slow', 'fast', 'rapid', 'unknown'].map((b) => [BAND_LABEL[b], filtered.filter((s) => band(s.power_kw) === b).length]);
    $('charts').innerHTML = barChart('Stations by operator', ops) + barChart('Connector types', conns) + barChart('Charging speed', bands);
  }

  /* ------------------------------------------------------------------ station table */
  function drawTable() {
    const body = document.querySelector('#station-table tbody');
    const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
    page = Math.min(page, pages - 1);
    const rows = filtered.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);
    body.innerHTML = rows.length ? rows.map((s) =>
      `<tr class="clickable" data-id="${esc(s.station_id)}" title="Show on map">
        <td><strong>${esc(s.station_name)}</strong></td><td>${esc(s.operator) || '<span class="na">—</span>'}</td>
        <td>${esc(s.city) || '<span class="na">—</span>'}</td><td>${esc(s.state)}</td>
        <td>${esc(s.charger_type) || '<span class="na">—</span>'}</td>
        <td class="num">${s.power_kw != null ? fmt(s.power_kw) : '<span class="na">—</span>'}</td>
        <td class="num">${fmt(s.connectors)}</td></tr>`).join('')
      : '<tr><td colspan="7" class="muted">No stations match the current filters.</td></tr>';
    $('table-summary').textContent = `${fmt(filtered.length)} station(s)`;
    $('page-label').textContent = `Page ${page + 1} of ${pages}`;
    $('prev-page').disabled = page === 0;
    $('next-page').disabled = page >= pages - 1;
  }

  $('station-table').addEventListener('click', (e) => {
    const tr = e.target.closest('tr[data-id]');
    if (!tr || !map) return;
    const m = markers.get(tr.dataset.id);
    if (!m) return;
    document.getElementById('station-map').scrollIntoView({ behavior: 'smooth', block: 'center' });
    const open = () => m.openPopup();
    if (cluster.zoomToShowLayer) cluster.zoomToShowLayer(m, open); else { map.setView(m.getLatLng(), 14); open(); }
  });
  $('prev-page').addEventListener('click', () => { page = Math.max(0, page - 1); drawTable(); });
  $('next-page').addEventListener('click', () => { page += 1; drawTable(); });

  $('export-stations').addEventListener('click', () => {
    if (!filtered.length) return;
    const cols = ['station_id', 'station_name', 'operator', 'city', 'state', 'district', 'latitude', 'longitude',
      'charger_type', 'power_kw', 'connectors', 'usage_type', 'source'];
    const q = (v) => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
    const csv = [cols.join(','), ...filtered.map((s) => cols.map((c) => q(s[c])).join(','))].join('\r\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    a.download = 'ev_stations_filtered.csv';
    a.click();
    URL.revokeObjectURL(a.href);
  });

  /* ------------------------------------------------------------------ filter wiring */
  ['f-search', 'f-operator', 'f-connector', 'f-usage'].forEach((id) =>
    $(id).addEventListener(id === 'f-search' ? 'input' : 'change', () => applyFilters(false)));
  document.querySelectorAll('#f-power .chip').forEach((chip) => chip.addEventListener('click', () => {
    const b = chip.dataset.band;
    if (activeBands.has(b)) activeBands.delete(b); else activeBands.add(b);
    chip.classList.toggle('on', activeBands.has(b));
    applyFilters(false);
  }));
  $('reset-filters').addEventListener('click', () => {
    ['f-search', 'f-operator', 'f-connector', 'f-usage'].forEach((id) => { $(id).value = ''; });
    ['slow', 'fast', 'rapid', 'unknown'].forEach((b) => activeBands.add(b));
    document.querySelectorAll('#f-power .chip').forEach((c) => c.classList.add('on'));
    applyFilters(true);
  });

  /* ------------------------------------------------------------------ region selector */
  $('state-select').addEventListener('change', (e) => {
    state = e.target.value;
    const url = new URL(window.location.href);
    url.searchParams.set('state', state);
    history.replaceState(null, '', url.pathname + url.search + window.location.hash);
    load();
    loadDistricts();
  });

  /* ------------------------------------------------------------------ districts */
  let districtRows = [];
  async function loadDistricts() {
    const ratio = $('target-ratio') ? Math.max(1, Number($('target-ratio').value) || 100) : 100;
    const radius = $('radius-km') ? Math.max(1, Number($('radius-km').value) || 10) : 10;
    try {
      const res = await fetch(`/api/gaps?state=${encodeURIComponent(state)}&target_ratio=${ratio}&radius_km=${radius}`);
      districtRows = await res.json();
    } catch (e) { districtRows = []; }
    drawDistricts();
  }

  function statusBadge(r) {
    if (r.status === 'EV count unavailable') return '<span class="badge na-badge">Data unavailable</span>';
    if (r.status === 'Demand gap' || r.status.startsWith('Geographic gap')) return `<span class="badge warn">${esc(r.status)}</span>`;
    return `<span class="badge ok">${esc(r.status)}</span>`;
  }

  function drawDistricts() {
    const q = $('d-search').value.trim().toLowerCase();
    const mode = $('d-filter').value;
    const rows = districtRows.filter((r) =>
      (!q || r.district.toLowerCase().includes(q)) &&
      (mode === 'all' || (mode === 'available') === (r.ev_count != null)));
    const body = document.querySelector('#district-table tbody');
    body.innerHTML = rows.length ? rows.map((r) =>
      `<tr><td><strong>${esc(r.district)}</strong></td><td>${esc(r.state)}</td>
       <td class="num">${r.ev_count != null ? fmt(r.ev_count) : '<span class="na">Unavailable</span>'}</td>
       <td class="num">${r.station_count ? fmt(r.station_count) : '<span class="na">Not recorded</span>'}</td>
       <td class="num">${r.estimated_additional_units != null ? fmt(r.estimated_additional_units) : '—'}</td>
       <td>${statusBadge(r)}</td></tr>`).join('')
      : '<tr><td colspan="6" class="muted">No districts match. Select a region or clear the search.</td></tr>';
  }
  $('d-search').addEventListener('input', drawDistricts);
  $('d-filter').addEventListener('change', drawDistricts);
  if ($('recalc')) $('recalc').addEventListener('click', loadDistricts);

  /* ------------------------------------------------------------------ start */
  showTab((location.hash || '#overview').slice(1));
  window.addEventListener('hashchange', () => showTab(location.hash.slice(1)));
  load();
  loadDistricts();
})();
