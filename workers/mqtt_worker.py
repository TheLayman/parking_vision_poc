"""MQTT worker — consumes parking:mqtt:events Redis Stream.

Run 4 instances via systemd:
    python -m workers.mqtt_worker  (WORKER_ID set in environment)

Each instance is a member of the 'mqtt-processors' consumer group.
Atomically transitions slot state using a Lua CAS script.
On winning a FREE→OCCUPIED or OCCUPIED→FREE transition:
  - INSERTs occupancy_events into Postgres
  - XADDs a camera task to parking:camera:tasks:{CAM_ID}
  - PUBLISHes a live event to parking:events:live
"""

from __future__ import annotations

import base64
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import redis

# Ensure the repo root is in the Python path when run as a module
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from db.client import get_connection, insert_occupancy_event
from webapp.helpers.slot_meta import load_slot_meta_by_id, get_slot_id_by_device_name
from webapp.helpers.data_io import load_yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("mqtt_worker")

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
SLOT_META_PATH = _REPO_ROOT / "config" / "slot_meta.yaml"
CAMERAS_YAML_PATH = _REPO_ROOT / "config" / "cameras.yaml"

STREAM_KEY = "parking:mqtt:events"
GROUP_NAME = "mqtt-processors"
WORKER_ID = os.environ.get("WORKER_ID", f"worker-{os.getpid()}")
BLOCK_MS = 1000            # XREADGROUP block timeout
AUTOCLAIM_IDLE_MS = 10_000  # reclaim messages idle >10s

# ── Lua CAS script: atomically transition slot state ─────────────────────────
# KEYS[1] = parking:slot:state  (Hash)
# ARGV[1] = slot_id field name
# ARGV[2] = expected state (FREE or OCCUPIED)
# ARGV[3] = new state (OCCUPIED or FREE)
# Returns 1 on success, 0 on conflict.
_CAS_SCRIPT = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
if not current then current = 'FREE' end
if current == ARGV[2] then
    redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
    return 1
