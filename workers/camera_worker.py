"""Camera worker — one process per physical camera.

Run as:
    python -m workers.camera_worker CAM_01

Reads from parking:camera:tasks:{CAM_ID}, performs PTZ move → settle → capture,
then enqueues the image to parking:inference:jobs.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import redis

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from webapp.camera_controller import CameraController
from webapp.helpers.data_io import load_yaml
from webapp.helpers.slot_meta import load_slot_meta_by_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("camera_worker")

# ── Config ────────────────────────────────────────────────────────────────────

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
CAMERAS_YAML_PATH = _REPO_ROOT / "config" / "cameras.yaml"
SLOT_META_PATH = _REPO_ROOT / "config" / "slot_meta.yaml"
SNAPSHOTS_DIR = Path(os.environ.get("SNAPSHOTS_DIR", "/data/snapshots"))

INFERENCE_STREAM = "parking:inference:jobs"
AUTOCLAIM_IDLE_MS = 30_000  # reclaim camera tasks idle >30s
BLOCK_MS = 2000


def _load_camera_config(cam_id: str) -> dict:
    """Load this camera's config from cameras.yaml."""
    data = load_yaml(CAMERAS_YAML_PATH) or {}
    cameras = data.get("cameras", {})
    if cam_id not in cameras:
        raise RuntimeError(f"Camera '{cam_id}' not found in cameras.yaml")
    return cameras[cam_id]


def _build_rtsp_url(cfg: dict) -> str:
    """Build RTSP URL from camera config, or use explicit rtsp_url if provided."""
    if cfg.get("rtsp_url"):
        return cfg["rtsp_url"]
    user = quote(cfg.get("user", "admin"), safe="")
    passwd = quote(cfg.get("password", "admin"), safe="")
    ip = cfg["ip"]
    return f"rtsp://{user}:{passwd}@{ip}/stream1"


def process_camera_task(
    r: redis.Redis,
    cam_id: str,
    cam_cfg: dict,
    controller: CameraController,
    stream_key: str,
    group_name: str,
    msg_id: bytes,
    fields: dict,
) -> bool:
    """Execute one camera task. Returns True to XACK, False to leave in PEL."""
    try:
        def _field(key: str, default="") -> str:
            v = fields.get(key.encode()) or fields.get(key, default)
            return v.decode() if isinstance(v, bytes) else str(v or default)

        slot_id = int(_field("slot_id", "0"))
        slot_name = _field("slot_name", str(slot_id))
        zone = _field("zone", "A")
        preset_str = _field("preset", "")
        trigger_ts = _field("trigger_ts", datetime.now(timezone.utc).isoformat())
        task_type = _field("task_type", "camera_capture")

        # Scheduled tasks (challan rechecks): skip if not yet due
        # XAUTOCLAIM will re-deliver when the idle timeout expires
        scheduled_at_str = _field("scheduled_at", "")
        if scheduled_at_str:
            try:
                scheduled_at = datetime.fromisoformat(
                    scheduled_at_str.replace("Z", "+00:00")
                )
                if scheduled_at > datetime.now(timezone.utc):
                    wait_s = (scheduled_at - datetime.now(timezone.utc)).total_seconds()
                    log.debug("Challan recheck for slot %s due in %.0fs — skipping (XAUTOCLAIM will retry)",
                              slot_name, wait_s)
                    return False  # do NOT ack — leave in PEL for XAUTOCLAIM to reclaim later
            except Exception:
                pass

        # Look up preset from camera config
        slot_presets = cam_cfg.get("slot_presets", {})
        preset = slot_presets.get(slot_id) or slot_presets.get(str(slot_id))
        if preset is None and preset_str:
            try:
                preset = int(preset_str)
            except ValueError:
                pass
        if preset is None:
            # Fallback: check slot_meta.yaml
            meta_by_id = load_slot_meta_by_id(SLOT_META_PATH)
            preset = meta_by_id.get(slot_id, {}).get("preset")

        if preset is None:
            log.error("No preset for slot %d (%s) on camera %s — skipping", slot_id, slot_name, cam_id)
            return True  # ack — nothing we can do

        settle_time = float(cam_cfg.get("settle_time", 8.0))
        capture_timeout = float(cam_cfg.get("capture_timeout", 10.0))

        log.info("Camera task: cam=%s slot=%s preset=%s type=%s", cam_id, slot_name, preset, task_type)

        # Step 1: PTZ move
        if not controller.move_to_preset(preset):
            log.error("PTZ move failed for slot %s on %s — acking (no retry)", slot_name, cam_id)
            return True

        # Step 2: Wait for camera to settle
        log.info("Waiting %.0fs for camera to settle...", settle_time)
        time.sleep(settle_time)

        # Step 3: Capture image
        capture_ts = datetime.now(timezone.utc)
        date_str = capture_ts.strftime("%Y-%m-%d")
        ts_str = capture_ts.strftime("%Y%m%d_%H%M%S")
        prefix = "challan" if task_type == "challan_recheck" else "slot"
        filename = f"{prefix}_{slot_id}_{ts_str}.jpg"
        output_dir = SNAPSHOTS_DIR / date_str
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / filename

        if not controller.capture_frame(output_path):
            log.error("Frame capture failed for slot %s — acking (no retry)", slot_name)
            return True

        image_path = str(output_path)
        log.info("Captured: %s", image_path)

        # Step 4: Enqueue inference job
        job_fields: dict = {
            "slot_id": str(slot_id),
            "slot_name": slot_name,
            "zone": zone,
            "camera_id": cam_id,
            "image_path": image_path,
            "capture_ts": capture_ts.isoformat(),
            "trigger_ts": trigger_ts,
            "task_type": task_type,
        }
        # Pass through challan recheck context
        for extra_key in ("first_plates", "first_image", "first_time",
                          "recheck_count", "capture_session_id", "lat", "lng"):
            val = _field(extra_key, "")
            if val:
                job_fields[extra_key] = val

        r.xadd(INFERENCE_STREAM, job_fields, maxlen=10_000, approximate=True)
        log.info("Inference job enqueued for slot %s", slot_name)
        return True

    except Exception as e:
        log.error("Camera task error: %s", e, exc_info=True)
        return True  # ack to avoid infinite retry loop on hard errors


