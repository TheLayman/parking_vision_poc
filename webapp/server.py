from __future__ import annotations
import json
import asyncio
import logging
import time as _time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
import os

import paho.mqtt.client as mqtt
import redis
import redis.asyncio as aioredis
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from webapp.helpers.data_io import load_yaml
from webapp.helpers.slot_meta import (
    load_slot_meta_by_id, get_slot_id_by_device_name,
    build_state_from_log,
)
from webapp.helpers.analytics import (
    parse_events_from_log,
    calculate_dwell_times,
    build_dwell_distribution, build_hourly_incidents,
    build_turnover_rates, build_peak_occupancy, build_occupancy_heatmap,
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


def _decode_redis_hash(raw: dict) -> dict[int, str]:
    """Decode a Redis hash with bytes keys/values to {int_id: str_value}."""
    result = {}
    for k, v in raw.items():
        try:
            sid = int(k.decode() if isinstance(k, bytes) else k)
            result[sid] = v.decode() if isinstance(v, bytes) else str(v)
        except Exception:
            pass
    return result


def _decode_redis_hash_json(raw: dict) -> dict[int, dict]:
    """Decode a Redis hash with JSON-encoded values to {int_id: dict}."""
    result = {}
    for k, v in raw.items():
        try:
            sid = int(k.decode() if isinstance(k, bytes) else k)
            result[sid] = json.loads(v.decode() if isinstance(v, bytes) else v)
        except Exception:
            pass
    return result


# ── MQTT configuration ────────────────────────────────────────────────────────
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "application/+/device/+/event/up")
ENABLE_MQTT = int(os.getenv("ENABLE_MQTT", "1"))

_mqtt_client = None

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


app = FastAPI(title="Smart Parking Dashboard", lifespan=_lifespan)
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


# ── MQTT: forward raw messages to Redis Stream ────────────────────────────────

def on_mqtt_message(client, userdata, msg):
    """Non-blocking: push raw ChirpStack uplink to Redis Stream for mqtt_worker."""
    try:
        payload = json.loads(msg.payload)

        # Enqueue to Redis Stream (O(1), non-blocking)
        r = get_redis()
        r.xadd(
            "parking:mqtt:events",
            {"payload": json.dumps(payload)},
            maxlen=100_000,
            approximate=True,
        )
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

    # Stream depth
    try:
        r = get_redis()
        status["stream_mqtt"] = r.xlen("parking:mqtt:events")
        try:
            pel = r.xpending("parking:mqtt:events", "mqtt-processors")
            status["mqtt_pending"] = pel.get("pending", 0) if isinstance(pel, dict) else 0
        except Exception:
            pass
    except Exception:
        pass

    # Sensor health summary
    try:
        r = get_redis()
        lastseen = _decode_redis_hash(r.hgetall("parking:sensor:lastseen"))
        now = datetime.now(timezone.utc)
        online = sum(1 for ts_str in lastseen.values()
                     if (now - datetime.fromisoformat(ts_str)).total_seconds() < 6000)
        status["sensors_total"] = len(lastseen)
        status["sensors_online"] = online
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
    r = get_redis()
    result = build_state_from_log(slot_ids=slot_ids, meta_by_id=meta_by_id,
                                     redis_client=r)

    # Augment with sensor health data
    try:
        result["sensor_lastseen"] = _decode_redis_hash(r.hgetall("parking:sensor:lastseen"))
        result["sensor_alerts"] = _decode_redis_hash_json(r.hgetall("parking:sensor:alerts"))
    except Exception:
        result["sensor_lastseen"] = {}
        result["sensor_alerts"] = {}

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


