from __future__ import annotations
import json
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List
import yaml
import threading
import paho.mqtt.client as mqtt
from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import math
import os

# Import camera controller
try:
    from webapp.camera_controller import (
        CameraController,
        CameraTaskQueue,
        camera_worker,
        ENABLE_CAMERA_CONTROL,
        CAMERA_SNAPSHOTS_DIR,
        CAMERA_IP,
        CAMERA_USER,
        CAMERA_PASS,
        RTSP_URL
    )
    _camera_available = True
except ImportError as e:
    print(f"Camera controller not available: {e}")
    _camera_available = False
    ENABLE_CAMERA_CONTROL = False

# Import license plate extractor
try:
    from webapp.license_plate_extractor import extract_license_plate
    _license_plate_available = True
except ImportError as e:
    print(f"License plate extractor not available: {e}")
    _license_plate_available = False

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

# Event log write lock for thread-safe writes
_event_log_lock = threading.Lock()

# Response caching for /state endpoint
_state_cache = None
_state_cache_time = None
STATE_CACHE_TTL = timedelta(seconds=30)

# Metadata caching with file change detection
_meta_cache = None
_meta_cache_mtime = None

# Event streaming connection tracking
_active_streams = 0
_max_streams = 50
_streams_lock = threading.Lock()

# Camera control components
_camera_queue = None
_camera_controller = None
_camera_worker_thread = None

def load_snapshot_data() -> dict:
    """Load snapshot data from YAML file."""
    if not SNAPSHOT_PATH.exists():
        return {"slots": {}}
    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if data else {"slots": {}}
    except (IOError, OSError) as e:
        print(f"Error reading snapshot file: {e}")
        return {"slots": {}}
    except yaml.YAMLError as e:
        print(f"YAML parsing error in snapshot file: {e}")
        return {"slots": {}}

def save_snapshot_data(data: dict):
    """Save snapshot data to YAML file."""
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False)
    except (IOError, OSError) as e:
        print(f"Error writing snapshot file: {e}")
    except yaml.YAMLError as e:
        print(f"YAML serialization error: {e}")

def calculate_distance(x, y, z, bx, by, bz) -> float:
    """Calculate Euclidean distance between current and baseline magnetic field vectors."""
    return math.sqrt((x - bx) ** 2 + (y - by) ** 2 + (z - bz) ** 2)

def _calculate_zone_stats(slot_ids: List[int], occupied_ids: set, meta_by_id: Dict[int, dict]) -> tuple:
    """Calculate zone statistics and counts. Returns (zones_stats, free_count, total_count)."""
    zones_stats = {}
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


def _update_slot_baseline(slot_snapshot: dict, r: float, y: float, b: float, alpha: float = 0.01):
    """Update baseline values with exponential moving average."""
    baseline_x = slot_snapshot.get("baseline_x")
    baseline_y = slot_snapshot.get("baseline_y")
    baseline_z = slot_snapshot.get("baseline_z")

    if baseline_x is not None and baseline_y is not None and baseline_z is not None:
        slot_snapshot["baseline_x"] = round(baseline_x * (1 - alpha) + r * alpha, 2)
        slot_snapshot["baseline_y"] = round(baseline_y * (1 - alpha) + y * alpha, 2)
        slot_snapshot["baseline_z"] = round(baseline_z * (1 - alpha) + b * alpha, 2)

def rotate_log_if_needed(max_size_mb=50):
    """Rotate event log if it exceeds max size to prevent disk exhaustion."""
    if not EVENT_LOG_PATH.exists():
        return
    try:
        file_size_mb = EVENT_LOG_PATH.stat().st_size / (1024 * 1024)
        if file_size_mb > max_size_mb:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_path = EVENT_LOG_PATH.with_stem(f"{EVENT_LOG_PATH.stem}_{timestamp}")
            EVENT_LOG_PATH.rename(backup_path)
            print(f"Rotated event log to {backup_path.name} (size: {file_size_mb:.1f} MB)")
    except Exception as e:
        print(f"Error rotating event log: {e}")

