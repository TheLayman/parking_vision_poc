function escapeHtml(str) {
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}

function fmtTs(ts) {
  try {
    const d = new Date(ts);
    if (!isNaN(d.getTime())) return d.toLocaleString();
  } catch { }
  return String(ts || "");
}

function parseSlotId(obj) {
  const raw = obj.slot_id;
  return typeof raw === 'number' ? raw : Number(raw);
}

function timeAgo(ts) {
  if (!ts) return "";
  try {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return "";
    const diffMs = Date.now() - d.getTime();
    if (diffMs < 0) return "just now";
    const diffSec = Math.floor(diffMs / 1000);
    if (diffSec < 60) return "just now";
    const diffMin = Math.floor(diffSec / 60);
    if (diffMin < 60) return `${diffMin}m ago`;
    const hours = Math.floor(diffMin / 60);
    const mins = diffMin % 60;
    if (hours < 24) {
      return mins > 0 ? `${hours}h ${mins}m ago` : `${hours}h ago`;
    }
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
  } catch { return ""; }
}

function humanEvent(e) {
  if (e.event === "calibration_started") {
    const name = e.slot_name || `Slot ${e.slot_id}`;
    return `${name} calibration started`;
  }
  if (e.event === "calibration_done") {
    const name = e.slot_name || `Slot ${e.slot_id}`;
    return `${name} calibration done`;
  }
  if (e.event === "bulk_calibration_started") {
    const zone = e.zone || "all";
    const total = e.total || "?";
    return `Bulk calibration started (${total} slots, zone ${zone})`;
  }
  if (e.event === "calibration") {
    return `${e.slot_name} calibrated`;
  }
  if (e.event === "device_alert") {
    const alert = e.alert_type === "battery_low" ? "battery low" : "temperature high";
    return `${e.slot_name} ${alert}`;
  }
  const name = e.slot_name || `Slot ${e.slot_id}`;
  const verb = e.new_state === "FREE" ? "vacated" : "occupied";
  return `${name} ${verb}`;
}

// Tab switching
function switchTab(tabId) {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabId);
  });
  document.querySelectorAll('.tab-content').forEach(content => {
    content.classList.toggle('active', content.id === `${tabId}-tab`);
  });
  if (tabId === 'analytics') loadAnalytics();
  else if (tabId === 'state-changes') loadStateChanges();
}

// Chart instances
let occupancyHeatmapChart = null;
let hourlyOccupancyChart = null;
let dwellDistributionChart = null;
let turnoverChart = null;
let zoneComparisonChart = null;

// Zone colors for charts
const zoneColors = {
  'A': { bg: 'rgba(59, 130, 246, 0.2)', border: '#3b82f6' },
  'B': { bg: 'rgba(34, 197, 94, 0.2)', border: '#22c55e' },
  'C': { bg: 'rgba(245, 158, 11, 0.2)', border: '#f59e0b' },
  'D': { bg: 'rgba(239, 68, 68, 0.2)', border: '#ef4444' },
  'E': { bg: 'rgba(139, 92, 246, 0.2)', border: '#8b5cf6' },
  'F': { bg: 'rgba(6, 182, 212, 0.2)', border: '#06b6d4' },
  'G': { bg: 'rgba(244, 114, 182, 0.2)', border: '#f472b6' }
};

function getZoneColor(zone) {
  return zoneColors[zone] || { bg: 'rgba(107, 114, 128, 0.2)', border: '#6b7280' };
}

const chartDefaults = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: {
      labels: { color: '#475569', font: { family: 'Inter' } }
    }
  },
  scales: {
    x: {
      ticks: { color: '#64748b', font: { family: 'Inter' } },
      grid: { color: 'rgba(0, 0, 0, 0.06)' }
    },
    y: {
      ticks: { color: '#64748b', font: { family: 'Inter' } },
      grid: { color: 'rgba(0, 0, 0, 0.06)' }
    }
  }
};

// ── Analytics ────────────────────────────────────────────────────────────────

async function loadAnalytics() {
  const range = document.getElementById('timeRange')?.value || '24h';
  const zone = document.getElementById('zoneFilter')?.value || '';

  try {
    const url = `/analytics/summary?range=${range}` + (zone ? `&zone=${zone}` : '');
    const res = await fetch(url);
    const data = await res.json();

    const hasData = data.total_occupancy_events > 0;

    document.querySelector('.analytics-grid').style.display = hasData ? 'grid' : 'none';
    document.querySelector('.stats-row').style.display = hasData ? 'grid' : 'none';
    document.getElementById('analyticsEmpty').style.display = hasData ? 'none' : 'flex';

    populateZoneFilter(data.zones || []);

    if (!hasData) return;

    renderAnalyticsStats(data);
    renderOccupancyHeatmap(data.heatmap);
    renderHourlyOccupancyChart(data.hourly_occupancy, zone);
    renderDwellDistributionChart(data.dwell_distribution, zone);
    renderTurnoverChart(data.turnover, data.zones);
    renderZoneComparisonChart(data.zone_stats, data.zones, data.turnover);
  } catch (err) {
    console.error('Failed to load analytics:', err);
  }
}

