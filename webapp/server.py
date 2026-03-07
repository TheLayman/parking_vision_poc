from __future__ import annotations
import json
import asyncio
import base64
import logging
import struct
import time as _time
import uuid
import queue as _queue
from collections import deque, defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
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
    parse_events_from_log,
    calculate_dwell_times,
    build_dwell_distribution, build_hourly_incidents,
    build_challan_summary,
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

app = FastAPI(title="Unauthorized Parking POC", lifespan=_lifespan)
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
_ALERTS_MAX = 500
_CAMERA_CAPTURES_MAX = 1000  # cap to prevent unbounded memory growth
_alerts_buffer: deque[dict] = deque(maxlen=_ALERTS_MAX)  # state-change alerts
_camera_captures: dict[tuple, dict] = {}  # (slot_id, ts) -> capture info
_camera_captures_order: deque[tuple] = deque()  # insertion-order keys for LRU eviction

# ── Challan (parking violation) tracking ──────────────────────────────────────
# Challan re-check interval in seconds (70s × 2 checks ≈ detect >2 min stays)
CHALLAN_RECHECK_INTERVAL = int(os.getenv("CHALLAN_RECHECK_INTERVAL", "70"))
# Dedup window: skip plate if it already has a challan/pending recheck within this many seconds
CHALLAN_DEDUP_WINDOW = int(os.getenv("CHALLAN_DEDUP_WINDOW", "600"))  # 10 minutes
CHALLAN_LOG_PATH = REPO_ROOT / "data" / "challan_events.jsonl"
_CHALLAN_MAX = 1000
_challan_records: deque[dict] = deque(maxlen=_CHALLAN_MAX)  # in-memory challan buffer
_challan_lock = threading.Lock()
_challan_pending: dict[str, dict] = {}  # plate_text -> pending recheck info

_max_streams = 50
_active_streams = 0
_streams_lock = threading.Lock()

# Camera control components
_camera_queue = None
_camera_controller = None
_camera_worker_thread = None

# Inference pipeline components (decoupled from camera worker)
INFERENCE_QUEUE_MAXSIZE = int(os.getenv("INFERENCE_QUEUE_MAXSIZE", "200"))
_inference_queue = _queue.Queue(maxsize=INFERENCE_QUEUE_MAXSIZE)
_inference_worker_thread = None
_inference_shutdown_event = threading.Event()

# MQTT message processing (decoupled from paho-mqtt network thread)
_mqtt_message_queue: _queue.Queue = _queue.Queue(maxsize=500)
_mqtt_worker_thread = None
_mqtt_worker_shutdown_event = threading.Event()

# Basic runtime metrics
_metrics_lock = threading.Lock()
_pipeline_metrics = {
    "inference_jobs_enqueued": 0,
    "inference_jobs_processed": 0,
    "inference_jobs_dropped": 0,
    "inference_job_errors": 0,
    "camera_enqueue_failures": 0,
    "openai_calls": 0,
    "openai_failures": 0,
    "openai_total_ms": 0.0,
    "openai_last_ms": 0.0,
    "capture_to_plate_last_ms": 0.0,
}


def _metric_inc(name: str, delta: int = 1):
    with _metrics_lock:
        _pipeline_metrics[name] = _pipeline_metrics.get(name, 0) + delta


def _metric_set(name: str, value):
    with _metrics_lock:
        _pipeline_metrics[name] = value


def _metric_snapshot() -> dict:
    with _metrics_lock:
        return dict(_pipeline_metrics)


def _safe_qsize(qobj) -> int:
    try:
        return qobj.qsize() if qobj is not None else 0
    except Exception:
        return 0


def _enqueue_inference_job(job: dict) -> bool:
    """Enqueue a heavy inference job so camera worker stays non-blocking."""
    job.setdefault("queued_at", datetime.now(timezone.utc).isoformat())

    try:
        if _inference_queue.qsize() >= INFERENCE_QUEUE_MAXSIZE:
            try:
                _inference_queue.get_nowait()
                _metric_inc("inference_jobs_dropped")
                log.warning("Inference queue full; dropped oldest job")
            except _queue.Empty:
                pass

        _inference_queue.put(job, block=False)
        _metric_inc("inference_jobs_enqueued")
        return True
    except _queue.Full:
        _metric_inc("inference_jobs_dropped")
        log.warning("Inference queue full; failed to enqueue job")
        return False


