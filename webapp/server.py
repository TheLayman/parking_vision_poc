from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yaml
import threading
import requests
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

APP_ROOT = Path(__file__).resolve().parent
REPO_ROOT = APP_ROOT.parent

SLOT_META_PATH = REPO_ROOT / "config" / "slot_meta.yaml"
EVENT_LOG_PATH = REPO_ROOT / "data" / "occupancy_events.jsonl"
EXTERNAL_API_URL = "http://localhost:3000/slots"

app = FastAPI(title="Parking Vision Dashboard")
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")

def poll_external_api():
    """Background task to poll external API (simulated via data.txt) and log events."""
    data_file_path = REPO_ROOT / "data.txt"
    previous_states: Dict[int, str] = {}
    
    while True:
        try:
            # Simulate API call by reading local file
            if data_file_path.exists():
                with open(data_file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                
                if content:
                    try:
                        data = json.loads(content)
                        # Expected format: [{"id": 1, "unique_id": "slot_7", "status": "{\"r\":0,\"y\":0,\"b\":0}"}]
                        
                        occupied_ids = []
                        # Reload slot ids to ensure we have current config
                        current_slots = load_slot_ids()
                        
                        for item in data:
                            try:
                                # Extract slot ID
                                slot_id = item.get("id")
                                if slot_id is None:
                                    continue
                                slot_id = int(slot_id)
                                
                                # Extract status
                                status_str = item.get("status")
                                if not status_str:
                                    continue
                                
                                # Parse nested JSON
                                status = json.loads(status_str)
                                r = status.get("r", 0)
                                y = status.get("y", 0)
                                b = status.get("b", 0)
                                
                                # Logic: if r,y,b are 1 -> occupied. else -> free (defaulting to free for 0,0,0)
                                # Explicit check for occupied state:
                                if r == 1 and y == 1 and b == 1:
                                    occupied_ids.append(slot_id)
                            except Exception as e:
                                print(f"Error parsing item: {e}")
                                continue
                        
                        meta_by_id = load_slot_meta_by_id()
                        all_configured_ids = load_slot_ids()
                        
                        # Calculate current states
                        current_states = {}
                        for sid in all_configured_ids:
                            current_states[sid] = "OCCUPIED" if sid in occupied_ids else "FREE"

                        # Detect changes
                        events_to_log = []
                        timestamp = datetime.now(timezone.utc).isoformat()
                        
                        for sid, state in current_states.items():
                            prev_state = previous_states.get(sid)
                            if prev_state and prev_state != state:
                                # State changed
                                meta = meta_by_id.get(sid, {})
                                change_event = {
                                    "event": "slot_state_changed",
                                    "ts": timestamp,
                                    "slot_id": sid,
                                    "slot_name": meta.get("name", str(sid)),
                                    "zone": meta.get("zone", "A"),
                                    "prev_state": prev_state,
                                    "new_state": state
                                }
                                events_to_log.append(change_event)
                        
                        # Update previous states
                        previous_states = current_states.copy()

                        # Calculate metrics
                        zones_stats = {}
                        total_count = len(all_configured_ids)
                        free_count = 0
                        
                        for sid in all_configured_ids:
                            is_occupied = sid in occupied_ids
                            # Get zone
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

                        snapshot_event = {
                            "event": "snapshot",
                            "ts": timestamp,
                            "occupied_ids": occupied_ids,
                            "zone_stats": zones_stats,
                            "total_count": total_count,
                            "free_count": free_count
                        }
                        events_to_log.append(snapshot_event)
                        
                        # Append to log
                        with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
                            for evt in events_to_log:
                                f.write(json.dumps(evt) + "\n")
                            
                    except json.JSONDecodeError:
                        print(f"Error decoding JSON from {data_file_path}")
                else:
                    print(f"File {data_file_path} is empty")
            else:
                print(f"File {data_file_path} not found")
                
        except Exception as e:
            print(f"Error polling data source: {e}")
            
        time.sleep(10)


@app.on_event("startup")
def start_polling():
    thread = threading.Thread(target=poll_external_api, daemon=True)
    thread.start()


def _load_yaml(path: Path):
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_slot_ids() -> List[int]:
    meta = load_slot_meta_by_id()
    return sorted(meta.keys())


def load_slot_meta_by_id() -> Dict[int, dict]:
    data = _load_yaml(SLOT_META_PATH)
    if not data:
        return {}

    meta: Dict[int, dict] = {}

    if isinstance(data, dict):
        for k, v in data.items():
            try:
                slot_id = int(k)
            except Exception:
                continue
            if isinstance(v, dict):
                meta[slot_id] = v
        return meta

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict) or "id" not in item:
                continue
            try:
                slot_id = int(item["id"])
            except Exception:
                continue
            meta[slot_id] = item

    return meta