function onAnalyticsFilterChange() {
  loadAnalytics();
}

function populateZoneFilter(zones) {
  const select = document.getElementById('zoneFilter');
  if (!select) return;
  const current = select.value;
  const options = '<option value="">All Zones</option>' +
    zones.map(z => `<option value="${z}"${z === current ? ' selected' : ''}>Zone ${z}</option>`).join('');
  select.innerHTML = options;

  // Also populate state changes zone filter
  const scSelect = document.getElementById('stateChangesZoneFilter');
  if (scSelect) {
    const scCurrent = scSelect.value;
    scSelect.innerHTML = '<option value="">All Zones</option>' +
      zones.map(z => `<option value="${z}"${z === scCurrent ? ' selected' : ''}>Zone ${z}</option>`).join('');
  }
}

function renderAnalyticsStats(data) {
  document.getElementById('statOccupancyEvents').textContent = (data.total_occupancy_events || 0).toLocaleString();

  const peak = data.peak_occupancy || {};
  document.getElementById('statPeakOcc').textContent = peak.peak_pct ? peak.peak_pct + '%' : '--';

  const turnover = data.turnover || {};
  document.getElementById('statTurnover').textContent = turnover.all != null ? turnover.all + 'x' : '--';

  const avgMin = data.avg_parking_minutes;
  const medMin = data.median_parking_minutes;
  let durationText = '--';
  if (avgMin > 0 && medMin > 0) durationText = `${avgMin} / ${medMin} min`;
  else if (avgMin > 0) durationText = `${avgMin} min`;
  document.getElementById('statAvgDuration').textContent = durationText;

  document.getElementById('statUtilization').textContent = data.utilization_pct != null ? data.utilization_pct + '%' : '--';
}

function renderHourlyOccupancyChart(hourlyData, selectedZone) {
  const ctx = document.getElementById('hourlyOccupancyChart')?.getContext('2d');
  if (!ctx) return;
  if (hourlyOccupancyChart) hourlyOccupancyChart.destroy();

  const badge = document.getElementById('hourlyChartBadge');
  if (badge) badge.textContent = selectedZone ? `Zone ${selectedZone}` : 'All Zones';

  const labels = (hourlyData || []).map(entry => {
    const d = new Date(entry.hour);
    return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  });

  let datasets;
  if (!selectedZone) {
    const allZones = new Set();
    (hourlyData || []).forEach(entry => {
      Object.keys(entry.zones || {}).forEach(z => allZones.add(z));
    });
    const zoneList = Array.from(allZones).sort();

    if (zoneList.length <= 1) {
      const counts = (hourlyData || []).map(e => e.all || 0);
      const peakVal = Math.max(...counts, 0);
      const peakIdx = peakVal > 0 ? counts.indexOf(peakVal) : -1;
      datasets = [{
        label: 'Occupancy Events',
        data: counts,
        backgroundColor: counts.map((v, i) => i === peakIdx ? 'rgba(59, 130, 246, 0.95)' : 'rgba(59, 130, 246, 0.45)'),
        borderColor: '#3b82f6',
        borderWidth: 2,
        borderRadius: 6,
      }];
    } else {
      datasets = zoneList.map(zone => {
        const color = getZoneColor(zone);
        return {
          label: `Zone ${zone}`,
          data: (hourlyData || []).map(e => (e.zones || {})[zone] || 0),
          backgroundColor: color.border + 'AA',
          borderColor: color.border,
          borderWidth: 1,
          borderRadius: 4,
        };
      });
    }
  } else {
    const color = getZoneColor(selectedZone);
    const counts = (hourlyData || []).map(e => (e.zones || {})[selectedZone] || 0);
    const peakVal = Math.max(...counts, 0);
    const peakIdx = peakVal > 0 ? counts.indexOf(peakVal) : -1;
    datasets = [{
      label: `Zone ${selectedZone}`,
      data: counts,
      backgroundColor: counts.map((v, i) => i === peakIdx ? color.border + 'F0' : color.border + '55'),
      borderColor: color.border,
      borderWidth: 2,
      borderRadius: 6,
    }];
  }

  hourlyOccupancyChart = new Chart(ctx, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      ...chartDefaults,
      plugins: { ...chartDefaults.plugins, title: { display: false } },
      scales: {
        ...chartDefaults.scales,
        x: { ...chartDefaults.scales.x, stacked: !selectedZone, ticks: { ...chartDefaults.scales.x.ticks, maxRotation: 45, autoSkip: true, maxTicksLimit: 24 } },
        y: { ...chartDefaults.scales.y, stacked: !selectedZone, beginAtZero: true, ticks: { ...chartDefaults.scales.y.ticks, callback: v => Number.isInteger(v) ? v : '' } }
      }
    }
  });
}

