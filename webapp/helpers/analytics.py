"""Occupancy analytics: time-series, dwell times, and incidents."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ── Event parsing (Postgres-backed, replaces JSONL) ──────────────────────────

def parse_events_from_log(event_log_path: Path, cutoff: datetime | None) -> dict:
    """Return occupancy state changes by querying PostgreSQL.

    The *event_log_path* parameter is ignored — all events are stored in Postgres.
    Returns ``{"snapshots": [], "state_changes": [...]}``.
    """
    try:
        from db.client import query_occupancy_events
    except ImportError:
        return {"snapshots": [], "state_changes": []}

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

    return {"snapshots": [], "state_changes": state_changes}


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
    """Count vehicles in dwell-time buckets."""
    filtered = all_dwells if zone is None else [d for d in all_dwells if d["zone"] == zone]
    buckets = {"0_5": 0, "5_15": 0, "15_30": 0, "30_60": 0, "60_120": 0, "120_plus": 0}
    gt_15 = 0
    for d in filtered:
        m = d["minutes"]
        if m <= 5:
            buckets["0_5"] += 1
        elif m <= 15:
            buckets["5_15"] += 1
        elif m <= 30:
            buckets["15_30"] += 1
            gt_15 += 1
        elif m <= 60:
            buckets["30_60"] += 1
            gt_15 += 1
        elif m <= 120:
            buckets["60_120"] += 1
            gt_15 += 1
        else:
            buckets["120_plus"] += 1
            gt_15 += 1
    return {"buckets": buckets, "gt_15m": gt_15, "total": len(filtered)}


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


def build_turnover_rates(state_changes: list, total_slots_by_zone: dict[str, int] | None = None) -> dict:
    """Calculate turnover rate per zone.

    Turnover = total OCCUPIED events / total slots in zone.
    Returns ``{"all": 3.2, "by_zone": {"A": 4.1, "B": 2.8}}``.
    """
    occ_by_zone: dict[str, int] = defaultdict(int)
    for sc in state_changes:
        if sc.get("new_state") == "OCCUPIED":
            occ_by_zone[sc["zone"]] += 1

    total_occ = sum(occ_by_zone.values())
    total_slots = sum((total_slots_by_zone or {}).values()) or 1

    by_zone = {}
    for zone, count in occ_by_zone.items():
        zone_slots = (total_slots_by_zone or {}).get(zone, 1) or 1
        by_zone[zone] = round(count / zone_slots, 1)

    return {
        "all": round(total_occ / total_slots, 1),
        "by_zone": by_zone,
    }


def build_peak_occupancy(state_changes: list, total_slots: int) -> dict:
    """Find peak simultaneous occupancy from state change events.

    Replays state changes chronologically, tracking how many slots are
    occupied at each moment.
    Returns ``{"peak_pct": 87.3, "peak_count": 2619, "peak_hour": "2026-04-12T14:00:00"}``.
    """
    if not state_changes or total_slots == 0:
        return {"peak_pct": 0, "peak_count": 0, "peak_hour": None}

    current_occupied = 0
    peak_count = 0
    peak_ts = None

    for sc in sorted(state_changes, key=lambda x: x["ts"]):
        if sc["new_state"] == "OCCUPIED":
            current_occupied += 1
        elif sc["new_state"] == "FREE":
            current_occupied = max(0, current_occupied - 1)
        if current_occupied > peak_count:
            peak_count = current_occupied
            peak_ts = sc["ts"]

    peak_pct = round(peak_count / total_slots * 100, 1) if total_slots else 0
    peak_hour = None
    if peak_ts:
        peak_hour = peak_ts.replace(minute=0, second=0, microsecond=0).isoformat()

    return {"peak_pct": peak_pct, "peak_count": peak_count, "peak_hour": peak_hour}


def build_occupancy_heatmap(state_changes: list) -> list[dict]:
    """Build day-of-week x hour-of-day occupancy heatmap data.

    Returns a list of ``{"day": 0-6 (Mon-Sun), "hour": 0-23, "value": count}``
    representing how many FREE->OCCUPIED transitions happen at each day/hour.
    """
    matrix: dict[tuple[int, int], int] = defaultdict(int)

    for sc in state_changes:
        if sc.get("new_state") == "OCCUPIED":
            ts = sc["ts"]
            day = ts.weekday()  # 0=Mon, 6=Sun
            hour = ts.hour
            matrix[(day, hour)] += 1

    result = []
    for day in range(7):
        for hour in range(24):
            result.append({"day": day, "hour": hour, "value": matrix.get((day, hour), 0)})
    return result


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
