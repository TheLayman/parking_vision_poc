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
from workers.base import stream_field, run_stream_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("camera_worker")

# ── Config ────────────────────────────────────────────────────────────────────

CAMERAS_YAML_PATH = _REPO_ROOT / "config" / "cameras.yaml"
SLOT_META_PATH = _REPO_ROOT / "config" / "slot_meta.yaml"
SNAPSHOTS_DIR = Path(os.environ.get("SNAPSHOTS_DIR", "/data/snapshots"))
INFERENCE_STREAM = "parking:inference:jobs"
AUTOCLAIM_IDLE_MS = 30_000
BLOCK_MS = 2000


def _load_camera_config(cam_id: str) -> dict:
    data = load_yaml(CAMERAS_YAML_PATH) or {}
    cameras = data.get("cameras", {})
    if cam_id not in cameras:
        raise RuntimeError(f"Camera '{cam_id}' not found in cameras.yaml")
    return cameras[cam_id]


def _build_rtsp_url(cfg: dict) -> str:
    if cfg.get("rtsp_url"):
        return cfg["rtsp_url"]
    user = quote(cfg.get("user", "admin"), safe="")
    passwd = quote(cfg.get("password", "admin"), safe="")
    return f"rtsp://{user}:{passwd}@{cfg['ip']}/stream1"


# ── Core processing ───────────────────────────────────────────────────────────

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
    """Execute one camera task. Returns True to XACK."""
    try:
        _f = lambda key, default="": stream_field(fields, key, default)

        slot_id = int(_f("slot_id", "0"))
        slot_name = _f("slot_name", str(slot_id))
        zone = _f("zone", "A")
        preset_str = _f("preset", "")
        trigger_ts = _f("trigger_ts", datetime.now(timezone.utc).isoformat())
        task_type = _f("task_type", "camera_capture")

        # Scheduled tasks: skip if not yet due
        scheduled_at_str = _f("scheduled_at", "")
        if scheduled_at_str:
            try:
                scheduled_at = datetime.fromisoformat(
                    scheduled_at_str.replace("Z", "+00:00")
                )
                now = datetime.now(timezone.utc)
                if scheduled_at > now:
                    return False  # leave in PEL for XAUTOCLAIM
            except Exception:
                pass

        # Look up preset
        slot_presets = cam_cfg.get("slot_presets", {})
        preset = slot_presets.get(slot_id) or slot_presets.get(str(slot_id))
        if preset is None and preset_str:
            try:
                preset = int(preset_str)
            except ValueError:
                pass
        if preset is None:
            meta_by_id = load_slot_meta_by_id(SLOT_META_PATH)
            preset = meta_by_id.get(slot_id, {}).get("preset")

        if preset is None:
            log.error("No preset for slot %d on camera %s — skipping", slot_id, cam_id)
            return True

        settle_time = float(cam_cfg.get("settle_time", 8.0))

        log.info("Camera task: cam=%s slot=%s preset=%s type=%s", cam_id, slot_name, preset, task_type)

        if not controller.move_to_preset(preset):
            log.error("PTZ move failed for slot %s — acking", slot_name)
            return True

        time.sleep(settle_time)

        # Capture image
        capture_ts = datetime.now(timezone.utc)
        date_str = capture_ts.strftime("%Y-%m-%d")
        prefix = "challan" if task_type == "challan_recheck" else "slot"
        filename = f"{prefix}_{slot_id}_{capture_ts.strftime('%Y%m%d_%H%M%S')}.jpg"
        output_dir = SNAPSHOTS_DIR / date_str
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / filename

        if not controller.capture_frame(output_path):
            log.error("Frame capture failed for slot %s — acking", slot_name)
            return True

        image_path = str(output_path)
        log.info("Captured: %s", image_path)

        # Enqueue inference job
        job_fields: dict = {
            "slot_id": str(slot_id), "slot_name": slot_name,
            "zone": zone, "camera_id": cam_id,
            "image_path": image_path,
            "capture_ts": capture_ts.isoformat(),
            "trigger_ts": trigger_ts, "task_type": task_type,
        }
        for extra_key in ("first_plates", "first_image", "first_time",
                          "capture_session_id", "lat", "lng"):
            val = _f(extra_key, "")
            if val:
                job_fields[extra_key] = val

        r.xadd(INFERENCE_STREAM, job_fields, maxlen=10_000, approximate=True)
        log.info("Inference job enqueued for slot %s", slot_name)
        return True

    except Exception as e:
        log.error("Camera task error: %s", e, exc_info=True)
        return True


# ── Entry point ──────────────────────────────────────────────────────────────

def run(cam_id: str):
    log.info("Camera worker starting for %s", cam_id)

    cam_cfg = _load_camera_config(cam_id)
    rtsp_url = _build_rtsp_url(cam_cfg)
    controller = CameraController(
        ip=cam_cfg["ip"],
        user=cam_cfg.get("user", "admin"),
        password=cam_cfg.get("password", "admin"),
        rtsp_url=rtsp_url,
    )

    stream_key = f"parking:camera:tasks:{cam_id}"
    group_name = f"cam-{cam_id}"

    # Bind process_camera_task to this camera's config for the stream loop
    def _process(r_conn, msg_id, fields):
        return process_camera_task(
            r_conn, cam_id, cam_cfg, controller,
            stream_key, group_name, msg_id, fields,
        )

    run_stream_worker(
        stream_key=stream_key,
        group_name=group_name,
        worker_id=f"{cam_id}-worker",
        process_fn=_process,
        autoclaim_idle_ms=AUTOCLAIM_IDLE_MS,
        autoclaim_count=5,
        xread_count=1,
        block_ms=BLOCK_MS,
        needs_db=False,
        on_shutdown=lambda: controller.close(),
        worker_label=f"Camera worker {cam_id}",
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m workers.camera_worker <CAM_ID>", file=sys.stderr)
        sys.exit(1)
    run(sys.argv[1])
