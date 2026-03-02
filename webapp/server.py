from __future__ import annotations
import json
import asyncio
import base64
import logging
import struct
import time as _time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import paho.mqtt.client as mqtt
from fastapi import FastAPI, Query, Request, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import os
import atexit

from webapp.helpers.data_io import (
    append_jsonl, append_jsonl_batch, load_jsonl_records,
    load_yaml, save_yaml, rotate_log_if_needed,
)
from webapp.helpers.slot_meta import (
    load_slot_meta_by_id, load_slot_ids, get_slot_id_by_device_name,
    calculate_zone_stats, build_state_from_log,
)
from webapp.helpers.analytics import (
    parse_events_from_log, build_occupancy_series,
    calculate_dwell_times, predict_occupancy,
)

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
_slots_snapshot: dict[str, dict] = {}
_slots_snapshot_dirty = False

# In-memory alerts ring buffer (avoids full log scan on /alerts)
_alerts_buffer: list[dict] = []  # state-change alerts
_camera_captures: dict[tuple, dict] = {}  # (slot_id, ts) -> capture info
_ALERTS_MAX = 500

# ── Challan (parking violation) tracking ──────────────────────────────────────
# Challan re-check interval in seconds (70s × 2 checks ≈ detect >2 min stays)
CHALLAN_RECHECK_INTERVAL = int(os.getenv("CHALLAN_RECHECK_INTERVAL", "70"))
CHALLAN_LOG_PATH = REPO_ROOT / "data" / "challan_events.jsonl"
_challan_records: list[dict] = []  # in-memory challan buffer
_challan_lock = threading.Lock()
_challan_pending: dict[str, dict] = {}  # plate_text -> pending recheck info
_CHALLAN_MAX = 1000

_max_streams = 50
_active_streams = 0
_streams_lock = threading.Lock()

# Camera control components
_camera_queue = None
_camera_controller = None
_camera_worker_thread = None

def load_snapshot_data() -> dict:
    """Load snapshot data from YAML file (cold-start only)."""
    try:
        data = load_yaml(SNAPSHOT_PATH)
        return data if data else {"slots": {}}
    except Exception as e:
        log.error("Error reading snapshot file: %s", e)
        return {"slots": {}}

def save_snapshot_data(data: dict):
    """Save snapshot data to YAML file."""
    save_yaml(SNAPSHOT_PATH, data)


def _extract_plates(image_path: str) -> dict:
    """Extract license plate strings from an image via GPT-4o vision.

    Returns ``{"plates": [...], "vehicle_detected": bool}``.
    """
    if not _license_plate_available:
        log.debug("License plate extraction not available")
        return {"plates": [], "vehicle_detected": True}
    try:
        log.info("Extracting license plates from %s...", image_path)
        result = extract_all_license_plates(image_path)
        plates = [
            p.get("plate_text", "")
            for p in result.get("plates", [])
            if p.get("plate_text") and p["plate_text"] != "UNKNOWN"
        ]
        return {"plates": plates, "vehicle_detected": result.get("vehicle_detected", True)}
    except Exception as e:
        log.error("Error extracting license plates: %s", e)
        return {"plates": [], "vehicle_detected": True}


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
_mqtt_previous_states: dict[int, str] = {}
_mqtt_last_snapshot_time = None

# Device mapping for command queuing: slot_id -> {"applicationId": str, "devEui": str}
_device_map: dict[int, dict] = {}


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

        meta_by_id = load_slot_meta_by_id(SLOT_META_PATH)
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
                slot_id = get_slot_id_by_device_name(dev_name, meta_by_id)
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


