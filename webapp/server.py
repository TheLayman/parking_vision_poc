from __future__ import annotations
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List
import yaml
import threading
import requests
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import math

APP_ROOT = Path(__file__).resolve().parent
REPO_ROOT = APP_ROOT.parent

SLOT_META_PATH = REPO_ROOT / "config" / "slot_meta.yaml"
EVENT_LOG_PATH = REPO_ROOT / "data" / "occupancy_events.jsonl"
SNAPSHOT_PATH = REPO_ROOT / "data" / "snapshot.yaml"

# Occupancy detection settings
DISTANCE_THRESHOLD = 7.5  # Magnetic field distance threshold
CONSECUTIVE_COUNT_REQUIRED = 3  # Number of consecutive readings to confirm state

app = FastAPI(title="Parking Vision Dashboard")
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")

_shutdown_event = threading.Event()

# Snapshot data lock for thread-safe access
_snapshot_lock = threading.Lock()

def load_snapshot_data() -> dict:
    """Load snapshot data from YAML file."""
    if not SNAPSHOT_PATH.exists():
        return {"slots": {}}
    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data else {"slots": {}}

def save_snapshot_data(data: dict):
    """Save snapshot data to YAML file."""
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False)

def calculate_distance(x, y, z, bx, by, bz) -> float:
    """Calculate Euclidean distance between current and baseline magnetic field vectors."""
    return math.sqrt((x - bx) ** 2 + (y - by) ** 2 + (z - bz) ** 2)

# ... (API constants)
API_URL = "http://localhost:8000/slots"
API_TOKEN = ""
ENABLE_POLLING = 1

def poll_external_api(): 
    """Background task to poll external API and log events."""
    previous_states: Dict[int, str] = {} 
    
    print(f"Starting poller. Target: {API_URL}")
    if not API_TOKEN:
        print("WARNING: No PARKING_API_TOKEN set. Requests might fail if auth is required.")

    while not _shutdown_event.is_set():
        try:
            headers = {}
            if API_TOKEN:
                headers["Authorization"] = f"Bearer {API_TOKEN}"

            response = requests.get(API_URL, headers=headers, timeout=5)
            
            if response.status_code == 200:
                try:
                    data = response.json()
                    occupied_ids = []
                    
                    with _snapshot_lock:
                        snapshot_data = load_snapshot_data()
                        slots_snapshot = snapshot_data.get("slots", {})
                    
                    for item in data:
                        try:
                            # Extract slot ID
                            slot_id = item.get("id")
                            if slot_id is None:
                                continue
                            slot_id = int(slot_id)
                            
                            # Extract status with magnetic field data
                            status_str = item.get("status")
                            if not status_str:
                                continue
                            
                            # Parse nested JSON
                            if isinstance(status_str, str):
                                status = json.loads(status_str)
                            else:
                                status = status_str
                            
                            # Get r,y,b magnetic field values
                            r = float(status.get("r", 0))
                            y = float(status.get("y", 0))
                            b = float(status.get("b", 0))
                            
                            # Get slot's baseline from snapshot
                            slot_key = str(slot_id)
                            slot_snapshot = slots_snapshot.get(slot_key, {})
                            
                            baseline_x = slot_snapshot.get("baseline_x")
                            baseline_y = slot_snapshot.get("baseline_y")
                            baseline_z = slot_snapshot.get("baseline_z")
                            consecutive_occupied = slot_snapshot.get("consecutive_occupied", 0)
                            
                            # Check if timestamp is same as last - skip processing but preserve state
                            timestamp_ms = item.get("timestamp", 0)
                            last_timestamp = slot_snapshot.get("last_timestamp", 0)
                            if timestamp_ms == last_timestamp:
                                # No new data - preserve current occupied state if threshold was met
                                if consecutive_occupied >= CONSECUTIVE_COUNT_REQUIRED:
                                    occupied_ids.append(slot_id)
                                continue
                            
                            slot_snapshot["last_timestamp"] = timestamp_ms
                            slot_snapshot["last_x"] = r
                            slot_snapshot["last_y"] = y
                            slot_snapshot["last_z"] = b
                            
                            # Check if baseline is calibrated
                            if baseline_x is not None and baseline_y is not None and baseline_z is not None:
                                distance = calculate_distance(r, y, b, baseline_x, baseline_y, baseline_z)
                                
                                if distance > DISTANCE_THRESHOLD:
                                    # Distance exceeds threshold, increment consecutive count
                                    consecutive_occupied = min(consecutive_occupied + 1, CONSECUTIVE_COUNT_REQUIRED)
                                elif distance <= DISTANCE_THRESHOLD*0.9:
                                    # Distance within threshold, reset consecutive count
                                    consecutive_occupied = 0
                                
                                slot_snapshot["consecutive_occupied"] = consecutive_occupied
                                slot_snapshot["last_distance"] = round(distance, 2)
                                
                                # If the slot is confirmed FREE (based on threshold), slowly update the baseline
                                if consecutive_occupied == 0:
                                    # Learning rate (Alpha) - very slow update
                                    ALPHA = 0.01
                                    
                                    # Update snapshots in memory
                                    slot_snapshot["baseline_x"] = round(baseline_x * (1 - ALPHA) + r * ALPHA, 2)
                                    slot_snapshot["baseline_y"] = round(baseline_y * (1 - ALPHA) + y * ALPHA, 2)
                                    slot_snapshot["baseline_z"] = round(baseline_z * (1 - ALPHA) + b * ALPHA, 2)
                                
                                # Occupied if consecutive count reaches threshold
                                if consecutive_occupied >= CONSECUTIVE_COUNT_REQUIRED:
                                    occupied_ids.append(slot_id)
                            else:
                                # No baseline set, treat as free (uncalibrated)
                                slot_snapshot["consecutive_occupied"] = 0
                            
                            slots_snapshot[slot_key] = slot_snapshot
                            
                        except Exception as e:
                            print(f"Error parsing item: {e}")
                            continue
                    
                    # Save updated snapshot data
                    with _snapshot_lock:
                        save_snapshot_data({"slots": slots_snapshot})
                    
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
                    print(f"Error decoding JSON response from {API_URL}")
            else:
                print(f"External API returned status {response.status_code}")
                
        except requests.exceptions.RequestException as e:
            print(f"Error connecting to external API: {e}")
        except Exception as e:
             print(f"Error in polling loop: {e}")
        
        # Use wait() instead of sleep() so we can be interrupted
        _shutdown_event.wait(timeout=5)
    
    print("Poller thread shutting down gracefully")

