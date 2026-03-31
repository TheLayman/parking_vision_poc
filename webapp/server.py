from __future__ import annotations
import json
import asyncio
import base64
import logging
import struct
import time as _time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import os

import paho.mqtt.client as mqtt
import redis
import redis.asyncio as aioredis
from fastapi import FastAPI, Query, Request, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from webapp.helpers.data_io import load_yaml, save_yaml
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

APP_ROOT = Path(__file__).resolve().parent
REPO_ROOT = APP_ROOT.parent

SLOT_META_PATH = REPO_ROOT / "config" / "slot_meta.yaml"

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
_redis_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=False)
    return _redis_client


# ── MQTT configuration ────────────────────────────────────────────────────────
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "application/+/device/+/event/up")
ENABLE_MQTT = int(os.getenv("ENABLE_MQTT", "1"))

CHIRPSTACK_HOST = os.getenv("CHIRPSTACK_HOST", "localhost")
CHIRPSTACK_GRPC_PORT = os.getenv("CHIRPSTACK_GRPC_PORT", "8080")
CHIRPSTACK_API_TOKEN = os.getenv("CHIRPSTACK_API_TOKEN", "")
CHIRPSTACK_APP_ID = os.getenv("CHIRPSTACK_APP_ID", "")

_mqtt_client = None

# ── Device map (slot_id -> {applicationId, devEui}) — persisted in Redis ─────
_device_map: dict[int, dict] = {}
_device_map_lock = threading.Lock()

# ── State cache ───────────────────────────────────────────────────────────────
_state_cache = None
_state_cache_time = None
_state_cache_lock = threading.Lock()
STATE_CACHE_TTL = timedelta(seconds=30)

# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(application: FastAPI):
    _startup()
    yield
    _shutdown()


