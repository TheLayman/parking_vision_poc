"""Inference worker — consumes parking:inference:jobs Redis Stream.

Run 6 instances via systemd:
    python -m workers.inference_worker  (WORKER_ID set in environment)

Each instance:
  1. Reads image from disk
  2. Calls OpenAI Vision API (retry 3x on 429 with exponential backoff)
  3. Stores parking:challan:pending:{slot_id} in Redis with 5-min TTL
  4. INSERTs into camera_captures and challan_events Postgres tables
  5. For challan rechecks: compares plates and records final challan decision
  6. PUBLISHes challan_completed event to parking:events:live
  7. On 3x failure: sends to parking:inference:deadletter
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import redis

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from db.client import insert_camera_capture, insert_challan_event
from webapp.license_plate_extractor import extract_all_license_plates
from workers.base import (
    stream_field, run_stream_worker, get_cam_for_slot, get_slot_presets,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("inference_worker")

# ── Config ────────────────────────────────────────────────────────────────────

WORKER_ID = os.environ.get("WORKER_ID", f"worker-{os.getpid()}")

STREAM_KEY = "parking:inference:jobs"
GROUP_NAME = "inference-workers"
DEADLETTER_KEY = "parking:inference:deadletter"
CHALLAN_PENDING_TTL = 300  # 5 minutes
CHALLAN_RECHECK_DELAY = int(os.environ.get("CHALLAN_RECHECK_INTERVAL", "70"))

MAX_RETRIES = 3
_RETRY_DELAYS = [2, 8, 30]
_PLATE_MATCH_THRESHOLD = 0.85


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
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            return extract_all_license_plates(image_path)
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
    """Process one inference job. Returns True to XACK."""
    _f = lambda key, default="": stream_field(fields, key, default)

    slot_id = int(_f("slot_id", "0"))
    slot_name = _f("slot_name", str(slot_id))
    zone = _f("zone", "A")
    camera_id = _f("camera_id", "CAM_01")
    image_path = _f("image_path", "")
    capture_ts_str = _f("capture_ts", datetime.now(timezone.utc).isoformat())
    trigger_ts = _f("trigger_ts", "")
    task_type = _f("task_type", "camera_capture")

    # Challan recheck context
    first_plates_raw = _f("first_plates", "")
    first_image = _f("first_image", "")
    first_time = _f("first_time", "")
    capture_session_id = _f("capture_session_id", "") or str(uuid.uuid4())

    # GPS coordinates (forwarded through the stream)
    lat_str = _f("lat", "")
    lng_str = _f("lng", "")
    slot_lat = float(lat_str) if lat_str else None
    slot_lng = float(lng_str) if lng_str else None

    log.info("Inference job: slot=%s type=%s image=%s", slot_name, task_type, image_path)

    if not image_path or not Path(image_path).exists():
        log.error("Image not found: %s — sending to deadletter", image_path)
        _send_to_deadletter(r, fields, "image_not_found")
        return True

    try:
        vision_result = _extract_plates_with_retry(image_path)
    except RuntimeError as e:
        log.error("OpenAI failed for slot %s: %s — deadlettering", slot_name, e)
        _send_to_deadletter(r, fields, str(e))
        return True

    license_plates = [
        p.get("plate_text", "") for p in vision_result.get("plates", [])
        if p.get("plate_text") and p["plate_text"] != "UNKNOWN"
    ]
    vehicle_detected = vision_result.get("vehicle_detected", True)
    capture_ts = datetime.fromisoformat(capture_ts_str.replace("Z", "+00:00"))

    # INSERT camera_capture
    try:
        insert_camera_capture(
            slot_id=slot_id, camera_id=camera_id, ts=capture_ts,
            image_path=image_path,
            ocr_result={"plates": license_plates, "vehicle_detected": vehicle_detected, "raw": vision_result},
            backend="openai", conn=db_conn,
        )
        db_conn.commit()
    except Exception as e:
        log.error("Failed to insert camera_capture: %s — skipping challan", e)
        db_conn.rollback()
        return True

    if task_type == "challan_recheck":
        _process_challan_recheck(
            r, db_conn, slot_id, slot_name, zone, camera_id,
            second_image=image_path, second_time=capture_ts_str,
            second_plates=license_plates, first_plates_raw=first_plates_raw,
            first_image=first_image, first_time=first_time,
            capture_session_id=capture_session_id, trigger_ts=trigger_ts,
            slot_lat=slot_lat, slot_lng=slot_lng,
        )
        return True

    # Standard capture: store pending state + schedule recheck
    if license_plates:
        pending_data = {
            "plates": license_plates, "slot_name": slot_name, "zone": zone,
            "first_image": image_path, "first_time": capture_ts_str,
            "capture_session_id": capture_session_id, "trigger_ts": trigger_ts,
        }
        r.set(f"parking:challan:pending:{slot_id}", json.dumps(pending_data),
              ex=CHALLAN_PENDING_TTL)

        cam_assignment = get_cam_for_slot(slot_id, camera_id)
        if cam_assignment:
            scheduled_ts = (
                datetime.now(timezone.utc) + timedelta(seconds=CHALLAN_RECHECK_DELAY)
            ).isoformat()

            presets = get_slot_presets(cam_assignment)
            preset = presets.get(slot_id) or presets.get(str(slot_id), "")

            task_fields = {
                "slot_id": str(slot_id), "slot_name": slot_name,
                "zone": zone, "preset": str(preset) if preset else "",
                "trigger_ts": trigger_ts, "task_type": "challan_recheck",
                "scheduled_at": scheduled_ts,
                "first_plates": json.dumps(license_plates),
                "first_image": image_path, "first_time": capture_ts_str,
                "capture_session_id": capture_session_id,
            }
            if slot_lat is not None:
                task_fields["lat"] = str(slot_lat)
            if slot_lng is not None:
                task_fields["lng"] = str(slot_lng)
            r.xadd(f"parking:camera:tasks:{cam_assignment}", task_fields,
                    maxlen=500, approximate=True)
            log.info("Challan recheck scheduled for slot %s in %ds", slot_name, CHALLAN_RECHECK_DELAY)

    log.info("Inference complete: slot=%s plates=%s", slot_name, license_plates)
    return True


def _process_challan_recheck(
    r: redis.Redis, db_conn, slot_id: int, slot_name: str, zone: str,
    camera_id: str, second_image: str, second_time: str, second_plates: list,
    first_plates_raw: str, first_image: str, first_time: str,
    capture_session_id: str, trigger_ts: str,
    slot_lat: float | None = None, slot_lng: float | None = None,
):
    """Compare first and second captures to decide challan."""
    try:
        first_plates = json.loads(first_plates_raw) if first_plates_raw else []
    except Exception:
        first_plates = [first_plates_raw] if first_plates_raw else []

    log.info("Challan recheck: slot=%s first=%s second=%s", slot_name, first_plates, second_plates)

    for plate_text in first_plates:
        if len(plate_text) > 13:
            plate_text = plate_text[:13]

        is_match = _any_plate_matches(plate_text, second_plates)
        status = "confirmed" if is_match else "cleared"
        challan_id = f"{slot_id}_{capture_session_id}_{plate_text}"

        try:
            insert_challan_event(
                challan_id=challan_id, slot_id=slot_id,
                license_plate=plate_text,
                confidence=0.9 if is_match else 0.0,
                status=status, ts=datetime.now(timezone.utc),
                metadata={
                    "slot_name": slot_name, "zone": zone,
                    "first_image": first_image, "first_time": first_time,
                    "second_image": second_image, "second_time": second_time,
                    "first_plates": first_plates, "second_plates": second_plates,
                    "capture_session_id": capture_session_id,
                    "trigger_ts": trigger_ts, "camera_id": camera_id,
                    "lat": slot_lat, "lng": slot_lng,
                },
                conn=db_conn,
            )
            db_conn.commit()
            log.info("Challan %s: plate=%s slot=%s", status.upper(), plate_text, slot_name)

            try:
                r.publish("parking:events:live", json.dumps({
                    "event": "challan_completed", "ts": second_time,
                    "plate_text": plate_text, "slot_id": slot_id,
                    "slot_name": slot_name, "zone": zone,
                    "challan": is_match, "capture_session_id": capture_session_id,
                }))
            except Exception as e:
                log.error("Failed to publish challan_completed: %s", e)
        except Exception as e:
            log.error("Failed to insert challan_event for %s: %s", plate_text, e)
            try:
                db_conn.rollback()
            except Exception:
                pass

    try:
        r.delete(f"parking:challan:pending:{slot_id}")
    except Exception:
        pass


def _send_to_deadletter(r: redis.Redis, fields: dict, reason: str):
    dl_fields = dict(fields)
    dl_fields[b"deadletter_reason"] = reason.encode()
    dl_fields[b"deadletter_ts"] = datetime.now(timezone.utc).isoformat().encode()
    try:
        r.xadd(DEADLETTER_KEY, dl_fields, maxlen=1000, approximate=True)
        log.warning("Job sent to deadletter: %s", reason)
    except Exception as e:
        log.error("Failed to write to deadletter: %s", e)


# ── Entry point ──────────────────────────────────────────────────────────────

def run():
    log.info("Inference worker starting (consumer: %s)", WORKER_ID)
    run_stream_worker(
        stream_key=STREAM_KEY,
        group_name=GROUP_NAME,
        worker_id=WORKER_ID,
        process_fn=process_inference_job,
        autoclaim_idle_ms=90_000,
        autoclaim_count=5,
        xread_count=1,
        block_ms=2000,
        needs_db=True,
        worker_label="Inference worker",
    )


if __name__ == "__main__":
    run()