@app.on_event("startup")
def start_polling():
    # Clear shutdown event in case it was set from previous run
    _shutdown_event.clear()
    
    if ENABLE_POLLING:
        print(f"Polling enabled. Starting background thread...")
        thread = threading.Thread(target=poll_external_api, daemon=True)
        thread.start()
    else:
        print("Polling disabled. Set ENABLE_POLLING=true to enable.")


@app.on_event("shutdown")
def stop_polling():
    print("Shutting down poller...")
    _shutdown_event.set()

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


@app.get("/slots")
def slots():
    """
    Returns slot data from data.txt file.
    Format matches the external API response.
    """
    data_txt_path = REPO_ROOT / "data.txt"
    
    if not data_txt_path.exists():
        return []
    
    try:
        with open(data_txt_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return []
            data = json.loads(content)
            return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []
    except Exception:
        return []


@app.get("/events")
async def events(request: Request):
    async def gen():
        EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVENT_LOG_PATH.touch(exist_ok=True)

        try:
            with open(EVENT_LOG_PATH, "r", encoding="utf-8") as f:
                f.seek(0, 2)  # tail from end
                while not _shutdown_event.is_set():
                    # Check if client disconnected
                    if await request.is_disconnected():
                        break
                        
                    line = f.readline()
                    if not line:
                        await asyncio.sleep(0.1)
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    yield f"data: {line}\n\n"
        except asyncio.CancelledError:
            # Handle cancellation during shutdown
            pass

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


@app.get("/snapshot")
def get_snapshot():
    """
    Returns the current snapshot data with baseline values and tracking info.
    """
    with _snapshot_lock:
        snapshot_data = load_snapshot_data()
    return snapshot_data


def _fetch_slot_data(headers: dict) -> list:
    """Fetch slot data from API (blocking call for use in thread pool)."""
    response = requests.get(API_URL, headers=headers, timeout=5)
    if response.status_code == 200:
        return response.json()
    return []


@app.post("/calibrate/{slot_id}")
async def calibrate_single_slot(slot_id: int):
    """
    Calibrate a single slot by taking 5 sample readings and averaging them
    to establish baseline x,y,z values.
    """
    samples_needed = 10
    sample_interval = 5  # seconds between samples
    
    samples: List[dict] = []
    
    headers = {}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    
    loop = asyncio.get_event_loop()
    
    for sample_num in range(samples_needed):
        try:
            # Run blocking request in thread pool to avoid blocking event loop
            data = await loop.run_in_executor(None, _fetch_slot_data, headers)
            
            for item in data:
                item_id = item.get("id")
                if item_id is None or int(item_id) != slot_id:
                    continue
                
                status_str = item.get("status")
                if not status_str:
                    continue
                
                if isinstance(status_str, str):
                    status = json.loads(status_str)
                else:
                    status = status_str
                
                r = float(status.get("r", 0))
                y = float(status.get("y", 0))
                b = float(status.get("b", 0))
                
                samples.append({"x": r, "y": y, "z": b})
                break
            
            # Wait before next sample (except for last iteration)
            if sample_num < samples_needed - 1:
                await asyncio.sleep(sample_interval)
                
        except Exception as e:
            print(f"Error during calibration sample {sample_num + 1}: {e}")
    
    if len(samples) == 0:
        return {"success": False, "message": f"Could not collect samples for slot {slot_id}"}
    
    # Calculate averages and save to snapshot
    avg_x = sum(s["x"] for s in samples) / len(samples)
    avg_y = sum(s["y"] for s in samples) / len(samples)
    avg_z = sum(s["z"] for s in samples) / len(samples)
    
    with _snapshot_lock:
        snapshot_data = load_snapshot_data()
        slots_snapshot = snapshot_data.get("slots", {})
        
        slot_key = str(slot_id)
        if slot_key not in slots_snapshot:
            slots_snapshot[slot_key] = {}
        
        slots_snapshot[slot_key]["baseline_x"] = round(avg_x, 2)
        slots_snapshot[slot_key]["baseline_y"] = round(avg_y, 2)
        slots_snapshot[slot_key]["baseline_z"] = round(avg_z, 2)
        slots_snapshot[slot_key]["consecutive_occupied"] = 0
        slots_snapshot[slot_key]["calibrated_at"] = datetime.now(timezone.utc).isoformat()
        
        save_snapshot_data({"slots": slots_snapshot})
    
    return {
        "success": True,
        "message": f"Calibrated slot {slot_id} with {len(samples)} samples",
        "baseline_x": round(avg_x, 2),
        "baseline_y": round(avg_y, 2),
        "baseline_z": round(avg_z, 2)
    }
