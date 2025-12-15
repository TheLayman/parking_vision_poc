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

let slots = [];
let stateById = {};

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

    const section = document.createElement("section");
    section.className = "zoneSection";

    const header = document.createElement("div");
    header.className = "zoneHeader";

    const title = document.createElement("div");
    title.className = "zoneTitle";
    title.textContent = `Zone ${zoneKey}: Occupancy (${z.occupied}/${z.total})`;

    const subtitle = document.createElement("div");
    subtitle.className = "zoneSubtitle";
    subtitle.textContent = `Free ${z.free} • Occupied ${z.occupied} • Total ${z.total}`;

    header.appendChild(title);
    header.appendChild(subtitle);
    section.appendChild(header);

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