app = FastAPI(title="Smart Parking Enforcement", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")

_shutdown_event = threading.Event()


# ── Startup / Shutdown ────────────────────────────────────────────────────────

def _startup():
    global _redis_client

    _shutdown_event.clear()

    # Validate Redis
    try:
        r = get_redis()
        r.ping()
        log.info("Redis connected at %s", REDIS_URL)
    except Exception as e:
        log.error("Redis connection failed at startup: %s", e)

    # Validate Postgres connection pool
    try:
        from db.client import get_pool
        pool = get_pool()
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        log.info("Postgres connected")
    except Exception as e:
        log.error("Postgres connection failed at startup: %s", e)

    # Restore device map from Redis
    _load_device_map_from_redis()

    # Fetch/refresh device map from ChirpStack gRPC API
    _fetch_devices_from_chirpstack()

    # Start device map flush thread (debounced, every 10s)
    threading.Thread(target=_device_map_flush_loop, daemon=True).start()

    if ENABLE_MQTT:
        log.info("Starting MQTT listener %s:%s ...", MQTT_BROKER, MQTT_PORT)
        start_mqtt_listener()
    else:
        log.info("MQTT disabled (ENABLE_MQTT=0)")


def _shutdown():
    log.info("Shutting down...")
    _shutdown_event.set()
    if ENABLE_MQTT:
        stop_mqtt_listener()
    try:
        from db.client import close_pool
        close_pool()
    except Exception:
        pass
    global _redis_client
    if _redis_client:
        try:
            _redis_client.close()
        except Exception:
            pass
        _redis_client = None


# ── Device map helpers ────────────────────────────────────────────────────────

def _load_device_map_from_redis():
    global _device_map
    try:
        r = get_redis()
        raw = r.hgetall("parking:device:map")
        new_map: dict[int, dict] = {}
        for k, v in raw.items():
            try:
                slot_id = int(k.decode() if isinstance(k, bytes) else k)
                info = json.loads(v.decode() if isinstance(v, bytes) else v)
                new_map[slot_id] = info
            except Exception:
                continue
        with _device_map_lock:
            _device_map.update(new_map)
        if new_map:
            log.info("Restored device map for %d slot(s) from Redis", len(new_map))
    except Exception as e:
        log.error("Failed to load device map from Redis: %s", e)


_device_map_dirty = False


def _save_device_map_to_redis():
    global _device_map_dirty
    try:
        r = get_redis()
        with _device_map_lock:
            items = list(_device_map.items())
            _device_map_dirty = False
        if items:
            mapping = {str(k): json.dumps(v) for k, v in items}
            r.hset("parking:device:map", mapping=mapping)
    except Exception as e:
        log.error("Failed to save device map to Redis: %s", e)


def _device_map_flush_loop():
    """Periodically flush dirty device map to Redis (every 10s)."""
    global _device_map_dirty
    while True:
        _time.sleep(10)
        if _device_map_dirty:
            _save_device_map_to_redis()


def _fetch_devices_from_chirpstack():
    if not CHIRPSTACK_API_TOKEN or not CHIRPSTACK_APP_ID:
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
                slot_id = get_slot_id_by_device_name(device.name, meta_by_id)
                if slot_id is not None:
                    with _device_map_lock:
                        _device_map[slot_id] = {
                            "applicationId": CHIRPSTACK_APP_ID,
                            "devEui": device.dev_eui,
                        }
                    matched += 1
            offset += limit
            if offset >= resp.total_count:
                break

        channel.close()
        if matched:
            log.info("ChirpStack API: mapped %d device(s)", matched)
            _save_device_map_to_redis()
    except Exception as e:
        log.error("Error fetching devices from ChirpStack: %s", e)


# ── Downlink command (calibrate / threshold) ──────────────────────────────────

def _enqueue_via_chirpstack_grpc(dev_eui: str, data_hex: str, fport: int = 2) -> bool:
    if not CHIRPSTACK_API_TOKEN:
        return False
    try:
        import grpc
        from chirpstack_api import api as cs_api

        channel = grpc.insecure_channel(f"{CHIRPSTACK_HOST}:{CHIRPSTACK_GRPC_PORT}")
        client = cs_api.DeviceServiceStub(channel)
        auth_token = [("authorization", f"Bearer {CHIRPSTACK_API_TOKEN}")]

        resp = client.Enqueue(
            cs_api.EnqueueDeviceQueueItemRequest(
                queue_item=cs_api.DeviceQueueItem(
                    dev_eui=dev_eui,
                    confirmed=False,
                    f_port=fport,
                    data=bytes.fromhex(data_hex),
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


def queue_command(slot_id: int, data_hex: str, fport: int = 2) -> bool:
    with _device_map_lock:
        device_info = _device_map.get(slot_id)
    if not device_info:
        log.error("No device mapping found for slot %s", slot_id)
        return False

    app_id = device_info.get("applicationId")
    dev_eui = device_info.get("devEui")
    if not (app_id and dev_eui):
        return False

    if CHIRPSTACK_API_TOKEN:
        if _enqueue_via_chirpstack_grpc(dev_eui, data_hex, fport):
            return True
        log.warning("gRPC enqueue failed for slot %s, falling back to MQTT", slot_id)

    if _mqtt_client is None:
        return False

    topic = f"application/{app_id}/device/{dev_eui}/command/down"
    try:
        data_b64 = base64.b64encode(bytes.fromhex(data_hex)).decode("ascii")
        payload = {"devEui": dev_eui, "confirmed": False, "fPort": fport, "data": data_b64}
        result = _mqtt_client.publish(topic, json.dumps(payload), qos=1)
        if result.rc != 0:
            return False
        log.info("Queued command for slot %s via MQTT", slot_id)
        return True
    except Exception as e:
        log.error("Error queuing command: %s", e)
        return False


# ── Uplink decoder ────────────────────────────────────────────────────────────

def decode_uplink(payload_base64: str) -> dict:
    try:
        status = base64.b64decode(payload_base64).hex().lower()
    except Exception:
        status = "unknown"
    return {"status": status, "timestamp": datetime.now(timezone.utc).isoformat()}


# ── MQTT: forward raw messages to Redis Stream ────────────────────────────────

def on_mqtt_message(client, userdata, msg):
    """Non-blocking: push raw ChirpStack uplink to Redis Stream for mqtt_worker."""
    try:
        payload = json.loads(msg.payload)
        # Update device map opportunistically (best-effort, no heavy work here)
        device_info = payload.get("deviceInfo", {})
        dev_eui = device_info.get("devEui")
        app_id = device_info.get("applicationId")
        device_name = device_info.get("deviceName", "")
        if dev_eui and app_id and device_name:
            meta_by_id = load_slot_meta_by_id(SLOT_META_PATH)
            slot_id = get_slot_id_by_device_name(device_name, meta_by_id)
            if slot_id is not None:
                new_info = {"applicationId": app_id, "devEui": dev_eui}
                with _device_map_lock:
                    if _device_map.get(slot_id) != new_info:
                        _device_map[slot_id] = new_info
                        _device_map_dirty = True

        # Enqueue to Redis Stream (O(1), non-blocking)
        r = get_redis()
        r.xadd(
            "parking:mqtt:events",
            {"payload": json.dumps(payload)},
            maxlen=100_000,
            approximate=True,
        )
        # Note: state cache is NOT invalidated here. The mqtt_worker publishes
        # to parking:events:live after processing, and clients get updates via SSE.
        # The 30s TTL naturally refreshes /state for polling clients.
    except Exception as e:
        log.error("Error enqueuing MQTT message to Redis: %s", e)


def on_mqtt_connect(client, userdata, flags, rc):
    if rc == 0:
        log.info("Connected to MQTT broker at %s:%s", MQTT_BROKER, MQTT_PORT)
        client.subscribe(MQTT_TOPIC)
    else:
        log.error("MQTT broker connection failed, rc=%s", rc)


def start_mqtt_listener():
    global _mqtt_client
    _mqtt_client = mqtt.Client()
    _mqtt_client.on_connect = on_mqtt_connect
    _mqtt_client.on_message = on_mqtt_message
    try:
        _mqtt_client.connect(MQTT_BROKER, MQTT_PORT)
        _mqtt_client.loop_start()
    except Exception as e:
        log.error("Error starting MQTT client: %s", e)


def stop_mqtt_listener():
    global _mqtt_client
    if _mqtt_client:
        _mqtt_client.loop_stop()
        _mqtt_client.disconnect()
        _mqtt_client = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(str(APP_ROOT / "static" / "index.html"))


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/health")
def health():
    """Liveness + readiness check for monitoring."""
    status = {"redis": "ok", "postgres": "ok", "mqtt": "ok"}
    http_status = 200

    try:
        get_redis().ping()
    except Exception as e:
        status["redis"] = f"error: {e}"
        http_status = 503

    try:
        from db.client import get_pool
        with get_pool().connection() as conn:
            conn.execute("SELECT 1")
    except Exception as e:
        status["postgres"] = f"error: {e}"
        http_status = 503

    if not ENABLE_MQTT:
        status["mqtt"] = "disabled"
    elif _mqtt_client is None or not _mqtt_client.is_connected():
        status["mqtt"] = "disconnected"

    # Stream depths
    try:
        r = get_redis()
        status["stream_mqtt"] = r.xlen("parking:mqtt:events")
        status["stream_inference"] = r.xlen("parking:inference:jobs")
        status["stream_deadletter"] = r.xlen("parking:inference:deadletter")
        # Consumer lag: count pending (unprocessed) messages per group
        try:
            pel = r.xpending("parking:mqtt:events", "mqtt-processors")
            status["mqtt_pending"] = pel.get("pending", 0) if isinstance(pel, dict) else 0
        except Exception:
            pass
        try:
            pel = r.xpending("parking:inference:jobs", "inference-workers")
            status["inference_pending"] = pel.get("pending", 0) if isinstance(pel, dict) else 0
        except Exception:
            pass
    except Exception:
        pass

    return Response(
        content=json.dumps(status),
        status_code=http_status,
        media_type="application/json",
    )


@app.get("/state")
def state():
    global _state_cache, _state_cache_time
    now = datetime.now(timezone.utc)

    with _state_cache_lock:
        if _state_cache and _state_cache_time and (now - _state_cache_time) < STATE_CACHE_TTL:
            return _state_cache

    meta_by_id = load_slot_meta_by_id(SLOT_META_PATH)
    slot_ids = sorted(meta_by_id.keys())
    result = build_state_from_log(None, slot_ids=slot_ids, meta_by_id=meta_by_id,
                                     redis_client=get_redis())

    with _state_cache_lock:
        _state_cache = result
        _state_cache_time = now
    return result


@app.get("/events")
async def events(request: Request):
    """SSE stream backed by Redis pub/sub on parking:events:live."""
    async def gen():
        r = aioredis.from_url(REDIS_URL, decode_responses=False)
        pubsub = r.pubsub()
        await pubsub.subscribe("parking:events:live")
        try:
            while not _shutdown_event.is_set():
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message and message.get("type") == "message":
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode()
                    yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await pubsub.unsubscribe("parking:events:live")
                await r.aclose()
            except Exception:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/analytics/summary")
def analytics_summary(range: str = Query(default="24h"),
                      zone: str = Query(default=None)):
    time_deltas = {
        "1h": timedelta(hours=1), "6h": timedelta(hours=6),
        "24h": timedelta(hours=24), "7d": timedelta(days=7), "all": None,
    }
    delta = time_deltas.get(range)
    now_ts = datetime.now(timezone.utc)
    cutoff = (now_ts - delta) if delta else None

    parsed = parse_events_from_log(None, cutoff)
    snapshots = parsed["snapshots"]
    state_changes = parsed["state_changes"]
    challans = parsed["challans"]

    all_zones = sorted({sc["zone"] for sc in state_changes} | {c["zone"] for c in challans})

    state_changes_filtered = (
        [sc for sc in state_changes if sc["zone"] == zone] if zone else state_changes
    )
    incidents = [
        sc for sc in state_changes_filtered
        if sc.get("prev_state") == "FREE" and sc.get("new_state") == "OCCUPIED"
    ]
    total_incidents = len(incidents)

    dwell_result = calculate_dwell_times(state_changes)
    all_dwells = dwell_result["all_dwells"]

    dwells_filtered = all_dwells if zone is None else [d for d in all_dwells if d["zone"] == zone]
    avg_parking_minutes = (
        round(sum(d["minutes"] for d in dwells_filtered) / len(dwells_filtered), 1)
        if dwells_filtered else 0
    )

    dwell_distribution = build_dwell_distribution(all_dwells, zone=zone)
    hourly_incidents = build_hourly_incidents(
        state_changes,
        start=cutoff if delta else None,
        end=now_ts if delta else None,
    )
    challan_summary = build_challan_summary(challans, zone=zone)

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
        "challan_summary": {k: v for k, v in challan_summary.items() if k != "by_zone"},
        "dwell_distribution": dwell_distribution,
        "hourly_incidents": hourly_incidents,
        "zones": all_zones,
        "zone_stats": zone_stats,
        "time_range": range,
    }


@app.get("/alerts")
def get_alerts(limit: int = Query(default=50, le=200), offset: int = Query(default=0)):
    """Recent OCCUPIED events LEFT JOINed with camera captures in Postgres."""
    try:
        from db.client import get_pool
        sql = """
            SELECT o.slot_id, o.ts, o.payload,
                   c.image_path, c.ocr_result
            FROM occupancy_events o
            LEFT JOIN camera_captures c
                ON c.slot_id = o.slot_id
                AND c.ts BETWEEN o.ts - INTERVAL '5 seconds' AND o.ts + INTERVAL '120 seconds'
            WHERE o.event_type = 'OCCUPIED'
            ORDER BY o.ts DESC
            LIMIT %s OFFSET %s
        """
        with get_pool().connection() as conn:
            rows = conn.execute(sql, (limit, offset)).fetchall()

        alerts = []
        for row in rows:
            payload = row["payload"] if isinstance(row["payload"], dict) else (
                json.loads(row["payload"]) if row["payload"] else {})
            ts = row["ts"]
            ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            ocr = row["ocr_result"] if isinstance(row["ocr_result"], dict) else (
                json.loads(row["ocr_result"]) if row["ocr_result"] else {})
            plates = ocr.get("plates", [])
            alerts.append({
                "event": "slot_state_changed",
                "ts": ts_str,
                "slot_id": row["slot_id"],
                "slot_name": payload.get("slot_name", str(row["slot_id"])),
                "zone": payload.get("zone", "A"),
                "prev_state": "FREE",
                "new_state": "OCCUPIED",
                "image_path": row.get("image_path", ""),
                "license_plate": plates[0] if plates else "UNKNOWN",
                "license_plates": plates,
            })

        return {"alerts": alerts, "total": len(alerts), "limit": limit, "offset": offset}
    except Exception as e:
        log.error("Error fetching alerts: %s", e)
        return {"alerts": [], "total": 0, "limit": limit, "offset": offset}


@app.get("/challans")
def get_challans(
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0),
    challan_only: bool = Query(default=False),
    zone: str = Query(default=None),
    since: str = Query(default=None),
):
    try:
        from db.client import query_challan_events
        rows = query_challan_events(
            zone=zone, challan_only=challan_only, since=since,
            limit=limit, offset=offset,
        )
        challans = []
        for row in rows:
            meta = row.get("metadata") or {}
            ts = row["ts"]
            ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            challans.append({
                "challan_id": row.get("challan_id"),
                "plate_text": row.get("license_plate") or "UNKNOWN",
                "slot_id": row["slot_id"],
                "slot_name": meta.get("slot_name", str(row["slot_id"])),
                "zone": meta.get("zone", "A"),
                "first_time": meta.get("first_time", ts_str),
                "second_time": ts_str,
                "first_image": meta.get("first_image", ""),
                "second_image": meta.get("second_image", ""),
                "challan": row.get("status") == "confirmed",
                "first_plates": meta.get("first_plates", []),
                "second_plates": meta.get("second_plates", []),
                "capture_session_id": meta.get("capture_session_id"),
            })
        return {"challans": challans, "total": len(challans),
                "limit": limit, "offset": offset}
    except Exception as e:
        log.error("Error fetching challans: %s", e)
        return {"challans": [], "total": 0, "limit": limit, "offset": offset}


@app.get("/challans/pending")
def get_challans_pending():
    """Returns currently pending challan rechecks from Redis."""
    pending_list = []
    try:
        r = get_redis()
        # SCAN for parking:challan:pending:* keys
        for key in r.scan_iter("parking:challan:pending:*"):
            raw = r.get(key)
            if not raw:
                continue
            try:
                info = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                key_str = key.decode() if isinstance(key, bytes) else key
                slot_id = key_str.split(":")[-1]
                pending_list.append({
                    "slot_id": int(slot_id),
                    "slot_name": info.get("slot_name", slot_id),
                    "zone": info.get("zone", "A"),
                    "plates": info.get("plates", []),
                    "first_time": info.get("first_time"),
                    "capture_session_id": info.get("capture_session_id"),
                })
            except Exception:
                continue
    except Exception as e:
        log.error("Error fetching pending challans: %s", e)
    return {"pending": pending_list, "count": len(pending_list)}


@app.post("/calibrate/{slot_id}")
def calibrate_slot(slot_id: int):
    CALIBRATION_COMMAND = "CC"
    success = queue_command(slot_id, CALIBRATION_COMMAND)
    if not success:
        with _device_map_lock:
            mapped = slot_id in _device_map
        if not mapped:
            raise HTTPException(status_code=404,
                                detail="Device not connected or mapped yet. Wait for an uplink.")
        raise HTTPException(status_code=500, detail="Failed to queue calibration command")
    return {"success": True, "message": f"Calibration command queued for slot {slot_id}"}


@app.post("/setThreshold/{slot_id}/{threshold}")
def setThreshold_slot(slot_id: int, threshold: float):
    threshold_int = int(threshold * 2)
    threshold_hex = struct.pack(">H", threshold_int).hex()
    THRESHOLD_COMMAND = "DD" + threshold_hex
    success = queue_command(slot_id, THRESHOLD_COMMAND)
    if not success:
        with _device_map_lock:
            mapped = slot_id in _device_map
        if not mapped:
            raise HTTPException(status_code=404,
                                detail="Device not connected or mapped yet. Wait for an uplink.")
        raise HTTPException(status_code=500, detail="Failed to queue threshold command")
    return {"success": True, "message": f"Threshold command queued for slot {slot_id}"}


@app.get("/camera/status")
def camera_status():
    """Returns worker health via Redis stream depths."""
    try:
        r = get_redis()
        return {
            "enabled": True,
            "stream_depths": {
                "mqtt_events": r.xlen("parking:mqtt:events"),
                "inference_jobs": r.xlen("parking:inference:jobs"),
                "inference_deadletter": r.xlen("parking:inference:deadletter"),
            },
        }
    except Exception as e:
        return {"enabled": True, "error": str(e)}


@app.get("/challan-dashboard", response_class=HTMLResponse)
def challan_dashboard():
    return FileResponse(str(APP_ROOT / "static" / "challan.html"))


@app.get("/snapshots/{date_or_file}/{filename}")
def get_snapshot_image(date_or_file: str, filename: str):
    """Serve camera snapshots from /data/snapshots/{date}/{filename}."""
    snapshots_dir = Path(os.environ.get("SNAPSHOTS_DIR", "/data/snapshots"))
    image_path = snapshots_dir / date_or_file / filename

    # Resolve to absolute paths and assert the result is still under SNAPSHOTS_DIR.
    # This blocks ../, URL-encoded traversal (%2e%2e), and symlink escapes.
    try:
        resolved = image_path.resolve()
        if not resolved.is_relative_to(snapshots_dir.resolve()):
            raise HTTPException(status_code=400, detail="Invalid path")
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail="Invalid path")

    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="Snapshot not found")

    return FileResponse(
        str(resolved),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000"},
    )
