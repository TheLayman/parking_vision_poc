from __future__ import annotations
import json
import asyncio
import base64
import logging
import struct
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List
import yaml
import threading
import paho.mqtt.client as mqtt
from fastapi import FastAPI, Query, Request, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import os
import atexit

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

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
        RTSP_URL,
        QUEUE_LOG_PATH
    )
    _camera_available = True
except ImportError as e:
    log.warning("Camera controller not available: %s", e)
    _camera_available = False
    ENABLE_CAMERA_CONTROL = False

# Import license plate extractor
try:
    from webapp.license_plate_extractor import extract_all_license_plates
    _license_plate_available = True
except ImportError as e:
    log.warning("License plate extractor not available: %s", e)
    _license_plate_available = False

APP_ROOT = Path(__file__).resolve().parent
REPO_ROOT = APP_ROOT.parent

SLOT_META_PATH = REPO_ROOT / "config" / "slot_meta.yaml"
EVENT_LOG_PATH = REPO_ROOT / "data" / "occupancy_events.jsonl"
SNAPSHOT_PATH = REPO_ROOT / "data" / "snapshot.yaml"


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(application: FastAPI):
    """FastAPI lifespan: startup / shutdown in one place."""
    _startup()
    yield
    _shutdown()

app = FastAPI(title="Parking Vision Dashboard", lifespan=_lifespan)
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

# In-memory snapshot state (loaded once at startup, flushed periodically)
_slots_snapshot: Dict[str, dict] = {}
_slots_snapshot_dirty = False

# In-memory alerts ring buffer (avoids full log scan on /alerts)
_alerts_buffer: List[dict] = []  # state-change alerts
_camera_captures: Dict[tuple, dict] = {}  # (slot_id, ts) -> capture info
_ALERTS_MAX = 500

# ── Challan (parking violation) tracking ──────────────────────────────────────
# Challan re-check interval in seconds (70s × 2 checks ≈ detect >2 min stays)
CHALLAN_RECHECK_INTERVAL = int(os.getenv("CHALLAN_RECHECK_INTERVAL", "70"))
CHALLAN_LOG_PATH = REPO_ROOT / "data" / "challan_events.jsonl"
_challan_records: List[dict] = []  # in-memory challan buffer
_challan_lock = threading.Lock()
_challan_pending: Dict[str, dict] = {}  # plate_text -> pending recheck info
_CHALLAN_MAX = 1000

# Reverse device-name lookup: device_name(upper) -> slot_id
_device_name_to_slot: Dict[str, int] = {}

# Metadata caching with file change detection
_meta_cache = None
_meta_cache_mtime = None
_device_name_map_mtime = None  # tracks when reverse map was last rebuilt
_max_streams = 50
_active_streams = 0
_streams_lock = threading.Lock()

# Camera control components
_camera_queue = None
_camera_controller = None
_camera_worker_thread = None

def load_snapshot_data() -> dict:
    """Load snapshot data from YAML file (cold-start only)."""
    if not SNAPSHOT_PATH.exists():
        return {"slots": {}}
    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if data else {"slots": {}}
    except (IOError, OSError, yaml.YAMLError) as e:
        log.error("Error reading snapshot file: %s", e)
        return {"slots": {}}

def save_snapshot_data(data: dict):
    """Save snapshot data to YAML file."""
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False)
    except (IOError, OSError, yaml.YAMLError) as e:
        log.error("Error writing snapshot file: %s", e)



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
            log.info("Rotated event log to %s (size: %.1f MB)", backup_path.name, file_size_mb)
    except Exception as e:
        log.error("Error rotating event log: %s", e)

# MQTT Configuration
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "application/+/device/+/event/up")
ENABLE_MQTT = int(os.getenv("ENABLE_MQTT", "1"))  # Enable MQTT by default

# ChirpStack gRPC API Configuration
CHIRPSTACK_HOST = os.getenv("CHIRPSTACK_HOST", "localhost")
CHIRPSTACK_GRPC_PORT = os.getenv("CHIRPSTACK_GRPC_PORT", "8080")
CHIRPSTACK_API_TOKEN = os.getenv("CHIRPSTACK_API_TOKEN", "")
CHIRPSTACK_APP_ID = os.getenv("CHIRPSTACK_APP_ID", "")  # Application UUID to fetch devices from

# MQTT client reference
_mqtt_client = None
_mqtt_previous_states: Dict[int, str] = {}
_mqtt_last_snapshot_time = None

# Device mapping for command queuing: slot_id -> {"applicationId": str, "devEui": str}
_device_map: Dict[int, dict] = {}


