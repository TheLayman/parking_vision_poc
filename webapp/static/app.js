function fmtTs(ts) {
  try {
    const d = new Date(ts);
    if (!isNaN(d.getTime())) return d.toLocaleString();
  } catch { }
  return String(ts || "");
}

function filenameFromPath(path) {
  if (!path) return null;
  return path.split('/').pop().split('\\').pop();
}

function parseSlotId(obj) {
  const raw = obj.slot_id;
  return typeof raw === 'number' ? raw : Number(raw);
}

function computeCols(count) {
  if (!count || count <= 0) return 1;
  return Math.max(1, Math.ceil(Math.sqrt(count)));
}

function humanEvent(e) {
  if (e.event === "calibration") {
    return `${e.slot_name} calibrated`;
  }
  const name = e.slot_name || `Slot ${e.slot_id}`;
  const verb = e.new_state === "FREE" ? "vacated" : "occupied";
  const overlap = typeof e.overlap_ratio === "number" ? e.overlap_ratio : Number(e.overlap_ratio);
  const overlapText = Number.isFinite(overlap) ? ` (overlap ${overlap.toFixed(2)})` : "";
  return `${name} ${verb}${overlapText}`;
}

// Tab switching
function switchTab(tabId) {
  // Update tab buttons
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabId);
  });

  // Update tab content
  document.querySelectorAll('.tab-content').forEach(content => {
    content.classList.toggle('active', content.id === `${tabId}-tab`);
  });

  // Load data when switching tabs
  if (tabId === 'analytics') {
    loadAnalytics();
  } else if (tabId === 'alerts') {
    loadAlerts();
  }
}

// Chart instances
let occupancyChart = null;
let dwellChart = null;
let predictionChart = null;

// Alerts state
let alertsCache = [];
let alertsLoadedOnce = false;

// Zone colors for charts
const zoneColors = {
  'A': { bg: 'rgba(99, 102, 241, 0.2)', border: '#6366f1' },
  'B': { bg: 'rgba(16, 185, 129, 0.2)', border: '#10b981' },
  'C': { bg: 'rgba(245, 158, 11, 0.2)', border: '#f59e0b' },
  'D': { bg: 'rgba(239, 68, 68, 0.2)', border: '#ef4444' },
  'E': { bg: 'rgba(168, 85, 247, 0.2)', border: '#a855f7' }
};

function getZoneColor(zone) {
  return zoneColors[zone] || { bg: 'rgba(107, 114, 128, 0.2)', border: '#6b7280' };
}

// Chart.js default options for dark theme
const chartDefaults = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: {
      labels: { color: '#c5cee0', font: { family: 'Inter' } }
    }
  },
  scales: {
    x: {
      ticks: { color: '#8b96b0', font: { family: 'Inter' } },
      grid: { color: 'rgba(42, 51, 80, 0.5)' }
    },
    y: {
      ticks: { color: '#8b96b0', font: { family: 'Inter' } },
      grid: { color: 'rgba(42, 51, 80, 0.5)' }
    }
  }
};

async function loadAnalytics() {
  const range = document.getElementById('timeRange')?.value || '24h';

  try {
    const res = await fetch(`/analytics/summary?range=${range}`);
    const data = await res.json();

    const hasData = data.occupancy_series && data.occupancy_series.length > 0;

    // Show/hide empty state
    document.querySelector('.analytics-grid').style.display = hasData ? 'grid' : 'none';
    document.querySelector('.stats-row').style.display = hasData ? 'grid' : 'none';
    document.getElementById('analyticsEmpty').style.display = hasData ? 'none' : 'flex';

    if (!hasData) return;

    renderOccupancyChart(data.occupancy_series);
    renderDwellChart(data.dwell_stats);
    renderPredictionChart(data.predictions, data.current_occupancy);
    renderStatsRow(data);
    renderPredictionsGrid(data.predictions, data.current_occupancy);

  } catch (err) {
    console.error('Failed to load analytics:', err);
  }
}