def describe_slot(slot_id: int, meta_by_id: Dict[int, dict]) -> dict:
    meta = meta_by_id.get(slot_id, {})
    name = meta.get("name") or str(slot_id)
    zone = meta.get("zone") or "A"
    return {"id": slot_id, "name": name, "zone": zone}


def build_state_from_log(
    slot_ids: List[int],
    meta_by_id: Dict[int, dict],
    max_events: int = 200,
) -> dict:
    state_by_id: Dict[int, str] = {slot_id: "FREE" for slot_id in slot_ids}
    since_by_id: Dict[int, str] = {slot_id: "" for slot_id in slot_ids}
    last_events: List[dict] = []

    if EVENT_LOG_PATH.exists():
        with open(EVENT_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                event_type = obj.get("event")
                ts = obj.get("ts", "")
                
                if event_type == "snapshot":
                    occupied_ids = set(obj.get("occupied_ids") or [])
                    for slot_id in slot_ids:
                        new_state = "OCCUPIED" if slot_id in occupied_ids else "FREE"
                        if state_by_id[slot_id] != new_state:
                             # If state changed abruptly via snapshot (e.g. restart), update since time
                             # Note: Ideally snapshot shouldn't be the primary source of truth for change times 
                             # if we rely on state_changed events, but for robustness we update.
                             # However, based on user request "update for every state change", 
                             # strict adherence to change events is better.
                             # Let's trust the snapshot for state but maybe not update 'since' unless we have to?
                             # Actually, if we miss an event, snapshot corrects the state.
                             # We should update 'since' if the state flips.
                             since_by_id[slot_id] = ts
                        state_by_id[slot_id] = new_state
                elif event_type == "slot_state_changed":
                    try:
                        slot_id = int(obj.get("slot_id"))
                    except Exception:
                        continue
                    if slot_id in state_by_id and obj.get("new_state") in ("FREE", "OCCUPIED"):
                        state_by_id[slot_id] = obj["new_state"]
                        since_by_id[slot_id] = ts

                if event_type in ("slot_state_changed",):
                    last_events.append(obj)

    if len(last_events) > max_events:
        last_events = last_events[-max_events:]

    slots = [describe_slot(slot_id, meta_by_id) for slot_id in slot_ids]

    zones: Dict[str, dict] = {}
    for slot in slots:
        zone = slot["zone"]
        if zone not in zones:
            zones[zone] = {"total": 0, "free": 0, "occupied": 0}
        zones[zone]["total"] += 1
        if state_by_id[slot["id"]] == "OCCUPIED":
            zones[zone]["occupied"] += 1
        else:
            zones[zone]["free"] += 1

    free_count = sum(1 for s in state_by_id.values() if s == "FREE")

    return {
        "slots": slots,
        "state_by_id": state_by_id,
        "since_by_id": since_by_id,
        "zones": zones,
        "free_count": free_count,
        "total_count": len(slot_ids),
        "recent_events": last_events,
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(str(APP_ROOT / "static" / "index.html"))


@app.get("/state")
def state():
    slot_ids = load_slot_ids()
    meta_by_id = load_slot_meta_by_id()
    return build_state_from_log(slot_ids=slot_ids, meta_by_id=meta_by_id)


@app.get("/events")
def events():
    def gen():
        EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVENT_LOG_PATH.touch(exist_ok=True)

        with open(EVENT_LOG_PATH, "r", encoding="utf-8") as f:
            f.seek(0, 2)  # tail from end
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.25)
                    continue
                line = line.strip()
                if not line:
                    continue
                yield f"data: {line}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/analytics/summary")