def _log_camera_capture(slot_id: int, slot_name: str, zone: str, image_path: str,
                        timestamp_str: str, mqtt_event_ts: str | None = None):
    """Log camera capture result to event log with license plate extraction and challan tracking."""
    capture_session_id = str(uuid.uuid4())
    extraction = _extract_plates(image_path)
    license_plates = extraction["plates"]
    vehicle_detected = extraction["vehicle_detected"]
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
        "vehicle_detected": vehicle_detected,
        "capture_session_id": capture_session_id,
    }
    if mqtt_event_ts:
        event["mqtt_event_ts"] = mqtt_event_ts
    try:
        append_jsonl(EVENT_LOG_PATH, event, lock=_event_log_lock)
        _camera_captures[(slot_id, timestamp_str)] = {
            "image_path": image_path,
            "license_plate": license_plate,
            "license_plates": license_plates,
            "vehicle_detected": vehicle_detected,
        }
    except Exception as e:
        log.error("Error logging camera capture: %s", e)

    # ── Challan tracking: schedule ONE batch re-check for all detected plates ──
    if license_plates and ENABLE_CAMERA_CONTROL and _camera_queue is not None:
        meta_by_id = load_slot_meta_by_id(SLOT_META_PATH)
        meta = meta_by_id.get(slot_id, {})
        preset = meta.get("preset")
        if preset:
            _schedule_slot_recheck(
                plates=license_plates,
                slot_id=slot_id,
                slot_name=slot_name,
                zone=zone,
                preset=preset,
                first_image=image_path,
                first_time=timestamp_str,
                capture_session_id=capture_session_id,
                mqtt_event_ts=mqtt_event_ts,
            )