function renderOccupancyChart(series) {
  const ctx = document.getElementById('occupancyChart')?.getContext('2d');
  if (!ctx) return;

  // Destroy existing chart
  if (occupancyChart) {
    occupancyChart.destroy();
  }

  // Get all zones from data
  const zones = new Set();
  series.forEach(entry => {
    Object.keys(entry.zones).forEach(z => zones.add(z));
  });

  // Build datasets
  const datasets = Array.from(zones).sort().map(zone => {
    const color = getZoneColor(zone);
    return {
      label: `Zone ${zone}`,
      data: series.map(entry => entry.zones[zone] || 0),
      borderColor: color.border,
      backgroundColor: color.bg,
      fill: true,
      tension: 0.3
    };
  });

  // Format time labels
  const labels = series.map(entry => {
    const d = new Date(entry.time);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  });

  occupancyChart = new Chart(ctx, {
    type: 'line',
    data: { labels, datasets },
    options: {
      ...chartDefaults,
      plugins: {
        ...chartDefaults.plugins,
        title: { display: false }
      },
      scales: {
        ...chartDefaults.scales,
        y: {
          ...chartDefaults.scales.y,
          min: 0,
          max: 100,
          ticks: {
            ...chartDefaults.scales.y.ticks,
            callback: v => `${v}%`
          }
        }
      }
    }
  });
}

function renderDwellChart(dwellStats) {
  const ctx = document.getElementById('dwellChart')?.getContext('2d');
  if (!ctx) return;

  if (dwellChart) {
    dwellChart.destroy();
  }

  const zones = Object.keys(dwellStats).sort();
  const values = zones.map(z => dwellStats[z]);
  const colors = zones.map(z => getZoneColor(z));

  dwellChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: zones.map(z => `Zone ${z}`),
      datasets: [{
        label: 'Avg Dwell Time (min)',
        data: values,
        backgroundColor: colors.map(c => c.bg),
        borderColor: colors.map(c => c.border),
        borderWidth: 2
      }]
    },
    options: {
      ...chartDefaults,
      plugins: {
        ...chartDefaults.plugins,
        legend: { display: false }
      },
      scales: {
        ...chartDefaults.scales,
        y: {
          ...chartDefaults.scales.y,
          beginAtZero: true,
          ticks: {
            ...chartDefaults.scales.y.ticks,
            callback: v => `${v} min`
          }
        }
      }
    }
  });
}

function renderPredictionChart(predictions, currentOccupancy) {
  const ctx = document.getElementById('predictionChart')?.getContext('2d');
  if (!ctx) return;

  if (predictionChart) {
    predictionChart.destroy();
  }

  const zones = Object.keys(predictions).sort();
  const predictedValues = zones.map(z => predictions[z]);
  const currentValues = zones.map(z => currentOccupancy[z]?.percent || 0);
  const colors = zones.map(z => getZoneColor(z));

  predictionChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: zones.map(z => `Zone ${z}`),
      datasets: [
        {
          label: 'Current',
          data: currentValues,
          backgroundColor: colors.map(c => c.bg),
          borderColor: colors.map(c => c.border),
          borderWidth: 2
        },
        {
          label: 'Predicted',
          data: predictedValues,
          backgroundColor: colors.map(c => c.border + '80'),
          borderColor: colors.map(c => c.border),
          borderWidth: 2,
          borderDash: [5, 5]
        }
      ]
    },
    options: {
      ...chartDefaults,
      scales: {
        ...chartDefaults.scales,
        y: {
          ...chartDefaults.scales.y,
          min: 0,
          max: 100,
          ticks: {
            ...chartDefaults.scales.y.ticks,
            callback: v => `${v}%`
          }
        }
      }
    }
  });
}