@app.get("/state-changes")
def get_state_changes(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0),
    zone: str = Query(default=None),
):
    """Recent occupancy state transitions from Postgres."""
    try:
        from db.client import query_occupancy_events
        rows = query_occupancy_events(limit=limit)

        changes = []
        for row in rows:
            if row["event_type"] not in ("OCCUPIED", "FREE"):
                continue
            payload = row.get("payload") or {}
            row_zone = payload.get("zone", "A")
            if zone and row_zone != zone:
                continue
            ts = row["ts"]
            ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            changes.append({
                "ts": ts_str,
                "slot_id": row["slot_id"],
                "slot_name": payload.get("slot_name", str(row["slot_id"])),
                "zone": row_zone,
                "prev_state": payload.get("prev_state", ""),
                "new_state": row["event_type"],
            })

        # Reverse to newest-first (query returns ASC)
        changes.reverse()
        changes = changes[:limit]

        return {"changes": changes, "total": len(changes), "limit": limit, "offset": offset}
    except Exception as e:
        log.error("Error fetching state changes: %s", e)
        return {"changes": [], "total": 0, "limit": limit, "offset": offset}


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
    state_changes = parsed["state_changes"]

    all_zones = sorted({sc["zone"] for sc in state_changes})

    state_changes_filtered = (
        [sc for sc in state_changes if sc["zone"] == zone] if zone else state_changes
    )
    occupancy_events = [
        sc for sc in state_changes_filtered
        if sc.get("prev_state") == "FREE" and sc.get("new_state") == "OCCUPIED"
    ]
    total_occupancy_events = len(occupancy_events)

    dwell_result = calculate_dwell_times(state_changes)
    all_dwells = dwell_result["all_dwells"]

    dwells_filtered = all_dwells if zone is None else [d for d in all_dwells if d["zone"] == zone]
    avg_parking_minutes = (
        round(sum(d["minutes"] for d in dwells_filtered) / len(dwells_filtered), 1)
        if dwells_filtered else 0
    )

    dwell_distribution = build_dwell_distribution(all_dwells, zone=zone)
    hourly_occupancy = build_hourly_incidents(
        state_changes,
        start=cutoff if delta else None,
        end=now_ts if delta else None,
    )

    # Per-zone stats (reuse build_dwell_distribution for consistent buckets)
    occupancy_by_zone: dict[str, int] = defaultdict(int)
    for sc in state_changes:
        if sc.get("prev_state") == "FREE" and sc.get("new_state") == "OCCUPIED":
            occupancy_by_zone[sc["zone"]] += 1

    zone_stats = {}
    for z in all_zones:
        z_dwell_dist = build_dwell_distribution(all_dwells, zone=z)
        z_dwells = [d["minutes"] for d in all_dwells if d["zone"] == z]
        z_avg = round(sum(z_dwells) / len(z_dwells), 1) if z_dwells else 0
        zone_stats[z] = {
            "total_occupancy_events": occupancy_by_zone[z],
            "avg_parking_minutes": z_avg,
            "dwell_distribution": z_dwell_dist,
        }

    # Current utilization from Redis
    meta = load_slot_meta_by_id(SLOT_META_PATH)
    total_slots = len(meta)
    try:
        r = get_redis()
        state_raw = r.hgetall("parking:slot:state")
        occupied = sum(1 for v in state_raw.values()
                       if (v.decode() if isinstance(v, bytes) else v) == "OCCUPIED")
        utilization_pct = round(occupied / total_slots * 100, 1) if total_slots else 0
    except Exception:
        utilization_pct = 0

    # Turnover rates
    slots_by_zone: dict[str, int] = defaultdict(int)
    for sid, m in meta.items():
        slots_by_zone[m.get("zone", "A")] += 1
    turnover = build_turnover_rates(state_changes, dict(slots_by_zone))

    # Peak occupancy
    peak = build_peak_occupancy(state_changes, total_slots)

    # Heatmap (day-of-week x hour-of-day)
    heatmap = build_occupancy_heatmap(state_changes)

    # Median dwell
    sorted_dwells = sorted(d["minutes"] for d in dwells_filtered) if dwells_filtered else []
    median_parking_minutes = 0
    if sorted_dwells:
        mid = len(sorted_dwells) // 2
        median_parking_minutes = round(
            sorted_dwells[mid] if len(sorted_dwells) % 2 else
            (sorted_dwells[mid - 1] + sorted_dwells[mid]) / 2, 1
        )

    return {
        "total_occupancy_events": total_occupancy_events,
        "avg_parking_minutes": avg_parking_minutes,
        "median_parking_minutes": median_parking_minutes,
        "utilization_pct": utilization_pct,
        "peak_occupancy": peak,
        "turnover": turnover,
        "dwell_distribution": dwell_distribution,
        "hourly_occupancy": hourly_occupancy,
        "heatmap": heatmap,
        "zones": all_zones,
        "zone_stats": zone_stats,
        "time_range": range,
    }