# MQTT Configuration
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "application/+/device/+/event/up")
ENABLE_MQTT = int(os.getenv("ENABLE_MQTT", "1"))  # Enable MQTT by default

# MQTT client reference
_mqtt_client = None
_mqtt_previous_states: Dict[int, str] = {}
_mqtt_last_snapshot_time = None


def _log_camera_capture(slot_id: int, slot_name: str, zone: str, image_path: str, timestamp_str: str):
    """Log camera capture result to event log with license plate extraction."""
    # Extract license plate from the captured image
    license_plate = "UNKNOWN"
    if _license_plate_available:
        try:
            print(f"Extracting license plate from {image_path}...")
            license_plate = extract_license_plate(image_path)
        except Exception as e:
            print(f"Error extracting license plate: {e}")
            license_plate = "UNKNOWN"
    else:
        print("License plate extraction not available")

    event = {
        "event": "camera_capture",
        "ts": timestamp_str,
        "slot_id": slot_id,
        "slot_name": slot_name,
        "zone": zone,
        "image_path": image_path,
        "license_plate": license_plate
    }
    try:
        with _event_log_lock:
            with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
                f.flush()
    except Exception as e:
        print(f"Error logging camera capture: {e}")

def _detect_state_changes(current_states: dict, previous_states: dict, meta_by_id: dict, timestamp_str: str) -> list:
    """Detect state changes and return change events. Also enqueue camera tasks if enabled."""
    events = []
    for sid, state in current_states.items():
        prev_state = previous_states.get(sid)
        if prev_state is not None and prev_state != state:
            meta = meta_by_id.get(sid, {})
            events.append({
                "event": "slot_state_changed",
                "ts": timestamp_str,
                "slot_id": sid,
                "slot_name": meta.get("name", str(sid)),
                "zone": meta.get("zone", "A"),
                "prev_state": prev_state,
                "new_state": state
            })

            # Enqueue camera task if enabled
            if ENABLE_CAMERA_CONTROL and _camera_queue is not None:
                preset = meta.get("preset")
                if preset:
                    try:
                        timestamp_obj = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))

                        # Create callback to log image path after capture
                        def make_callback(slot_id, slot_name, zone, timestamp_str):
                            def callback(success, image_path, error_msg):
                                if success and image_path:
                                    _log_camera_capture(slot_id, slot_name, zone, image_path, timestamp_str)
                            return callback

                        _camera_queue.add_task({
                            "slot_id": sid,
                            "slot_name": meta.get("name", str(sid)),
                            "zone": meta.get("zone", "A"),
                            "preset": preset,
                            "timestamp": timestamp_obj,
                            "event_id": f"{sid}_{timestamp_str}",
                            "callback": make_callback(sid, meta.get("name", str(sid)), meta.get("zone", "A"), timestamp_str)
                        })
                    except Exception as e:
                        print(f"Error enqueueing camera task for slot {sid}: {e}")
    return events


def decode_uplink(payload_base64: str) -> dict:
    """
    Decode the LoRaWAN uplink payload.
    
    Payload format (8 bytes):
        - Bytes 0-1: X accelerometer (signed int16, divide by 100)
        - Bytes 2-3: Y accelerometer (signed int16, divide by 100)
        - Bytes 4-5: Z accelerometer (signed int16, divide by 100)
        - Bytes 6-7: Temperature (signed int16, divide by 100) - ignored
        
    Args:
        payload_base64: Base64 encoded payload string
        
    Returns:
        dict with x, y, z values and timestamp
    """
    data = base64.b64decode(payload_base64)
    
    def read_int16(index):
        """Read a signed 16-bit integer from bytes at given index"""
        val = (data[index] << 8) | data[index + 1]
        if val & 0x8000:  # Check sign bit
            val -= 0x10000  # Convert to signed
        return val
    
    # Parse accelerometer values (each is 2 bytes, scaled by 100)
    x = read_int16(0) / 100
    y = read_int16(2) / 100
    z = read_int16(4) / 100
    
    timestamp = datetime.now(timezone.utc).isoformat()
    
    return {"x": x, "y": y, "z": z, "timestamp": timestamp}


