"""MQTT worker — consumes parking:mqtt:events Redis Stream.

Run 4 instances via systemd:
    python -m workers.mqtt_worker  (WORKER_ID set in environment)

Each instance is a member of the 'mqtt-processors' consumer group.
Atomically transitions slot state using a Lua CAS script.
On winning a FREE→OCCUPIED or OCCUPIED→FREE transition:
  - INSERTs occupancy_events into Postgres
  - PUBLISHes a live event to parking:events:live
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import redis

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from db.client import insert_occupancy_event
from webapp.helpers.slot_meta import load_slot_meta_by_id, get_slot_id_by_device_name
from workers.base import run_stream_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("mqtt_worker")

# ── Config ────────────────────────────────────────────────────────────────────

SLOT_META_PATH = _REPO_ROOT / "config" / "slot_meta.yaml"

STREAM_KEY = "parking:mqtt:events"
GROUP_NAME = "mqtt-processors"
WORKER_ID = os.environ.get("WORKER_ID", f"worker-{os.getpid()}")

# ── Lua CAS script: atomically transition slot state ─────────────────────────
_CAS_SCRIPT = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
if not current then current = 'FREE' end
if current == ARGV[2] then
    redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
    return 1
end
return 0
"""

# ── Status codes (1-byte uplink from BMM350 firmware) ────────────────────────
STATUS_FREE = "00"
STATUS_OCCUPIED = "01"
STATUS_BATTERY_LOW = "09"
STATUS_TEMP_HIGH = "0a"
STATUS_CALIBRATION_DONE = "cd"

# ── Payload decoding ─────────────────────────────────────────────────────────

def decode_uplink(payload_base64: str) -> dict:
    try:
        data_bytes = base64.b64decode(payload_base64)
        status = data_bytes.hex().lower()
    except Exception as e:
        log.error("decode_uplink error: %s", e)
        status = "unknown"
    return {"status": status, "timestamp": datetime.now(timezone.utc).isoformat()}


# ── Device alert helper ───────────────────────────────────────────────────────

def _handle_device_alert(r, db_conn, slot_id, slot_name, zone, device_eui, ts, ts_str, alert_type):
    """Record a device health alert (battery_low / temperature_high)."""
    log.warning("%s for slot %s", alert_type.replace("_", " ").upper(), slot_name)
    r.hset("parking:sensor:alerts", str(slot_id),
            json.dumps({"type": alert_type, "ts": ts_str}))
    insert_occupancy_event(
        slot_id=slot_id, event_type=alert_type, device_eui=device_eui,
        ts=ts, payload={"slot_name": slot_name, "zone": zone}, conn=db_conn,
    )
    db_conn.commit()
    r.publish("parking:events:live", json.dumps({
        "event": "device_alert", "ts": ts_str,
        "slot_id": slot_id, "slot_name": slot_name, "zone": zone,
        "alert_type": alert_type,
    }))
    return True


# ── Core processing ───────────────────────────────────────────────────────────