function renderStatsRow(data) {
  const { summary, current_occupancy, dwell_stats } = data;

  // Calculate overall stats
  let totalOccupied = 0;
  let totalSlots = 0;
  Object.values(current_occupancy || {}).forEach(z => {
    totalOccupied += z.occupied || 0;
    totalSlots += z.total || 0;
  });

  const overallPct = totalSlots > 0 ? ((totalOccupied / totalSlots) * 100).toFixed(1) : 0;

  // Average dwell time across all zones
  const dwellValues = Object.values(dwell_stats || {});
  const avgDwell = dwellValues.length > 0
    ? (dwellValues.reduce((a, b) => a + b, 0) / dwellValues.length).toFixed(1)
    : '--';

  // Update stat cards
  document.getElementById('statOccupancy').textContent = `${overallPct}%`;
  document.getElementById('statDwell').textContent = avgDwell !== '--' ? `${avgDwell} min` : '--';
  document.getElementById('statEvents').textContent = summary?.total_events || 0;
  document.getElementById('statPoints').textContent = summary?.data_points || 0;
}

function renderPredictionsGrid(predictions, currentOccupancy) {
  const container = document.getElementById('predictionsGrid');
  if (!container) return;

  const zones = Object.keys(predictions || {}).sort();

  if (zones.length === 0) {
    container.innerHTML = '<div class="prediction-loading">Not enough data for predictions yet</div>';
    return;
  }

  const html = zones.map(zone => {
    const current = currentOccupancy[zone]?.percent || 0;
    const predicted = predictions[zone] || 0;
    const diff = predicted - current;
    const trendClass = diff > 2 ? 'up' : diff < -2 ? 'down' : 'stable';
    const trendIcon = diff > 2 ? '↑' : diff < -2 ? '↓' : '→';
    const trendText = diff > 2 ? 'Rising' : diff < -2 ? 'Falling' : 'Stable';
    const color = getZoneColor(zone);

    return `
      <div class="prediction-card" style="border-left: 3px solid ${color.border}">
        <div class="prediction-header">
          <span class="prediction-zone">Zone ${zone}</span>
          <span class="prediction-trend ${trendClass}">${trendIcon} ${trendText}</span>
        </div>
        <div class="prediction-values">
          <div class="prediction-current">
            <span class="prediction-label">Current</span>
            <span class="prediction-value">${current.toFixed(0)}%</span>
          </div>
          <div class="prediction-arrow">→</div>
          <div class="prediction-next">
            <span class="prediction-label">Predicted</span>
            <span class="prediction-value" style="color: ${color.border}">${predicted.toFixed(0)}%</span>
          </div>
        </div>
      </div>
    `;
  }).join('');

  container.innerHTML = html;
}

let slots = [];
let stateById = {};
let sinceById = {};
let serverZoneStats = null; // use server-provided zone stats when available
let collapsedZones = new Set();
let calibratingSlots = new Set(); // Track slots currently being calibrated
let failedSlots = new Map(); // Track slots that failed calibration with error message
let slotPlates = {}; // slot_id -> [plate1, plate2, ...] from camera_capture SSE events
let pendingRechecks = {}; // slot_id -> {plates: [...], slot_name, ...}

function getSlotStatus(slotId) {
  const raw = stateById[slotId] || "FREE";
  return raw === "OCCUPIED" ? "OCCUPIED" : "FREE";
}

function computeZoneStats() {
  // Prefer server-provided zone stats to avoid duplicate computation
  if (serverZoneStats) return serverZoneStats;

  const zones = {};
  for (const s of slots) {
    const zoneKey = s.zone || "A";
    if (!zones[zoneKey]) zones[zoneKey] = { total: 0, occupied: 0, free: 0 };
    zones[zoneKey].total += 1;
    if (getSlotStatus(s.id) === "OCCUPIED") zones[zoneKey].occupied += 1;
    else zones[zoneKey].free += 1;
  }
  return zones;
}

function computeTotals(zones) {
  let total = 0;
  let free = 0;
  for (const k of Object.keys(zones || {})) {
    total += zones[k].total || 0;
    free += zones[k].free || 0;
  }
  return { free, total };
}

function renderSummary(freeCount, totalCount) {
  const el = document.getElementById("summary");
  el.textContent = `Free: ${freeCount}/${totalCount}`;
}