function renderOccupancyHeatmap(heatmapData) {
  const ctx = document.getElementById('occupancyHeatmap')?.getContext('2d');
  if (!ctx) return;
  if (occupancyHeatmapChart) occupancyHeatmapChart.destroy();

  const dayLabels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const maxVal = Math.max(...(heatmapData || []).map(d => d.value), 1);

  occupancyHeatmapChart = new Chart(ctx, {
    type: 'matrix',
    data: {
      datasets: [{
        label: 'Occupancy Events',
        data: (heatmapData || []).map(d => ({ x: d.hour, y: d.day, v: d.value })),
        backgroundColor(ctx) {
          const v = ctx.dataset.data[ctx.dataIndex]?.v || 0;
          const alpha = Math.min(0.1 + (v / maxVal) * 0.85, 0.95);
          return `rgba(99, 102, 241, ${alpha})`;
        },
        borderColor: 'rgba(255, 255, 255, 0.6)',
        borderWidth: 1,
        width: ({chart}) => (chart.chartArea || {}).width / 24 - 1,
        height: ({chart}) => (chart.chartArea || {}).height / 7 - 1,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => {
              const d = items[0]?.raw;
              return d ? `${dayLabels[d.y]} ${String(d.x).padStart(2, '0')}:00` : '';
            },
            label: (item) => ` ${item.raw.v} events`,
          }
        }
      },
      scales: {
        x: {
          type: 'linear',
          position: 'top',
          min: -0.5,
          max: 23.5,
          offset: false,
          ticks: {
            stepSize: 1,
            callback: v => Number.isInteger(v) && v >= 0 && v <= 23 ? String(v).padStart(2, '0') + ':00' : '',
            color: '#64748b',
            font: { family: 'Inter', size: 10 },
            maxRotation: 0,
          },
          grid: { display: false },
        },
        y: {
          type: 'linear',
          min: -0.5,
          max: 6.5,
          offset: false,
          ticks: {
            stepSize: 1,
            callback: v => Number.isInteger(v) && v >= 0 && v <= 6 ? dayLabels[v] : '',
            color: '#64748b',
            font: { family: 'Inter', size: 11 },
          },
          grid: { display: false },
        }
      }
    }
  });
}

function renderDwellDistributionChart(dist, selectedZone) {
  const ctx = document.getElementById('dwellDistributionChart')?.getContext('2d');
  if (!ctx) return;
  if (dwellDistributionChart) dwellDistributionChart.destroy();

  const badge = document.getElementById('dwellChartBadge');
  if (badge) badge.textContent = selectedZone ? `Zone ${selectedZone}` : 'All Zones';

  const buckets = (dist || {}).buckets || {};
  const data = [
    buckets['0_5'] || 0,
    buckets['5_15'] || 0,
    buckets['15_30'] || 0,
    buckets['30_60'] || 0,
    buckets['60_120'] || 0,
    buckets['120_plus'] || 0,
  ];
  const labels = ['0-5 min', '5-15 min', '15-30 min', '30-60 min', '1-2 hrs', '2+ hrs'];
  const colors = [
    'rgba(34, 197, 94, 0.7)',
    'rgba(59, 130, 246, 0.7)',
    'rgba(245, 158, 11, 0.7)',
    'rgba(239, 68, 68, 0.7)',
    'rgba(139, 92, 246, 0.7)',
    'rgba(236, 72, 153, 0.7)',
  ];

  dwellDistributionChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'Vehicles',
        data,
        backgroundColor: colors,
        borderColor: colors.map(c => c.replace('0.7', '1')),
        borderWidth: 1,
        borderRadius: 4,
      }]
    },
    options: {
      ...chartDefaults,
      plugins: { ...chartDefaults.plugins, legend: { display: false } },
      scales: {
        x: { ...chartDefaults.scales.x },
        y: {
          ...chartDefaults.scales.y,
          beginAtZero: true,
          ticks: { ...chartDefaults.scales.y.ticks, callback: v => Number.isInteger(v) ? v : '' }
        }
      }
    }
  });
}

function renderTurnoverChart(turnover, zones) {
  const ctx = document.getElementById('turnoverChart')?.getContext('2d');
  if (!ctx) return;
  if (turnoverChart) turnoverChart.destroy();

  const byZone = (turnover || {}).by_zone || {};
  const zoneList = zones || Object.keys(byZone);
  const data = zoneList.map(z => byZone[z] || 0);

  turnoverChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: zoneList.map(z => `Zone ${z}`),
      datasets: [{
        label: 'Turnover Rate',
        data,
        backgroundColor: zoneList.map(z => getZoneColor(z).border + 'AA'),
        borderColor: zoneList.map(z => getZoneColor(z).border),
        borderWidth: 1,
        borderRadius: 6,
      }]
    },
    options: {
      ...chartDefaults,
      plugins: {
        ...chartDefaults.plugins,
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (item) => ` ${item.raw}x turnover (events per slot)`,
          }
        }
      },
      scales: {
        x: { ...chartDefaults.scales.x },
        y: {
          ...chartDefaults.scales.y,
          beginAtZero: true,
          title: { display: true, text: 'events / slot', color: '#94a3b8', font: { family: 'Inter', size: 11 } },
        }
      }
    }
  });
}