def _make_batch_recheck_callback(pending_key: str):
    """Create callback for batch challan recheck capture completion.

    One camera task covers ALL plates detected in the original capture.
    This callback extracts plates from the single recheck image and
    compares against every plate in the batch, emitting one challan
    record per plate.
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

        first_plates = pending["plates"]
        slot_id = pending["slot_id"]
        slot_name = pending["slot_name"]
        zone = pending["zone"]
        preset = pending["preset"]
        first_image = pending["first_image"]
        first_time = pending["first_time"]
        recheck_count = pending.get("recheck_count", 0)
        capture_session_id = pending.get("capture_session_id")
        mqtt_event_ts = pending.get("mqtt_event_ts")

        log.info("Batch challan recheck #%d complete for %d plate(s) at slot %s",
                 recheck_count + 1, len(first_plates), slot_name)

        ts_str = datetime.now(timezone.utc).isoformat()
        second_image = image_path

        # Extract plates from second capture (ONE GPT-4o call for all plates)
        new_plates = _extract_plates(second_image)["plates"]

        # Compare each original plate against the second image
        for plate_text in first_plates:
            is_match = plate_text in new_plates
            _record_challan(
                plate_text=plate_text,
                slot_id=slot_id,
                slot_name=slot_name,
                zone=zone,
                first_image=first_image,
                first_time=first_time,
                second_image=second_image,
                second_time=ts_str,
                challan=is_match,
                first_plates=first_plates,
                second_plates=new_plates,
                capture_session_id=capture_session_id,
                mqtt_event_ts=mqtt_event_ts,
            )

        # Schedule batch recheck for NEW plates not in original list (max 1 re-recheck)
        unseen_plates = [p for p in new_plates if p not in first_plates]
        if unseen_plates and recheck_count < 1:
            new_session_id = str(uuid.uuid4())
            _schedule_slot_recheck(
                plates=unseen_plates,
                slot_id=slot_id,
                slot_name=slot_name,
                zone=zone,
                preset=preset,
                first_image=second_image,
                first_time=ts_str,
                capture_session_id=new_session_id,
                mqtt_event_ts=mqtt_event_ts,
                recheck_count=recheck_count + 1,
            )

    return callback


def _schedule_slot_recheck(plates: list, slot_id: int, slot_name: str,
                           zone: str, preset: int, first_image: str,
                           first_time: str, capture_session_id: str | None = None,
                           mqtt_event_ts: str | None = None,
                           recheck_count: int = 0):
    """Schedule a SINGLE re-check for ALL detected plates at a slot.

    Instead of enqueueing N tasks (one per plate), this enqueues ONE
    ``challan_recheck`` task carrying the full plates list.  The camera
    worker performs one move/settle/capture cycle and the batch callback
    compares all plates against the single second image.

    The pending key uses ``{slot_id}_{capture_session_id}`` to avoid
    collisions when the same plate re-occupies the same slot.
    """
    if not capture_session_id:
        capture_session_id = str(uuid.uuid4())

    key = f"{slot_id}_{capture_session_id}"
    with _challan_lock:
        _challan_pending[key] = {
            "plates": list(plates),
            "slot_id": slot_id,
            "slot_name": slot_name,
            "zone": zone,
            "preset": preset,
            "first_image": first_image,
            "first_time": first_time,
            "recheck_count": recheck_count,
            "capture_session_id": capture_session_id,
            "mqtt_event_ts": mqtt_event_ts,
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
        "plates": list(plates),
        "first_image": first_image,
        "first_time": first_time,
        "recheck_count": recheck_count,
        "capture_session_id": capture_session_id,
        "callback": _make_batch_recheck_callback(key),
    })
    log.info("Batch challan recheck scheduled for %d plate(s) at slot %s in %ds (via camera queue)",
             len(plates), slot_name, CHALLAN_RECHECK_INTERVAL)


def _record_challan(plate_text: str, slot_id: int, slot_name: str, zone: str,
                     first_image: str, first_time: str,
                     second_image: str, second_time: str,
                     challan: bool, second_plates: list = None,
                     first_plates: list = None,
                     capture_session_id: str = None,
                     mqtt_event_ts: str = None):
    """Write a challan record to disk and keep in memory.

    Also broadcasts a ``challan_completed`` event to the SSE log so
    the challan dashboard can update in near-real-time.
    """
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
    # Attach optional audit fields when present
    for key, val in [("first_plates", first_plates), ("second_plates", second_plates),
                     ("capture_session_id", capture_session_id), ("mqtt_event_ts", mqtt_event_ts)]:
        if val is not None:
            record[key] = val

    with _challan_lock:
        _challan_records.append(record)
        if len(_challan_records) > _CHALLAN_MAX:
            _challan_records.pop(0)

    # Persist to file
    try:
        append_jsonl(CHALLAN_LOG_PATH, record)
        status = "CHALLAN" if challan else "cleared"
        log.info("Challan record: plate=%s slot=%s %s", plate_text, slot_name, status)
    except Exception as e:
        log.error("Error writing challan record: %s", e)

    # Broadcast challan_completed event via SSE
    sse_event = {
        "event": "challan_completed",
        "ts": second_time,
        "plate_text": plate_text,
        "slot_id": slot_id,
        "slot_name": slot_name,
        "zone": zone,
        "challan": challan,
    }
    if capture_session_id:
        sse_event["capture_session_id"] = capture_session_id
    try:
        append_jsonl(EVENT_LOG_PATH, sse_event, lock=_event_log_lock)
    except Exception as e:
        log.error("Error broadcasting challan_completed event: %s", e)


def _make_camera_capture_callback(slot_id: int, slot_name: str, zone: str,
                                  timestamp_str: str, mqtt_event_ts: str | None = None):
    """Create callback for camera capture completion."""
    def callback(success, image_path, error_msg):
        if success and image_path:
            _log_camera_capture(slot_id, slot_name, zone, image_path, timestamp_str,
                                mqtt_event_ts=mqtt_event_ts)
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
                        # Pass the MQTT event timestamp so LoRa delay can be measured
                        mqtt_event_ts = timestamp_str

                        _camera_queue.add_task({
                            "slot_id": sid,
                            "slot_name": slot_name,
                            "zone": zone,
                            "preset": preset,
                            "timestamp": timestamp_obj,
                            "event_id": f"{sid}_{timestamp_str}",
                            "callback": _make_camera_capture_callback(
                                sid, slot_name, zone, timestamp_str,
                                mqtt_event_ts=mqtt_event_ts)
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


def _log_events_to_file(events: list):
    """Write events to event log file."""
    if not events:
        return
    rotate_log_if_needed(EVENT_LOG_PATH, max_size_mb=50)
    append_jsonl_batch(EVENT_LOG_PATH, events, lock=_event_log_lock)


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


def _process_mqtt_sensor_data(slot_id: int, status: str, timestamp_str: str, meta_by_id: dict[int, dict]):
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

    zones_stats, free_count, total_count = calculate_zone_stats(all_configured_ids, occupied_ids, meta_by_id)

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
        meta_by_id = load_slot_meta_by_id(SLOT_META_PATH)
        slot_id = get_slot_id_by_device_name(device_name, meta_by_id)
        
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
                # Support both old single-plate and new batch format
                plates = task.get("plates")
                if not plates:
                    # Legacy recovery: single plate_text → wrap in list
                    pt = task.get("plate_text", "")
                    plates = [pt] if pt else []
                with _challan_lock:
                    _challan_pending[pending_key] = {
                        "plates": plates,
                        "slot_id": task.get("slot_id"),
                        "slot_name": task.get("slot_name", ""),
                        "zone": task.get("zone", ""),
                        "preset": task.get("preset"),
                        "first_image": task.get("first_image", ""),
                        "first_time": task.get("first_time", ""),
                        "recheck_count": task.get("recheck_count", 0),
                        "capture_session_id": task.get("capture_session_id"),
                        "mqtt_event_ts": task.get("mqtt_event_ts"),
                    }
                task["callback"] = _make_batch_recheck_callback(pending_key)
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
    records = load_jsonl_records(EVENT_LOG_PATH, _ALERTS_MAX * 10)  # load enough to find alerts
    for obj in records:
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
    # Keep only the last N alerts
    while len(_alerts_buffer) > _ALERTS_MAX:
        _alerts_buffer.pop(0)
    log.info("Loaded %d alerts and %d camera captures from log", len(_alerts_buffer), len(_camera_captures))

    # Load challan records
    _init_challan_from_log()


def _init_challan_from_log():
    """Populate in-memory challan records from log file on startup."""
    loaded = load_jsonl_records(CHALLAN_LOG_PATH, _CHALLAN_MAX)
    _challan_records.extend(loaded)
    if loaded:
        log.info("Loaded %d challan records from log", len(loaded))


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
    slot_ids = load_slot_ids(SLOT_META_PATH)
    meta_by_id = load_slot_meta_by_id(SLOT_META_PATH)
    result = build_state_from_log(EVENT_LOG_PATH, slot_ids=slot_ids, meta_by_id=meta_by_id)

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

@app.get("/analytics/summary")
def analytics_summary(range: str = Query(default="24h")):
    """Returns analytics data: occupancy series, dwell stats, predictions, summary."""
    time_deltas = {"1h": timedelta(hours=1), "6h": timedelta(hours=6), "24h": timedelta(hours=24), "7d": timedelta(days=7), "all": None}
    delta = time_deltas.get(range)
    cutoff = (datetime.now(timezone.utc) - delta) if delta else None

    snapshots, state_changes = parse_events_from_log(EVENT_LOG_PATH, cutoff)
    occupancy_series = build_occupancy_series(snapshots)
    dwell_stats = calculate_dwell_times(state_changes)
    predictions = predict_occupancy(occupancy_series)

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
            entry["license_plates"] = capture.get("license_plates", [])
        else:
            entry["license_plate"] = "UNKNOWN"
            entry["license_plates"] = []
        merged.append(entry)

    merged.sort(key=lambda x: x.get("ts", ""), reverse=True)
    total = len(merged)
    paginated = merged[offset:offset + limit]

    return {"alerts": paginated, "total": total, "limit": limit, "offset": offset}


@app.get("/challans")
def get_challans(limit: int = Query(default=100, le=500), offset: int = Query(default=0),
                 challan_only: bool = Query(default=False),
                 zone: str = Query(default=None),
                 since: str = Query(default=None)):
    """
    Returns challan (violation) records.
    Each record has: plate_text, first_image, first_time, second_image, second_time, challan,
    first_plates, second_plates, capture_session_id, mqtt_event_ts.
    Use challan_only=true to filter for confirmed violations only.
    Use zone=X to filter by zone. Use since=ISO8601 to filter by date.
    """
    with _challan_lock:
        records = list(_challan_records)

    if challan_only:
        records = [r for r in records if r.get("challan")]

    if zone:
        records = [r for r in records if r.get("zone") == zone]

    if since:
        records = [r for r in records if r.get("first_time", "") >= since]

    records.sort(key=lambda r: r.get("first_time", ""), reverse=True)
    total = len(records)
    paginated = records[offset:offset + limit]

    return {"challans": paginated, "total": total, "limit": limit, "offset": offset}


@app.get("/challans/pending")
def get_challans_pending():
    """Returns currently pending challan rechecks with countdown info."""
    now = datetime.now(timezone.utc)
    pending_list = []
    with _challan_lock:
        for key, info in _challan_pending.items():
            entry = {
                "pending_key": key,
                "slot_id": info.get("slot_id"),
                "slot_name": info.get("slot_name"),
                "zone": info.get("zone"),
                "plates": info.get("plates", []),
                "first_time": info.get("first_time"),
                "capture_session_id": info.get("capture_session_id"),
            }
            pending_list.append(entry)
    return {"pending": pending_list, "count": len(pending_list)}


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
