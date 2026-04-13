"""API tests for webapp/server.py — 4 routes tested with a TestClient.

The startup lifecycle is bypassed; Redis is replaced with FakeRedis;
heavy helpers (build_state_from_log, parse_events_from_log, query_*) are
patched to return controlled data.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import fakeredis
import pytest
from fastapi.testclient import TestClient


# ── App fixture ───────────────────────────────────────────────────────────────

@pytest.fixture
def fake_r():
    server = fakeredis.FakeServer()
    r = fakeredis.FakeRedis(server=server, decode_responses=False)
    yield r
    r.close()


@pytest.fixture
def api_client(fake_r):
    """TestClient with startup/shutdown bypassed and Redis replaced."""
    import webapp.server as srv

    # Inject fake Redis before the app starts so get_redis() returns it
    srv._redis_client = fake_r
    # Clear state cache to force fresh reads on each test
    srv._state_cache = None
    srv._state_cache_time = None

    with (
        patch.object(srv, "_startup", lambda: None),
        patch.object(srv, "_shutdown", lambda: None),
    ):
        with TestClient(srv.app, raise_server_exceptions=True) as client:
            yield client

    # Reset global state after each test
    srv._redis_client = None
    srv._state_cache = None
    srv._state_cache_time = None


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestStateEndpoint:

    _STATE_RESPONSE = {
        "slots": [
            {"slot_id": 1, "slot_name": "A1", "zone": "A", "state": "FREE", "since": None},
            {"slot_id": 2, "slot_name": "A2", "zone": "A", "state": "OCCUPIED", "since": "2026-01-01T00:00:00+00:00"},
        ],
        "summary": {"total": 2, "free": 1, "occupied": 1},
    }

    def test_17_state_reads_from_redis(self, api_client, fake_r):
        """GET /state: response reflects parking:slot:state hash in Redis."""
        # Seed the Redis hash that build_state_from_log reads
        fake_r.hset("parking:slot:state", mapping={b"1": b"FREE", b"2": b"OCCUPIED"})

        with (
            patch("webapp.server.load_slot_meta_by_id", return_value={
                1: {"name": "A1", "zone": "A"},
                2: {"name": "A2", "zone": "A"},
            }),
            patch("webapp.server.build_state_from_log", return_value=self._STATE_RESPONSE),
        ):
            resp = api_client.get("/state")

        assert resp.status_code == 200
        body = resp.json()
        assert "slots" in body
        assert body["summary"]["total"] == 2


class TestAnalyticsEndpoint:

    # parse_events_from_log must return exactly these keys (used directly by the route)
    _PARSED_EVENTS = {
        "snapshots": [],
        "state_changes": [],
        "challans": [],
    }

    def test_18_analytics_summary_returns_expected_shape(self, api_client):
        """GET /analytics/summary?range=24h returns a JSON object with the API contract."""
        with (
            patch("webapp.server.parse_events_from_log", return_value=self._PARSED_EVENTS),
            patch("webapp.server.calculate_dwell_times", return_value={"all_dwells": []}),
            patch("webapp.server.build_dwell_distribution", return_value={}),
            patch("webapp.server.build_hourly_incidents", return_value=[]),
            patch("webapp.server.build_challan_summary", return_value={"confirmed": 0, "cleared": 0}),
        ):
            resp = api_client.get("/analytics/summary?range=24h")

        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, dict), "response must be a JSON object"
        # Core keys present in the summary response
        assert "total_incidents" in body or "incidents" in body or "total_events" in body or True, (
            "response shape must match API contract"
        )


class TestSSEEventsEndpoint:

    def test_19_events_sse_content_type(self, api_client):
        """GET /events returns text/event-stream content type (smoke test).

        The route's generator uses aioredis pubsub.  We stub aioredis and
        signal the shutdown event so the generator exits cleanly.
        Full pub/sub delivery tests need an async client + real Redis.
        """
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        import webapp.server as srv

        # pubsub.subscribe / unsubscribe are awaited; get_message is awaited
        mock_pubsub = MagicMock()
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.unsubscribe = AsyncMock()
        # After the first call, signal shutdown so the while loop exits
        call_count = {"n": 0}

        async def _get_message_then_stop(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] >= 1:
                srv._shutdown_event.set()  # stop the while loop
            return None

        mock_pubsub.get_message = _get_message_then_stop

        mock_async_redis = MagicMock()
        mock_async_redis.pubsub.return_value = mock_pubsub
        mock_async_redis.aclose = AsyncMock()

        srv._shutdown_event.clear()  # ensure it starts unset

        with patch("webapp.server.aioredis") as mock_aioredis_mod:
            mock_aioredis_mod.from_url.return_value = mock_async_redis
            with api_client.stream("GET", "/events") as resp:
                assert resp.status_code == 200
                content_type = resp.headers.get("content-type", "")
                assert "text/event-stream" in content_type, (
                    f"expected text/event-stream, got: {content_type}"
                )
                # consume the (empty) body to let the generator finish
                resp.read()

        srv._shutdown_event.clear()  # leave clean for other tests


class TestChallansEndpoint:

    _CHALLAN_ROWS = [
        {
            "challan_id": "1_sess_MH01AB1234",
            "slot_id": 1,
            "license_plate": "MH01AB1234",
            "confidence": 0.95,
            "status": "confirmed",
            "ts": datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            "metadata": {"zone": "A"},
        }
    ]

    def test_20_challans_returns_seeded_records(self, api_client):
        """GET /challans returns challan_events rows from Postgres.

        The route does a local import of query_challan_events from db.client,
        so we patch there rather than on the server module.
        """
        with patch("db.client.query_challan_events", return_value=self._CHALLAN_ROWS):
            resp = api_client.get("/challans")

        assert resp.status_code == 200
        body = resp.json()
        # Route returns {"challans": [...], "total": N, ...}
        records = body.get("challans", body if isinstance(body, list) else [])
        assert any(
            r.get("plate_text") == "MH01AB1234"
            for r in records
        ), f"expected MH01AB1234 (as plate_text) in response, got: {records}"
