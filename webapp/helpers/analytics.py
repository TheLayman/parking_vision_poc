"""Occupancy analytics: time-series, dwell times, incidents, and challan stats."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def parse_events_from_log(event_log_path: Path,
                          cutoff: datetime | None) -> dict:
    """Parse event log into categorised lists.

    Returns a dict with keys ``"snapshots"``, ``"state_changes"``,
    ``"challans"`` — each a list of dicts filtered to events after *cutoff*.
    """
    snapshots: list[dict] = []
    state_changes: list[dict] = []
    challans: list[dict] = []

    if not event_log_path.exists():
        return {"snapshots": snapshots, "state_changes": state_changes, "challans": challans}

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
                elif event_type == "challan_completed":
                    challans.append({
                        "ts": ts,
                        "plate_text": obj.get("plate_text"),
                        "slot_id": obj.get("slot_id"),
                        "slot_name": obj.get("slot_name"),
                        "zone": obj.get("zone", "A"),
                        "challan": obj.get("challan", False),
                    })
            except Exception:
                continue

    return {"snapshots": snapshots, "state_changes": state_changes, "challans": challans}


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
    """Calculate average dwell time per zone from state change events.

    Returns ``{"avg": {"B": 12.3}, "all_dwells": [{"zone": "B", "minutes": 5.2}, ...]}``.
    """
    slot_occupied_at: dict[int, tuple[datetime, str]] = {}  # slot_id -> (ts, zone)
    all_dwells: list[dict] = []

    for change in sorted(state_changes, key=lambda x: x["ts"]):
        slot_id = change["slot_id"]
        zone = change["zone"]

        if change["new_state"] == "OCCUPIED":
            slot_occupied_at[slot_id] = (change["ts"], zone)
        elif change["new_state"] == "FREE" and slot_id in slot_occupied_at:
            occupied_ts, occ_zone = slot_occupied_at.pop(slot_id)
            dwell_minutes = (change["ts"] - occupied_ts).total_seconds() / 60
            if 0 < dwell_minutes < 1440:  # Cap at 24 hours
                all_dwells.append({"zone": occ_zone, "minutes": round(dwell_minutes, 2)})

    # Compute averages per zone
    by_zone: dict[str, list[float]] = defaultdict(list)
    for d in all_dwells:
        by_zone[d["zone"]].append(d["minutes"])

    avg = {
        zone: round(sum(times) / len(times), 1)
        for zone, times in by_zone.items()
        if times
    }

    return {"avg": avg, "all_dwells": all_dwells}


def build_dwell_distribution(all_dwells: list[dict], zone: str | None = None) -> dict:
    """Count vehicles parked longer than 15, 30, 45, and 60 minutes.

    Args:
        all_dwells: list of ``{"zone": str, "minutes": float}`` dicts.
        zone: optional zone filter; ``None`` means all zones.

    Returns ``{"gt_15m": int, "gt_30m": int, "gt_45m": int, "gt_1h": int}``.
    Buckets are cumulative (a 50-min stay counts in gt_15m, gt_30m, gt_45m).
    """
    filtered = all_dwells if zone is None else [d for d in all_dwells if d["zone"] == zone]
    return {
        "gt_15m": sum(1 for d in filtered if d["minutes"] > 15),
        "gt_30m": sum(1 for d in filtered if d["minutes"] > 30),
        "gt_45m": sum(1 for d in filtered if d["minutes"] > 45),
        "gt_1h":  sum(1 for d in filtered if d["minutes"] > 60),
    }


def build_hourly_incidents(state_changes: list) -> list[dict]:
    """Group FREE→OCCUPIED transitions by hour for charting.

    Returns a sorted list of ``{"hour": "2026-03-02T11:00", "all": int, "zones": {"B": int}}``.
    """
    hourly: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for change in state_changes:
        if change.get("prev_state") == "FREE" and change.get("new_state") == "OCCUPIED":
            ts: datetime = change["ts"]
            hour_key = ts.strftime("%Y-%m-%dT%H:00")
            zone = change.get("zone", "A")
            hourly[hour_key][zone] += 1

    result = []
    for hour_key in sorted(hourly.keys()):
        zones = dict(hourly[hour_key])
        result.append({
            "hour": hour_key,
            "all": sum(zones.values()),
            "zones": zones,
        })
    return result


def build_challan_summary(challans: list, zone: str | None = None) -> dict:
    """Summarise challan events.

    Args:
        challans: list of parsed challan_completed events.
        zone: optional zone filter.

    Returns ``{"total": int, "confirmed": int, "cleared": int,
               "by_zone": {"B": {"confirmed": int, "cleared": int}}}``.
    """
    filtered = challans if zone is None else [c for c in challans if c.get("zone") == zone]
    confirmed = sum(1 for c in filtered if c.get("challan"))
    cleared = sum(1 for c in filtered if not c.get("challan"))

    by_zone: dict[str, dict[str, int]] = defaultdict(lambda: {"confirmed": 0, "cleared": 0})
    for c in challans:  # always build full zone breakdown
        z = c.get("zone", "A")
        if c.get("challan"):
            by_zone[z]["confirmed"] += 1
        else:
            by_zone[z]["cleared"] += 1

    return {
        "total": len(filtered),
        "confirmed": confirmed,
        "cleared": cleared,
        "by_zone": dict(by_zone),
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