function renderZones(zones) {
  const bar = document.getElementById("zoneBar");
  bar.innerHTML = "";
  const keys = Object.keys(zones || {}).sort();
  for (const k of keys) {
    const z = zones[k];
    const chip = document.createElement("div");
    chip.className = "zoneChip";
    chip.textContent = `Zone ${k}: Occupancy (${z.occupied}/${z.total})`;
    bar.appendChild(chip);
  }
}

function renderZoneSections(zones) {
  const container = document.getElementById("zoneSections");
  const zoneKeys = Object.keys(zones || {}).sort();

  // Build a set of expected section IDs for cleanup
  const expectedIds = new Set(zoneKeys.map(k => `zone-section-${k}`));

  // Remove sections that no longer exist
  for (const child of Array.from(container.children)) {
    if (!expectedIds.has(child.id)) child.remove();
  }

  for (const zoneKey of zoneKeys) {
    const z = zones[zoneKey];
    const isCollapsed = collapsedZones.has(zoneKey);
    const sectionId = `zone-section-${zoneKey}`;
    let section = document.getElementById(sectionId);

    // Create section skeleton if it doesn't exist yet
    if (!section) {
      section = document.createElement("section");
      section.id = sectionId;
      section.className = "zoneSection";

      const header = document.createElement("div");
      header.className = "zoneHeader";

      const titleRow = document.createElement("div");
      titleRow.className = "zoneTitleRow";

      const toggle = document.createElement("button");
      toggle.className = "zoneToggle";
      toggle.type = "button";
      toggle.addEventListener("click", () => {
        if (collapsedZones.has(zoneKey)) collapsedZones.delete(zoneKey);
        else collapsedZones.add(zoneKey);
        refreshLayout();
      });

      const title = document.createElement("div");
      title.className = "zoneTitle";

      titleRow.appendChild(toggle);
      titleRow.appendChild(title);

      const subtitle = document.createElement("div");
      subtitle.className = "zoneSubtitle";

      header.appendChild(titleRow);
      header.appendChild(subtitle);
      section.appendChild(header);

      const grid = document.createElement("div");
      grid.className = "zoneSlotGrid";
      section.appendChild(grid);

      container.appendChild(section);
    }

    // Update header text
    if (isCollapsed) section.classList.add("collapsed");
    else section.classList.remove("collapsed");

    const toggle = section.querySelector(".zoneToggle");
    toggle.textContent = isCollapsed ? "▸" : "▾";
    toggle.setAttribute("aria-label", isCollapsed ? `Expand Zone ${zoneKey}` : `Collapse Zone ${zoneKey}`);
    toggle.setAttribute("aria-expanded", isCollapsed ? "false" : "true");

    section.querySelector(".zoneTitle").textContent = `Zone ${zoneKey}: Occupancy (${z.occupied}/${z.total})`;
    section.querySelector(".zoneSubtitle").textContent = `Free ${z.free} • Occupied ${z.occupied} • Total ${z.total}`;

    if (isCollapsed) {
      // Clear grid when collapsed
      const grid = section.querySelector(".zoneSlotGrid");
      if (grid) grid.innerHTML = "";
      continue;
    }

    // Diff-update slot tiles
    const grid = section.querySelector(".zoneSlotGrid");
    const zoneSlots = slots
      .filter(s => (s.zone || "A") === zoneKey)
      .slice()
      .sort((a, b) => a.id - b.id);

    grid.style.setProperty("--cols", String(computeCols(zoneSlots.length)));

    // Build a map of existing tiles by slot id
    const existingTiles = {};
    for (const tile of Array.from(grid.children)) {
      const sid = tile.dataset.slotId;
      if (sid) existingTiles[sid] = tile;
    }

    const expectedSlotIds = new Set(zoneSlots.map(s => String(s.id)));

    // Remove tiles for slots no longer in this zone
    for (const [sid, tile] of Object.entries(existingTiles)) {
      if (!expectedSlotIds.has(sid)) tile.remove();
    }

    for (const s of zoneSlots) {
      const status = getSlotStatus(s.id);
      const tileId = String(s.id);
      let tile = existingTiles[tileId];

      if (!tile) {
        // Create new tile
        tile = _createSlotTile(s);
        grid.appendChild(tile);
      }

      // Update tile state
      tile.className = `slot ${status === "OCCUPIED" ? "occupied" : "free"}`;
      tile.querySelector(".slotState").textContent = status;

      const sinceTs = sinceById[s.id];
      let sinceText = "";
      if (sinceTs) {
        try {
          const d = new Date(sinceTs);
          if (!isNaN(d.getTime())) sinceText = "Since " + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        } catch { }
      }

      // Build meta HTML: since + plates + pending recheck
      let metaHtml = sinceText;
      const plates = slotPlates[s.id];
      if (plates && plates.length > 0 && status === "OCCUPIED") {
        metaHtml += '<div class="slot-plates">' +
          plates.map(p => `<span class="plate-badge-sm">${p}</span>`).join(' ') +
          '</div>';
      }
      const pending = pendingRechecks[s.id];
      if (pending) {
        metaHtml += '<div class="slot-pending-recheck">⏳ Recheck pending</div>';
      }
      tile.querySelector(".slotMeta").innerHTML = metaHtml;

      // Update calibrate button state
      _updateCalibrateBtnState(tile.querySelector(".slot-calibrate-btn"), s.id);
    }
  }
}