def _cleanup_pending_recheck(pending_key: str, reason: str):
    """Remove a pending recheck entry when processing cannot continue."""
    with _challan_lock:
        _challan_pending.pop(pending_key, None)
    log.warning("Dropped pending recheck %s: %s", pending_key, reason)


def _run_batch_recheck_inference(pending_key: str, image_path: str):
    """Process challan recheck OCR and record outcomes (runs in inference worker)."""
    with _challan_lock:
        pending = _challan_pending.pop(pending_key, None)
    if pending is None:
        log.warning("Challan recheck worker: no pending entry for key %s", pending_key)
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
    new_plates = _extract_plates(second_image)["plates"]

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

    unseen_plates = [p for p in new_plates if not _any_plate_matches(p, first_plates)]
    unseen_plates = [p for p in unseen_plates if not _has_recent_challan_or_pending(p)]
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


def _inference_worker():
    """Background worker for OpenAI-heavy post-capture processing."""
    log.info("Inference worker thread started")

    if _license_plate_available:
        try:
            from webapp.license_plate_extractor import warm_up
            warm_up()
        except Exception as e:
            log.warning("OCR pre-warm failed (will warm on first job): %s", e)

    while not _inference_shutdown_event.is_set():
        try:
            job = _inference_queue.get(timeout=1.0)
        except _queue.Empty:
            continue

        started = _time.time()
        job_type = job.get("job_type", "unknown")

        try:
            if job_type == "camera_capture":
                _log_camera_capture(
                    slot_id=job["slot_id"],
                    slot_name=job["slot_name"],
                    zone=job["zone"],
                    image_path=job["image_path"],
                    timestamp_str=job["timestamp_str"],
                    mqtt_event_ts=job.get("mqtt_event_ts"),
                )

                mqtt_ts = job.get("mqtt_event_ts")
                if mqtt_ts:
                    try:
                        capture_to_plate_ms = (
                            datetime.now(timezone.utc) -
                            datetime.fromisoformat(mqtt_ts.replace("Z", "+00:00"))
                        ).total_seconds() * 1000.0
                        _metric_set("capture_to_plate_last_ms", round(capture_to_plate_ms, 2))
                    except Exception:
                        pass

            elif job_type == "challan_recheck":
                _run_batch_recheck_inference(
                    pending_key=job["pending_key"],
                    image_path=job["image_path"],
                )
            else:
                log.warning("Unknown inference job_type=%s", job_type)

            _metric_inc("inference_jobs_processed")
        except Exception as e:
            _metric_inc("inference_job_errors")
            log.error("Inference worker error for job_type=%s: %s", job_type, e)
        finally:
            elapsed_ms = (_time.time() - started) * 1000.0
            log.info("Inference job complete type=%s duration_ms=%.1f queue_depth=%d",
                     job_type, elapsed_ms, _safe_qsize(_inference_queue))

    log.info("Inference worker thread stopped")

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
    """Extract license plate strings from an image via OpenAI vision.

    Returns ``{"plates": [...], "vehicle_detected": bool}``.
    """
    if not _license_plate_available:
        log.debug("License plate extraction not available")
        return {"plates": [], "vehicle_detected": True}
    try:
        started = _time.time()
        _metric_inc("openai_calls")
        log.info("Extracting license plates from %s...", image_path)
        result = extract_all_license_plates(image_path)
        elapsed_ms = (_time.time() - started) * 1000.0
        with _metrics_lock:
            _pipeline_metrics["openai_total_ms"] += elapsed_ms
            _pipeline_metrics["openai_last_ms"] = round(elapsed_ms, 2)
        plates = [
            p.get("plate_text", "")
            for p in result.get("plates", [])
            if p.get("plate_text") and p["plate_text"] != "UNKNOWN"
        ]
        return {"plates": plates, "vehicle_detected": result.get("vehicle_detected", True)}
    except Exception as e:
        _metric_inc("openai_failures")
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
_mqtt_last_logged_occupied: set[int] | None = None  # suppress identical periodic snapshots

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
        cap_key = (slot_id, timestamp_str)
        key_is_new = cap_key not in _camera_captures
        # Always overwrite with real inference results (placeholder may already exist from callback)
        _camera_captures[cap_key] = {
            "image_path": image_path,
            "license_plate": license_plate,
            "license_plates": license_plates,
            "vehicle_detected": vehicle_detected,
        }
        if key_is_new:
            _camera_captures_order.append(cap_key)
            # Evict oldest entries when over cap
            while len(_camera_captures_order) > _CAMERA_CAPTURES_MAX:
                old_key = _camera_captures_order.popleft()
                _camera_captures.pop(old_key, None)
    except Exception as e:
        log.error("Error logging camera capture: %s", e)

    # ── Challan tracking: schedule ONE batch re-check for all detected plates ──
    if license_plates and ENABLE_CAMERA_CONTROL and _camera_queue is not None:
        # Filter out plates that already have a recent challan or pending recheck
        fresh_plates = []
        for p in license_plates:
            if _has_recent_challan_or_pending(p):
                log.info("Skipping plate %s at slot %s — already processed within %ds window",
                         p, slot_name, CHALLAN_DEDUP_WINDOW)
            else:
                fresh_plates.append(p)
        if fresh_plates:
            meta_by_id = load_slot_meta_by_id(SLOT_META_PATH)
            meta = meta_by_id.get(slot_id, {})
            preset = meta.get("preset")
            if preset:
                _schedule_slot_recheck(
                    plates=fresh_plates,
                    slot_id=slot_id,
                    slot_name=slot_name,
                    zone=zone,
                    preset=preset,
                    first_image=image_path,
                    first_time=timestamp_str,
                    capture_session_id=capture_session_id,
                    mqtt_event_ts=mqtt_event_ts,
                )


