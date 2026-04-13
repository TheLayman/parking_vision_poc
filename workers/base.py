"""Shared utilities for Redis Stream workers.

Provides the stream consumer loop, field extraction, signal handling,
and DB reconnection logic for the mqtt_worker.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from pathlib import Path
from typing import Callable

import redis

_REPO_ROOT = Path(__file__).resolve().parent.parent

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

log = logging.getLogger(__name__)


# ── Stream field extraction ──────────────────────────────────────────────────

def stream_field(fields: dict, key: str, default: str = "") -> str:
    """Extract a string value from a Redis Stream message.

    Handles both byte and string keys/values returned by redis-py
    with decode_responses=False.
    """
    v = fields.get(key.encode()) or fields.get(key, default)
    return v.decode() if isinstance(v, bytes) else str(v or default)


# ── DB reconnect ─────────────────────────────────────────────────────────────

def ensure_db_conn(db_conn):
    """Return a live DB connection, reconnecting if broken."""
    from db.client import get_connection
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


# ── Stream consumer loop ─────────────────────────────────────────────────────

def run_stream_worker(
    stream_key: str,
    group_name: str,
    worker_id: str,
    process_fn: Callable,
    *,
    autoclaim_idle_ms: int = 10_000,
    autoclaim_count: int = 10,
    xread_count: int = 10,
    block_ms: int = 1000,
    needs_db: bool = False,
    on_shutdown: Callable | None = None,
    worker_label: str = "worker",
):
    """Generic Redis Stream consumer loop with XAUTOCLAIM crash recovery.

    Args:
        stream_key: Redis stream to consume.
        group_name: Consumer group name.
        worker_id: Consumer ID within the group.
        process_fn: Called as process_fn(r, db_conn, msg_id, fields) if needs_db,
                    or process_fn(r, msg_id, fields) otherwise.
                    Must return True to XACK, False to leave in PEL.
        autoclaim_idle_ms: Reclaim messages idle longer than this.
        autoclaim_count: Max messages to reclaim per cycle.
        xread_count: Max messages per XREADGROUP call.
        block_ms: XREADGROUP block timeout.
        needs_db: Whether to maintain a DB connection.
        on_shutdown: Optional cleanup callback.
        worker_label: Label for log messages.
    """
    r = redis.Redis.from_url(REDIS_URL, decode_responses=False)

    try:
        r.xgroup_create(stream_key, group_name, id="0", mkstream=True)
        log.info("Created consumer group %s on %s", group_name, stream_key)
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise

    db_conn = None
    if needs_db:
        from db.client import get_connection
        db_conn = get_connection()
        log.info("Postgres connection established")

    running = True

    def _handle_signal(sig, frame):
        nonlocal running
        log.info("Signal %s — shutting down %s", sig, worker_label)
        running = False

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    autoclaim_cursor = "0-0"

    def _call_process(msg_id, fields):
        nonlocal db_conn
        if needs_db:
            db_conn = ensure_db_conn(db_conn)
            return process_fn(r, db_conn, msg_id, fields)
        return process_fn(r, msg_id, fields)

    while running:
        # XAUTOCLAIM: reclaim idle messages (crash recovery)
        try:
            claimed = r.xautoclaim(
                stream_key, group_name, worker_id,
                min_idle_time=autoclaim_idle_ms,
                start_id=autoclaim_cursor,
                count=autoclaim_count,
            )
            new_cursor, claimed_messages = claimed[0], claimed[1]
            if claimed_messages:
                log.info("Reclaimed %d idle message(s)", len(claimed_messages))
                for msg_id, fields in claimed_messages:
                    if _call_process(msg_id, fields):
                        r.xack(stream_key, group_name, msg_id)
            autoclaim_cursor = new_cursor if claimed_messages else "0-0"
        except redis.exceptions.ResponseError:
            autoclaim_cursor = "0-0"
        except Exception as e:
            log.error("XAUTOCLAIM error: %s", e)
            autoclaim_cursor = "0-0"
            time.sleep(1)

        # XREADGROUP: read new messages
        try:
            results = r.xreadgroup(
                group_name, worker_id,
                {stream_key: ">"},
                count=xread_count,
                block=block_ms,
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

        for _stream_name, messages in results:
            for msg_id, fields in messages:
                if _call_process(msg_id, fields):
                    r.xack(stream_key, group_name, msg_id)

    log.info("%s stopped", worker_label.capitalize())

    if on_shutdown:
        on_shutdown()

    if db_conn:
        try:
            db_conn.close()
        except Exception:
            pass
