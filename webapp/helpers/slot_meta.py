"""Slot metadata loading, zone statistics, and device-name mapping."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from webapp.helpers.data_io import load_yaml

log = logging.getLogger(__name__)

# ── Module-level caches ──────────────────────────────────────────────────────
_meta_cache = None
_meta_cache_mtime = None
_device_name_to_slot: dict[str, int] = {}
_device_name_map_mtime = None


# ── Metadata loading ─────────────────────────────────────────────────────────

def load_slot_meta_by_id(meta_path: Path) -> dict[int, dict]:
    """Load slot metadata from a YAML file, with file-mtime caching."""
    global _meta_cache, _meta_cache_mtime

    if not meta_path.exists():
        return {}

    # Check if cache is still valid based on file modification time
    try:
        current_mtime = meta_path.stat().st_mtime
        if _meta_cache is not None and _meta_cache_mtime == current_mtime:
            return _meta_cache
    except Exception:
        pass  # If stat fails, proceed to reload

    data = load_yaml(meta_path)
    if not data:
        return {}

    meta: dict[int, dict] = {}

    if isinstance(data, dict):
        for k, v in data.items():
            try:
                slot_id = int(k)
            except Exception:
                continue
            if isinstance(v, dict):
                meta[slot_id] = v
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, dict) or "id" not in item:
                continue
            try:
                slot_id = int(item["id"])
            except Exception:
                continue
            meta[slot_id] = item

    # Update cache
    _meta_cache = meta
    _meta_cache_mtime = current_mtime
    return meta


def load_slot_ids(meta_path: Path) -> list[int]:
    """Return sorted slot IDs from metadata."""
    return sorted(load_slot_meta_by_id(meta_path).keys())


# ── Device-name mapping ─────────────────────────────────────────────────────

def get_slot_id_by_device_name(device_name: str, meta_by_id: dict[int, dict]) -> int | None:
    """Map MQTT device name to slot ID via cached reverse lookup (O(1))."""
    _rebuild_device_name_map_if_needed(meta_by_id)
    return _device_name_to_slot.get(device_name.upper())


def _rebuild_device_name_map_if_needed(meta_by_id: dict[int, dict]):
    """Rebuild the reverse device-name -> slot_id map when metadata changes."""
    global _device_name_to_slot, _device_name_map_mtime
    if _device_name_map_mtime == _meta_cache_mtime and _device_name_to_slot:
        return
    lookup: dict[str, int] = {}
    for slot_id, meta in meta_by_id.items():
        dn = meta.get("device_name")
        if dn:
            lookup[dn.upper()] = slot_id
        name = meta.get("name", "")
        if name:
            lookup.setdefault(name.upper(), slot_id)
    _device_name_to_slot = lookup
    _device_name_map_mtime = _meta_cache_mtime


# ── Zone statistics ──────────────────────────────────────────────────────────

def calculate_zone_stats(slot_ids: list[int], occupied_ids: set,
                         meta_by_id: dict[int, dict]) -> tuple:
    """Calculate zone statistics. Returns ``(zones_stats, free_count, total_count)``."""
    zones_stats: dict[str, dict] = {}
    free_count = 0
    total_count = len(slot_ids)

    for sid in slot_ids:
        is_occupied = sid in occupied_ids
        meta = meta_by_id.get(sid, {})
        zone = meta.get("zone", "A")

        if zone not in zones_stats:
            zones_stats[zone] = {"total": 0, "free": 0, "occupied": 0}

        zones_stats[zone]["total"] += 1
        if is_occupied:
            zones_stats[zone]["occupied"] += 1
        else:
            zones_stats[zone]["free"] += 1
            free_count += 1

    return zones_stats, free_count, total_count


# ── State reconstruction from event log ──────────────────────────────────────

def build_state_from_log(event_log_path: Path, slot_ids: list[int],
                         meta_by_id: dict[int, dict],
                         max_events: int = 200) -> dict:
    """Rebuild current slot state by replaying the event log."""
    state_by_id: dict[int, str] = {sid: "FREE" for sid in slot_ids}
    since_by_id: dict[int, str] = {sid: "" for sid in slot_ids}
    last_events: list[dict] = []

    if event_log_path.exists():
        with open(event_log_path, "r", encoding="utf-8") as f:
            for line in f:
                if not (line := line.strip()):
                    continue
                try:
                    obj = json.loads(line)
                    event_type = obj.get("event")
                    ts = obj.get("ts", "")

                    if event_type == "snapshot":
                        occupied_ids = set(obj.get("occupied_ids") or [])
                        for sid in slot_ids:
                            new_state = "OCCUPIED" if sid in occupied_ids else "FREE"
                            if state_by_id[sid] != new_state:
                                since_by_id[sid] = ts
                            state_by_id[sid] = new_state
                    elif event_type == "slot_state_changed":
                        sid = int(obj.get("slot_id"))
                        if sid in state_by_id and obj.get("new_state") in ("FREE", "OCCUPIED"):
                            state_by_id[sid] = obj["new_state"]
                            since_by_id[sid] = ts
                        last_events.append(obj)
                except Exception:
                    continue

    slots = [
        {
            "id": sid,
            "name": meta_by_id.get(sid, {}).get("name") or str(sid),
            "zone": meta_by_id.get(sid, {}).get("zone") or "A",
        }
        for sid in slot_ids
    ]
    occupied_ids = {sid for sid, st in state_by_id.items() if st == "OCCUPIED"}
    zones, free_count, total_count = calculate_zone_stats(slot_ids, occupied_ids, meta_by_id)

    return {
        "slots": slots,
        "state_by_id": state_by_id,
        "since_by_id": since_by_id,
        "zones": zones,
        "free_count": free_count,
        "total_count": total_count,
        "recent_events": last_events[-max_events:],
    }
