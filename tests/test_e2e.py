"""End-to-end tests — 3 tests covering the critical deployment paths.

These tests wire up the actual worker functions against FakeRedis (no real
Redis required) and a MagicMock Postgres connection.  OpenAI and the camera
hardware are stubbed.

Critical paths per the deployment checklist:
  E2E #21 — Full flow: MQTT uplink → challan issued
  E2E #22 — No duplicate: rapid FREE→OCCUPIED→FREE doesn't create 2 tasks
  E2E #23 — Crash recovery: XAUTOCLAIM picks up an un-ACK'd message
"""
from __future__ import annotations

import base64
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from workers.mqtt_worker import (
    GROUP_NAME as MQTT_GROUP,
    STREAM_KEY as MQTT_STREAM,
    process_mqtt_message,
)
from workers.camera_worker import (
    INFERENCE_STREAM,
    process_camera_task,
)
from workers.inference_worker import (
    process_inference_job,
)

pytestmark = pytest.mark.e2e


# ── Helpers ───────────────────────────────────────────────────────────────────

def _b64(hex_str: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_str)).decode()


def _mqtt_fields(status_hex: str, device_name: str = "SENSOR_01") -> dict:
    payload = {
        "deviceInfo": {"deviceName": device_name, "devEui": "aabbccdd11223344"},
        "data": _b64(status_hex),
    }
    return {b"payload": json.dumps(payload).encode()}


_SLOT_META = {1: {"name": "A1", "zone": "A", "preset": 2}}
_CAM_ASSIGNMENT = {1: "CAM_01"}
_PLATE_RESULT = {
    "plates": [{"plate_text": "MH01AB1234", "confidence": 0.95}],
    "vehicle_detected": True,
}


def _make_db_mock() -> MagicMock:
    db = MagicMock()
    db.execute.return_value = MagicMock()
    db.commit.return_value = None
    db.rollback.return_value = None
    return db


def _new_redis() -> fakeredis.FakeRedis:
    server = fakeredis.FakeServer()
    return fakeredis.FakeRedis(server=server, decode_responses=False)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestFullFlow:

    def test_21_mqtt_uplink_to_challan(self, tmp_path):
        """FULL FLOW: ChirpStack MQTT uplink → camera capture → inference → challan insert.

        All three workers run in sequence against a shared FakeRedis.
        Camera hardware and OpenAI are mocked.
        Asserts:
          - camera_captures INSERT called (via db mock)
          - parking:challan:pending:1 set in Redis (recheck scheduled)
        """
        r = _new_redis()
        db = _make_db_mock()

        with (
            patch("workers.mqtt_worker.load_slot_meta_by_id", return_value=_SLOT_META),
            patch("workers.mqtt_worker.get_slot_id_by_device_name", return_value=1),
            patch("workers.mqtt_worker._load_camera_assignment", return_value=_CAM_ASSIGNMENT),
            patch("workers.inference_worker.extract_all_license_plates", return_value=_PLATE_RESULT),
            patch("workers.inference_worker._get_cam_id_for_slot", return_value="CAM_01"),
            patch("workers.inference_worker._get_slot_presets", return_value={1: 2}),
        ):
            return self._run_full_flow(r, db, tmp_path)

    def _run_full_flow(self, r, db, tmp_path):
        # ── Step 1: mqtt_worker processes the uplink ──────────────────────────
        r.xgroup_create(MQTT_STREAM, MQTT_GROUP, id="0", mkstream=True)
        r.xadd(MQTT_STREAM, _mqtt_fields("01"))  # OCCUPIED
        results = r.xreadgroup(MQTT_GROUP, "worker-test", {MQTT_STREAM: ">"}, count=1)
        msg_id, fields = results[0][1][0]

        with patch.object(r, "eval", return_value=1):  # CAS wins
            ok = process_mqtt_message(r, db, msg_id, fields)
        assert ok
        r.xack(MQTT_STREAM, MQTT_GROUP, msg_id)

        # ── Step 2: camera_worker processes the camera task ───────────────────
        cam_stream = "parking:camera:tasks:CAM_01"
        cam_group = "cam-CAM_01"
        r.xgroup_create(cam_stream, cam_group, id="0", mkstream=True)

        cam_results = r.xreadgroup(cam_group, "cam-worker-test", {cam_stream: ">"}, count=1)
        assert cam_results, "camera task must be in stream after mqtt_worker step"

        cam_msg_id, cam_fields = cam_results[0][1][0]

        # Mock camera: PTZ succeeds, capture writes a fake JPEG
        ctrl = MagicMock()
        ctrl.move_to_preset.return_value = True

        def _write_jpeg(path: Path) -> bool:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xff\xd8\xff\xe0FAKEJPEG")
            return True

        ctrl.capture_frame.side_effect = _write_jpeg

        cam_cfg = {
            "slot_presets": {1: 2, "1": 2},
            "settle_time": 0,
            "capture_timeout": 5.0,
        }

        with patch("workers.camera_worker.SNAPSHOTS_DIR", tmp_path):
            ok = process_camera_task(
                r, "CAM_01", cam_cfg, ctrl,
                cam_stream, cam_group,
                cam_msg_id, cam_fields,
            )
        assert ok
        r.xack(cam_stream, cam_group, cam_msg_id)

        # ── Step 3: inference_worker processes the inference job ──────────────
        inf_group = "inference-workers"
        r.xgroup_create(INFERENCE_STREAM, inf_group, id="0", mkstream=True)

        inf_results = r.xreadgroup(inf_group, "inf-worker-test", {INFERENCE_STREAM: ">"}, count=1)
        assert inf_results, "inference job must be in stream after camera_worker step"

        inf_msg_id, inf_fields = inf_results[0][1][0]

        ok = process_inference_job(r, db, inf_msg_id, inf_fields)
        assert ok
        r.xack(INFERENCE_STREAM, inf_group, inf_msg_id)

        # ── Assertions ────────────────────────────────────────────────────────
        # camera_captures INSERT + commit must have been called
        db.execute.assert_called()
        db.commit.assert_called()

        # Recheck (challan pending) must be scheduled in Redis
        pending_raw = r.get(b"parking:challan:pending:1")
        assert pending_raw is not None, "challan pending key must be set after inference"
        pending = json.loads(pending_raw)
        assert "MH01AB1234" in pending["plates"]