def analytics_summary(range: str = Query(default="24h")):
    """
    Returns analytics data for the dashboard:
    - occupancy_series: time-series of zone occupancy percentages
    - dwell_stats: average dwell time per zone (in minutes)
    - predictions: simple moving average prediction for next period
    - summary: overall stats
    """
    meta_by_id = load_slot_meta_by_id()
    
    # Determine time filter
    now = datetime.now(timezone.utc)
    time_deltas = {
        "1h": timedelta(hours=1),
        "6h": timedelta(hours=6),
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7),
        "all": None
    }
    delta = time_deltas.get(range)
    cutoff = (now - delta) if delta else None
    
    # Parse events from JSONL
    snapshots = []
    state_changes = []
    
    if EVENT_LOG_PATH.exists():
        with open(EVENT_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                
                # Parse timestamp
                ts_str = obj.get("ts")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except Exception:
                    continue
                
                # Filter by time range
                if cutoff and ts < cutoff:
                    continue
                
                event_type = obj.get("event")
                if event_type == "snapshot":
                    snapshots.append({
                        "ts": ts,
                        "zone_stats": obj.get("zones", obj.get("zone_stats", {})),
                        "occupied_ids": obj.get("occupied_ids", []),
                        "free_count": obj.get("free_count", 0),
                        "total_count": obj.get("total_count", 0)
                    })
                elif event_type == "slot_state_changed":
                    state_changes.append({
                        "ts": ts,
                        "slot_id": obj.get("slot_id"),
                        "slot_name": obj.get("slot_name"),
                        "zone": obj.get("zone", "A"),
                        "prev_state": obj.get("prev_state"),
                        "new_state": obj.get("new_state")
                    })
    
    # Build occupancy time series from snapshots
    occupancy_series = []
    for snap in snapshots:
        zone_data = {}
        zone_stats = snap.get("zone_stats", {})
        for zone, stats in zone_stats.items():
            total = stats.get("total", 0)
            occupied = stats.get("occupied", 0)
            pct = (occupied / total * 100) if total > 0 else 0
            zone_data[zone] = round(pct, 1)
        
        occupancy_series.append({
            "time": snap["ts"].isoformat(),
            "zones": zone_data
        })
    
    # Calculate dwell times from state change pairs
    # Track when each slot became occupied
    slot_occupied_at: Dict[int, datetime] = {}
    dwell_times_by_zone: Dict[str, List[float]] = {}
    
    for change in sorted(state_changes, key=lambda x: x["ts"]):
        slot_id = change["slot_id"]
        zone = change["zone"]
        
        if change["new_state"] == "OCCUPIED":
            slot_occupied_at[slot_id] = change["ts"]
        elif change["new_state"] == "FREE" and slot_id in slot_occupied_at:
            occupied_ts = slot_occupied_at.pop(slot_id)
            dwell_minutes = (change["ts"] - occupied_ts).total_seconds() / 60
            if dwell_minutes > 0 and dwell_minutes < 1440:  # Cap at 24 hours
                if zone not in dwell_times_by_zone:
                    dwell_times_by_zone[zone] = []
                dwell_times_by_zone[zone].append(dwell_minutes)
    
    # Calculate average dwell time per zone
    dwell_stats = {}
    for zone, times in dwell_times_by_zone.items():
        if times:
            dwell_stats[zone] = round(sum(times) / len(times), 1)
    
    # Simple Moving Average prediction (last 5 data points)
    predictions = {}
    if len(occupancy_series) >= 2:
        # Get all zones from the data
        all_zones = set()
        for entry in occupancy_series:
            all_zones.update(entry["zones"].keys())
        
        for zone in all_zones:
            recent_values = []
            for entry in occupancy_series[-5:]:
                if zone in entry["zones"]:
                    recent_values.append(entry["zones"][zone])
            
            if recent_values:
                # Simple moving average
                avg = sum(recent_values) / len(recent_values)
                # Add trend adjustment (difference between last two)
                if len(recent_values) >= 2:
                    trend = recent_values[-1] - recent_values[-2]
                    predicted = avg + (trend * 0.5)  # Damped trend
                else:
                    predicted = avg
                predictions[zone] = round(max(0, min(100, predicted)), 1)
    
    # Summary statistics
    total_state_changes = len(state_changes)
    total_snapshots = len(snapshots)
    
    # Current occupancy from latest snapshot
    current_occupancy = {}
    if snapshots:
        latest = snapshots[-1]
        zone_stats = latest.get("zone_stats", {})
        for zone, stats in zone_stats.items():
            total = stats.get("total", 0)
            occupied = stats.get("occupied", 0)
            current_occupancy[zone] = {
                "occupied": occupied,
                "total": total,
                "percent": round((occupied / total * 100) if total > 0 else 0, 1)
            }
    
    return {
        "occupancy_series": occupancy_series,
        "dwell_stats": dwell_stats,
        "predictions": predictions,
        "current_occupancy": current_occupancy,
        "summary": {
            "total_events": total_state_changes,
            "total_snapshots": total_snapshots,
            "time_range": range,
            "data_points": len(occupancy_series)
        }
    }