def _save_device_map():
    """Persist device map into snapshot.yaml so it survives server restarts."""
    try:
        with _snapshot_lock:
            data = load_snapshot_data()
            data["device_map"] = {str(k): v for k, v in _device_map.items()}
            save_snapshot_data(data)
    except Exception as e:
        log.error("Error persisting device map: %s", e)


def _load_device_map():
    """Restore device map from snapshot.yaml on startup."""
    global _device_map
    try:
        data = load_snapshot_data()
        saved = data.get("device_map", {})
        for k, v in saved.items():
            _device_map[int(k)] = v
        if _device_map:
            log.info("Restored device map for %d slot(s)", len(_device_map))
    except Exception as e:
        log.error("Error loading device map: %s", e)


def _fetch_devices_from_chirpstack():
    """Fetch all devices from ChirpStack gRPC API and populate _device_map."""
    if not CHIRPSTACK_API_TOKEN or not CHIRPSTACK_APP_ID:
        log.info("ChirpStack API not configured (missing CHIRPSTACK_API_TOKEN or CHIRPSTACK_APP_ID)")
        return

    try:
        import grpc
        from chirpstack_api import api as cs_api

        target = f"{CHIRPSTACK_HOST}:{CHIRPSTACK_GRPC_PORT}"
        channel = grpc.insecure_channel(target)
        client = cs_api.DeviceServiceStub(channel)
        auth_token = [("authorization", f"Bearer {CHIRPSTACK_API_TOKEN}")]

        meta_by_id = load_slot_meta_by_id()
        matched = 0
        offset = 0
        limit = 100

        while True:
            resp = client.List(
                cs_api.ListDevicesRequest(
                    application_id=CHIRPSTACK_APP_ID,
                    limit=limit,
                    offset=offset,
                ),
                metadata=auth_token,
            )

            for device in resp.result:
                dev_name = device.name
                dev_eui = device.dev_eui

                # Match ChirpStack device name to a slot
                slot_id = _get_slot_id_by_device_name(dev_name, meta_by_id)
                if slot_id is not None:
                    _device_map[slot_id] = {
                        "applicationId": CHIRPSTACK_APP_ID,
                        "devEui": dev_eui,
                    }
                    matched += 1

            offset += limit
            if offset >= resp.total_count:
                break

        channel.close()

        if matched:
            log.info("ChirpStack API: mapped %d device(s) to slots", matched)
            _save_device_map()
        else:
            log.info("ChirpStack API: no devices matched any slot names")

    except Exception as e:
        log.error("Error fetching devices from ChirpStack: %s", e)


def _enqueue_via_chirpstack_grpc(dev_eui: str, data_hex: str, fport: int = 2) -> bool:
    """Enqueue a downlink via ChirpStack gRPC API (creates a real queue item)."""
    if not CHIRPSTACK_API_TOKEN:
        return False

    try:
        import grpc
        from chirpstack_api import api as cs_api

        target = f"{CHIRPSTACK_HOST}:{CHIRPSTACK_GRPC_PORT}"
        channel = grpc.insecure_channel(target)
        client = cs_api.DeviceServiceStub(channel)
        auth_token = [("authorization", f"Bearer {CHIRPSTACK_API_TOKEN}")]

        data_bytes = bytes.fromhex(data_hex)

        resp = client.Enqueue(
            cs_api.EnqueueDeviceQueueItemRequest(
                queue_item=cs_api.DeviceQueueItem(
                    dev_eui=dev_eui,
                    confirmed=False,
                    f_port=fport,
                    data=data_bytes,
                )
            ),
            metadata=auth_token,
        )
        channel.close()
        log.info("ChirpStack gRPC: enqueued downlink for %s (id: %s)", dev_eui, resp.id)
        return True
    except Exception as e:
        log.error("ChirpStack gRPC enqueue error: %s", e)
        return False