/** Create a fresh slot tile DOM element. */
function _createSlotTile(s) {
  const tile = document.createElement("div");
  tile.dataset.slotId = String(s.id);

  const top = document.createElement("div");
  top.className = "slotTop";

  const name = document.createElement("div");
  name.className = "slotName";
  name.textContent = s.name || `Slot ${s.id}`;

  const badge = document.createElement("div");
  badge.className = "slotState";
  top.appendChild(name);
  top.appendChild(badge);

  const meta = document.createElement("div");
  meta.className = "slotMeta";

  const calibrateBtn = document.createElement("button");
  calibrateBtn.className = "slot-calibrate-btn";
  calibrateBtn.setAttribute("data-slot-id", s.id);
  calibrateBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    handleSlotCalibrate(s.id, calibrateBtn);
  });

  tile.appendChild(top);
  tile.appendChild(meta);
  tile.appendChild(calibrateBtn);
  return tile;
}

/** Update calibrate button visual state without recreating it. */
function _updateCalibrateBtnState(btn, slotId) {
  if (!btn) return;
  if (calibratingSlots.has(slotId)) {
    btn.disabled = true;
    btn.classList.add("loading");
    btn.classList.remove("error");
    btn.innerHTML = `
      <svg class="spin" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M21 12a9 9 0 1 1-6.219-8.56"/>
      </svg>
      ...
    `;
  } else if (failedSlots.has(slotId)) {
    btn.classList.add("error");
    btn.classList.remove("loading");
    btn.title = failedSlots.get(slotId) || "Calibration failed";
    btn.disabled = false;
    btn.innerHTML = `
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <line x1="18" y1="6" x2="6" y2="18"/>
        <line x1="6" y1="6" x2="18" y2="18"/>
      </svg>
      Failed
    `;
  } else {
    btn.disabled = false;
    btn.classList.remove("loading", "error");
    btn.title = "";
    btn.innerHTML = `
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="12" cy="12" r="3"/>
        <path d="M12 2v4M12 18v4M2 12h4M18 12h4"/>
      </svg>
      Calibrate
    `;
  }
}

function refreshLayout() {
  const zones = computeZoneStats();
  const totals = computeTotals(zones);
  renderSummary(totals.free, totals.total);
  renderZones(zones);
  renderZoneSections(zones);
}

function prependLog(obj) {
  const list = document.getElementById("logList");
  const item = document.createElement("div");
  item.className = "logItem";

  const line = document.createElement("div");
  line.className = "logLine";
  line.textContent = humanEvent(obj);

  const ts = document.createElement("div");
  ts.className = "logTs";
  ts.textContent = fmtTs(obj.ts);

  item.appendChild(line);
  item.appendChild(ts);

  list.insertBefore(item, list.firstChild);
}