function renderZoneComparisonChart(zoneStats, zones, turnover) {
  const ctx = document.getElementById('zoneComparisonChart')?.getContext('2d');
  if (!ctx) return;
  if (zoneComparisonChart) zoneComparisonChart.destroy();

  const zoneList = zones || Object.keys(zoneStats || {});
  const byZone = (turnover || {}).by_zone || {};

  zoneComparisonChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: zoneList.map(z => `Zone ${z}`),
      datasets: [
        {
          label: 'Occupancy Events',
          data: zoneList.map(z => zoneStats?.[z]?.total_occupancy_events || 0),
          backgroundColor: 'rgba(99, 102, 241, 0.7)',
          borderColor: '#6366f1',
          borderWidth: 1,
          borderRadius: 4,
        },
        {
          label: 'Avg Dwell (min)',
          data: zoneList.map(z => zoneStats?.[z]?.avg_parking_minutes || 0),
          backgroundColor: 'rgba(16, 185, 129, 0.7)',
          borderColor: '#10b981',
          borderWidth: 1,
          borderRadius: 4,
        },
        {
          label: 'Turnover (x)',
          data: zoneList.map(z => byZone[z] || 0),
          backgroundColor: 'rgba(245, 158, 11, 0.7)',
          borderColor: '#f59e0b',
          borderWidth: 1,
          borderRadius: 4,
        }
      ]
    },
    options: {
      ...chartDefaults,
      scales: {
        ...chartDefaults.scales,
        y: { ...chartDefaults.scales.y, beginAtZero: true, ticks: { ...chartDefaults.scales.y.ticks, callback: v => Number.isInteger(v) ? v : '' } }
      }
    }
  });
}

// ── State Changes Tab ────────────────────────────────────────────────────────

let stateChangesCache = [];

async function loadStateChanges() {
  const zone = document.getElementById('stateChangesZoneFilter')?.value || '';
  try {
    const url = `/state-changes?limit=100` + (zone ? `&zone=${zone}` : '');
    const res = await fetch(url);
    const data = await res.json();
    stateChangesCache = data.changes || [];
    renderStateChanges(stateChangesCache);
  } catch (err) {
    console.error('Failed to load state changes:', err);
    document.getElementById('stateChangesEmpty').style.display = 'flex';
    document.getElementById('stateChangesList').style.display = 'none';
  }
}

function renderStateChanges(changes) {
  const container = document.getElementById('stateChangesList');
  const emptyState = document.getElementById('stateChangesEmpty');

  if (changes.length === 0) {
    container.style.display = 'none';
    emptyState.style.display = 'flex';
    return;
  }

  container.style.display = 'flex';
  emptyState.style.display = 'none';

  const html = changes.map(change => {
    const ts = new Date(change.ts);
    const timeStr = ts.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const dateStr = ts.toLocaleDateString([], { month: 'short', day: 'numeric' });
    const stateClass = change.new_state === 'OCCUPIED' ? 'occupied' : 'free';
    const stateIcon = change.new_state === 'OCCUPIED' ? '&#x1F697;' : '&#x2705;';

    return `
      <div class="alert-card ${stateClass}">
        <div class="alert-icon-compact">${stateIcon}</div>
        <div class="alert-content">
          <div class="alert-header">
            <span class="alert-slot">${escapeHtml(change.slot_name)}</span>
            <span class="alert-zone">Zone ${escapeHtml(change.zone)}</span>
            <span class="alert-state-badge ${stateClass}">${escapeHtml(change.new_state)}</span>
          </div>
        </div>
        <div class="alert-time-compact">${dateStr} ${timeStr}</div>
      </div>
    `;
  }).join('');

  container.innerHTML = html;
}

// ── Dashboard ────────────────────────────────────────────────────────────────

let slots = [];
let stateById = {};
let sinceById = {};
let serverZoneStats = null;
let sensorLastseen = {};
let sensorAlerts = {};
let collapsedZones = new Set();
let calibratingSlots = {};
let searchQuery = "";
const SLOTS_PER_PAGE = 50;
let zonePages = {};

const SENSOR_OFFLINE_THRESHOLD_MS = 6000 * 1000; // ~100 minutes

function filterSlotsBySearch(slotList) {
  if (!searchQuery) return slotList;
  const q = searchQuery.toLowerCase();
  return slotList.filter(s => {
    const name = (s.name || `Slot ${s.id}`).toLowerCase();
    return name.includes(q);
  });
}

function getSlotStatus(slotId) {
  return (stateById[slotId] || "FREE") === "OCCUPIED" ? "OCCUPIED" : "FREE";
}