def _log_camera_capture(slot_id: int, slot_name: str, zone: str, image_path: str, timestamp_str: str):
    """Log camera capture result to event log with license plate extraction and challan tracking."""
    license_plates = []
    vehicle_detected = True  # default True when extractor is unavailable

    if _license_plate_available:
        try:
            log.info("Extracting license plates from %s...", image_path)
            extraction_result = extract_all_license_plates(image_path)
            vehicle_detected = extraction_result.get("vehicle_detected", True)
            plates_list = extraction_result.get("plates", [])
            for p in plates_list:
                pt = p.get("plate_text", "UNKNOWN")
                if pt and pt != "UNKNOWN":
                    license_plates.append(pt)
        except Exception as e:
            log.error("Error extracting license plates: %s", e)
    else:
        log.debug("License plate extraction not available")

    # Use best plate for backward-compatible single-plate field
    license_plate = license_plates[0] if license_plates else "UNKNOWN"

    event = {
        "event": "camera_capture",
        "ts": timestamp_str,
        "slot_id": slot_id,
        "slot_name": slot_name,
        "zone": zone,
        "image_path": image_path,
        "license_plate": license_plate,
        "license_plates": license_plates,
        "vehicle_detected": vehicle_detected
    }
    try:
        with _event_log_lock:
            with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
                f.flush()
        # Update in-memory capture lookup
        _camera_captures[(slot_id, timestamp_str)] = {
            "image_path": image_path,
            "license_plate": license_plate,
            "license_plates": license_plates,
            "vehicle_detected": vehicle_detected,
        }
    except Exception as e:
        log.error("Error logging camera capture: %s", e)

    # ── Challan tracking: schedule re-check for each detected plate ──────
    if license_plates and ENABLE_CAMERA_CONTROL and _camera_queue is not None:
        meta_by_id = load_slot_meta_by_id()
        meta = meta_by_id.get(slot_id, {})
        preset = meta.get("preset")
        if preset:
            for plate in license_plates:
                _schedule_challan_recheck(
                    plate_text=plate,
                    slot_id=slot_id,
                    slot_name=slot_name,
                    zone=zone,
                    preset=preset,
                    first_image=image_path,
                    first_time=timestamp_str,
                )


def _make_challan_recheck_callback(pending_key: str):
    """Create callback for challan recheck capture completion.

    The camera worker handles the move/settle/capture cycle.  This callback
    performs the plate comparison and challan recording after the image has
    been captured successfully.
    """
    def callback(success, image_path, error_msg):
        with _challan_lock:
            pending = _challan_pending.pop(pending_key, None)
        if pending is None:
            log.warning("Challan recheck callback: no pending entry for key %s", pending_key)
            return

        if not success or not image_path:
            log.error("Challan recheck capture failed for %s: %s", pending_key, error_msg)
            return

        plate_text = pending["plate_text"]
        slot_id = pending["slot_id"]
        slot_name = pending["slot_name"]
        zone = pending["zone"]
        preset = pending["preset"]
        first_image = pending["first_image"]
        first_time = pending["first_time"]
        recheck_count = pending.get("recheck_count", 0)

        log.info("Challan recheck #%d complete for plate %s at slot %s",
                 recheck_count + 1, plate_text, slot_name)

        ts_str = datetime.now(timezone.utc).isoformat()
        second_image = image_path

        # Extract plates from new capture
        new_plates = []
        if _license_plate_available:
            try:
                result = extract_all_license_plates(second_image)
                for p in result.get("plates", []):
                    pt = p.get("plate_text", "UNKNOWN")
                    if pt and pt != "UNKNOWN":
                        new_plates.append(pt)
            except Exception as e:
                log.error("Error extracting plates in challan recheck: %s", e)

        if plate_text in new_plates:
            # Same plate still present → CHALLAN
            _record_challan(
                plate_text=plate_text,
                slot_id=slot_id,
                slot_name=slot_name,
                zone=zone,
                first_image=first_image,
                first_time=first_time,
                second_image=second_image,
                second_time=ts_str,
                challan=True,
            )
        else:
            # Different plates or plates not found
            if new_plates and recheck_count < 1:
                for np_text in new_plates:
                    _schedule_challan_recheck(
                        plate_text=np_text,
                        slot_id=slot_id,
                        slot_name=slot_name,
                        zone=zone,
                        preset=preset,
                        first_image=second_image,
                        first_time=ts_str,
                    )
                    new_key = f"{np_text}_{slot_id}"
                    with _challan_lock:
                        if new_key in _challan_pending:
                            _challan_pending[new_key]["recheck_count"] = recheck_count + 1

            # Record the check (no challan)
            _record_challan(
                plate_text=plate_text,
                slot_id=slot_id,
                slot_name=slot_name,
                zone=zone,
                first_image=first_image,
                first_time=first_time,
                second_image=second_image,
                second_time=ts_str,
                challan=False,
                second_plates=new_plates,
            )

    return callback