def _get_slot_id_by_device_name(device_name: str, meta_by_id: Dict[int, dict]) -> int | None:
    """
    Map MQTT device name to slot ID.
    Looks for a 'device_name' field in slot metadata, or matches by slot name.
    """
    for slot_id, meta in meta_by_id.items():
        # First check for explicit device_name field
        if meta.get("device_name") == device_name:
            return slot_id
        # Fallback: check if name matches device name (case-insensitive)
        if meta.get("name", "").upper() == device_name.upper():
            return slot_id
    return None


def _process_mqtt_sensor_data(slot_id: int, x: float, y: float, z: float, timestamp_str: str):
    """Process sensor data from MQTT and update occupancy state."""
    global _mqtt_previous_states, _mqtt_last_snapshot_time
    
    meta_by_id = load_slot_meta_by_id()
    all_configured_ids = load_slot_ids()
    
    with _snapshot_lock:
        snapshot_data = load_snapshot_data()
        slots_snapshot = snapshot_data.get("slots", {})
        slot_key = str(slot_id)
        slot_snapshot = slots_snapshot.get(slot_key, {})
        
        # Check for duplicate data
        if (x == slot_snapshot.get("last_x") and 
            y == slot_snapshot.get("last_y") and 
            z == slot_snapshot.get("last_z")):
            return
        
        # Update last readings
        slot_snapshot.update({"last_x": x, "last_y": y, "last_z": z})
        
        baseline_x = slot_snapshot.get("baseline_x")
        baseline_y = slot_snapshot.get("baseline_y")
        baseline_z = slot_snapshot.get("baseline_z")
        consecutive_occupied = slot_snapshot.get("consecutive_occupied", 0)
        
        if baseline_x is not None and baseline_y is not None and baseline_z is not None:
            distance = calculate_distance(x, y, z, baseline_x, baseline_y, baseline_z)
            
            if distance > DISTANCE_THRESHOLD:
                consecutive_occupied = min(consecutive_occupied + 1, CONSECUTIVE_COUNT_REQUIRED)
            elif distance <= DISTANCE_THRESHOLD * 0.9:
                consecutive_occupied = 0
            
            slot_snapshot["consecutive_occupied"] = consecutive_occupied
            slot_snapshot["last_distance"] = round(distance, 2)
            
            if consecutive_occupied == 0:
                _update_slot_baseline(slot_snapshot, x, y, z)
        else:
            slot_snapshot["consecutive_occupied"] = 0
        
        slots_snapshot[slot_key] = slot_snapshot
        
        # Build occupied_ids from current snapshot (avoid reloading)
        occupied_ids = {
            sid for sid in all_configured_ids
            if slots_snapshot.get(str(sid), {}).get("consecutive_occupied", 0) >= CONSECUTIVE_COUNT_REQUIRED
        }
        
        save_snapshot_data({"slots": slots_snapshot})
    
    # State change detection and logging
    current_states = {sid: "OCCUPIED" if sid in occupied_ids else "FREE" for sid in all_configured_ids}
    timestamp = datetime.now(timezone.utc)
    
    events_to_log = _detect_state_changes(current_states, _mqtt_previous_states, meta_by_id, timestamp_str)
    has_state_changes = bool(events_to_log)
    _mqtt_previous_states = current_states.copy()
    
    zones_stats, free_count, total_count = _calculate_zone_stats(all_configured_ids, occupied_ids, meta_by_id)
    
    # Log snapshot periodically or on state changes
    if _mqtt_last_snapshot_time is None:
        _mqtt_last_snapshot_time = timestamp
    
    if has_state_changes or (timestamp - _mqtt_last_snapshot_time) >= timedelta(minutes=1):
        events_to_log.append({
            "event": "snapshot",
            "ts": timestamp_str,
            "occupied_ids": list(occupied_ids),
            "zone_stats": zones_stats,
            "total_count": total_count,
            "free_count": free_count
        })
        _mqtt_last_snapshot_time = timestamp
    
    if events_to_log:
        rotate_log_if_needed(max_size_mb=50)
        with _event_log_lock:
            with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
                for evt in events_to_log:
                    f.write(json.dumps(evt) + "\n")
                f.flush()