end
return 0
"""

# ── Camera assignment cache ───────────────────────────────────────────────────
_slot_to_camera: dict[int, str] = {}
_cameras_yaml_mtime: float | None = None


def _load_camera_assignment() -> dict[int, str]:
    """Build slot_id → CAM_ID map from config/cameras.yaml."""
    global _slot_to_camera, _cameras_yaml_mtime
    if not CAMERAS_YAML_PATH.exists():
        return {}
    try:
        mtime = CAMERAS_YAML_PATH.stat().st_mtime
        if _cameras_yaml_mtime == mtime and _slot_to_camera:
            return _slot_to_camera
        data = load_yaml(CAMERAS_YAML_PATH) or {}
        mapping: dict[int, str] = {}
        for cam_id, cfg in data.get("cameras", {}).items():
            for slot_id_str in cfg.get("slot_presets", {}):
                mapping[int(slot_id_str)] = cam_id
        _slot_to_camera = mapping
        _cameras_yaml_mtime = mtime
        return mapping
    except Exception as e:
        log.error("Failed to load cameras.yaml: %s", e)
        return _slot_to_camera


# ── Payload decoding (same logic as server.py) ────────────────────────────────

def decode_uplink(payload_base64: str) -> dict:
    try:
        data_bytes = base64.b64decode(payload_base64)
        status = data_bytes.hex().lower()
    except Exception as e:
        log.error("decode_uplink error: %s", e)
        status = "unknown"
    return {"status": status, "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Core processing ───────────────────────────────────────────────────────────

def process_mqtt_message(r: redis.Redis, db_conn, message_id: bytes, fields: dict) -> bool:
    """Process one message from the stream.

    Returns True if XACK should be sent, False to leave in PEL for retry.
    """
    try:
        raw = fields.get(b"payload") or fields.get("payload", b"{}")
        if isinstance(raw, bytes):
            raw = raw.decode()
        payload = json.loads(raw)
    except Exception as e:
        log.error("Failed to parse message %s: %s", message_id, e)
        return True  # malformed — ack and discard

    try:
        device_info = payload.get("deviceInfo", {})
        device_name = device_info.get("deviceName", "Unknown")
        device_eui = device_info.get("devEui")

        raw_data = payload.get("data")
        if not raw_data:
            log.debug("No data in payload for device %s", device_name)
            return True

        decoded = decode_uplink(raw_data)
        status = decoded["status"]
        ts_str = decoded["timestamp"]
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))

        meta_by_id = load_slot_meta_by_id(SLOT_META_PATH)
        slot_id = get_slot_id_by_device_name(device_name, meta_by_id)
        if slot_id is None:
            log.warning("Device '%s' not mapped to any slot", device_name)
            return True

        slot_meta = meta_by_id.get(slot_id, {})
        slot_name = slot_meta.get("name", str(slot_id))
        zone = slot_meta.get("zone", "A")

        log.info("device=%s slot=%s status=%s ts=%s", device_name, slot_name, status, ts_str)

        # Map status to event_type
        if status == "01":
            event_type = "OCCUPIED"
            expected_state = "FREE"
            new_state = "OCCUPIED"
        elif status == "00":
            event_type = "FREE"
            expected_state = "OCCUPIED"
            new_state = "FREE"
        elif status == "cd":
            log.info("Calibration Done for slot %s", slot_name)
            insert_occupancy_event(
                slot_id=slot_id,
                event_type="calibration",
                device_eui=device_eui,
                ts=ts,
                payload={"slot_name": slot_name, "zone": zone},
                conn=db_conn,
            )
            db_conn.commit()
            return True
        else:
            log.debug("Ignoring unknown status '%s' from device %s", status, device_name)
            return True

        # Atomic CAS: only proceed if state actually changes
        cas_result = r.eval(
            _CAS_SCRIPT,
            1,
            "parking:slot:state",
            str(slot_id),
            expected_state,
            new_state,
        )

        if cas_result == 0:
            log.debug("CAS lost for slot %s (already %s) — dropping", slot_name, new_state)
            return True

        # CAS won — record state change time
        r.hset("parking:slot:since", str(slot_id), ts_str)

        # Insert into Postgres
        insert_occupancy_event(
            slot_id=slot_id,
            event_type=event_type,
            device_eui=device_eui,
            ts=ts,
            payload={
                "slot_name": slot_name,
                "zone": zone,
                "prev_state": expected_state,
                "new_state": new_state,
            },
            conn=db_conn,
        )
        db_conn.commit()

        # Enqueue camera task (only for FREE→OCCUPIED)
        if event_type == "OCCUPIED":
            cam_assignment = _load_camera_assignment()
            cam_id = cam_assignment.get(slot_id)
            if cam_id:
                preset = slot_meta.get("preset")
                r.xadd(
                    f"parking:camera:tasks:{cam_id}",
                    {
                        "slot_id": str(slot_id),
                        "slot_name": slot_name,
                        "zone": zone,
                        "preset": str(preset) if preset else "",
                        "trigger_ts": ts_str,
                        "event_type": event_type,
                        "device_eui": device_eui or "",
                    },
                    maxlen=500,
                    approximate=True,
                )
                log.info("Camera task enqueued for slot %s → %s", slot_name, cam_id)
            else:
                log.warning("No camera mapped for slot %d (slot_name=%s)", slot_id, slot_name)

        # Publish live event for SSE
        live_event = {
            "event": "slot_state_changed",
            "ts": ts_str,
            "slot_id": slot_id,
            "slot_name": slot_name,
            "zone": zone,
            "prev_state": expected_state,
            "new_state": new_state,
        }
        r.publish("parking:events:live", json.dumps(live_event))

        log.info("Slot %s transitioned %s→%s", slot_name, expected_state, new_state)
        return True

    except Exception as e:
        log.error("Error processing message %s: %s", message_id, e, exc_info=True)
        try:
            db_conn.rollback()
        except Exception:
            pass
        return False  # leave in PEL for XAUTOCLAIM retry


# ── Worker loop ───────────────────────────────────────────────────────────────

def run():
    log.info("MQTT worker starting (consumer: %s)", WORKER_ID)

    r = redis.Redis.from_url(REDIS_URL, decode_responses=False)

    # Create consumer group if it doesn't exist
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
        log.info("Signal %s received — shutting down", sig)
        _running = False

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    autoclaim_cursor = "0-0"

    while _running:
        # 1. XAUTOCLAIM: reclaim messages idle >10s (crash recovery)
        try:
            claimed = r.xautoclaim(
                STREAM_KEY, GROUP_NAME, WORKER_ID,
                min_idle_time=AUTOCLAIM_IDLE_MS,
                start_id=autoclaim_cursor,
                count=10,
            )
            # claimed = (next_cursor, [(id, fields), ...], deleted_ids)
            autoclaim_cursor_new = claimed[0]
            claimed_messages = claimed[1]
            if claimed_messages:
                log.info("Reclaimed %d idle message(s)", len(claimed_messages))
                for msg_id, fields in claimed_messages:
                    ok = process_mqtt_message(r, db_conn, msg_id, fields)
                    if ok:
                        r.xack(STREAM_KEY, GROUP_NAME, msg_id)
            # Reset cursor to 0-0 after one pass to avoid infinite loop
            autoclaim_cursor = autoclaim_cursor_new if claimed_messages else "0-0"
        except redis.exceptions.ResponseError:
            autoclaim_cursor = "0-0"
        except Exception as e:
            log.error("XAUTOCLAIM error: %s", e)
            time.sleep(1)

        # 2. XREADGROUP: read new messages
        try:
            results = r.xreadgroup(
                GROUP_NAME, WORKER_ID,
                {STREAM_KEY: ">"},
                count=10,
                block=BLOCK_MS,
            )
        except redis.exceptions.ConnectionError as e:
            log.error("Redis connection error: %s — retrying in 2s", e)
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
                ok = process_mqtt_message(r, db_conn, msg_id, fields)
                if ok:
                    r.xack(STREAM_KEY, GROUP_NAME, msg_id)

    log.info("MQTT worker stopped")
    try:
        db_conn.close()
    except Exception:
        pass


if __name__ == "__main__":
    run()