function isSensorOffline(slotId) {
  const ts = sensorLastseen[slotId];
  if (!ts) return false; // no data yet, don't flag
  try {
    const d = new Date(ts);
    return (Date.now() - d.getTime()) > SENSOR_OFFLINE_THRESHOLD_MS;
  } catch { return false; }
}

function getSensorAlert(slotId) {
  return sensorAlerts[slotId] || null;
}

function computeZoneStats() {
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
  let total = 0, free = 0;
  for (const k of Object.keys(zones || {})) {
    total += zones[k].total || 0;
    free += zones[k].free || 0;
  }
  return { free, total };
}

function renderZones(zones) {
  const bar = document.getElementById("zoneBar");
  bar.innerHTML = "";
  const keys = Object.keys(zones || {}).sort();
  for (const k of keys) {
    const z = zones[k];
    const pct = z.total > 0 ? (z.occupied / z.total * 100) : 0;

    const chip = document.createElement("div");
    chip.className = "zoneChip";
    chip.setAttribute("data-zone", k);

    const label = document.createElement("div");
    label.className = "zoneChip-label";
    label.innerHTML = `<strong>Zone ${escapeHtml(k)}</strong> <span style="color:var(--muted)">${z.occupied}/${z.total}</span>`;

    const barEl = document.createElement("div");
    barEl.className = "zoneChip-bar";
    const fill = document.createElement("div");
    fill.className = "zoneChip-bar-fill";
    fill.style.width = pct + "%";
    if (pct > 80) fill.style.background = "var(--occupied)";
    else if (pct > 50) fill.style.background = "#f59e0b";
    else fill.style.background = "var(--free)";
    barEl.appendChild(fill);

    chip.appendChild(label);
    chip.appendChild(barEl);

    chip.addEventListener("click", () => {
      const section = document.getElementById("zone-section-" + k);
      if (section) {
        collapsedZones.delete(k);
        refreshLayout();
        setTimeout(() => section.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
      }
    });

    bar.appendChild(chip);
  }
}

function renderZoneSections(zones) {
  const container = document.getElementById("zoneSections");
  const zoneKeys = Object.keys(zones || {}).sort();
  const expectedIds = new Set(zoneKeys.map(k => `zone-section-${k}`));

  for (const child of Array.from(container.children)) {
    if (!expectedIds.has(child.id)) child.remove();
  }

  for (const zoneKey of zoneKeys) {
    const z = zones[zoneKey];
    const isCollapsed = collapsedZones.has(zoneKey);
    const sectionId = `zone-section-${zoneKey}`;
    let section = document.getElementById(sectionId);

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

      const calibrateZoneBtn = document.createElement("button");
      calibrateZoneBtn.className = "zoneCalibrateBtn";
      calibrateZoneBtn.type = "button";
      calibrateZoneBtn.textContent = "Calibrate Zone";
      calibrateZoneBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        calibrateZone(zoneKey, calibrateZoneBtn);
      });

      titleRow.appendChild(toggle);
      titleRow.appendChild(title);
      titleRow.appendChild(calibrateZoneBtn);

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

    if (isCollapsed) section.classList.add("collapsed");
    else section.classList.remove("collapsed");

    const toggle = section.querySelector(".zoneToggle");
    toggle.textContent = isCollapsed ? "\u25B8" : "\u25BE";
    toggle.setAttribute("aria-expanded", !isCollapsed);

    section.querySelector(".zoneTitle").textContent = `Zone ${zoneKey}: Occupancy (${z.occupied}/${z.total})`;
    section.querySelector(".zoneSubtitle").textContent = `Free ${z.free} \u2022 Occupied ${z.occupied} \u2022 Total ${z.total}`;

    // Occupancy bar
    let occBar = section.querySelector(".zone-occupancy-bar");
    if (!occBar) {
      occBar = document.createElement("div");
      occBar.className = "zone-occupancy-bar";
      const occFill = document.createElement("div");
      occFill.className = "zone-occupancy-bar-fill";
      occBar.appendChild(occFill);
      section.querySelector(".zoneHeader").appendChild(occBar);
    }
    const occPct = z.total > 0 ? (z.occupied / z.total * 100) : 0;
    const occFill = occBar.querySelector(".zone-occupancy-bar-fill");
    occFill.style.width = occPct + "%";
    if (occPct > 80) occFill.style.background = "var(--occupied)";
    else if (occPct > 50) occFill.style.background = "#f59e0b";
    else occFill.style.background = "var(--free)";

    if (isCollapsed) {
      const grid = section.querySelector(".zoneSlotGrid");
      if (grid) grid.innerHTML = "";
      continue;
    }

    // Slot tiles
    const grid = section.querySelector(".zoneSlotGrid");
    const allZoneSlots = filterSlotsBySearch(
      slots.filter(s => (s.zone || "A") === zoneKey)
    ).sort((a, b) => a.id - b.id);

    // Pagination
    const currentPage = zonePages[zoneKey] || 1;
    const visibleCount = currentPage * SLOTS_PER_PAGE;
    const zoneSlots = allZoneSlots.slice(0, visibleCount);
    const remaining = allZoneSlots.length - visibleCount;

    const existingTiles = {};
    for (const tile of Array.from(grid.children)) {
      if (tile.classList.contains("showMoreBtn")) continue;
      const sid = tile.dataset.slotId;
      if (sid) existingTiles[sid] = tile;
    }
    const expectedSlotIds = new Set(zoneSlots.map(s => String(s.id)));
    for (const [sid, tile] of Object.entries(existingTiles)) {
      if (!expectedSlotIds.has(sid)) tile.remove();
    }

    // Remove old show-more button
    const oldShowMore = grid.querySelector(".showMoreBtn");
    if (oldShowMore) oldShowMore.remove();

    for (const s of zoneSlots) {
      const status = getSlotStatus(s.id);
      const offline = isSensorOffline(s.id);
      const alert = getSensorAlert(s.id);
      const isCalibrating = !!calibratingSlots[s.id];
      const tileId = String(s.id);
      let tile = existingTiles[tileId];

      if (!tile) {
        tile = _createSlotTile(s);
        grid.appendChild(tile);
      }

      // Update state class
      let tileClass = `slot ${status === "OCCUPIED" ? "occupied" : "free"}`;
      if (offline) tileClass += " sensor-offline";
      if (alert) tileClass += " sensor-alert";
      if (isCalibrating) tileClass += " calibrating";
      tile.className = tileClass;

      const stateEl = tile.querySelector(".slotState");
      if (isCalibrating) {
        stateEl.innerHTML = '<span class="calibrating-text">Calibrating...</span>';
      } else {
        stateEl.textContent = offline ? "OFFLINE" : status;
      }

      // Meta: duration + sensor health indicators
      const sinceTs = sinceById[s.id];
      let metaHtml = "";

      // Duration in current state
      if (sinceTs) {
        const ago = timeAgo(sinceTs);
        if (ago) metaHtml += `<div class="slot-duration">${ago}</div>`;
      }

      // Sensor health
      if (offline) {
        metaHtml += '<div class="slot-health-alert">&#x26A0; Offline</div>';
      }
      if (alert) {
        const alertIcon = alert.type === "battery_low" ? "&#x1F50B;" : "&#x1F321;";
        const alertLabel = alert.type === "battery_low" ? "Battery low" : "Temp high";
        metaHtml += `<div class="slot-health-alert">${alertIcon} ${alertLabel}</div>`;
      }
      if (isCalibrating) {
        metaHtml += '<div class="slot-calibrating-badge">Calibrating...</div>';
      }
      tile.querySelector(".slotMeta").innerHTML = metaHtml;
    }

    // Show more button
    if (remaining > 0) {
      const showMoreBtn = document.createElement("button");
      showMoreBtn.className = "showMoreBtn";
      showMoreBtn.textContent = `Show more (${remaining} remaining)`;
      showMoreBtn.addEventListener("click", () => {
        zonePages[zoneKey] = (zonePages[zoneKey] || 1) + 1;
        refreshLayout();
      });
      grid.appendChild(showMoreBtn);
    }
  }
}

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

  tile.appendChild(top);
  tile.appendChild(meta);

  // Click handler to show popover
  tile.addEventListener("click", (e) => {
    e.stopPropagation();
    showSlotPopover(s, tile);
  });

  return tile;
}

