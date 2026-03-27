"""PostgreSQL client for Smart Parking Enforcement.

Workers use get_connection() — one persistent connection per process.
The API server uses get_pool() — thread-safe connection pool.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://parking:parking@localhost/parking")

# ── Connection pool (API server only) ────────────────────────────────────────

_pool = None


def get_pool():
    """Return a thread-safe connection pool (lazy-init). Use in the API server."""
    global _pool
    if _pool is None:
        from psycopg_pool import ConnectionPool
        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        log.info("Postgres connection pool opened")
    return _pool


def close_pool():
    global _pool
    if _pool:
        _pool.close()
        _pool = None


# ── Simple connection (workers) ───────────────────────────────────────────────

def get_connection() -> psycopg.Connection:
    """Open a single connection. Use in worker processes (one per process)."""
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


# ── Insert helpers ────────────────────────────────────────────────────────────

def insert_occupancy_event(
    slot_id: int,
    event_type: str,
    device_eui: str = None,
    ts: datetime = None,
    payload: dict = None,
    *,
    conn: psycopg.Connection = None,
):
    ts = ts or datetime.now(timezone.utc)
    _payload = json.dumps(payload) if payload else None
    sql = (
        "INSERT INTO occupancy_events (slot_id, event_type, device_eui, ts, payload) "
        "VALUES (%s, %s, %s, %s, %s)"
    )
    args = (slot_id, event_type, device_eui, ts, _payload)
    if conn:
        conn.execute(sql, args)
    else:
        with get_pool().connection() as c:
            c.execute(sql, args)


def insert_challan_event(
    challan_id: str,
    slot_id: int,
    license_plate: str = None,
    confidence: float = None,
    status: str = "no_plate",
    ts: datetime = None,
    metadata: dict = None,
    *,
    conn: psycopg.Connection = None,
):
    ts = ts or datetime.now(timezone.utc)
    _meta = json.dumps(metadata) if metadata else None
    sql = (
        "INSERT INTO challan_events "
        "(challan_id, slot_id, license_plate, confidence, status, ts, metadata) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)"
    )
    args = (challan_id, slot_id, license_plate, confidence, status, ts, _meta)
    if conn:
        conn.execute(sql, args)
    else:
        with get_pool().connection() as c:
            c.execute(sql, args)


def insert_camera_capture(
    slot_id: int,
    camera_id: str,
    ts: datetime,
    image_path: str,
    ocr_result: dict = None,
    backend: str = "openai",
    *,
    conn: psycopg.Connection = None,
):
    _ocr = json.dumps(ocr_result) if ocr_result else None
    sql = (
        "INSERT INTO camera_captures (slot_id, camera_id, ts, image_path, ocr_result, backend) "
        "VALUES (%s, %s, %s, %s, %s, %s)"
    )
    args = (slot_id, camera_id, ts, image_path, _ocr, backend)
    if conn:
        conn.execute(sql, args)
    else:
        with get_pool().connection() as c:
            c.execute(sql, args)


# ── Query helpers ─────────────────────────────────────────────────────────────

def query_occupancy_events(
    cutoff: datetime = None,
    slot_id: int = None,
    event_type: str = None,
    limit: int = 10_000,
) -> list[dict]:
    conditions = []
    params: list = []
    if cutoff:
        conditions.append("ts >= %s")
        params.append(cutoff)
    if slot_id is not None:
        conditions.append("slot_id = %s")
        params.append(slot_id)
    if event_type:
        conditions.append("event_type = %s")
        params.append(event_type)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    sql = (
        f"SELECT slot_id, event_type, device_eui, ts, payload "
        f"FROM occupancy_events {where} ORDER BY ts ASC LIMIT %s"
    )
    with get_pool().connection() as c:
        rows = c.execute(sql, params).fetchall()
    result = []
    for row in rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else (
            json.loads(row["payload"]) if row["payload"] else {}
        )
        result.append({
            "slot_id": row["slot_id"],
            "event_type": row["event_type"],
            "device_eui": row["device_eui"],
            "ts": row["ts"],
            "payload": payload,
        })
    return result


def query_challan_events(
    cutoff: datetime = None,
    zone: str = None,
    challan_only: bool = False,
    since: str = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict]:
    conditions = []
    params: list = []
    if cutoff:
        conditions.append("ts >= %s")
        params.append(cutoff)
    if since:
        conditions.append("ts >= %s")
        params.append(since)
    if challan_only:
        conditions.append("status = 'confirmed'")
    if zone:
        conditions.append("metadata->>'zone' = %s")
        params.append(zone)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])
    sql = (
        f"SELECT challan_id, slot_id, license_plate, confidence, status, ts, metadata "
        f"FROM challan_events {where} ORDER BY ts DESC LIMIT %s OFFSET %s"
    )
    with get_pool().connection() as c:
        rows = c.execute(sql, params).fetchall()
    result = []
    for row in rows:
        meta = row["metadata"] if isinstance(row["metadata"], dict) else (
            json.loads(row["metadata"]) if row["metadata"] else {}
        )
        result.append({
            "challan_id": row["challan_id"],
            "slot_id": row["slot_id"],
            "license_plate": row["license_plate"],
            "confidence": row["confidence"],
            "status": row["status"],
            "ts": row["ts"],
            "metadata": meta,
        })
    return result


def query_camera_captures(
    slot_id: int = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    conditions = []
    params: list = []
    if slot_id is not None:
        conditions.append("slot_id = %s")
        params.append(slot_id)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])
    sql = (
        f"SELECT slot_id, camera_id, ts, image_path, ocr_result, backend "
        f"FROM camera_captures {where} ORDER BY ts DESC LIMIT %s OFFSET %s"
    )
    with get_pool().connection() as c:
        rows = c.execute(sql, params).fetchall()
    result = []
    for row in rows:
        ocr = row["ocr_result"] if isinstance(row["ocr_result"], dict) else (
            json.loads(row["ocr_result"]) if row["ocr_result"] else {}
        )
        result.append({
            "slot_id": row["slot_id"],
            "camera_id": row["camera_id"],
            "ts": row["ts"],
            "image_path": row["image_path"],
            "ocr_result": ocr,
            "backend": row["backend"],
        })
    return result