def process_mqtt_message(r: redis.Redis, db_conn, message_id: bytes, fields: dict) -> bool:
    """Process one message from the stream. Returns True to XACK."""
    try:
        raw = fields.get(b"payload") or fields.get("payload", b"{}")
        if isinstance(raw, bytes):
            raw = raw.decode()
        payload = json.loads(raw)
    except Exception as e:
        log.error("Failed to parse message %s: %s", message_id, e)
        return True

    try:
        device_info = payload.get("deviceInfo", {})
        device_name = device_info.get("deviceName", "Unknown")
        device_eui = device_info.get("devEui")

        raw_data = payload.get("data")
        if not raw_data:
            return True

        decoded = decode_uplink(raw_data)
        status = decoded["status"]
        ts_str = decoded["timestamp"]
        ts = datetime.fromisoformat(ts_str)

        meta_by_id = load_slot_meta_by_id(SLOT_META_PATH)
        slot_id = get_slot_id_by_device_name(device_name, meta_by_id)
        if slot_id is None:
            log.warning("Device '%s' not mapped to any slot", device_name)
            return True

        slot_meta = meta_by_id.get(slot_id, {})
        slot_name = slot_meta.get("name", str(slot_id))
        zone = slot_meta.get("zone", "A")

        log.info("device=%s slot=%s status=%s", device_name, slot_name, status)
        r.hset("parking:sensor:lastseen", str(slot_id), ts_str)

        rx_info = payload.get("rxInfo", [])
        if rx_info:
            best_rssi = max((ri.get("rssi", -999) for ri in rx_info), default=-999)
            if best_rssi > -999:
                r.hset("parking:sensor:rssi", str(slot_id), str(best_rssi))

        # Device health alerts
        if status == STATUS_BATTERY_LOW:
            return _handle_device_alert(
                r, db_conn, slot_id, slot_name, zone, device_eui, ts, ts_str, "battery_low")
        if status == STATUS_TEMP_HIGH:
            return _handle_device_alert(
                r, db_conn, slot_id, slot_name, zone, device_eui, ts, ts_str, "temperature_high")

        # Occupancy state transitions
        if status == STATUS_OCCUPIED:
            event_type, expected_state, new_state = "OCCUPIED", "FREE", "OCCUPIED"
        elif status == STATUS_FREE:
            event_type, expected_state, new_state = "FREE", "OCCUPIED", "FREE"
        elif status == STATUS_CALIBRATION_DONE:
            log.info("Calibration Done for slot %s", slot_name)
            r.hdel("parking:slot:calibrating", str(slot_id))
            insert_occupancy_event(
                slot_id=slot_id, event_type="calibration", device_eui=device_eui,
                ts=ts, payload={"slot_name": slot_name, "zone": zone}, conn=db_conn,
            )
            db_conn.commit()
            r.publish("parking:events:live", json.dumps({
                "event": "calibration_done", "ts": ts_str,
                "slot_id": slot_id, "slot_name": slot_name, "zone": zone,
            }))
            return True
        else:
            return True

        # Atomic CAS: only proceed if state actually changes
        cas_result = r.eval(
            _CAS_SCRIPT, 1, "parking:slot:state",
            str(slot_id), expected_state, new_state,
        )
        if cas_result == 0:
            return True

        r.hset("parking:slot:since", str(slot_id), ts_str)

        # Insert into Postgres — revert Redis CAS on failure
        try:
            insert_occupancy_event(
                slot_id=slot_id, event_type=event_type, device_eui=device_eui,
                ts=ts, payload={
                    "slot_name": slot_name, "zone": zone,
                    "prev_state": expected_state, "new_state": new_state,
                },
                conn=db_conn,
            )
            db_conn.commit()
        except Exception as db_err:
            log.error("Postgres INSERT failed for slot %s — reverting: %s", slot_name, db_err)
            try:
                db_conn.rollback()
            except Exception:
                pass
            try:
                r.hset("parking:slot:state", str(slot_id), expected_state)
                r.hdel("parking:slot:since", str(slot_id))
            except Exception as redis_err:
                log.error("Redis revert failed for slot %s: %s", slot_name, redis_err)
            return False

        # Publish live SSE event
        r.publish("parking:events:live", json.dumps({
            "event": "slot_state_changed", "ts": ts_str,
            "slot_id": slot_id, "slot_name": slot_name, "zone": zone,
            "prev_state": expected_state, "new_state": new_state,
        }))

        log.info("Slot %s transitioned %s→%s", slot_name, expected_state, new_state)
        return True

    except Exception as e:
        log.error("Error processing message %s: %s", message_id, e, exc_info=True)
        try:
            db_conn.rollback()
        except Exception:
            pass
        return False


# ── Entry point ──────────────────────────────────────────────────────────────

def run():
    log.info("MQTT worker starting (consumer: %s)", WORKER_ID)
    run_stream_worker(
        stream_key=STREAM_KEY,
        group_name=GROUP_NAME,
        worker_id=WORKER_ID,
        process_fn=process_mqtt_message,
        autoclaim_idle_ms=10_000,
        autoclaim_count=10,
        xread_count=10,
        block_ms=1000,
        needs_db=True,
        worker_label="MQTT worker",
    )


if __name__ == "__main__":
    run()
