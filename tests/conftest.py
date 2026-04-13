"""Shared pytest fixtures for parking_vision_poc tests."""
from __future__ import annotations

import os
import pytest
import fakeredis
from unittest.mock import MagicMock


# ── Redis ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_redis():
    """FakeRedis instance with Streams support (fakeredis v2)."""
    server = fakeredis.FakeServer()
    r = fakeredis.FakeRedis(server=server, decode_responses=False)
    yield r
    r.close()


# ── Postgres ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_db():
    """Mock psycopg connection for unit tests that don't need a real DB."""
    conn = MagicMock()
    conn.execute.return_value = MagicMock()
    conn.commit.return_value = None
    conn.rollback.return_value = None
    return conn


@pytest.fixture(scope="module")
def pg_conn():
    """Real Postgres connection for integration tests.

    Skipped when TEST_DATABASE_URL is not set or the DB is unreachable.
    """
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://parking:parking@localhost/parking_test",
    )
    try:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(url, row_factory=dict_row)
        # Ensure schema tables exist
        _bootstrap_schema(conn)
        yield conn
        conn.close()
    except Exception as exc:
        pytest.skip(f"Postgres not available ({exc})")


def _bootstrap_schema(conn):
    """Create test tables if they don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS occupancy_events (
            id BIGSERIAL PRIMARY KEY,
            slot_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            device_eui TEXT,
            ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            payload JSONB
        )
    """)
    conn.commit()