def _schedule_challan_recheck(plate_text: str, slot_id: int, slot_name: str,
                               zone: str, preset: int, first_image: str,
                               first_time: str):
    """Schedule a re-check by pushing a delayed task onto the camera queue.

    Instead of using a threading.Timer (which bypasses the camera queue and
    causes race conditions), this enqueues a ``challan_recheck`` task with a
    ``scheduled_at`` field set to *now + CHALLAN_RECHECK_INTERVAL*.  The camera
    worker will hold the task until it's due, then move/capture through the
    single-threaded worker — eliminating concurrent camera access.
    """
    key = f"{plate_text}_{slot_id}"
    with _challan_lock:
        _challan_pending[key] = {
            "plate_text": plate_text,
            "slot_id": slot_id,
            "slot_name": slot_name,
            "zone": zone,
            "preset": preset,
            "first_image": first_image,
            "first_time": first_time,
            "recheck_count": 0,
        }

    if _camera_queue is None:
        log.warning("Camera queue not available for challan recheck scheduling")
        return

    scheduled_at = (datetime.now(timezone.utc) + timedelta(seconds=CHALLAN_RECHECK_INTERVAL)).isoformat()
    _camera_queue.add_task({
        "task_type": "challan_recheck",
        "slot_id": slot_id,
        "slot_name": slot_name,
        "zone": zone,
        "preset": preset,
        "scheduled_at": scheduled_at,
        "pending_key": key,
        "plate_text": plate_text,
        "first_image": first_image,
        "first_time": first_time,
        "recheck_count": 0,
        "callback": _make_challan_recheck_callback(key),
    })
    log.info("Challan recheck scheduled for plate %s at slot %s in %ds (via camera queue)",
             plate_text, slot_name, CHALLAN_RECHECK_INTERVAL)


def _record_challan(plate_text: str, slot_id: int, slot_name: str, zone: str,
                     first_image: str, first_time: str,
                     second_image: str, second_time: str,
                     challan: bool, second_plates: list = None):
    """Write a challan record to disk and keep in memory."""
    record = {
        "plate_text": plate_text,
        "slot_id": slot_id,
        "slot_name": slot_name,
        "zone": zone,
        "first_image": first_image,
        "first_time": first_time,
        "second_image": second_image,
        "second_time": second_time,
        "challan": challan,
    }
    if second_plates is not None:
        record["second_plates"] = second_plates

    with _challan_lock:
        _challan_records.append(record)
        if len(_challan_records) > _CHALLAN_MAX:
            _challan_records.pop(0)

    # Persist to file
    try:
        CHALLAN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CHALLAN_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
        status = "CHALLAN" if challan else "cleared"
        log.info("Challan record: plate=%s slot=%s %s", plate_text, slot_name, status)
    except Exception as e:
        log.error("Error writing challan record: %s", e)


def _make_camera_capture_callback(slot_id: int, slot_name: str, zone: str, timestamp_str: str):
    """Create callback for camera capture completion."""
    def callback(success, image_path, error_msg):
        if success and image_path:
            _log_camera_capture(slot_id, slot_name, zone, image_path, timestamp_str)
    return callback

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
            if ENABLE_CAMERA_CONTROL and _camera_queue is not None and prev_state == "FREE" and state == "OCCUPIED":
                preset = meta.get("preset")
                if preset:
                    try:
                        timestamp_obj = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                        slot_name = meta.get("name", str(sid))
                        zone = meta.get("zone", "A")

                        _camera_queue.add_task({
                            "slot_id": sid,
                            "slot_name": slot_name,
                            "zone": zone,
                            "preset": preset,
                            "timestamp": timestamp_obj,
                            "event_id": f"{sid}_{timestamp_str}",
                            "callback": _make_camera_capture_callback(sid, slot_name, zone, timestamp_str)
                        })
                    except Exception as e:
                        log.error("Error enqueueing camera task for slot %s: %s", sid, e)
    return events


def decode_uplink(payload_base64: str) -> dict:
    """
    Decode the LoRaWAN uplink payload.
    
    New Payload format:
        - String: "cd" (Calibration Done), "00" (Empty), "01" (Occupied)
        
    Args:
        payload_base64: Base64 encoded payload string
        
    Returns:
        dict with status and timestamp
    """
    try:
        data_bytes = base64.b64decode(payload_base64)
        # Convert raw bytes to hex string (e.g. b'\x00' -> "00", b'\xcd' -> "cd")
        status = data_bytes.hex().lower()
    except Exception as e:
        log.error("Error decoding payload: %s", e)
        status = "unknown"
    
    timestamp = datetime.now(timezone.utc).isoformat()
    return {"status": status, "timestamp": timestamp}


def _get_slot_id_by_device_name(device_name: str, meta_by_id: Dict[int, dict]) -> int | None:
    """Map MQTT device name to slot ID via cached reverse lookup (O(1))."""
    _rebuild_device_name_map_if_needed(meta_by_id)
    return _device_name_to_slot.get(device_name.upper())