// Alerts functions
async function loadAlerts() {
  try {
    const res = await fetch('/alerts?limit=100');
    const data = await res.json();

    alertsCache = data.alerts || [];
    alertsLoadedOnce = true;

    renderAlerts(alertsCache);
  } catch (err) {
    console.error('Failed to load alerts:', err);
    document.getElementById('alertsEmpty').style.display = 'flex';
    document.getElementById('alertsList').style.display = 'none';
  }
}

function renderAlerts(alerts) {
  const container = document.getElementById('alertsList');
  const emptyState = document.getElementById('alertsEmpty');

  if (alerts.length === 0) {
    container.style.display = 'none';
    emptyState.style.display = 'flex';
    return;
  }

  container.style.display = 'grid';
  emptyState.style.display = 'none';

  const html = alerts.map((alert, index) => {
    const ts = new Date(alert.ts);
    const timeStr = ts.toLocaleString();
    const stateClass = alert.new_state === 'OCCUPIED' ? 'occupied' : 'free';
    const stateIcon = alert.new_state === 'OCCUPIED' ? '🚗' : '✅';

    // Handle image
    let imageHtml = '';
    if (alert.image_path) {
      const filename = filenameFromPath(alert.image_path);
      imageHtml = `
        <div class="alert-image">
          <img src="/snapshots/${filename}" alt="Slot ${alert.slot_name}" loading="lazy" />
        </div>
      `;
    } else {
      imageHtml = `
        <div class="alert-no-image">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
            <circle cx="8.5" cy="8.5" r="1.5"/>
            <polyline points="21 15 16 10 5 21"/>
          </svg>
          <span>No image</span>
        </div>
      `;
    }

    // Handle license plates (support array)
    const licensePlates = alert.license_plates && alert.license_plates.length > 0
      ? alert.license_plates
      : (alert.license_plate && alert.license_plate !== 'UNKNOWN' ? [alert.license_plate] : []);
    const hasPlate = licensePlates.length > 0;
    const plateClass = hasPlate ? 'has-plate' : 'no-plate';
    const platesHtml = hasPlate
      ? licensePlates.map(p => `<span class="plate-badge-sm">${p}</span>`).join(' ')
      : 'UNKNOWN';

    return `
      <div class="alert-card ${stateClass}" style="--index: ${index}">
        ${imageHtml}
        <div class="alert-content">
          <div class="alert-header">
            <span class="alert-icon">${stateIcon}</span>
            <span class="alert-slot">${alert.slot_name}</span>
            <span class="alert-zone">Zone ${alert.zone}</span>
            <span class="alert-state-badge ${stateClass}">${alert.new_state}</span>
          </div>
          <div class="alert-details">
            <div class="alert-transition">
              <span class="prev-state">${alert.prev_state}</span>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="5" y1="12" x2="19" y2="12"/>
                <polyline points="12 5 19 12 12 19"/>
              </svg>
              <span class="new-state">${alert.new_state}</span>
            </div>
            <div class="alert-license-plate ${plateClass}">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <rect x="2" y="7" width="20" height="10" rx="2" ry="2"/>
                <line x1="6" y1="11" x2="6" y2="13"/>
                <line x1="18" y1="11" x2="18" y2="13"/>
              </svg>
              <span class="plate-text">${platesHtml}</span>
            </div>
            <div class="alert-time">${timeStr}</div>
          </div>
        </div>
      </div>
    `;
  }).join('');

  container.innerHTML = html;
}