def on_mqtt_message(client, userdata, msg):
    """
    Handle incoming MQTT messages from ChirpStack/LoRaWAN.
    Decodes the payload and processes sensor data for occupancy detection.
    """
    global _mqtt_last_snapshot_time
    
    try:
        payload = json.loads(msg.payload)
        device_name = payload.get('deviceInfo', {}).get('deviceName', 'Unknown')
        
        # Get the raw payload (base64 encoded)
        raw_data = payload.get('data')
        
        if not raw_data:
            print(f"Device {device_name}: No data in payload")
            return
        
        # Decode the LoRaWAN payload
        decoded = decode_uplink(raw_data)
        x, y, z = decoded['x'], decoded['y'], decoded['z']
        timestamp_str = decoded['timestamp']
        
        print(f"MQTT Device: {device_name} | x: {x}, y: {y}, z: {z} | ts: {timestamp_str}")
        
        # Map device name to slot ID
        meta_by_id = load_slot_meta_by_id()
        slot_id = _get_slot_id_by_device_name(device_name, meta_by_id)
        
        if slot_id is None:
            print(f"Warning: Device '{device_name}' not mapped to any slot")
            return
        
        # Process the sensor data
        _process_mqtt_sensor_data(slot_id, x, y, z, timestamp_str)
        
        # Add sample for calibration if active
        add_calibration_sample(slot_id, x, y, z)
        
    except Exception as e:
        print(f"Error processing MQTT message: {e}")


def on_mqtt_connect(client, userdata, flags, rc):
    """Handle MQTT connection and subscribe to topic."""
    if rc == 0:
        print(f"Connected to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}")
        client.subscribe(MQTT_TOPIC)
        print(f"Subscribed to topic: {MQTT_TOPIC}")
    else:
        print(f"Failed to connect to MQTT broker, return code: {rc}")


def start_mqtt_listener():
    """Start the MQTT client in a background thread."""
    global _mqtt_client
    
    _mqtt_client = mqtt.Client()
    _mqtt_client.on_connect = on_mqtt_connect
    _mqtt_client.on_message = on_mqtt_message
    
    try:
        _mqtt_client.connect(MQTT_BROKER, MQTT_PORT)
        print(f"Starting MQTT listener for LoRaWAN uplink messages...")
        _mqtt_client.loop_start()  # Start background thread for MQTT
    except Exception as e:
        print(f"Error starting MQTT client: {e}")


def stop_mqtt_listener():
    """Stop the MQTT client."""
    global _mqtt_client
    if _mqtt_client is not None:
        print("Stopping MQTT listener...")
        _mqtt_client.loop_stop()
        _mqtt_client.disconnect()
        _mqtt_client = None