def run(cam_id: str):
    log.info("Camera worker starting for %s", cam_id)

    cam_cfg = _load_camera_config(cam_id)
    ip = cam_cfg["ip"]
    user = cam_cfg.get("user", "admin")
    password = cam_cfg.get("password", "admin")
    rtsp_url = _build_rtsp_url(cam_cfg)

    controller = CameraController(ip=ip, user=user, password=password, rtsp_url=rtsp_url)
    log.info("CameraController initialised for %s at %s", cam_id, ip)

    r = redis.Redis.from_url(REDIS_URL, decode_responses=False)

    stream_key = f"parking:camera:tasks:{cam_id}"
    group_name = f"cam-{cam_id}"

    try:
        r.xgroup_create(stream_key, group_name, id="0", mkstream=True)
        log.info("Created consumer group %s on %s", group_name, stream_key)
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" in str(e):
            log.debug("Consumer group %s already exists", group_name)
        else:
            raise

    _running = True

    def _handle_signal(sig, frame):
        nonlocal _running
        log.info("Signal %s — shutting down camera worker %s", sig, cam_id)
        _running = False

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    autoclaim_cursor = "0-0"

    while _running:
        # XAUTOCLAIM: reclaim tasks idle >30s
        try:
            claimed = r.xautoclaim(
                stream_key, group_name, f"{cam_id}-worker",
                min_idle_time=AUTOCLAIM_IDLE_MS,
                start_id=autoclaim_cursor,
                count=5,
            )
            autoclaim_cursor_new = claimed[0]
            claimed_messages = claimed[1]
            if claimed_messages:
                log.info("Reclaimed %d idle camera task(s)", len(claimed_messages))
                for msg_id, fields in claimed_messages:
                    ok = process_camera_task(
                        r, cam_id, cam_cfg, controller,
                        stream_key, group_name, msg_id, fields,
                    )
                    if ok:
                        r.xack(stream_key, group_name, msg_id)
            autoclaim_cursor = autoclaim_cursor_new if claimed_messages else "0-0"
        except Exception as e:
            log.error("XAUTOCLAIM error: %s", e)
            autoclaim_cursor = "0-0"

        # XREADGROUP: read new tasks
        try:
            results = r.xreadgroup(
                group_name, f"{cam_id}-worker",
                {stream_key: ">"},
                count=1,
                block=BLOCK_MS,
            )
        except redis.exceptions.ConnectionError as e:
            log.error("Redis connection error: %s — retrying", e)
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

        for stream_name, messages in results:
            for msg_id, fields in messages:
                ok = process_camera_task(
                    r, cam_id, cam_cfg, controller,
                    stream_key, group_name, msg_id, fields,
                )
                if ok:
                    r.xack(stream_key, group_name, msg_id)

    log.info("Camera worker %s stopped", cam_id)
    try:
        controller.close()
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m workers.camera_worker <CAM_ID>", file=sys.stderr)
        sys.exit(1)
    run(sys.argv[1])
