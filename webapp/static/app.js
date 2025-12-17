function fmtTs(ts) {
  try {
    const d = new Date(ts);
    if (!isNaN(d.getTime())) return d.toLocaleString();
  } catch {}
  return String(ts || "");
}

function computeCols(count) {
  if (!count || count <= 0) return 1;
  return Math.max(1, Math.ceil(Math.sqrt(count)));
}

function humanEvent(e) {
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
  
  // Load analytics data when switching to analytics tab
  if (tabId === 'analytics') {
    loadAnalytics();
  }
}

// Chart instances
let occupancyChart = null;
let dwellChart = null;
let predictionChart = null;

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
let collapsedZones = new Set();

function getSlotStatus(slotId) {
  const raw = stateById[slotId] || "FREE";
  return raw === "OCCUPIED" ? "OCCUPIED" : "FREE";
}

function computeZoneStats() {
  /** @type {Record<string, { total: number, occupied: number, free: number }>} */
  const zones = {};

  for (const s of slots) {
    const zoneKey = s.zone || "A";
    if (!zones[zoneKey]) zones[zoneKey] = { total: 0, occupied: 0, free: 0 };

    zones[zoneKey].total += 1;
    const status = getSlotStatus(s.id);
    if (status === "OCCUPIED") zones[zoneKey].occupied += 1;
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
  container.innerHTML = "";

  const zoneKeys = Object.keys(zones || {}).sort();
  for (const zoneKey of zoneKeys) {
    const z = zones[zoneKey];
    const isCollapsed = collapsedZones.has(zoneKey);

    const section = document.createElement("section");
    section.className = "zoneSection";
    if (isCollapsed) section.classList.add("collapsed");

    const header = document.createElement("div");
    header.className = "zoneHeader";

    const titleRow = document.createElement("div");
    titleRow.className = "zoneTitleRow";

    const toggle = document.createElement("button");
    toggle.className = "zoneToggle";
    toggle.type = "button";
    toggle.setAttribute("aria-label", isCollapsed ? `Expand Zone ${zoneKey}` : `Collapse Zone ${zoneKey}`);
    toggle.setAttribute("aria-expanded", isCollapsed ? "false" : "true");
    toggle.textContent = isCollapsed ? "▸" : "▾";
    toggle.addEventListener("click", () => {
      if (collapsedZones.has(zoneKey)) collapsedZones.delete(zoneKey);
      else collapsedZones.add(zoneKey);
      refreshLayout();
    });

    const title = document.createElement("div");
    title.className = "zoneTitle";
    title.textContent = `Zone ${zoneKey}: Occupancy (${z.occupied}/${z.total})`;

    titleRow.appendChild(toggle);
    titleRow.appendChild(title);

    const subtitle = document.createElement("div");
    subtitle.className = "zoneSubtitle";
    subtitle.textContent = `Free ${z.free} • Occupied ${z.occupied} • Total ${z.total}`;

    header.appendChild(titleRow);
    header.appendChild(subtitle);
    section.appendChild(header);

    if (isCollapsed) {
      container.appendChild(section);
      continue;
    }

    const grid = document.createElement("div");
    grid.className = "zoneSlotGrid";

    const zoneSlots = slots
      .filter((s) => (s.zone || "A") === zoneKey)
      .slice()
      .sort((a, b) => a.id - b.id);

    grid.style.setProperty("--cols", String(computeCols(zoneSlots.length)));

    for (const s of zoneSlots) {
      const status = getSlotStatus(s.id);
      const tile = document.createElement("div");
      tile.className = `slot ${status === "OCCUPIED" ? "occupied" : "free"}`;

      const top = document.createElement("div");
      top.className = "slotTop";

      const name = document.createElement("div");
      name.className = "slotName";
      name.textContent = s.name || `Slot ${s.id}`;

      const badge = document.createElement("div");
      badge.className = "slotState";
      badge.textContent = status;

      top.appendChild(name);
      top.appendChild(badge);

      const meta = document.createElement("div");
      meta.className = "slotMeta";
      meta.textContent = `id ${s.id}`;

      tile.appendChild(top);
      tile.appendChild(meta);

      grid.appendChild(tile);
    }

    section.appendChild(grid);
    container.appendChild(section);
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

async function init() {
  const res = await fetch("/state");
  const data = await res.json();

  slots = (data.slots || []).slice().sort((a, b) => a.id - b.id);
  stateById = data.state_by_id || {};

  refreshLayout();

  const recent = (data.recent_events || []).slice().reverse();
  for (const e of recent) {
    prependLog(e);
  }

  const es = new EventSource("/events");
  es.onmessage = (msg) => {
    try {
      const obj = JSON.parse(msg.data);
      if (obj.event === "snapshot") {
        const occupied = new Set(obj.occupied_ids || []);
        for (const s of slots) {
          stateById[s.id] = occupied.has(s.id) ? "OCCUPIED" : "FREE";
        }
        refreshLayout();
        return;
      }

      if (obj.event === "slot_state_changed") {
        if (typeof obj.slot_id === "number") {
          stateById[obj.slot_id] = obj.new_state;
        } else {
          const id = Number(obj.slot_id);
          if (!isNaN(id)) stateById[id] = obj.new_state;
        }
        refreshLayout();
        prependLog(obj);
      }
    } catch (e) {
      // ignore parse errors
    }
  };
}

init();