@app.on_event("startup")
def start_app():
    global _camera_queue, _camera_controller, _camera_worker_thread

    # Clear shutdown event in case it was set from previous run
    _shutdown_event.clear()

    # Initialize camera system if enabled
    if ENABLE_CAMERA_CONTROL and _camera_available:
        try:
            print("Initializing camera control system...")
            _camera_queue = CameraTaskQueue()
            _camera_controller = CameraController(
                ip=CAMERA_IP,
                user=CAMERA_USER,
                password=CAMERA_PASS,
                rtsp_url=RTSP_URL
            )

            # Ensure snapshot directory exists
            CAMERA_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

            # Start camera worker thread
            _camera_worker_thread = threading.Thread(
                target=camera_worker,
                args=(_camera_controller, _camera_queue),
                daemon=True,
                name="CameraWorker"
            )
            _camera_worker_thread.start()
            print(f"Camera control enabled (IP: {CAMERA_IP})")
        except Exception as e:
            print(f"Failed to initialize camera system: {e}")
            print("Continuing without camera control...")
    else:
        if not ENABLE_CAMERA_CONTROL:
            print("Camera control disabled (ENABLE_CAMERA_CONTROL not set)")
        else:
            print("Camera controller module not available")

    # Start MQTT listener if enabled
    if ENABLE_MQTT:
        print(f"MQTT enabled. Starting listener for {MQTT_BROKER}:{MQTT_PORT}...")
        start_mqtt_listener()
    else:
        print("MQTT disabled. Set ENABLE_MQTT=1 to enable.")


@app.on_event("shutdown")
def stop_app():
    print("Shutting down...")
    _shutdown_event.set()
    
    # Shutdown MQTT listener
    if ENABLE_MQTT:
        stop_mqtt_listener()

    # Shutdown camera worker
    if _camera_queue is not None:
        print("Shutting down camera worker...")
        _camera_queue.shutdown_event.set()

def _load_yaml(path: Path):
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def load_slot_ids() -> List[int]:
    meta = load_slot_meta_by_id()
    return sorted(meta.keys())


def load_slot_meta_by_id() -> Dict[int, dict]:
    global _meta_cache, _meta_cache_mtime

    if not SLOT_META_PATH.exists():
        return {}

    # Check if cache is still valid based on file modification time
    try:
        current_mtime = SLOT_META_PATH.stat().st_mtime
        if _meta_cache is not None and _meta_cache_mtime == current_mtime:
            return _meta_cache
    except Exception:
        pass  # If stat fails, proceed to reload

    # Load and parse metadata
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


def describe_slot(slot_id: int, meta_by_id: Dict[int, dict]) -> dict:
    meta = meta_by_id.get(slot_id, {})
    name = meta.get("name") or str(slot_id)
    zone = meta.get("zone") or "A"
    return {"id": slot_id, "name": name, "zone": zone}


