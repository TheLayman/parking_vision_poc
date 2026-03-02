"""Occupancy analytics: time-series, dwell times, and predictions."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def parse_events_from_log(event_log_path: Path,
                          cutoff: datetime | None) -> tuple[list, list]:
    """Parse snapshots and state changes from the event log.

    Returns ``(snapshots, state_changes)`` filtered to events after *cutoff*.
    """
    snapshots: list[dict] = []
    state_changes: list[dict] = []

    if not event_log_path.exists():
        return snapshots, state_changes

    with open(event_log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                ts_str = obj.get("ts")
                if not ts_str:
                    continue
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))

                if cutoff and ts < cutoff:
                    continue

                event_type = obj.get("event")
                if event_type == "snapshot":
                    snapshots.append({
                        "ts": ts,
                        "zone_stats": obj.get("zones", obj.get("zone_stats", {})),
                        "occupied_ids": obj.get("occupied_ids", []),
                        "free_count": obj.get("free_count", 0),
                        "total_count": obj.get("total_count", 0),
                    })
                elif event_type == "slot_state_changed":
                    state_changes.append({
                        "ts": ts,
                        "slot_id": obj.get("slot_id"),
                        "slot_name": obj.get("slot_name"),
                        "zone": obj.get("zone", "A"),
                        "prev_state": obj.get("prev_state"),
                        "new_state": obj.get("new_state"),
                    })
            except Exception:
                continue

    return snapshots, state_changes


def build_occupancy_series(snapshots: list) -> list:
    """Convert snapshots to a time-series of zone occupancy percentages."""
    series: list[dict] = []
    for snap in snapshots:
        zone_data: dict[str, float] = {}
        for zone, stats in snap.get("zone_stats", {}).items():
            total = stats.get("total", 0)
            occupied = stats.get("occupied", 0)
            pct = (occupied / total * 100) if total > 0 else 0
            zone_data[zone] = round(pct, 1)
        series.append({"time": snap["ts"].isoformat(), "zones": zone_data})
    return series


def calculate_dwell_times(state_changes: list) -> dict:
    """Calculate average dwell time per zone from state change events."""
    slot_occupied_at: dict[int, datetime] = {}
    dwell_times_by_zone: dict[str, list[float]] = {}

    for change in sorted(state_changes, key=lambda x: x["ts"]):
        slot_id = change["slot_id"]
        zone = change["zone"]

        if change["new_state"] == "OCCUPIED":
            slot_occupied_at[slot_id] = change["ts"]
        elif change["new_state"] == "FREE" and slot_id in slot_occupied_at:
            occupied_ts = slot_occupied_at.pop(slot_id)
            dwell_minutes = (change["ts"] - occupied_ts).total_seconds() / 60
            if 0 < dwell_minutes < 1440:  # Cap at 24 hours
                dwell_times_by_zone.setdefault(zone, []).append(dwell_minutes)

    return {
        zone: round(sum(times) / len(times), 1)
        for zone, times in dwell_times_by_zone.items()
        if times
    }


def predict_occupancy(occupancy_series: list) -> dict:
    """Simple moving average prediction with trend adjustment."""
    if len(occupancy_series) < 2:
        return {}

    all_zones: set[str] = set()
    for entry in occupancy_series:
        all_zones.update(entry["zones"].keys())

    predictions: dict[str, float] = {}
    for zone in all_zones:
        recent = [e["zones"][zone] for e in occupancy_series[-5:] if zone in e["zones"]]
        if recent:
            avg = sum(recent) / len(recent)
            if len(recent) >= 2:
                trend = recent[-1] - recent[-2]
                predicted = avg + (trend * 0.5)
            else:
                predicted = avg
            predictions[zone] = round(max(0, min(100, predicted)), 1)

    return predictions
