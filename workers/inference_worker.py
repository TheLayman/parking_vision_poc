"""Inference worker — consumes parking:inference:jobs Redis Stream.

Run 6 instances via systemd:
    python -m workers.inference_worker  (WORKER_ID set in environment)

Each instance:
  1. Reads image from disk
  2. Calls OpenAI Vision API (retry 3× on 429 with exponential backoff)
  3. Stores parking:challan:pending:{slot_id} in Redis with 5-min TTL
  4. INSERTs into camera_captures and challan_events Postgres tables
  5. For challan rechecks: compares plates and records final challan decision
  6. PUBLISHes challan_completed event to parking:events:live
  7. On 3× failure: sends to parking:inference:deadletter
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import redis

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from db.client import get_connection, insert_camera_capture, insert_challan_event
from webapp.license_plate_extractor import extract_all_license_plates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("inference_worker")

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
WORKER_ID = os.environ.get("WORKER_ID", f"worker-{os.getpid()}")

STREAM_KEY = "parking:inference:jobs"
GROUP_NAME = "inference-workers"
DEADLETTER_KEY = "parking:inference:deadletter"
CHALLAN_PENDING_TTL = 300  # 5 minutes
CHALLAN_RECHECK_DELAY = int(os.environ.get("CHALLAN_RECHECK_INTERVAL", "70"))
BLOCK_MS = 2000
AUTOCLAIM_IDLE_MS = 90_000  # reclaim inference jobs idle >90s

MAX_RETRIES = 3
_RETRY_DELAYS = [2, 8, 30]  # seconds between retries

# Plate match threshold (same as server.py POC)
_PLATE_MATCH_THRESHOLD = 0.85

SNAPSHOTS_DIR = Path(os.environ.get("SNAPSHOTS_DIR", "/data/snapshots"))


# ── Plate matching ────────────────────────────────────────────────────────────

def _plates_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    return SequenceMatcher(None, a, b).ratio() >= _PLATE_MATCH_THRESHOLD


def _any_plate_matches(plate: str, plate_list: list[str]) -> bool:
    return any(_plates_match(plate, p) for p in plate_list)


# ── OpenAI call with retry ────────────────────────────────────────────────────

def _extract_plates_with_retry(image_path: str) -> dict:
    """Call extract_all_license_plates with exponential backoff on 429."""
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            result = extract_all_license_plates(image_path)
            return result
        except Exception as e:
            last_exc = e
            err_str = str(e).lower()
            if "429" in err_str or "rate_limit" in err_str or "too many requests" in err_str:
                delay = _RETRY_DELAYS[attempt]
                log.warning("OpenAI 429 on attempt %d/%d — backing off %ds",
                            attempt + 1, MAX_RETRIES, delay)
                time.sleep(delay)
            else:
                log.error("OpenAI error on attempt %d/%d: %s", attempt + 1, MAX_RETRIES, e)
                if attempt < MAX_RETRIES - 1:
                    time.sleep(_RETRY_DELAYS[attempt])
                else:
                    break
    raise RuntimeError(f"OpenAI failed after {MAX_RETRIES} attempts: {last_exc}")


# ── Core processing ───────────────────────────────────────────────────────────

def process_inference_job(r: redis.Redis, db_conn, msg_id: bytes, fields: dict) -> bool:
    """Process one inference job. Returns True to XACK, False on retryable error."""

    def _field(key: str, default="") -> str:
        v = fields.get(key.encode()) or fields.get(key, default)
        return v.decode() if isinstance(v, bytes) else str(v or default)

    slot_id = int(_field("slot_id", "0"))
    slot_name = _field("slot_name", str(slot_id))
    zone = _field("zone", "A")
    camera_id = _field("camera_id", "CAM_01")
    image_path = _field("image_path", "")
    capture_ts_str = _field("capture_ts", datetime.now(timezone.utc).isoformat())
    trigger_ts = _field("trigger_ts", "")
    task_type = _field("task_type", "camera_capture")

    # Challan recheck context
    first_plates_raw = _field("first_plates", "")
    first_image = _field("first_image", "")
    first_time = _field("first_time", "")
    try:
        recheck_count = int(_field("recheck_count", "0"))
    except ValueError:
        recheck_count = 0
    capture_session_id = _field("capture_session_id", "") or str(uuid.uuid4())

    log.info("Inference job: slot=%s type=%s image=%s", slot_name, task_type, image_path)

    if not image_path or not Path(image_path).exists():
        log.error("Image not found: %s — sending to deadletter", image_path)
        _send_to_deadletter(r, fields, "image_not_found")
        return True  # ack — can't retry without the image

    # Call OpenAI Vision with retry
    try:
        vision_result = _extract_plates_with_retry(image_path)
    except RuntimeError as e:
        log.error("OpenAI failed for slot %s: %s — sending to deadletter", slot_name, e)
        _send_to_deadletter(r, fields, str(e))
        return True  # ack — already dead-lettered

    license_plates = [
        p.get("plate_text", "") for p in vision_result.get("plates", [])
        if p.get("plate_text") and p["plate_text"] != "UNKNOWN"
    ]
    vehicle_detected = vision_result.get("vehicle_detected", True)
    best_plate = license_plates[0] if license_plates else None
    best_conf = vision_result.get("plates", [{}])[0].get("confidence", 0.0) if license_plates else 0.0

    capture_ts = datetime.fromisoformat(capture_ts_str.replace("Z", "+00:00"))

    # INSERT camera_capture — challan scheduling is contingent on this succeeding
    capture_recorded = False
    try:
        insert_camera_capture(
            slot_id=slot_id,
            camera_id=camera_id,
            ts=capture_ts,
            image_path=image_path,
            ocr_result={
                "plates": license_plates,
                "vehicle_detected": vehicle_detected,
                "raw": vision_result,
            },
            backend="openai",
            conn=db_conn,
        )
        db_conn.commit()
        capture_recorded = True
    except Exception as e:
        log.error("Failed to insert camera_capture: %s — skipping challan scheduling", e)
        db_conn.rollback()

    if not capture_recorded:
        return True  # ack — no point scheduling recheck with no first-capture record

    if task_type == "challan_recheck":
        _process_challan_recheck(
            r=r,
            db_conn=db_conn,
            slot_id=slot_id,
            slot_name=slot_name,
            zone=zone,
            camera_id=camera_id,
            second_image=image_path,
            second_time=capture_ts_str,
            second_plates=license_plates,
            first_plates_raw=first_plates_raw,
            first_image=first_image,
            first_time=first_time,
            capture_session_id=capture_session_id,
            trigger_ts=trigger_ts,
        )
        return True

    # Standard camera_capture: store pending state + schedule recheck
    if license_plates:
        pending_data = {
            "plates": license_plates,
            "slot_name": slot_name,
            "zone": zone,
            "first_image": image_path,
            "first_time": capture_ts_str,
            "recheck_count": 0,
            "capture_session_id": capture_session_id,
            "trigger_ts": trigger_ts,
        }
        r.set(
            f"parking:challan:pending:{slot_id}",
            json.dumps(pending_data),
            ex=CHALLAN_PENDING_TTL,
        )

        # Schedule camera recheck task
        cam_assignment = _get_cam_id_for_slot(slot_id, camera_id)
        if cam_assignment:
            scheduled_at = datetime.now(timezone.utc)
            # We encode the delay in the stream message; camera_worker handles it
            from datetime import timedelta
            scheduled_ts = (
                datetime.now(timezone.utc) + timedelta(seconds=CHALLAN_RECHECK_DELAY)
            ).isoformat()

            slot_presets = _get_slot_presets(cam_assignment)
            preset = slot_presets.get(slot_id) or slot_presets.get(str(slot_id), "")

            r.xadd(
                f"parking:camera:tasks:{cam_assignment}",
                {
                    "slot_id": str(slot_id),
                    "slot_name": slot_name,
                    "zone": zone,
                    "preset": str(preset) if preset else "",
                    "trigger_ts": trigger_ts,
                    "task_type": "challan_recheck",
                    "scheduled_at": scheduled_ts,
                    "first_plates": json.dumps(license_plates),
                    "first_image": image_path,
                    "first_time": capture_ts_str,
                    "recheck_count": "0",
                    "capture_session_id": capture_session_id,
                },
                maxlen=500,
                approximate=True,
            )
            log.info("Challan recheck scheduled for slot %s in %ds", slot_name, CHALLAN_RECHECK_DELAY)
        else:
            log.warning("Cannot schedule challan recheck for slot %d — camera not found", slot_id)

    log.info("Inference complete: slot=%s plates=%s", slot_name, license_plates)
    return True


def _process_challan_recheck(
    r: redis.Redis, db_conn, slot_id: int, slot_name: str, zone: str,
    camera_id: str, second_image: str, second_time: str, second_plates: list,
    first_plates_raw: str, first_image: str, first_time: str,
    capture_session_id: str, trigger_ts: str,
):
    """Compare first and second captures to decide challan."""
    try:
        first_plates = json.loads(first_plates_raw) if first_plates_raw else []
    except Exception:
        first_plates = [first_plates_raw] if first_plates_raw else []

    log.info("Challan recheck: slot=%s first=%s second=%s",
             slot_name, first_plates, second_plates)

    for plate_text in first_plates:
        if len(plate_text) > 13:
            log.warning("Plate text too long (%d chars), truncating: %s", len(plate_text), plate_text)
            plate_text = plate_text[:13]

        is_match = _any_plate_matches(plate_text, second_plates)
        status = "confirmed" if is_match else "cleared"
        challan_id = f"{slot_id}_{capture_session_id}_{plate_text}"

        try:
            insert_challan_event(
                challan_id=challan_id,
                slot_id=slot_id,
                license_plate=plate_text,
                confidence=0.9 if is_match else 0.0,
                status=status,
                ts=datetime.now(timezone.utc),
                metadata={
                    "slot_name": slot_name,
                    "zone": zone,
                    "first_image": first_image,
                    "first_time": first_time,
                    "second_image": second_image,
                    "second_time": second_time,
                    "first_plates": first_plates,
                    "second_plates": second_plates,
                    "capture_session_id": capture_session_id,
                    "trigger_ts": trigger_ts,
                    "camera_id": camera_id,
                },
                conn=db_conn,
            )
            db_conn.commit()
            log.info("Challan %s: plate=%s slot=%s", status.upper(), plate_text, slot_name)

            # Publish only after successful commit
            live_event = {
                "event": "challan_completed",
                "ts": second_time,
                "plate_text": plate_text,
                "slot_id": slot_id,
                "slot_name": slot_name,
                "zone": zone,
                "challan": is_match,
                "capture_session_id": capture_session_id,
            }
            try:
                r.publish("parking:events:live", json.dumps(live_event))
            except Exception as e:
                log.error("Failed to publish challan_completed event: %s", e)
        except Exception as e:
            log.error("Failed to insert challan_event for %s: %s", plate_text, e)
            try:
                db_conn.rollback()
            except Exception:
                pass

    # Clear pending state
    try:
        r.delete(f"parking:challan:pending:{slot_id}")
    except Exception:
        pass


_cameras_yaml_cache: dict = {}
_cameras_yaml_mtime: float | None = None


def _get_cam_id_for_slot(slot_id: int, fallback_cam: str) -> str | None:
    """Look up which camera covers slot_id from cameras.yaml."""
    _refresh_cameras_cache()
    for cam_id, cfg in _cameras_yaml_cache.items():
        slot_presets = cfg.get("slot_presets", {})
        if slot_id in slot_presets or str(slot_id) in slot_presets:
            return cam_id
    return fallback_cam  # assume same camera as the capture


def _get_slot_presets(cam_id: str) -> dict:
    _refresh_cameras_cache()
    return _cameras_yaml_cache.get(cam_id, {}).get("slot_presets", {})


def _refresh_cameras_cache():
    global _cameras_yaml_cache, _cameras_yaml_mtime
    cameras_path = _REPO_ROOT / "config" / "cameras.yaml"
    if not cameras_path.exists():
        return
    try:
        mtime = cameras_path.stat().st_mtime
        if mtime == _cameras_yaml_mtime:
            return
        from webapp.helpers.data_io import load_yaml
        data = load_yaml(cameras_path) or {}
        _cameras_yaml_cache = data.get("cameras", {})
        _cameras_yaml_mtime = mtime
    except Exception as e:
        log.error("Failed to refresh cameras.yaml: %s", e)


def _send_to_deadletter(r: redis.Redis, fields: dict, reason: str):
    """Move a failed job to the dead-letter stream."""
    dl_fields = {k: v for k, v in fields.items()}
    dl_fields[b"deadletter_reason"] = reason.encode()
    dl_fields[b"deadletter_ts"] = datetime.now(timezone.utc).isoformat().encode()
    try:
        r.xadd(DEADLETTER_KEY, dl_fields, maxlen=1000, approximate=True)
        log.warning("Job sent to deadletter: %s", reason)
    except Exception as e:
        log.error("Failed to write to deadletter: %s", e)


# ── DB reconnect helper ───────────────────────────────────────────────────────

def _ensure_db_conn(db_conn):
    """Return a live DB connection, reconnecting if the current one is broken."""
    try:
        db_conn.execute("SELECT 1")
        return db_conn
    except Exception:
        log.warning("DB connection lost — reconnecting")
        try:
            db_conn.close()
        except Exception:
            pass
        return get_connection()


# ── Worker loop ───────────────────────────────────────────────────────────────

def run():
    log.info("Inference worker starting (consumer: %s)", WORKER_ID)

    r = redis.Redis.from_url(REDIS_URL, decode_responses=False)

    try:
        r.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
        log.info("Created consumer group %s on %s", GROUP_NAME, STREAM_KEY)
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" in str(e):
            log.debug("Consumer group %s already exists", GROUP_NAME)
        else:
            raise

    db_conn = get_connection()
    log.info("Postgres connection established")

    _running = True

    def _handle_signal(sig, frame):
        nonlocal _running
        log.info("Signal %s — shutting down inference worker", sig)
        _running = False

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    autoclaim_cursor = "0-0"

    while _running:
        # XAUTOCLAIM: reclaim jobs idle >90s
        try:
            claimed = r.xautoclaim(
                STREAM_KEY, GROUP_NAME, WORKER_ID,
                min_idle_time=AUTOCLAIM_IDLE_MS,
                start_id=autoclaim_cursor,
                count=5,
            )
            autoclaim_cursor_new = claimed[0]
            claimed_messages = claimed[1]
            if claimed_messages:
                log.info("Reclaimed %d idle inference job(s)", len(claimed_messages))
                for msg_id, fields in claimed_messages:
                    db_conn = _ensure_db_conn(db_conn)
                    ok = process_inference_job(r, db_conn, msg_id, fields)
                    if ok:
                        r.xack(STREAM_KEY, GROUP_NAME, msg_id)
            autoclaim_cursor = autoclaim_cursor_new if claimed_messages else "0-0"
        except Exception as e:
            log.error("XAUTOCLAIM error: %s", e)
            autoclaim_cursor = "0-0"

        # XREADGROUP: read new jobs
        try:
            results = r.xreadgroup(
                GROUP_NAME, WORKER_ID,
                {STREAM_KEY: ">"},
                count=1,
                block=BLOCK_MS,
            )
        except redis.exceptions.ConnectionError as e:
            log.error("Redis connection error: %s — retrying", e)
            time.sleep(2)
            try:
                r = redis.Redis.from_url(REDIS_URL, decode_responses=False)
            except Exception:
                pass
            continue
        except Exception as e:
            log.error("XREADGROUP error: %s", e)
            time.sleep(1)
            continue

        if not results:
            continue

        for stream_name, messages in results:
            for msg_id, fields in messages:
                db_conn = _ensure_db_conn(db_conn)
                ok = process_inference_job(r, db_conn, msg_id, fields)
                if ok:
                    r.xack(STREAM_KEY, GROUP_NAME, msg_id)

    log.info("Inference worker stopped")
    try:
        db_conn.close()
    except Exception:
        pass


if __name__ == "__main__":
    run()