# ---------------------------------------------------------------------------
# Fuzzy license-plate matching
# ---------------------------------------------------------------------------
_PLATE_MATCH_THRESHOLD = 0.85          # min similarity ratio (0-1)

def _plates_match(a: str, b: str) -> bool:
    """Return True if two plate strings are identical or very similar.

    OCR often confuses visually similar characters (B/8, P/R, 0/O, Z/2,
    etc.).  A high SequenceMatcher ratio handles the common single- or
    double-character misread without risking false positives on genuinely
    different plates.  Plates whose lengths differ by more than 1 are
    never considered a match.
    """
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    return SequenceMatcher(None, a, b).ratio() >= _PLATE_MATCH_THRESHOLD


def _any_plate_matches(plate: str, plate_list: list[str]) -> bool:
    """Check if *plate* fuzzy-matches any entry in *plate_list*."""
    return any(_plates_match(plate, p) for p in plate_list)


def _make_batch_recheck_callback(pending_key: str):
    """Create callback for batch challan recheck capture completion.

    One camera task covers ALL plates detected in the original capture.
    This callback extracts plates from the single recheck image and
    compares against every plate in the batch, emitting one challan
    record per plate.
    """
    def callback(success, image_path, error_msg):
        if not success or not image_path:
            _cleanup_pending_recheck(pending_key, f"capture failed: {error_msg}")
            log.error("Challan recheck capture failed for %s: %s", pending_key, error_msg)
            return
        if not _enqueue_inference_job({
            "job_type": "challan_recheck",
            "pending_key": pending_key,
            "image_path": image_path,
        }):
            _cleanup_pending_recheck(pending_key, "inference queue full")

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


