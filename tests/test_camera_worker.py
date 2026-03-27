"""Unit tests for workers/camera_worker.py — 4 tests covering PTZ + capture paths.

CameraController is mocked; SNAPSHOTS_DIR is redirected to tmp_path so no
real filesystem path (/data/snapshots) is required.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from workers.camera_worker import process_camera_task, INFERENCE_STREAM

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def r():
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server, decode_responses=False)
    # Camera worker needs a consumer group on the camera stream
    client.xgroup_create("parking:camera:tasks:CAM_01", "cam-CAM_01", id="0", mkstream=True)
    yield client
    client.close()


@pytest.fixture
def cam_cfg():
    return {
        "ip": "192.168.1.100",
        "user": "admin",
        "password": "admin",
        "settle_time": 0,        # no sleeping in tests
        "capture_timeout": 5.0,
        "slot_presets": {1: 2, "1": 2},
    }


@pytest.fixture
def controller():
    ctrl = MagicMock()
    ctrl.move_to_preset.return_value = True
    ctrl.capture_frame.return_value = True
    return ctrl


def _make_task_fields(
    slot_id: int = 1,
    slot_name: str = "A1",
    zone: str = "A",
    preset: str = "2",
    task_type: str = "camera_capture",
    scheduled_at: str = "",
) -> dict:
    return {
        b"slot_id": str(slot_id).encode(),
        b"slot_name": slot_name.encode(),
        b"zone": zone.encode(),
        b"preset": preset.encode(),
        b"trigger_ts": b"2026-01-01T00:00:00+00:00",
        b"task_type": task_type.encode(),
        b"scheduled_at": scheduled_at.encode(),
        b"event_type": b"OCCUPIED",
        b"device_eui": b"aabbccdd",
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestProcessCameraTask:

    def test_6_happy_path_ptz_capture_enqueues_inference(
        self, r, cam_cfg, controller, tmp_path, monkeypatch
    ):
        """HAPPY PATH: PTZ move → settle → capture → inference job enqueued.

        capture_frame writes a fake JPEG; one job must appear in parking:inference:jobs.
        """
        monkeypatch.setattr("workers.camera_worker.SNAPSHOTS_DIR", tmp_path)

        def _write_and_succeed(path: Path) -> bool:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xff\xd8\xff\xe0FAKEJPEG")
            return True

        controller.capture_frame.side_effect = _write_and_succeed

        fields = _make_task_fields()

        result = process_camera_task(
            r, "CAM_01", cam_cfg, controller,
            "parking:camera:tasks:CAM_01", "cam-CAM_01",
            b"6-0", fields,
        )

        assert result is True, "task must be ACK'd"
        controller.move_to_preset.assert_called_once_with(2)
        assert r.xlen(INFERENCE_STREAM) == 1, "exactly one inference job must be enqueued"

        # Verify the job contains the slot info
        jobs = r.xrange(INFERENCE_STREAM)
        job_fields = jobs[0][1]
        assert job_fields[b"slot_id"] == b"1"
        assert job_fields[b"camera_id"] == b"CAM_01"
        assert job_fields[b"task_type"] == b"camera_capture"

    def test_7_camera_timeout_acked_no_inference(
        self, r, cam_cfg, controller, tmp_path, monkeypatch
    ):
        """CAMERA TIMEOUT: PTZ move raises TimeoutError → task ACK'd, no inference job."""
        monkeypatch.setattr("workers.camera_worker.SNAPSHOTS_DIR", tmp_path)
        controller.move_to_preset.side_effect = TimeoutError("PTZ command timed out")

        fields = _make_task_fields()

        result = process_camera_task(
            r, "CAM_01", cam_cfg, controller,
            "parking:camera:tasks:CAM_01", "cam-CAM_01",
            b"7-0", fields,
        )

        assert result is True, "error tasks must be ACK'd to avoid infinite retry"
        assert r.xlen(INFERENCE_STREAM) == 0, "no inference job on timeout"

    def test_8_camera_offline_connection_refused(
        self, r, cam_cfg, controller, tmp_path, monkeypatch
    ):
        """CAMERA OFFLINE: ConnectionRefusedError → task ACK'd, no inference job."""
        monkeypatch.setattr("workers.camera_worker.SNAPSHOTS_DIR", tmp_path)
        controller.move_to_preset.side_effect = ConnectionRefusedError("connection refused")

        fields = _make_task_fields()

        result = process_camera_task(
            r, "CAM_01", cam_cfg, controller,
            "parking:camera:tasks:CAM_01", "cam-CAM_01",
            b"8-0", fields,
        )

        assert result is True
        assert r.xlen(INFERENCE_STREAM) == 0

    def test_9_empty_capture_acked_no_inference(
        self, r, cam_cfg, controller, tmp_path, monkeypatch
    ):
        """EMPTY CAPTURE: capture_frame returns False → task ACK'd, no inference job."""
        monkeypatch.setattr("workers.camera_worker.SNAPSHOTS_DIR", tmp_path)
        controller.move_to_preset.return_value = True
        controller.capture_frame.return_value = False  # no frame acquired

        fields = _make_task_fields()

        result = process_camera_task(
            r, "CAM_01", cam_cfg, controller,
            "parking:camera:tasks:CAM_01", "cam-CAM_01",
            b"9-0", fields,
        )

        assert result is True
        assert r.xlen(INFERENCE_STREAM) == 0, "no inference job when capture fails"
