"""Occupancy analytics: time-series, dwell times, incidents, and challan stats."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ── Event parsing (Postgres-backed, replaces JSONL) ──────────────────────────

def parse_events_from_log(event_log_path: Path, cutoff: datetime | None) -> dict:
    """Return categorised event lists by querying PostgreSQL.

    The *event_log_path* parameter is ignored in production — all events are
    stored in Postgres (occupancy_events and challan_events tables).

    Returns the same shape as the legacy JSONL parser so all callers above
    (build_occupancy_series, calculate_dwell_times, etc.) are unchanged:
        {"snapshots": [], "state_changes": [...], "challans": [...]}
    """
    try:
        from db.client import query_occupancy_events, query_challan_events
    except ImportError:
        return {"snapshots": [], "state_changes": [], "challans": []}

    state_changes: list[dict] = []
    try:
        occ_rows = query_occupancy_events(cutoff=cutoff)
        for row in occ_rows:
            if row["event_type"] not in ("OCCUPIED", "FREE"):
                continue
            payload = row.get("payload") or {}
            ts = row["ts"]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            state_changes.append({
                "ts": ts,
                "slot_id": row["slot_id"],
                "slot_name": payload.get("slot_name", str(row["slot_id"])),
                "zone": payload.get("zone", "A"),
                "prev_state": "FREE" if row["event_type"] == "OCCUPIED" else "OCCUPIED",
                "new_state": row["event_type"],
            })
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("query_occupancy_events failed: %s", e)

    challans: list[dict] = []
    try:
        ch_rows = query_challan_events(cutoff=cutoff)
        for row in ch_rows:
            meta = row.get("metadata") or {}
            ts = row["ts"]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            challans.append({
                "ts": ts,
                "plate_text": row.get("license_plate") or "",
                "slot_id": row["slot_id"],
                "slot_name": meta.get("slot_name", str(row["slot_id"])),
                "zone": meta.get("zone", "A"),
                "challan": row.get("status") == "confirmed",
            })
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("query_challan_events failed: %s", e)

    return {"snapshots": [], "state_changes": state_changes, "challans": challans}


# ── Analytics functions (unchanged) ─────────────────────────────────────────

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
    slot_occupied_at: dict[int, tuple[datetime, str]] = {}
    all_dwells: list[dict] = []

    for change in sorted(state_changes, key=lambda x: x["ts"]):
        slot_id = change["slot_id"]
        zone = change["zone"]

        if change["new_state"] == "OCCUPIED":
            slot_occupied_at[slot_id] = (change["ts"], zone)
        elif change["new_state"] == "FREE" and slot_id in slot_occupied_at:
            occupied_ts, occ_zone = slot_occupied_at.pop(slot_id)
            dwell_minutes = (change["ts"] - occupied_ts).total_seconds() / 60
            if 0 < dwell_minutes < 1440:
                all_dwells.append({"zone": occ_zone, "minutes": round(dwell_minutes, 2)})

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
    """Count vehicles parked longer than 15, 30, 45, and 60 minutes."""
    filtered = all_dwells if zone is None else [d for d in all_dwells if d["zone"] == zone]
    gt_15 = gt_30 = gt_45 = gt_60 = 0
    for d in filtered:
        m = d["minutes"]
        if m > 15: gt_15 += 1
        if m > 30: gt_30 += 1
        if m > 45: gt_45 += 1
        if m > 60: gt_60 += 1
    return {"gt_15m": gt_15, "gt_30m": gt_30, "gt_45m": gt_45, "gt_1h": gt_60}


def build_hourly_incidents(state_changes: list,
                           start: datetime | None = None,
                           end: datetime | None = None) -> list[dict]:
    """Group FREE→OCCUPIED transitions by hour for charting."""
    hourly: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for change in state_changes:
        if change.get("prev_state") == "FREE" and change.get("new_state") == "OCCUPIED":
            ts: datetime = change["ts"]
            hour_key = ts.replace(minute=0, second=0, microsecond=0).isoformat()
            zone = change.get("zone", "A")
            hourly[hour_key][zone] += 1

    if start and end and start <= end:
        current = start.replace(minute=0, second=0, microsecond=0)
        end_hour = end.replace(minute=0, second=0, microsecond=0)
        while current <= end_hour:
            hour_key = current.isoformat()
            _ = hourly[hour_key]
            current += timedelta(hours=1)

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
    """Summarise challan events."""
    filtered = challans if zone is None else [c for c in challans if c.get("zone") == zone]
    confirmed = sum(1 for c in filtered if c.get("challan"))
    cleared = sum(1 for c in filtered if not c.get("challan"))

    by_zone: dict[str, dict[str, int]] = defaultdict(lambda: {"confirmed": 0, "cleared": 0})
    for c in challans:
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