def _rebuild_device_name_map_if_needed(meta_by_id: Dict[int, dict]):
    """Rebuild the reverse device-name → slot_id map when metadata changes."""
    global _device_name_to_slot, _device_name_map_mtime
    if _device_name_map_mtime == _meta_cache_mtime and _device_name_to_slot:
        return
    lookup: Dict[str, int] = {}
    for slot_id, meta in meta_by_id.items():
        dn = meta.get("device_name")
        if dn:
            lookup[dn.upper()] = slot_id
        name = meta.get("name", "")
        if name:
            lookup.setdefault(name.upper(), slot_id)
    _device_name_to_slot = lookup
    _device_name_map_mtime = _meta_cache_mtime


def _log_events_to_file(events: list):
    """Write events to event log file."""
    if not events:
        return

    rotate_log_if_needed(max_size_mb=50)
    with _event_log_lock:
        with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")
            f.flush()


def queue_command(slot_id: int, data_hex: str, fport: int = 2) -> bool:
    """
    Queue a downlink command for a device.
    Prefers ChirpStack gRPC Enqueue API; falls back to MQTT publish.
    For Class A devices, this will be sent after the next uplink.
    """
    device_info = _device_map.get(slot_id)
    if not device_info:
        log.error("No device mapping found for slot %s", slot_id)
        return False

    app_id = device_info.get("applicationId")
    dev_eui = device_info.get("devEui")

    if not (app_id and dev_eui):
        log.error("Incomplete device info for slot %s", slot_id)
        return False

    # --- Prefer gRPC Enqueue (creates a real ChirpStack queue item) ---
    if CHIRPSTACK_API_TOKEN:
        if _enqueue_via_chirpstack_grpc(dev_eui, data_hex, fport):
            return True
        log.warning("gRPC enqueue failed for slot %s, falling back to MQTT", slot_id)

    # --- Fallback: MQTT publish ---
    if _mqtt_client is None:
        log.error("MQTT client not initialized and gRPC unavailable")
        return False

    topic = f"application/{app_id}/device/{dev_eui}/command/down"

    try:
        data_bytes = bytes.fromhex(data_hex)
        data_b64 = base64.b64encode(data_bytes).decode("ascii")

        payload = {
            "devEui": dev_eui,
            "confirmed": False,
            "fPort": fport,
            "data": data_b64
        }

        result = _mqtt_client.publish(topic, json.dumps(payload), qos=1)
        if result.rc != 0:
            log.warning("publish returned rc=%s for slot %s", result.rc, slot_id)
            return False
        log.info("Queued command for slot %s via MQTT (topic: %s)", slot_id, topic)
        return True
    except Exception as e:
        log.error("Error queuing command: %s", e)
        return False


def _process_mqtt_sensor_data(slot_id: int, status: str, timestamp_str: str, meta_by_id: Dict[int, dict]):
    """Process sensor data from MQTT and update occupancy state."""
    global _mqtt_previous_states, _mqtt_last_snapshot_time, _state_cache, _slots_snapshot_dirty

    all_configured_ids = sorted(meta_by_id.keys())

    with _snapshot_lock:
        slot_key = str(slot_id)
        slot_snapshot = _slots_snapshot.get(slot_key, {})

        if status in ("00", "01"):
            slot_snapshot["last_status"] = status
        elif status == "cd":
            log.info("Device reported Calibration Done (cd)")

        _slots_snapshot[slot_key] = slot_snapshot
        _slots_snapshot_dirty = True

        occupied_ids = {
            sid for sid in all_configured_ids
            if _slots_snapshot.get(str(sid), {}).get("last_status") == "01"
        }

    # Invalidate cache so next /state request gets fresh data
    _state_cache = None

    # State change detection and logging
    current_states = {sid: "OCCUPIED" if sid in occupied_ids else "FREE" for sid in all_configured_ids}
    timestamp = datetime.now(timezone.utc)

    events_to_log = _detect_state_changes(current_states, _mqtt_previous_states, meta_by_id, timestamp_str)
    
    # If "cd" (Calibration Done) was received, log a specific event
    if status == "cd":
        events_to_log.append({
            "event": "device_calibration",
            "ts": timestamp_str,
            "slot_id": slot_id,
            "slot_name": meta_by_id.get(slot_id, {}).get("name", str(slot_id)),
            "message": "Device completed calibration"
        })

    # Populate in-memory alerts buffer for state changes
    for evt in events_to_log:
        if evt.get("event") == "slot_state_changed":
            _alerts_buffer.append(evt)
            if len(_alerts_buffer) > _ALERTS_MAX:
                _alerts_buffer.pop(0)

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
        # Flush in-memory snapshot to disk periodically
        _flush_snapshot_to_disk()

    _log_events_to_file(events_to_log)


def _flush_snapshot_to_disk():
    """Write the in-memory slots snapshot to disk if dirty."""
    global _slots_snapshot_dirty
    if not _slots_snapshot_dirty:
        return
    with _snapshot_lock:
        save_snapshot_data({"slots": dict(_slots_snapshot)})
        _slots_snapshot_dirty = False


