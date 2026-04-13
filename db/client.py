"""PostgreSQL client for Smart Parking Dashboard POC.

Workers use get_connection() — one persistent connection per process.
The API server uses get_pool() — thread-safe connection pool.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)


def _json_col(val) -> dict:
    """Deserialize a JSONB column that may arrive as dict, str, or None."""
    if isinstance(val, dict):
        return val
    return json.loads(val) if val else {}


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://parking:parking@localhost/parking")

# ── Connection pool (API server only) ────────────────────────────────────────

_pool = None
_pool_lock = threading.Lock()


def get_pool():
    """Return a thread-safe connection pool (lazy-init). Use in the API server."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:  # double-checked locking
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
        payload = _json_col(row["payload"])
        result.append({
            "slot_id": row["slot_id"],
            "event_type": row["event_type"],
            "device_eui": row["device_eui"],
            "ts": row["ts"],
            "payload": payload,
        })
    return result