// ── Slot Detail Popover ─────────────────────────────────────────────────────

function showSlotPopover(slot, tileEl) {
  hideSlotPopover();

  const status = getSlotStatus(slot.id);
  const offline = isSensorOffline(slot.id);
  const alert = getSensorAlert(slot.id);
  const isCalibrating = !!calibratingSlots[slot.id];
  const sinceTs = sinceById[slot.id];
  const lastSeen = sensorLastseen[slot.id];

  const popover = document.createElement("div");
  popover.id = "slotPopover";
  popover.className = "slot-popover";

  let stateText = isCalibrating ? "Calibrating" : (offline ? "OFFLINE" : status);
  let stateClass = status === "OCCUPIED" ? "occupied" : "free";
  if (isCalibrating) stateClass = "calibrating";

  let html = `
    <div class="popover-header">
      <strong>${escapeHtml(slot.name || 'Slot ' + slot.id)}</strong>
      <button class="popover-close" onclick="hideSlotPopover()">&times;</button>
    </div>
    <div class="popover-body">
      <div class="popover-row"><span class="popover-label">Zone</span><span>${escapeHtml(slot.zone || 'A')}</span></div>
      <div class="popover-row"><span class="popover-label">State</span><span class="popover-state ${stateClass}">${stateText}</span></div>
  `;

  if (sinceTs) {
    const ago = timeAgo(sinceTs);
    html += `<div class="popover-row"><span class="popover-label">Duration</span><span>${ago || '--'}</span></div>`;
  }

  if (lastSeen) {
    html += `<div class="popover-row"><span class="popover-label">Last seen</span><span>${fmtTs(lastSeen)}</span></div>`;
  }

  if (alert) {
    const alertLabel = alert.type === "battery_low" ? "Battery low" : "Temperature high";
    html += `<div class="popover-row popover-alert"><span class="popover-label">Alert</span><span>${alertLabel}</span></div>`;
  }

  if (offline) {
    html += `<div class="popover-row popover-alert"><span class="popover-label">Sensor</span><span>Offline</span></div>`;
  }

  html += `</div>`;

  if (isCalibrating) {
    html += `<div class="popover-footer"><button class="popover-calibrate-btn" disabled>Calibrating...</button></div>`;
  } else {
    html += `<div class="popover-footer"><button class="popover-calibrate-btn" id="popoverCalibrateBtn">Recalibrate</button></div>`;
  }

  popover.innerHTML = html;

  // Position offscreen first so we can measure
  popover.style.top = "-9999px";
  popover.style.left = "-9999px";
  document.body.appendChild(popover);

  // Use rAF to get correct dimensions after layout
  requestAnimationFrame(() => {
    const rect = tileEl.getBoundingClientRect();
    const pw = popover.offsetWidth;
    const ph = popover.offsetHeight;
    let top = rect.bottom + 8;
    let left = rect.left + (rect.width / 2) - (pw / 2);

    if (left < 8) left = 8;
    if (left + pw > window.innerWidth - 8) left = window.innerWidth - pw - 8;
    if (top + ph > window.innerHeight - 8) {
      top = rect.top - ph - 8;
    }
    if (top < 8) top = 8;

    popover.style.top = top + "px";
    popover.style.left = left + "px";
  });

  // Wire calibrate button
  const calBtn = document.getElementById("popoverCalibrateBtn");
  if (calBtn) {
    calBtn.addEventListener("click", async () => {
      calBtn.disabled = true;
      calBtn.textContent = "Sending...";
      try {
        const res = await fetch(`/calibrate/${slot.id}`, { method: "POST" });
        if (res.ok) {
          calibratingSlots[slot.id] = { ts: new Date().toISOString() };
          calBtn.textContent = "Calibrating...";
          refreshLayout();
        } else {
          const err = await res.json().catch(() => ({}));
          calBtn.textContent = err.detail || "Failed";
          setTimeout(() => { calBtn.textContent = "Recalibrate"; calBtn.disabled = false; }, 3000);
        }
      } catch {
        calBtn.textContent = "Error";
        setTimeout(() => { calBtn.textContent = "Recalibrate"; calBtn.disabled = false; }, 3000);
      }
    });
  }

  // Close on outside click
  setTimeout(() => {
    document.addEventListener("click", _popoverOutsideClick);
  }, 0);
}

