"""Unit tests for workers/inference_worker.py — 4 tests.

extract_all_license_plates (OpenAI call) is mocked.
DB inserts are tested via the mock_db fixture (MagicMock psycopg conn).
time.sleep is patched to keep retry tests fast.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import fakeredis
import pytest

from workers.inference_worker import (
    DEADLETTER_KEY,
    process_inference_job,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_fields(image_path: str, task_type: str = "camera_capture") -> dict:
    return {
        b"slot_id": b"1",
        b"slot_name": b"A1",
        b"zone": b"A",
        b"camera_id": b"CAM_01",
        b"image_path": image_path.encode(),
        b"capture_ts": b"2026-01-01T10:00:00+00:00",
        b"trigger_ts": b"2026-01-01T09:59:55+00:00",
        b"task_type": task_type.encode(),
        b"first_plates": b"",
        b"first_image": b"",
        b"first_time": b"",
        b"recheck_count": b"0",
        b"capture_session_id": b"test-session-001",
    }


_PLATE_RESULT = {
    "plates": [{"plate_text": "MH01AB1234", "confidence": 0.95}],
    "vehicle_detected": True,
}
_NO_PLATE_RESULT = {
    "plates": [],
    "vehicle_detected": True,
}

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
def _patch_cameras():
    """Stub out cameras.yaml reads so tests don't need the config file."""
    with (
        patch("workers.inference_worker._get_cam_id_for_slot", return_value="CAM_01"),
        patch("workers.inference_worker._get_slot_presets", return_value={1: 2}),
    ):
        yield


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestProcessInferenceJob:

    def test_10_happy_path_plate_detected(self, r, db, tmp_path):
        """HAPPY PATH: image exists + OpenAI returns plate → camera_captures INSERT + pending key set."""
        img = tmp_path / "slot_1_test.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0FAKEJPEG")

        fields = _make_fields(str(img))

        with patch("workers.inference_worker.extract_all_license_plates", return_value=_PLATE_RESULT):
            result = process_inference_job(r, db, b"10-0", fields)

        assert result is True, "job must be ACK'd"
        db.execute.assert_called_once()
        db.commit.assert_called_once()

        # Pending challan key must be set for the recheck scheduler
        pending_raw = r.get(b"parking:challan:pending:1")
        assert pending_raw is not None, "pending key must be set when plate found"
        pending = json.loads(pending_raw)
        assert "MH01AB1234" in pending["plates"]

        # Recheck camera task must be enqueued
        assert r.xlen(b"parking:camera:tasks:CAM_01") == 1

    def test_11_openai_429_dead_lettered_after_3_retries(self, r, db, tmp_path):
        """OPENAI 429: all 3 attempts fail → message goes to deadletter, job ACK'd."""
        img = tmp_path / "slot_1_test.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0FAKEJPEG")

        fields = _make_fields(str(img))

        # Raise 429 on every attempt
        side_effects = [Exception("openai 429 too many requests")] * 3

        with patch("workers.inference_worker.extract_all_license_plates", side_effect=side_effects) as mock_ocr:
            with patch("workers.inference_worker.time.sleep") as mock_sleep:  # fast-forward backoff
                result = process_inference_job(r, db, b"11-0", fields)

        assert result is True, "deadlettered job must still be ACK'd"
        assert r.xlen(DEADLETTER_KEY) == 1, "failed job must go to deadletter stream"
        assert mock_ocr.call_count == 3, "must attempt exactly 3 times"
        # At least the first two retries have a sleep between them
        assert mock_sleep.call_count >= 2

        # No DB insert on complete failure
        db.execute.assert_not_called()

    def test_12_image_not_found_dead_lettered(self, r, db):
        """IMAGE NOT FOUND: non-existent path → deadletter, ACK'd, no DB insert."""
        fields = _make_fields("/tmp/this_file_does_not_exist_9999.jpg")

        result = process_inference_job(r, db, b"12-0", fields)

        assert result is True
        assert r.xlen(DEADLETTER_KEY) == 1, "missing image must go to deadletter"
        db.execute.assert_not_called()

    def test_13_no_plate_text_inserts_capture_no_challan(self, r, db, tmp_path):
        """NO PLATE TEXT: OpenAI returns empty plates → capture INSERT, no pending key."""
        img = tmp_path / "slot_1_empty.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0FAKEJPEG")

        fields = _make_fields(str(img))

        with patch("workers.inference_worker.extract_all_license_plates", return_value=_NO_PLATE_RESULT):
            result = process_inference_job(r, db, b"13-0", fields)

        assert result is True, "no-plate result is not a failure — must be ACK'd"
        # camera_capture still inserted (even without a plate)
        db.execute.assert_called_once()
        db.commit.assert_called_once()

        # No pending challan key — nothing to match
        assert r.get(b"parking:challan:pending:1") is None
        # No recheck task enqueued — no plate to follow up on
        assert r.xlen(b"parking:camera:tasks:CAM_01") == 0