def on_mqtt_message(client, userdata, msg):
    """
    Handle incoming MQTT messages from ChirpStack/LoRaWAN.
    Decodes the payload and processes sensor status.
    """
    try:
        payload = json.loads(msg.payload)
        device_name = payload.get('deviceInfo', {}).get('deviceName', 'Unknown')
        
        raw_data = payload.get('data')
        if not raw_data:
            log.debug("Device %s: No data in payload", device_name)
            return
        
        decoded = decode_uplink(raw_data)
        status = decoded['status']
        timestamp_str = decoded['timestamp']
        
        log.info("MQTT Device: %s | status: %s | ts: %s", device_name, status, timestamp_str)
        
        # Single meta load for the entire message processing
        meta_by_id = load_slot_meta_by_id()
        slot_id = _get_slot_id_by_device_name(device_name, meta_by_id)
        
        if slot_id is None:
            log.warning("Device '%s' not mapped to any slot", device_name)
            return
        
        # Update device map only when info changes
        app_id = payload.get('deviceInfo', {}).get('applicationId')
        dev_eui = payload.get('deviceInfo', {}).get('devEui')
        
        if app_id and dev_eui:
            new_info = {"applicationId": app_id, "devEui": dev_eui}
            if _device_map.get(slot_id) != new_info:
                _device_map[slot_id] = new_info
                _save_device_map()

        # Process the sensor data (meta_by_id passed to avoid re-loading)
        _process_mqtt_sensor_data(slot_id, status, timestamp_str, meta_by_id)
        
    except Exception as e:
        log.error("Error processing MQTT message: %s", e)


def on_mqtt_connect(client, userdata, flags, rc):
    """Handle MQTT connection and subscribe to topic."""
    if rc == 0:
        log.info("Connected to MQTT broker at %s:%s", MQTT_BROKER, MQTT_PORT)
        client.subscribe(MQTT_TOPIC)
        log.info("Subscribed to topic: %s", MQTT_TOPIC)
    else:
        log.error("Failed to connect to MQTT broker, return code: %s", rc)


def start_mqtt_listener():
    """Start the MQTT client in a background thread."""
    global _mqtt_client
    
    _mqtt_client = mqtt.Client()
    _mqtt_client.on_connect = on_mqtt_connect
    _mqtt_client.on_message = on_mqtt_message
    
    try:
        _mqtt_client.connect(MQTT_BROKER, MQTT_PORT)
        log.info("Starting MQTT listener for LoRaWAN uplink messages...")
        _mqtt_client.loop_start()  # Start background thread for MQTT
    except Exception as e:
        log.error("Error starting MQTT client: %s", e)


def stop_mqtt_listener():
    """Stop the MQTT client."""
    global _mqtt_client
    if _mqtt_client is not None:
        log.info("Stopping MQTT listener...")
        _mqtt_client.loop_stop()
        _mqtt_client.disconnect()
        _mqtt_client = None

def _startup():
    global _camera_queue, _camera_controller, _camera_worker_thread, _slots_snapshot

    _shutdown_event.clear()

    # Load snapshot into memory
    data = load_snapshot_data()
    _slots_snapshot = data.get("slots", {})
    log.info("Loaded in-memory snapshot with %d slot(s)", len(_slots_snapshot))

    # Initialise in-memory alerts from log (last N events)
    _init_alerts_from_log()

    # Initialize camera system if enabled
    if ENABLE_CAMERA_CONTROL and _camera_available:
        try:
            log.info("Initializing camera control system...")
            _camera_queue = CameraTaskQueue()
            _camera_controller = CameraController(
                ip=CAMERA_IP,
                user=CAMERA_USER,
                password=CAMERA_PASS,
                rtsp_url=RTSP_URL
            )
            CAMERA_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

            # Recover pending tasks from previous run
            _recover_pending_tasks()

            _camera_worker_thread = threading.Thread(
                target=camera_worker,
                args=(_camera_controller, _camera_queue),
                daemon=True,
                name="CameraWorker"
            )
            _camera_worker_thread.start()
            log.info("Camera control enabled (IP: %s)", CAMERA_IP)
        except Exception as e:
            log.error("Failed to initialize camera system: %s", e)
    else:
        log.info("Camera control %s", "disabled" if not ENABLE_CAMERA_CONTROL else "module not available")

    if ENABLE_MQTT:
        log.info("MQTT enabled. Starting listener for %s:%s...", MQTT_BROKER, MQTT_PORT)
        start_mqtt_listener()
    else:
        log.info("MQTT disabled. Set ENABLE_MQTT=1 to enable.")

    _load_device_map()
    _fetch_devices_from_chirpstack()