def _has_recent_challan_or_pending(plate_text: str) -> bool:
    """Return True if *plate_text* already has a challan record or pending
    recheck within the configured ``CHALLAN_DEDUP_WINDOW`` (default 10 min).

    This prevents the same plate from being processed multiple times when it
    is visible from overlapping camera presets or cascaded rechecks.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=CHALLAN_DEDUP_WINDOW)).isoformat()
    with _challan_lock:
        # Check completed challan records
        for rec in reversed(_challan_records):
            ft = rec.get("first_time", "")
            if ft < cutoff:
                break  # records are appended chronologically; no need to check older ones
            if _plates_match(rec.get("plate_text", ""), plate_text):
                return True
        # Check pending rechecks
        for pending in _challan_pending.values():
            if _any_plate_matches(plate_text, pending.get("plates", [])):
                return True
    return False


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

    # Safety-net dedup + append under a single lock to prevent TOCTOU duplicates
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=CHALLAN_DEDUP_WINDOW)).isoformat()
    with _challan_lock:
        for rec in reversed(_challan_records):
            if rec.get("first_time", "") < cutoff:
                break
            if _plates_match(rec.get("plate_text", ""), plate_text):
                log.info("Duplicate challan skipped for plate %s at slot %s (already recorded)",
                         plate_text, slot_name)
                return
        _challan_records.append(record)

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
            # Pre-populate so the image appears in /alerts immediately (before OCR completes).
            # license_plate will be updated to the real value once inference finishes.
            cap_key = (slot_id, timestamp_str)
            if cap_key not in _camera_captures:
                _camera_captures[cap_key] = {
                    "image_path": image_path,
                    "license_plate": "UNKNOWN",
                    "license_plates": [],
                    "vehicle_detected": True,
                }
                _camera_captures_order.append(cap_key)
                while len(_camera_captures_order) > _CAMERA_CAPTURES_MAX:
                    old_key = _camera_captures_order.popleft()
                    _camera_captures.pop(old_key, None)

            enqueued = _enqueue_inference_job({
                "job_type": "camera_capture",
                "slot_id": slot_id,
                "slot_name": slot_name,
                "zone": zone,
                "image_path": image_path,
                "timestamp_str": timestamp_str,
                "mqtt_event_ts": mqtt_event_ts,
            })
            if not enqueued:
                _metric_inc("camera_enqueue_failures")
                log.warning("Dropped camera inference job for slot %s (queue full)", slot_name)
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

            # Enqueue camera task if enabled (both FREE→OCCUPIED and OCCUPIED→FREE)
            if ENABLE_CAMERA_CONTROL and _camera_queue is not None and ((prev_state == "FREE" and state == "OCCUPIED") or (prev_state == "OCCUPIED" and state == "FREE")):
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
    global _mqtt_previous_states, _mqtt_last_snapshot_time, _state_cache, _slots_snapshot_dirty, _mqtt_last_logged_occupied

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

    has_state_changes = bool(events_to_log)
    _mqtt_previous_states = current_states.copy()

    zones_stats, free_count, total_count = calculate_zone_stats(all_configured_ids, occupied_ids, meta_by_id)

    # Log snapshot on state changes, or periodically (5 min) only when occupancy actually changed
    if _mqtt_last_snapshot_time is None:
        _mqtt_last_snapshot_time = timestamp

    periodic_due = (timestamp - _mqtt_last_snapshot_time) >= timedelta(minutes=5)
    occupancy_changed = (_mqtt_last_logged_occupied is None or occupied_ids != _mqtt_last_logged_occupied)

    if has_state_changes or (periodic_due and occupancy_changed):
        events_to_log.append({
            "event": "snapshot",
            "ts": timestamp_str,
            "occupied_ids": list(occupied_ids),
            "zone_stats": zones_stats,
            "total_count": total_count,
            "free_count": free_count
        })
        _mqtt_last_snapshot_time = timestamp
        _mqtt_last_logged_occupied = set(occupied_ids)
        # Flush in-memory snapshot to disk periodically
        _flush_snapshot_to_disk()
    elif periodic_due:
        # Occupancy unchanged — still flush in-memory state & reset timer, but skip the log line
        _mqtt_last_snapshot_time = timestamp
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


def _mqtt_worker():
    """Background worker that processes MQTT messages off the paho network thread."""
    log.info("MQTT worker thread started")
    while not _mqtt_worker_shutdown_event.is_set():
        try:
            payload = _mqtt_message_queue.get(timeout=1.0)
        except _queue.Empty:
            continue

        try:
            device_name = payload.get('deviceInfo', {}).get('deviceName', 'Unknown')

            raw_data = payload.get('data')
            if not raw_data:
                log.debug("Device %s: No data in payload", device_name)
                continue

            decoded = decode_uplink(raw_data)
            status = decoded['status']
            timestamp_str = decoded['timestamp']

            log.info("MQTT Device: %s | status: %s | ts: %s", device_name, status, timestamp_str)

            meta_by_id = load_slot_meta_by_id(SLOT_META_PATH)
            slot_id = get_slot_id_by_device_name(device_name, meta_by_id)

            if slot_id is None:
                log.warning("Device '%s' not mapped to any slot", device_name)
                continue

            app_id = payload.get('deviceInfo', {}).get('applicationId')
            dev_eui = payload.get('deviceInfo', {}).get('devEui')

            if app_id and dev_eui:
                new_info = {"applicationId": app_id, "devEui": dev_eui}
                if _device_map.get(slot_id) != new_info:
                    _device_map[slot_id] = new_info
                    _save_device_map()

            _process_mqtt_sensor_data(slot_id, status, timestamp_str, meta_by_id)

        except Exception as e:
            log.error("Error processing MQTT message: %s", e)

    log.info("MQTT worker thread stopped")


def on_mqtt_message(client, userdata, msg):
    """
    Handle incoming MQTT messages — non-blocking.
    Just parses and enqueues the payload; heavy work is done in _mqtt_worker.
    """
    try:
        payload = json.loads(msg.payload)
        _mqtt_message_queue.put_nowait(payload)
    except _queue.Full:
        log.warning("MQTT message queue full — dropping message")
    except Exception as e:
        log.error("Error queuing MQTT message: %s", e)


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
    global _camera_queue, _camera_controller, _camera_worker_thread, _slots_snapshot, _inference_worker_thread, _mqtt_worker_thread

    _shutdown_event.clear()
    _inference_shutdown_event.clear()
    _mqtt_worker_shutdown_event.clear()

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

    # Start inference worker (decouples OpenAI calls from camera worker)
    _inference_worker_thread = threading.Thread(
        target=_inference_worker,
        daemon=True,
        name="InferenceWorker",
    )
    _inference_worker_thread.start()

    _mqtt_worker_thread = threading.Thread(
        target=_mqtt_worker,
        daemon=True,
        name="MQTTWorker",
    )
    _mqtt_worker_thread.start()

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
    _inference_shutdown_event.set()
    _mqtt_worker_shutdown_event.set()

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
    if _camera_controller is not None:
        try:
            _camera_controller.close()
        except Exception:
            pass
    if _inference_worker_thread is not None and _inference_worker_thread.is_alive():
        log.info("Shutting down inference worker...")
        _inference_worker_thread.join(timeout=15)
        if _inference_worker_thread.is_alive():
            log.warning("Inference worker did not terminate within 15s")
        else:
            log.info("Inference worker stopped gracefully")
    if _mqtt_worker_thread is not None and _mqtt_worker_thread.is_alive():
        log.info("Shutting down MQTT worker...")
        _mqtt_worker_thread.join(timeout=5)
        if _mqtt_worker_thread.is_alive():
            log.warning("MQTT worker did not terminate within 5s")
        else:
            log.info("MQTT worker stopped gracefully")


def _atexit_handler():
    """Safety net for unclean exits (Ctrl+C, kill) that bypass the FastAPI lifespan."""
    _shutdown_event.set()
    _inference_shutdown_event.set()
    _mqtt_worker_shutdown_event.set()
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
            _camera_captures_order.append(key)
    # Evict oldest camera captures over cap
    while len(_camera_captures_order) > _CAMERA_CAPTURES_MAX:
        old_key = _camera_captures_order.popleft()
        _camera_captures.pop(old_key, None)
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
    meta_by_id = load_slot_meta_by_id(SLOT_META_PATH)
    slot_ids = sorted(meta_by_id.keys())
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
def analytics_summary(range: str = Query(default="24h"),
                      zone: str = Query(default=None)):
    """Returns unauthorized parking analytics data."""
    time_deltas = {"1h": timedelta(hours=1), "6h": timedelta(hours=6), "24h": timedelta(hours=24), "7d": timedelta(days=7), "all": None}
    delta = time_deltas.get(range)
    now_ts = datetime.now(timezone.utc)
    cutoff = (now_ts - delta) if delta else None

    parsed = parse_events_from_log(EVENT_LOG_PATH, cutoff)
    snapshots = parsed["snapshots"]
    state_changes = parsed["state_changes"]
    challans = parsed["challans"]

    # Collect available zones
    all_zones = sorted({sc["zone"] for sc in state_changes} | {c["zone"] for c in challans})

    # Filter by zone if requested
    if zone:
        state_changes_filtered = [sc for sc in state_changes if sc["zone"] == zone]
    else:
        state_changes_filtered = state_changes

    # Total unauthorized incidents = FREE→OCCUPIED transitions
    incidents = [sc for sc in state_changes_filtered
                 if sc.get("prev_state") == "FREE" and sc.get("new_state") == "OCCUPIED"]
    total_incidents = len(incidents)

    # Dwell times (always compute from full data, then filter)
    dwell_result = calculate_dwell_times(state_changes)
    all_dwells = dwell_result["all_dwells"]

    # Average parking time for the selected zone
    dwells_filtered = all_dwells if zone is None else [d for d in all_dwells if d["zone"] == zone]
    avg_parking_minutes = round(sum(d["minutes"] for d in dwells_filtered) / len(dwells_filtered), 1) if dwells_filtered else 0

    # Dwell distribution buckets
    dwell_distribution = build_dwell_distribution(all_dwells, zone=zone)

    # Hourly incidents series (continuous buckets for bounded ranges)
    hourly_incidents = build_hourly_incidents(
        state_changes,
        start=cutoff if delta else None,
        end=now_ts if delta else None,
    )

    # Challan summary
    challan_summary = build_challan_summary(challans, zone=zone)

    # Pre-group data by zone to avoid O(zones × n) rescans
    incidents_by_zone: dict[str, int] = defaultdict(int)
    for sc in state_changes:
        if sc.get("prev_state") == "FREE" and sc.get("new_state") == "OCCUPIED":
            incidents_by_zone[sc["zone"]] += 1
    dwells_by_zone: dict[str, list[float]] = defaultdict(list)
    for d in all_dwells:
        dwells_by_zone[d["zone"]].append(d["minutes"])
    challans_by_zone: dict[str, list[dict]] = defaultdict(list)
    for c in challans:
        challans_by_zone[c.get("zone", "A")].append(c)

    # Per-zone stats
    zone_stats = {}
    for z in all_zones:
        z_dwell_list = dwells_by_zone[z]
        z_avg = round(sum(z_dwell_list) / len(z_dwell_list), 1) if z_dwell_list else 0
        z_challans = challans_by_zone[z]
        z_confirmed = sum(1 for c in z_challans if c.get("challan"))
        gt_15 = gt_30 = gt_45 = gt_60 = 0
        for m in z_dwell_list:
            if m > 15: gt_15 += 1
            if m > 30: gt_30 += 1
            if m > 45: gt_45 += 1
            if m > 60: gt_60 += 1
        zone_stats[z] = {
            "total_incidents": incidents_by_zone[z],
            "avg_parking_minutes": z_avg,
            "challans_generated": z_confirmed,
            "dwell_distribution": {"gt_15m": gt_15, "gt_30m": gt_30, "gt_45m": gt_45, "gt_1h": gt_60},
        }

    return {
        "total_incidents": total_incidents,
        "avg_parking_minutes": avg_parking_minutes,
        "challans_generated": challan_summary["confirmed"],
        # by_zone omitted here — per-zone breakdown is already in zone_stats
        "challan_summary": {k: v for k, v in challan_summary.items() if k != "by_zone"},
        "dwell_distribution": dwell_distribution,
        "hourly_incidents": hourly_incidents,
        "zones": all_zones,
        "zone_stats": zone_stats,
        "time_range": range,
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
            entry["image_path"] = capture["image_path"]
            entry["vehicle_detected"] = capture.get("vehicle_detected", True)
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
    inference_queue_size = 0
    inference_worker_active = False

    try:
        if _camera_controller:
            is_available = _camera_controller.is_available()
        if _camera_queue:
            queue_size = _camera_queue.queue.qsize()
        if _camera_worker_thread:
            worker_active = _camera_worker_thread.is_alive()
        inference_queue_size = _safe_qsize(_inference_queue)
        if _inference_worker_thread:
            inference_worker_active = _inference_worker_thread.is_alive()
    except Exception as e:
        log.error("Error checking camera status: %s", e)

    metrics = _metric_snapshot()
    openai_avg_ms = round(metrics["openai_total_ms"] / metrics["openai_calls"], 2) if metrics["openai_calls"] else 0.0

    return {
        "enabled": True,
        "available": is_available,
        "queue_size": queue_size,
        "worker_active": worker_active,
        "camera_ip": CAMERA_IP,
        "inference_queue_size": inference_queue_size,
        "inference_worker_active": inference_worker_active,
        "metrics": {
            "inference_jobs_enqueued": metrics["inference_jobs_enqueued"],
            "inference_jobs_processed": metrics["inference_jobs_processed"],
            "inference_jobs_dropped": metrics["inference_jobs_dropped"],
            "inference_job_errors": metrics["inference_job_errors"],
            "openai_calls": metrics["openai_calls"],
            "openai_failures": metrics["openai_failures"],
            "openai_last_ms": metrics["openai_last_ms"],
            "openai_avg_ms": openai_avg_ms,
            "capture_to_plate_last_ms": metrics["capture_to_plate_last_ms"],
        }
    }