function _popoverOutsideClick(e) {
  const popover = document.getElementById("slotPopover");
  if (popover && !popover.contains(e.target)) {
    hideSlotPopover();
  }
}

function hideSlotPopover() {
  const existing = document.getElementById("slotPopover");
  if (existing) existing.remove();
  document.removeEventListener("click", _popoverOutsideClick);
}

// ── Zone-level + Bulk Calibrate ─────────────────────────────────────────────

async function calibrateZone(zoneKey, btnEl) {
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = "Calibrating...";
  }
  try {
    const res = await fetch("/calibrate/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ zone: zoneKey }),
    });
    const data = await res.json();
    if (btnEl) {
      btnEl.textContent = `Calibrating ${data.sent || 0} slots...`;
      setTimeout(() => {
        btnEl.textContent = "Calibrate Zone";
        btnEl.disabled = false;
      }, 5000);
    }
  } catch {
    if (btnEl) {
      btnEl.textContent = "Failed";
      setTimeout(() => {
        btnEl.textContent = "Calibrate Zone";
        btnEl.disabled = false;
      }, 3000);
    }
  }
}

async function calibrateAll() {
  const btn = document.getElementById("calibrateAllBtn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Calibrating...";
  }
  try {
    const res = await fetch("/calibrate/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await res.json();
    if (btn) {
      btn.textContent = `Calibrating ${data.sent || 0} slots...`;
      setTimeout(() => {
        btn.textContent = "Calibrate All";
        btn.disabled = false;
      }, 5000);
    }
  } catch {
    if (btn) {
      btn.textContent = "Failed";
      setTimeout(() => {
        btn.textContent = "Calibrate All";
        btn.disabled = false;
      }, 3000);
    }
  }
}

// ── KPIs + Layout ───────────────────────────────────────────────────────────

function updateKPIs() {
  const zones = computeZoneStats();
  const totals = computeTotals(zones);

  const totalEl = document.getElementById('kpiTotalSlots');
  const occEl = document.getElementById('kpiOccupancy');
  const zonesEl = document.getElementById('kpiZones');
  const sensorsEl = document.getElementById('kpiSensors');
  const alertsEl = document.getElementById('kpiAlerts');

  if (totalEl) totalEl.textContent = totals.total.toLocaleString();
  if (zonesEl) zonesEl.textContent = Object.keys(zones).length;

  if (occEl) {
    const pct = totals.total > 0 ? ((totals.total - totals.free) / totals.total * 100) : 0;
    occEl.textContent = pct.toFixed(1) + '%';
    occEl.className = 'kpi-value';
    if (pct > 80) occEl.classList.add('kpi-value--high');
    else if (pct < 30) occEl.classList.add('kpi-value--low');
  }

  // Sensors online
  if (sensorsEl) {
    const now = Date.now();
    let online = 0;
    for (const sid of Object.keys(sensorLastseen)) {
      try {
        const d = new Date(sensorLastseen[sid]);
        if ((now - d.getTime()) < SENSOR_OFFLINE_THRESHOLD_MS) online++;
      } catch { }
    }
    const total = Object.keys(sensorLastseen).length || totals.total;
    sensorsEl.textContent = total > 0 ? `${online}/${total}` : '--';
  }

  // Device alerts
  if (alertsEl) {
    alertsEl.textContent = Object.keys(sensorAlerts).length;
  }
}

function refreshLayout() {
  const zones = computeZoneStats();
  renderZones(zones);
  renderZoneSections(zones);
  updateKPIs();
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

// ── Init ─────────────────────────────────────────────────────────────────────

async function init() {
  const res = await fetch("/state");
  const data = await res.json();

  slots = (data.slots || []).slice().sort((a, b) => a.id - b.id);
  stateById = data.state_by_id || {};
  sinceById = data.since_by_id || {};
  serverZoneStats = data.zones || null;
  sensorLastseen = data.sensor_lastseen || {};
  sensorAlerts = data.sensor_alerts || {};

  // Load calibrating state from server
  const serverCalibrating = data.calibrating || {};
  for (const [sid, val] of Object.entries(serverCalibrating)) {
    calibratingSlots[Number(sid)] = val;
  }

  // Always start collapsed
  const zoneKeys = new Set(slots.map(s => s.zone || "A"));
  for (const z of zoneKeys) collapsedZones.add(z);

  refreshLayout();

  // Wire up search input
  const searchInput = document.getElementById("slotSearch");
  if (searchInput) {
    searchInput.addEventListener("input", (e) => {
      searchQuery = e.target.value.trim();
      zonePages = {}; // reset pagination on search
      refreshLayout();
    });
  }

  // Wire up calibrate-all button
  const calAllBtn = document.getElementById("calibrateAllBtn");
  if (calAllBtn) {
    calAllBtn.addEventListener("click", calibrateAll);
  }

  // Load initial change log
  try {
    const scRes = await fetch("/state-changes?limit=20");
    const scData = await scRes.json();
    const changes = scData.changes || [];
    for (let i = changes.length - 1; i >= 0; i--) {
      prependLog(changes[i]);
    }
  } catch (err) {
    console.error("Failed to load initial change log:", err);
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
          sensorLastseen[id] = obj.ts;
        }
        refreshLayout();
        prependLog(obj);

        // Live update state changes tab if active (respect zone filter)
        const scTab = document.getElementById('state-changes-tab');
        if (scTab && scTab.classList.contains('active')) {
          const scZoneFilter = document.getElementById('stateChangesZoneFilter')?.value || '';
          if (!scZoneFilter || obj.zone === scZoneFilter) {
            stateChangesCache.unshift(obj);
            stateChangesCache = stateChangesCache.slice(0, 100);
            renderStateChanges(stateChangesCache);
          }
        }
      }

      if (obj.event === "device_alert") {
        const id = parseSlotId(obj);
        if (!isNaN(id)) {
          sensorAlerts[id] = { type: obj.alert_type, ts: obj.ts };
          sensorLastseen[id] = obj.ts;
        }
        refreshLayout();
        prependLog(obj);
      }

      // Clear device alert when sensor sends a normal occupancy update
      if (obj.event === "slot_state_changed") {
        const alertId = parseSlotId(obj);
        if (!isNaN(alertId) && sensorAlerts[alertId]) {
          delete sensorAlerts[alertId];
        }
      }

      // Calibration events
      if (obj.event === "calibration_started") {
        const id = parseSlotId(obj);
        if (!isNaN(id)) {
          calibratingSlots[id] = { ts: obj.ts };
        }
        refreshLayout();
        prependLog(obj);
      }

      if (obj.event === "calibration_done") {
        const id = parseSlotId(obj);
        if (!isNaN(id)) {
          delete calibratingSlots[id];
        }
        refreshLayout();
        prependLog(obj);
      }

      if (obj.event === "bulk_calibration_started") {
        prependLog(obj);
      }
    } catch (e) {
      // ignore parse errors
    }
  };
}

init();