def _shutdown():
    log.info("Shutting down...")
    _shutdown_event.set()

    # Flush snapshot to disk
    _flush_snapshot_to_disk()

    if ENABLE_MQTT:
        stop_mqtt_listener()
    if _camera_queue is not None:
        log.info("Shutting down camera worker...")
        _camera_queue.shutdown_event.set()
        if _camera_worker_thread is not None and _camera_worker_thread.is_alive():
            _camera_worker_thread.join(timeout=15)
            if _camera_worker_thread.is_alive():
                log.warning("Camera worker did not terminate within 15s")
            else:
                log.info("Camera worker stopped gracefully")


def _atexit_handler():
    """Safety net for unclean exits (Ctrl+C, kill) that bypass the FastAPI lifespan."""
    _shutdown_event.set()
    if _camera_queue is not None:
        _camera_queue.shutdown_event.set()
    _flush_snapshot_to_disk()

atexit.register(_atexit_handler)


def _recover_pending_tasks():
    """Recover pending tasks from data/camera_queue.jsonl after a crash/restart."""
    if _camera_queue is None:
        return

    pending = _camera_queue.recover_tasks(max_age_seconds=300)
    if not pending:
        log.info("No pending tasks to recover from queue log")
        _camera_queue.compact_log()
        return

    camera_count = 0
    challan_count = 0

    for task in pending:
        task_type = task.get("task_type", "camera_capture")

        if task_type == "challan_recheck":
            pending_key = task.get("pending_key")
            if pending_key:
                with _challan_lock:
                    _challan_pending[pending_key] = {
                        "plate_text": task.get("plate_text", ""),
                        "slot_id": task.get("slot_id"),
                        "slot_name": task.get("slot_name", ""),
                        "zone": task.get("zone", ""),
                        "preset": task.get("preset"),
                        "first_image": task.get("first_image", ""),
                        "first_time": task.get("first_time", ""),
                        "recheck_count": task.get("recheck_count", 0),
                    }
                task["callback"] = _make_challan_recheck_callback(pending_key)
            challan_count += 1
        elif task_type == "camera_capture":
            slot_id = task.get("slot_id")
            slot_name = task.get("slot_name", str(slot_id))
            zone = task.get("zone", "")
            ts_str = task.get("queued_at", datetime.now(timezone.utc).isoformat())
            task["callback"] = _make_camera_capture_callback(slot_id, slot_name, zone, ts_str)
            camera_count += 1
        else:
            continue

        _camera_queue.add_task(task, skip_log=True)

    log.info("Recovered %d camera tasks, %d challan rechecks from queue log",
             camera_count, challan_count)
    _camera_queue.compact_log()