class TestNoDuplicateCameraTask:

    @patch("workers.mqtt_worker.load_slot_meta_by_id", return_value=_SLOT_META)
    @patch("workers.mqtt_worker.get_slot_id_by_device_name", return_value=1)
    @patch("workers.mqtt_worker._load_camera_assignment", return_value=_CAM_ASSIGNMENT)
    def test_22_rapid_events_produce_exactly_one_camera_task(self, *_mocks):
        """NO DUPLICATE: rapid FREE→OCCUPIED→FREE processed by two parallel workers.

        Worker-A processes the OCCUPIED event (CAS wins).
        Worker-B processes the same OCCUPIED event (CAS loses — slot already OCCUPIED).
        Result: exactly 1 camera task in stream.
        """
        r = _new_redis()
        db_a = _make_db_mock()
        db_b = _make_db_mock()

        r.xgroup_create(MQTT_STREAM, MQTT_GROUP, id="0", mkstream=True)
        r.xadd(MQTT_STREAM, _mqtt_fields("01"))
        r.xadd(MQTT_STREAM, _mqtt_fields("01"))  # rapid duplicate

        results = r.xreadgroup(MQTT_GROUP, "worker-A", {MQTT_STREAM: ">"}, count=2)
        messages = results[0][1]

        errors = []

        def _worker_a():
            try:
                mid, flds = messages[0]
                # Worker A wins the CAS
                with patch.object(r, "eval", return_value=1):
                    ok = process_mqtt_message(r, db_a, mid, flds)
                assert ok
            except Exception as exc:
                errors.append(exc)

        def _worker_b():
            try:
                mid, flds = messages[1]
                # Worker B loses the CAS (slot already OCCUPIED)
                with patch.object(r, "eval", return_value=0):
                    ok = process_mqtt_message(r, db_b, mid, flds)
                assert ok
            except Exception as exc:
                errors.append(exc)

        t_a = threading.Thread(target=_worker_a)
        t_b = threading.Thread(target=_worker_b)
        t_a.start()
        t_b.start()
        t_a.join(timeout=5)
        t_b.join(timeout=5)

        assert not errors, f"worker threads raised: {errors}"

        # Critical: exactly 1 camera task
        assert r.xlen(b"parking:camera:tasks:CAM_01") == 1, (
            "duplicate OCCUPIED events must result in exactly 1 camera task"
        )

        # Worker B (CAS lose) must NOT have written to DB
        db_b.execute.assert_not_called()


class TestCrashRecovery:

    @patch("workers.mqtt_worker.load_slot_meta_by_id", return_value=_SLOT_META)
    @patch("workers.mqtt_worker.get_slot_id_by_device_name", return_value=1)
    @patch("workers.mqtt_worker._load_camera_assignment", return_value=_CAM_ASSIGNMENT)
    def test_23_xautoclaim_recovers_unacked_message(self, *_mocks):
        """CRASH RECOVERY: worker-A dies mid-message, worker-B reclaims via XAUTOCLAIM.

        The test simulates a crash by claiming without ACKing, then immediately
        calling XAUTOCLAIM (min_idle_time=0 to bypass the real 10s wait).

        Asserts:
          - worker-B processes the message successfully
          - occupancy_events INSERT called exactly once (no duplicate)
          - PEL is empty after worker-B ACKs
        """
        r = _new_redis()
        db_b = _make_db_mock()

        r.xgroup_create(MQTT_STREAM, MQTT_GROUP, id="0", mkstream=True)
        r.xadd(MQTT_STREAM, _mqtt_fields("01"))

        # Worker-A claims the message but "crashes" before ACKing
        results = r.xreadgroup(MQTT_GROUP, "worker-A", {MQTT_STREAM: ">"}, count=1)
        assert results
        # (worker-A is now dead — no xack call)

        # Worker-B recovers via XAUTOCLAIM (min_idle_time=0 = instant reclaim in tests)
        claimed = r.xautoclaim(
            MQTT_STREAM, MQTT_GROUP, "worker-B",
            min_idle_time=0, start_id="0-0", count=10,
        )
        claimed_messages = claimed[1]
        assert len(claimed_messages) == 1, "worker-B must reclaim the pending message"

        reclaimed_id, reclaimed_fields = claimed_messages[0]
        with patch.object(r, "eval", return_value=1):
            ok = process_mqtt_message(r, db_b, reclaimed_id, reclaimed_fields)

        assert ok
        r.xack(MQTT_STREAM, MQTT_GROUP, reclaimed_id)

        # Exactly 1 occupancy INSERT (no duplicate from worker-A which never committed)
        db_b.execute.assert_called_once()
        db_b.commit.assert_called_once()

        # PEL is now empty
        pending = r.xpending(MQTT_STREAM, MQTT_GROUP)
        assert pending["pending"] == 0, "PEL must be empty after recovery"