async function init() {
  const res = await fetch("/state");
  const data = await res.json();

  slots = (data.slots || []).slice().sort((a, b) => a.id - b.id);
  stateById = data.state_by_id || {};
  sinceById = data.since_by_id || {};
  serverZoneStats = data.zones || null;

  refreshLayout();

  const recent = (data.recent_events || []).slice();
  for (const e of recent) {
    prependLog(e);
  }

  // Load pending rechecks on startup
  fetchPendingRechecks();
  // Poll pending rechecks every 15 seconds
  setInterval(fetchPendingRechecks, 15000);

  const es = new EventSource("/events");
  es.onmessage = (msg) => {
    try {
      const obj = JSON.parse(msg.data);
      if (obj.event === "snapshot") {
        const occupied = new Set(obj.occupied_ids || []);
        for (const s of slots) {
          stateById[s.id] = occupied.has(s.id) ? "OCCUPIED" : "FREE";
          // Clear plates when slot becomes free
          if (!occupied.has(s.id)) delete slotPlates[s.id];
        }
        serverZoneStats = obj.zone_stats || null;
        refreshLayout();
        return;
      }

      if (obj.event === "slot_state_changed") {
        serverZoneStats = null;
        const id = parseSlotId(obj);
        if (!isNaN(id)) {
          stateById[id] = obj.new_state;
          sinceById[id] = obj.ts;
          if (obj.new_state === "FREE") delete slotPlates[id];
        }
        refreshLayout();
        prependLog(obj);

        if (alertsLoadedOnce) {
          alertsCache.unshift(obj);
          alertsCache = alertsCache.slice(0, 100);
          const alertsTab = document.getElementById('alerts-tab');
          if (alertsTab && alertsTab.classList.contains('active')) {
            renderAlerts(alertsCache);
          }
        }
      }

      // Track detected plates on slot tiles
      if (obj.event === "camera_capture") {
        const id = parseSlotId(obj);
        if (!isNaN(id) && obj.license_plates && obj.license_plates.length > 0) {
          slotPlates[id] = obj.license_plates;
          refreshLayout();
        }
      }

      // Refresh pending rechecks when a challan completes
      if (obj.event === "challan_completed") {
        fetchPendingRechecks();
      }
    } catch (e) {
      // ignore parse errors
    }
  };
}

async function fetchPendingRechecks() {
  try {
    const res = await fetch('/challans/pending');
    const data = await res.json();
    const newPending = {};
    for (const p of (data.pending || [])) {
      if (p.slot_id != null) newPending[p.slot_id] = p;
    }
    const changed = JSON.stringify(pendingRechecks) !== JSON.stringify(newPending);
    pendingRechecks = newPending;
    if (changed) refreshLayout();
  } catch (e) {
    // silently ignore
  }
}

async function handleSlotCalibrate(slotId, btn) {
  if (calibratingSlots.has(slotId)) return;
  failedSlots.delete(slotId);
  calibratingSlots.add(slotId);
  _updateCalibrateBtnState(btn, slotId);

  try {
    const res = await fetch(`/calibrate/${slotId}`, { method: "POST" });
    if (!res.ok) throw new Error(`Server returned ${res.status}`);
    const data = await res.json();
    if (!data.success) throw new Error(data.message || "Calibration failed");

    calibratingSlots.delete(slotId);
    const currentBtn = document.querySelector(`.slot-calibrate-btn[data-slot-id="${slotId}"]`);
    if (!currentBtn) return;

    // Show success briefly
    currentBtn.classList.remove("loading");
    currentBtn.classList.add("success");
    currentBtn.disabled = true;
    currentBtn.innerHTML = `
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <polyline points="20 6 9 17 4 12"/>
      </svg>
      Done!
    `;
    const slot = slots.find(s => s.id === slotId);
    prependLog({ event: "calibration", ts: new Date().toISOString(), slot_name: slot ? slot.name : `Slot ${slotId}`, new_state: "CALIBRATED" });

    setTimeout(() => {
      const b = document.querySelector(`.slot-calibrate-btn[data-slot-id="${slotId}"]`);
      if (b) { b.classList.remove("success"); _updateCalibrateBtnState(b, slotId); }
    }, 2000);
  } catch (err) {
    console.error("Calibration error:", err);
    calibratingSlots.delete(slotId);
    failedSlots.set(slotId, err.message || "Calibration failed");
    const currentBtn = document.querySelector(`.slot-calibrate-btn[data-slot-id="${slotId}"]`);
    if (currentBtn) _updateCalibrateBtnState(currentBtn, slotId);

    setTimeout(() => {
      failedSlots.delete(slotId);
      const b = document.querySelector(`.slot-calibrate-btn[data-slot-id="${slotId}"]`);
      if (b && b.classList.contains("error")) _updateCalibrateBtnState(b, slotId);
    }, 5000);
  }
}

init();
