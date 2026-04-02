"""Unit tests for workers/mqtt_worker.py — 5 tests covering the core processing paths.

Tests use fakeredis for Redis + MagicMock for Postgres.
r.eval (the Lua CAS script) is patched to control win/lose behaviour; the
important assertions are on downstream Redis state and DB call counts.
"""
from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from workers.mqtt_worker import (
    GROUP_NAME,
    STREAM_KEY,
    process_mqtt_message,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _b64(hex_str: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_str)).decode()


def _make_fields(status_hex: str, device_name: str = "SENSOR_01") -> dict:
    payload = {
        "deviceInfo": {"deviceName": device_name, "devEui": "aabbccdd11223344"},
        "data": _b64(status_hex),
    }
    return {b"payload": json.dumps(payload).encode()}


_SLOT_META = {1: {"name": "A1", "zone": "A", "preset": 2}}
_CAM_ASSIGNMENT = {1: "CAM_01"}

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def r():
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server, decode_responses=False)
    yield client
    client.close()


@pytest.fixture
def db(mock_db):
    return mock_db


@pytest.fixture(autouse=True)
def _patch_meta():
    """Patch slot-meta lookups and camera assignment for all tests in this module."""
    with (
        patch("workers.mqtt_worker.load_slot_meta_by_id", return_value=_SLOT_META),
        patch("workers.mqtt_worker.get_slot_id_by_device_name", return_value=1),
        patch("workers.mqtt_worker.get_slot_to_camera_map", return_value=_CAM_ASSIGNMENT),
    ):
        yield


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestProcessMqttMessage:

    def test_1_happy_path_free_to_occupied(self, r, db):
        """HAPPY PATH: FREE slot receives OCCUPIED uplink.

        Expects: DB INSERT + commit, camera task enqueued in stream.
        """
        fields = _make_fields("01")  # 0x01 → OCCUPIED

        with patch.object(r, "eval", return_value=1):  # CAS wins: FREE→OCCUPIED
            result = process_mqtt_message(r, db, b"1-0", fields)

        assert result is True, "should return True (XACK)"
        assert r.xlen(b"parking:camera:tasks:CAM_01") == 1, "camera task must be enqueued"
        db.execute.assert_called_once()
        db.commit.assert_called_once()

    def test_2_cas_loses_no_camera_task(self, r, db):
        """CAS LOSES: two workers process the same OCCUPIED event simultaneously.

        The worker that loses the CAS must NOT enqueue a camera task and must
        NOT write to Postgres — the first worker already handled it.
        """
        fields = _make_fields("01")

        with patch.object(r, "eval", return_value=0):  # CAS loses (slot already OCCUPIED)
            result = process_mqtt_message(r, db, b"2-0", fields)

        assert result is True
        assert r.xlen(b"parking:camera:tasks:CAM_01") == 0, "no camera task on CAS loss"
        db.execute.assert_not_called()
        db.commit.assert_not_called()

    def test_3_deduplication_occupied_on_occupied(self, r, db):
        """DEDUPLICATION: sensor noise — OCCUPIED arrives for already-OCCUPIED slot.

        CAS expected_state='FREE' but slot is 'OCCUPIED' → CAS returns 0.
        No second camera task should be enqueued.
        """
        fields = _make_fields("01")

        # First event: wins
        with patch.object(r, "eval", return_value=1):
            process_mqtt_message(r, db, b"3-0", fields)

        db_second = MagicMock()
        # Second (duplicate) event: loses
        with patch.object(r, "eval", return_value=0):
            result = process_mqtt_message(r, db_second, b"3-1", fields)

        assert result is True
        assert r.xlen(b"parking:camera:tasks:CAM_01") == 1, "exactly 1 camera task total"
        db_second.execute.assert_not_called()

    def test_4_malformed_payload_acked_without_crash(self, r, db):
        """MALFORMED PAYLOAD: invalid JSON → worker acks and discards, no crash."""
        fields = {b"payload": b"not-valid-json{{{"}

        result = process_mqtt_message(r, db, b"4-0", fields)

        assert result is True, "malformed message must be ACK'd (not retried forever)"
        db.execute.assert_not_called()
        assert r.xlen(b"parking:camera:tasks:CAM_01") == 0

    def test_5_xautoclaim_reclaims_pending_message(self, r, db):
        """XAUTOCLAIM: message idle >10 s is reclaimed by a second worker.

        Simulated by:
        1. worker-A claims the message via XREADGROUP but does not ACK
        2. XAUTOCLAIM with min_idle_time=0 (immediately claimable)
        3. worker-B processes the reclaimed message

        Asserts: message processed exactly once, PEL empty after ACK.
        """
        r.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)

        # Add the message and have worker-A claim it without ACKing
        fields = _make_fields("01")
        r.xadd(STREAM_KEY, fields)
        results = r.xreadgroup(GROUP_NAME, "worker-A", {STREAM_KEY: ">"}, count=1)
        assert results, "worker-A must receive the message"
        msg_id = results[0][1][0][0]  # stream → messages → first → id

        # Worker-B reclaims via XAUTOCLAIM (min_idle_time=0 so it's instant)
        claimed = r.xautoclaim(
            STREAM_KEY, GROUP_NAME, "worker-B",
            min_idle_time=0, start_id="0-0", count=10,
        )
        claimed_messages = claimed[1]
        assert len(claimed_messages) == 1, "worker-B must reclaim the pending message"

        reclaimed_id, reclaimed_fields = claimed_messages[0]
        with patch.object(r, "eval", return_value=1):
            result = process_mqtt_message(r, db, reclaimed_id, reclaimed_fields)

        assert result is True
        r.xack(STREAM_KEY, GROUP_NAME, reclaimed_id)

        pending = r.xpending(STREAM_KEY, GROUP_NAME)
        assert pending["pending"] == 0, "PEL must be empty after worker-B ACKs"
