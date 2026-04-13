"""Slot metadata loading, zone statistics, and device-name mapping."""

from __future__ import annotations

import logging
import os
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

    try:
        current_mtime = meta_path.stat().st_mtime
        if _meta_cache is not None and _meta_cache_mtime == current_mtime:
            return _meta_cache
    except Exception:
        pass

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

    _meta_cache = meta
    try:
        _meta_cache_mtime = meta_path.stat().st_mtime
    except Exception:
        pass
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


# ── State from Redis (replaces JSONL replay) ─────────────────────────────────

def build_state_from_log(slot_ids: list[int],
                         meta_by_id: dict[int, dict],
                         redis_client=None) -> dict:
    """Read current slot state from Redis Hash parking:slot:state.

    Pass *redis_client* to reuse an existing connection (avoids per-call churn).
    """
    try:
        if redis_client is not None:
            r = redis_client
        else:
            import redis as _redis
            r = _redis.Redis.from_url(
                os.environ.get("REDIS_URL", "redis://localhost:6379"),
                decode_responses=False, socket_timeout=2,
            )
        state_raw = r.hgetall("parking:slot:state")   # {b"1": b"OCCUPIED", ...}
        since_raw = r.hgetall("parking:slot:since")   # {b"1": b"ts_str", ...}
        if redis_client is None:
            r.close()
    except Exception as e:
        log.error("Redis read failed in build_state_from_log: %s — returning all FREE", e)
        state_raw = {}
        since_raw = {}

    state_by_id: dict[int, str] = {}
    since_by_id: dict[int, str] = {}
    for sid in slot_ids:
        raw_state = state_raw.get(str(sid).encode(), b"FREE")
        state_by_id[sid] = raw_state.decode() if isinstance(raw_state, bytes) else str(raw_state)
        raw_since = since_raw.get(str(sid).encode(), b"")
        since_by_id[sid] = raw_since.decode() if isinstance(raw_since, bytes) else str(raw_since)

    occupied_ids = {sid for sid, st in state_by_id.items() if st == "OCCUPIED"}
    zones, free_count, total_count = calculate_zone_stats(slot_ids, occupied_ids, meta_by_id)

    slots = []
    for sid in slot_ids:
        m = meta_by_id.get(sid, {})
        slot = {
            "id": sid,
            "name": m.get("name") or str(sid),
            "zone": m.get("zone") or "A",
        }
        if m.get("lat") is not None:
            slot["lat"] = m["lat"]
        if m.get("lng") is not None:
            slot["lng"] = m["lng"]
        slots.append(slot)

    return {
        "slots": slots,
        "state_by_id": state_by_id,
        "since_by_id": since_by_id,
        "zones": zones,
        "free_count": free_count,
        "total_count": total_count,
        "recent_events": [],
    }