def _init_alerts_from_log():
    """Populate in-memory alerts buffer and camera captures from the log on startup."""
    if not EVENT_LOG_PATH.exists():
        return
    try:
        with open(EVENT_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    event_type = obj.get("event")
                    if event_type == "slot_state_changed":
                        _alerts_buffer.append(obj)
                    elif event_type == "camera_capture":
                        key = (obj.get("slot_id"), obj.get("ts"))
                        _camera_captures[key] = {
                            "image_path": obj.get("image_path"),
                            "license_plate": obj.get("license_plate", "UNKNOWN"),
                            "license_plates": obj.get("license_plates", []),
                            "vehicle_detected": obj.get("vehicle_detected", True),
                        }
                except Exception:
                    continue
        # Keep only the last N alerts
        while len(_alerts_buffer) > _ALERTS_MAX:
            _alerts_buffer.pop(0)
        log.info("Loaded %d alerts and %d camera captures from log", len(_alerts_buffer), len(_camera_captures))
    except Exception as e:
        log.error("Error loading alerts from log: %s", e)

    # Load challan records
    _init_challan_from_log()


def _init_challan_from_log():
    """Populate in-memory challan records from log file on startup."""
    if not CHALLAN_LOG_PATH.exists():
        return
    try:
        with open(CHALLAN_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    _challan_records.append(record)
                except Exception:
                    continue
        while len(_challan_records) > _CHALLAN_MAX:
            _challan_records.pop(0)
        log.info("Loaded %d challan records from log", len(_challan_records))
    except Exception as e:
        log.error("Error loading challan records: %s", e)

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

    slots = [
        {"id": sid, "name": meta_by_id.get(sid, {}).get("name") or str(sid), "zone": meta_by_id.get(sid, {}).get("zone") or "A"}
        for sid in slot_ids
    ]
    occupied_ids = {sid for sid, st in state_by_id.items() if st == "OCCUPIED"}
    zones, free_count, total_count = _calculate_zone_stats(slot_ids, occupied_ids, meta_by_id)

    return {
        "slots": slots,
        "state_by_id": state_by_id,
        "since_by_id": since_by_id,
        "zones": zones,
        "free_count": free_count,
        "total_count": total_count,
        "recent_events": last_events[-max_events:],
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(str(APP_ROOT / "static" / "index.html"))


@app.get("/favicon.ico")
def favicon():
    """Return 204 No Content for favicon to avoid 404 errors."""
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


@app.post("/calibrate/{slot_id}")
def calibrate_slot(slot_id: int):
    """
    Trigger calibration for a specific slot.
    Sends command 'CC' (hex) to the device.
    """
    # Hex code for calibration command
    # Adjust this value if your device expects a different command
    CALIBRATION_COMMAND = "CC"
    
    success = queue_command(slot_id, CALIBRATION_COMMAND)
    
    if not success:
        if slot_id not in _device_map:
            raise HTTPException(status_code=404, detail="Device not connected or mapped yet. Wait for an uplink.")
        raise HTTPException(status_code=500, detail="Failed to queue calibration command")
        
    return {"success": True, "message": f"Calibration command queued for slot {slot_id}"}

@app.post("/setThreshold/{slot_id}/{threshold}")
def setThreshold_slot(slot_id: int, threshold: float):
    """
    Set threshold for a specific slot.
    Sends threshold in hex to the device.
    """
    # Hex code for threshold command
    # Scale to integer (multiply by 2 as before) and pack as big-endian uint16
    threshold_int = int(threshold * 2)
    threshold_hex = struct.pack(">H", threshold_int).hex()
    THRESHOLD_COMMAND = "DD" + threshold_hex
    
    success = queue_command(slot_id, THRESHOLD_COMMAND)
    
    if not success:
        if slot_id not in _device_map:
            raise HTTPException(status_code=404, detail="Device not connected or mapped yet. Wait for an uplink.")
        raise HTTPException(status_code=500, detail="Failed to queue threshold command")
        
    return {"success": True, "message": f"Threshold command queued for slot {slot_id}"}


@app.get("/events")
async def events(request: Request):
    global _active_streams

    # Check connection limit
    with _streams_lock:
        if _active_streams >= _max_streams:
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
    """Returns the current in-memory snapshot data."""
    return {"slots": dict(_slots_snapshot)}


@app.get("/alerts")
def get_alerts(limit: int = Query(default=50, le=200), offset: int = Query(default=0)):
    """
    Returns recent state change events (alerts) with optional images and license plates.
    Uses in-memory buffer — no full log scan needed.
    """
    # Filter to FREE->OCCUPIED and merge camera captures
    merged = []
    for alert in _alerts_buffer:
        if alert.get("prev_state") != "FREE" or alert.get("new_state") != "OCCUPIED":
            continue
        entry = dict(alert)  # shallow copy
        key = (alert.get("slot_id"), alert.get("ts"))
        capture = _camera_captures.get(key)
        if capture:
            if not capture.get("vehicle_detected", True):
                continue  # skip false positives
            entry["image_path"] = capture["image_path"]
            entry["license_plate"] = capture["license_plate"]
        else:
            entry["license_plate"] = "UNKNOWN"
        merged.append(entry)

    merged.sort(key=lambda x: x.get("ts", ""), reverse=True)
    total = len(merged)
    paginated = merged[offset:offset + limit]

    return {"alerts": paginated, "total": total, "limit": limit, "offset": offset}


@app.get("/challans")
def get_challans(limit: int = Query(default=100, le=500), offset: int = Query(default=0),
                 challan_only: bool = Query(default=False)):
    """
    Returns challan (violation) records.
    Each record has: plate_text, first_image, first_time, second_image, second_time, challan.
    Use challan_only=true to filter for confirmed violations only.
    """
    with _challan_lock:
        records = list(_challan_records)

    if challan_only:
        records = [r for r in records if r.get("challan")]

    records.sort(key=lambda r: r.get("first_time", ""), reverse=True)
    total = len(records)
    paginated = records[offset:offset + limit]

    return {"challans": paginated, "total": total, "limit": limit, "offset": offset}


@app.get("/challan-dashboard", response_class=HTMLResponse)
def challan_dashboard():
    """Serves the challan dashboard page."""
    return FileResponse(str(APP_ROOT / "static" / "challan.html"))


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
        log.error("Error checking camera status: %s", e)

    return {
        "enabled": True,
        "available": is_available,
        "queue_size": queue_size,
        "worker_active": worker_active,
        "camera_ip": CAMERA_IP
    }