def build_state_from_log(slot_ids: List[int], meta_by_id: Dict[int, dict], max_events: int = 200) -> dict:
    state_by_id: Dict[int, str] = {slot_id: "FREE" for slot_id in slot_ids}
    since_by_id: Dict[int, str] = {slot_id: "" for slot_id in slot_ids}
    last_events: List[dict] = []

    if EVENT_LOG_PATH.exists():
        with open(EVENT_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if not (line := line.strip()):
                    continue
                try:
                    obj = json.loads(line)
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
                        slot_id = int(obj.get("slot_id"))
                        if slot_id in state_by_id and obj.get("new_state") in ("FREE", "OCCUPIED"):
                            state_by_id[slot_id] = obj["new_state"]
                            since_by_id[slot_id] = ts
                        last_events.append(obj)
                except Exception:
                    continue

    slots = [describe_slot(slot_id, meta_by_id) for slot_id in slot_ids]
    zones: Dict[str, dict] = {}
    for slot in slots:
        zone = slot["zone"]
        if zone not in zones:
            zones[zone] = {"total": 0, "free": 0, "occupied": 0}
        zones[zone]["total"] += 1
        zones[zone]["occupied" if state_by_id[slot["id"]] == "OCCUPIED" else "free"] += 1

    return {
        "slots": slots,
        "state_by_id": state_by_id,
        "since_by_id": since_by_id,
        "zones": zones,
        "free_count": sum(1 for s in state_by_id.values() if s == "FREE"),
        "total_count": len(slot_ids),
        "recent_events": last_events[-max_events:],
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(str(APP_ROOT / "static" / "index.html"))


@app.get("/favicon.ico")
def favicon():
    """Return 204 No Content for favicon to avoid 404 errors."""
    from fastapi import Response
    return Response(status_code=204)


@app.get("/state")
def state():
    global _state_cache, _state_cache_time
    now = datetime.now(timezone.utc)

    # Return cached response if still valid
    if _state_cache and _state_cache_time and (now - _state_cache_time) < STATE_CACHE_TTL:
        return _state_cache

    # Build fresh state
    slot_ids = load_slot_ids()
    meta_by_id = load_slot_meta_by_id()
    result = build_state_from_log(slot_ids=slot_ids, meta_by_id=meta_by_id)

    # Update cache
    _state_cache = result
    _state_cache_time = now
    return result


@app.get("/events")
async def events(request: Request):
    global _active_streams

    # Check connection limit
    with _streams_lock:
        if _active_streams >= _max_streams:
            from fastapi import HTTPException
            raise HTTPException(status_code=503, detail="Too many active event streams")
        _active_streams += 1

    try:
        async def gen():
            EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            EVENT_LOG_PATH.touch(exist_ok=True)

            try:
                with open(EVENT_LOG_PATH, "r", encoding="utf-8") as f:
                    f.seek(0, 2)  # tail from end
                    backoff = 0.1
                    while not _shutdown_event.is_set():
                        # Check if client disconnected
                        if await request.is_disconnected():
                            break

                        line = f.readline()
                        if line:
                            line = line.strip()
                            if line:
                                yield f"data: {line}\n\n"
                            backoff = 0.1  # Reset backoff on successful read
                        else:
                            await asyncio.sleep(backoff)
                            backoff = min(backoff * 1.5, 1.0)  # Exponential backoff up to 1s
            except asyncio.CancelledError:
                # Handle cancellation during shutdown
                pass

        return StreamingResponse(gen(), media_type="text/event-stream")
    finally:
        # Decrement active streams counter when connection closes
        with _streams_lock:
            _active_streams -= 1

def _parse_events_from_log(cutoff: datetime | None) -> tuple[list, list]:
    """Parse snapshots and state changes from event log with optional time filter."""
    snapshots, state_changes = [], []

    if not EVENT_LOG_PATH.exists():
        return snapshots, state_changes

    with open(EVENT_LOG_PATH, "r", encoding="utf-8") as f:
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
            except Exception:
                continue

    return snapshots, state_changes

def _build_occupancy_series(snapshots: list) -> list:
    """Convert snapshots to time series of zone occupancy percentages."""
    occupancy_series = []
    for snap in snapshots:
        zone_data = {}
        for zone, stats in snap.get("zone_stats", {}).items():
            total = stats.get("total", 0)
            occupied = stats.get("occupied", 0)
            pct = (occupied / total * 100) if total > 0 else 0
            zone_data[zone] = round(pct, 1)

        occupancy_series.append({"time": snap["ts"].isoformat(), "zones": zone_data})

    return occupancy_series

def _calculate_dwell_times(state_changes: list) -> dict:
    """Calculate average dwell time per zone from state changes."""
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
            if 0 < dwell_minutes < 1440:  # Cap at 24 hours
                dwell_times_by_zone.setdefault(zone, []).append(dwell_minutes)

    return {zone: round(sum(times) / len(times), 1) for zone, times in dwell_times_by_zone.items() if times}

def _predict_occupancy(occupancy_series: list) -> dict:
    """Simple moving average prediction with trend adjustment."""
    if len(occupancy_series) < 2:
        return {}

    all_zones = set()
    for entry in occupancy_series:
        all_zones.update(entry["zones"].keys())

    predictions = {}
    for zone in all_zones:
        recent_values = [entry["zones"][zone] for entry in occupancy_series[-5:] if zone in entry["zones"]]

        if recent_values:
            avg = sum(recent_values) / len(recent_values)
            if len(recent_values) >= 2:
                trend = recent_values[-1] - recent_values[-2]
                predicted = avg + (trend * 0.5)
            else:
                predicted = avg
            predictions[zone] = round(max(0, min(100, predicted)), 1)

    return predictions

@app.get("/analytics/summary")
def analytics_summary(range: str = Query(default="24h")):
    """Returns analytics data: occupancy series, dwell stats, predictions, summary."""
    time_deltas = {"1h": timedelta(hours=1), "6h": timedelta(hours=6), "24h": timedelta(hours=24), "7d": timedelta(days=7), "all": None}
    delta = time_deltas.get(range)
    cutoff = (datetime.now(timezone.utc) - delta) if delta else None

    snapshots, state_changes = _parse_events_from_log(cutoff)
    occupancy_series = _build_occupancy_series(snapshots)
    dwell_stats = _calculate_dwell_times(state_changes)
    predictions = _predict_occupancy(occupancy_series)

    current_occupancy = {}
    if snapshots:
        for zone, stats in snapshots[-1].get("zone_stats", {}).items():
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
            "total_events": len(state_changes),
            "total_snapshots": len(snapshots),
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


# Storage for calibration samples (slot_id -> list of {x, y, z} samples)
_calibration_samples: Dict[int, list] = {}
_calibration_active: Dict[int, bool] = {}


async def _collect_mqtt_calibration_samples(slot_id: int, samples_needed: int = 10, timeout: int = 60) -> list:
    """
    Collect calibration samples from MQTT data.
    Waits for new MQTT readings to come in and collects them.
    """
    _calibration_samples[slot_id] = []
    _calibration_active[slot_id] = True
    
    start_time = datetime.now(timezone.utc)
    max_wait = timedelta(seconds=timeout)
    
    while len(_calibration_samples.get(slot_id, [])) < samples_needed:
        if datetime.now(timezone.utc) - start_time > max_wait:
            break
        await asyncio.sleep(1)
    
    _calibration_active[slot_id] = False
    samples = _calibration_samples.get(slot_id, [])
    _calibration_samples.pop(slot_id, None)
    return samples


def add_calibration_sample(slot_id: int, x: float, y: float, z: float):
    """Called from MQTT handler to add calibration samples when calibration is active."""
    if _calibration_active.get(slot_id, False):
        if slot_id not in _calibration_samples:
            _calibration_samples[slot_id] = []
        _calibration_samples[slot_id].append({"x": x, "y": y, "z": z})


@app.post("/calibrate/{slot_id}")
async def calibrate_single_slot(slot_id: int):
    """
    Calibrate a slot by collecting 5 sample readings from MQTT and averaging them.
    Waits up to 120 seconds for samples to arrive.
    """
    if slot_id < 1:
        return {"success": False, "message": "Invalid slot_id: must be positive integer"}

    meta_by_id = load_slot_meta_by_id()
    if slot_id not in meta_by_id:
        return {"success": False, "message": f"Slot {slot_id} not configured in slot_meta.yaml"}

    # Collect 5 samples from MQTT
    print(f"Starting calibration for slot {slot_id}, waiting for 5 samples...")
    samples = await _collect_mqtt_calibration_samples(slot_id, samples_needed=5, timeout=120)
    
    if len(samples) < 5:
        return {
            "success": False, 
            "message": f"Insufficient samples: {len(samples)}/5. Timeout waiting for MQTT data. Check if sensor is transmitting."
        }
    
    # Calculate averages
    avg_x = sum(s["x"] for s in samples) / len(samples)
    avg_y = sum(s["y"] for s in samples) / len(samples)
    avg_z = sum(s["z"] for s in samples) / len(samples)
    
    if abs(avg_x) < 0.1 and abs(avg_y) < 0.1 and abs(avg_z) < 0.1:
        return {"success": False, "message": "Invalid baseline: all values near zero. Check sensor connection."}

    with _snapshot_lock:
        snapshot_data = load_snapshot_data()
        slots_snapshot = snapshot_data.get("slots", {})
        slot_key = str(slot_id)
        slots_snapshot.setdefault(slot_key, {}).update({
            "baseline_x": round(avg_x, 2),
            "baseline_y": round(avg_y, 2),
            "baseline_z": round(avg_z, 2),
            "consecutive_occupied": 0,
            "calibrated_at": datetime.now(timezone.utc).isoformat()
        })
        save_snapshot_data({"slots": slots_snapshot})
    
    print(f"Calibration complete for slot {slot_id}: x={round(avg_x, 2)}, y={round(avg_y, 2)}, z={round(avg_z, 2)}")

    return {
        "success": True,
        "message": f"Calibrated slot {slot_id} with {len(samples)} samples",
        "baseline_x": round(avg_x, 2),
        "baseline_y": round(avg_y, 2),
        "baseline_z": round(avg_z, 2)
    }


@app.get("/alerts")
def get_alerts(limit: int = Query(default=50, le=200), offset: int = Query(default=0)):
    """
    Returns recent state change events (alerts) with optional images and license plates.
    Sorted by timestamp descending (newest first).
    """
    alerts = []
    camera_captures = {}

    if not EVENT_LOG_PATH.exists():
        return {"alerts": [], "total": 0, "limit": limit, "offset": offset}

    # Read events from log
    with _event_log_lock:
        with open(EVENT_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    event_type = obj.get("event")

                    if event_type == "slot_state_changed":
                        alerts.append(obj)
                    elif event_type == "camera_capture":
                        # Store camera captures by slot_id and timestamp for matching
                        key = (obj.get("slot_id"), obj.get("ts"))
                        camera_captures[key] = {
                            "image_path": obj.get("image_path"),
                            "license_plate": obj.get("license_plate", "UNKNOWN")
                        }
                except Exception:
                    continue

    # Merge camera captures with alerts
    for alert in alerts:
        key = (alert.get("slot_id"), alert.get("ts"))
        if key in camera_captures:
            alert["image_path"] = camera_captures[key]["image_path"]
            alert["license_plate"] = camera_captures[key]["license_plate"]
        else:
            # Default license plate to UNKNOWN if no camera capture
            alert["license_plate"] = "UNKNOWN"

    # Sort newest first
    alerts.sort(key=lambda x: x.get("ts", ""), reverse=True)

    total = len(alerts)
    paginated = alerts[offset:offset+limit]

    return {
        "alerts": paginated,
        "total": total,
        "limit": limit,
        "offset": offset
    }


@app.get("/snapshots/{filename}")
def get_snapshot_image(filename: str):
    """
    Serves captured camera snapshot images.
    Validates filename to prevent directory traversal.
    """
    # Validate filename (no path components)
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    if not ENABLE_CAMERA_CONTROL or not _camera_available:
        raise HTTPException(status_code=503, detail="Camera control not enabled")

    snapshot_path = CAMERA_SNAPSHOTS_DIR / filename

    if not snapshot_path.exists() or not snapshot_path.is_file():
        raise HTTPException(status_code=404, detail="Snapshot not found")

    return FileResponse(
        str(snapshot_path),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000"}  # Cache for 1 year
    )


@app.get("/camera/status")
def camera_status():
    """Returns camera system status."""
    if not ENABLE_CAMERA_CONTROL or not _camera_available:
        return {
            "enabled": False,
            "message": "Camera control disabled"
        }

    is_available = False
    queue_size = 0
    worker_active = False

    try:
        if _camera_controller:
            is_available = _camera_controller.is_available()
        if _camera_queue:
            queue_size = _camera_queue.queue.qsize()
        if _camera_worker_thread:
            worker_active = _camera_worker_thread.is_alive()
    except Exception as e:
        print(f"Error checking camera status: {e}")

    return {
        "enabled": True,
        "available": is_available,
        "queue_size": queue_size,
        "worker_active": worker_active,
        "camera_ip": CAMERA_IP
    }
