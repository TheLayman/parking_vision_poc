"""
Camera control module for PTZ camera operations and RTSP frame capture.

This module provides thread-safe camera control functionality including:
- Moving PTZ camera to preset positions
- Capturing frames via RTSP stream
- Sequential task queue processing to avoid concurrent camera commands
"""

import os
import time
import queue
import json
import uuid
import threading
import cv2
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from requests.auth import HTTPDigestAuth
from urllib.parse import quote
from dotenv import load_dotenv

env_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=env_path)


# Configuration from environment variables
ENABLE_CAMERA_CONTROL = os.getenv("ENABLE_CAMERA_CONTROL", "false").lower() in ("true", "1", "yes")
CAMERA_IP = os.getenv("CAMERA_IP", "192.168.1.100")
CAMERA_USER = os.getenv("CAMERA_USER", "admin")
CAMERA_PASS = os.getenv("CAMERA_PASS", "admin")
RTSP_URL = os.getenv(
    "CAMERA_RTSP_URL",
    f"rtsp://{quote(CAMERA_USER, safe='')}:{quote(CAMERA_PASS, safe='')}@{CAMERA_IP}/stream1"
)

# Camera operation parameters
CAMERA_SETTLE_TIME = float(os.getenv("CAMERA_SETTLE_TIME", "8"))  # Seconds to wait after camera movement
CAMERA_CAPTURE_TIMEOUT = float(os.getenv("CAMERA_CAPTURE_TIMEOUT", "10"))  # Seconds to wait for RTSP frame
MAX_QUEUE_SIZE = int(os.getenv("CAMERA_QUEUE_MAXSIZE", "50"))  # Maximum number of pending camera tasks

# Storage paths
REPO_ROOT = Path(__file__).parent.parent
CAMERA_SNAPSHOTS_DIR = REPO_ROOT / "data" / "camera_snapshots"
QUEUE_LOG_PATH = REPO_ROOT / "data" / "camera_queue.jsonl"


class CameraController:
    """Handles PTZ camera movement and RTSP frame capture."""

    def __init__(self, ip: str, user: str, password: str, rtsp_url: str):
        """
        Initialize camera controller.

        Args:
            ip: Camera IP address
            user: Camera username
            password: Camera password
            rtsp_url: RTSP stream URL
        """
        self.ip = ip
        self.user = user
        self.password = password
        self.rtsp_url = rtsp_url
        self.auth = HTTPDigestAuth(user, password)
        self.session = requests.Session()
        self.session.auth = self.auth

    def move_to_preset(self, preset_number: int) -> bool:
        """
        Move camera to preset position (Tyco Illustra API).

        Args:
            preset_number: Preset position number (1-256)

        Returns:
            True if command succeeded, False otherwise
        """
        if not 1 <= preset_number <= 256:
            print(f"Invalid preset number: {preset_number} (must be 1-256)")
            return False

        try:
            # Tyco Illustra ISAPI format
            url = f"http://{self.ip}/ISAPI/PTZCtrl/channels/1/presets/{preset_number}/goto"

            print(f"Moving camera to preset {preset_number}...")
            response = self.session.put(
                url,
                timeout=5
            )

            # Check if command was accepted (200-299 range)
            success = 200 <= response.status_code < 300

            if success:
                print(f"Camera movement command accepted (preset {preset_number})")
            else:
                print(f"Camera movement command failed (preset {preset_number}): Status {response.status_code}")

            return success

        except requests.exceptions.Timeout:
            print(f"Camera connection timeout for preset {preset_number}")
            return False
        except Exception as e:
            print(f"Camera movement error (preset {preset_number}): {e}")
            return False

    def capture_frame(self, output_path: Path) -> bool:
        """
        Capture frame from RTSP stream and save to file.

        Args:
            output_path: Path where to save the captured frame

        Returns:
            True if capture succeeded, False otherwise
        """
        cap = None
        try:
            print(f"Capturing frame from RTSP stream...")
            cap = cv2.VideoCapture(self.rtsp_url)

            if not cap.isOpened():
                print("Failed to open RTSP stream")
                return False

            # Set small buffer to get most recent frame
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            # Flush buffer by reading a few frames
            for _ in range(10):
                cap.read()

            # Capture with timeout
            start_time = time.time()
            ret = False
            frame = None

            while time.time() - start_time < CAMERA_CAPTURE_TIMEOUT:
                ret, frame = cap.read()
                if ret:
                    break
                time.sleep(0.1)

            if not ret or frame is None:
                print(f"Failed to capture frame within {CAMERA_CAPTURE_TIMEOUT}s timeout")
                return False

            # Ensure output directory exists
            output_path.parent.mkdir(parents=True, exist_ok=True)

            # Save frame as JPEG with 85% quality
            cv2.imwrite(str(output_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            print(f"Frame saved: {output_path}")
            return True

        except Exception as e:
            print(f"Frame capture error: {e}")
            return False
        finally:
            if cap is not None:
                cap.release()

    def is_available(self) -> bool:
        """
        Check if camera is reachable.

        Returns:
            True if camera responds, False otherwise
        """
        try:
            response = self.session.get(
                f"http://{self.ip}/",
                timeout=3
            )
            return response.status_code in (200, 401)  # 401 means auth required but reachable
        except Exception:
            return False

    def close(self):
        """Close persistent HTTP session resources."""
        try:
            self.session.close()
        except Exception:
            pass


class CameraTaskQueue:
    """Thread-safe queue for camera tasks with JSONL persistence."""

    def __init__(self, queue_log_path: Path = None):
        """Initialize the camera task queue.

        Args:
            queue_log_path: Path to the JSONL persistence file.
                            Defaults to QUEUE_LOG_PATH.
        """
        self.queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self.shutdown_event = threading.Event()
        self.queue_log_path = queue_log_path or QUEUE_LOG_PATH
        self._log_lock = threading.Lock()
        self._completion_count = 0
        self._compact_every = 100

    def add_task(self, task: dict, skip_log: bool = False):
        """
        Add a camera task to the queue.

        Assigns a unique task_id and queued_at timestamp if not already present,
        persists the task to the JSONL log, then enqueues it in memory.

        Args:
            task: Dictionary containing task details (slot_id, preset, etc.)
            skip_log: If True, skip writing to JSONL (used during recovery).
        """
        # Assign identity fields if not present
        if "task_id" not in task:
            task["task_id"] = str(uuid.uuid4())
        if "queued_at" not in task:
            task["queued_at"] = datetime.now(timezone.utc).isoformat()
        if "task_type" not in task:
            task["task_type"] = "camera_capture"

        # Persist to JSONL before enqueuing
        if not skip_log:
            self._append_to_log(task)

        try:
            # Check if queue is full
            if self.queue.qsize() >= MAX_QUEUE_SIZE:
                # Drop oldest task
                try:
                    dropped = self.queue.get_nowait()
                    print(f"WARNING: Camera queue full. Dropped task for slot {dropped.get('slot_id')}")
                except queue.Empty:
                    pass

            # Add new task
            self.queue.put(task, block=False)

        except queue.Full:
            print(f"WARNING: Failed to enqueue camera task for slot {task.get('slot_id')}")

    def complete_task(self, task_id: str):
        """Mark a task as completed in the JSONL log."""
        self._append_to_log({"task_id": task_id, "status": "completed"})
        self._completion_count += 1
        if self._completion_count >= self._compact_every:
            self._completion_count = 0
            self.compact_log()

    def get_task(self, timeout: float = 1.0) -> dict:
        """
        Get next task from queue (blocking with timeout).

        Args:
            timeout: Maximum time to wait for a task in seconds

        Returns:
            Task dictionary or None if timeout or queue empty
        """
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    # ── Persistence helpers ───────────────────────────────────────────────

    def _append_to_log(self, entry: dict):
        """Append a single entry to the JSONL log, filtering non-serializable fields."""
        serializable = {}
        for k, v in entry.items():
            if k == "callback":
                continue  # skip non-serializable callables
            if isinstance(v, datetime):
                serializable[k] = v.isoformat()
            else:
                serializable[k] = v
        try:
            self.queue_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_lock:
                with open(self.queue_log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(serializable) + "\n")
                    f.flush()
        except Exception as e:
            print(f"WARNING: Failed to write queue log: {e}")

    def compact_log(self, max_age_seconds: int = 300):
        """Rewrite the JSONL keeping only non-completed, non-stale entries."""
        try:
            if not self.queue_log_path.exists():
                return

            with self._log_lock:
                entries = []
                completed_ids = set()
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)

                with open(self.queue_log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            if obj.get("status") == "completed":
                                completed_ids.add(obj.get("task_id"))
                            else:
                                entries.append(obj)
                        except Exception:
                            continue

                # Filter: keep only non-completed, non-stale
                surviving = []
                for entry in entries:
                    if entry.get("task_id") in completed_ids:
                        continue
                    queued_at = entry.get("queued_at", "")
                    if queued_at:
                        try:
                            task_time = datetime.fromisoformat(queued_at)
                            if task_time.tzinfo is None:
                                task_time = task_time.replace(tzinfo=timezone.utc)
                            if task_time < cutoff:
                                continue
                        except Exception:
                            pass
                    surviving.append(entry)

                # Rewrite
                with open(self.queue_log_path, "w", encoding="utf-8") as f:
                    for entry in surviving:
                        f.write(json.dumps(entry) + "\n")
                    f.flush()

                print(f"Queue log compacted: {len(surviving)} pending tasks kept")
        except Exception as e:
            print(f"WARNING: Failed to compact queue log: {e}")

    def recover_tasks(self, max_age_seconds: int = 300) -> list:
        """Read the JSONL and return pending tasks (non-completed, not stale)."""
        if not self.queue_log_path.exists():
            return []

        entries = []
        completed_ids = set()
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)

        try:
            with open(self.queue_log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if obj.get("status") == "completed":
                            completed_ids.add(obj.get("task_id"))
                        else:
                            entries.append(obj)
                    except Exception:
                        continue
        except Exception as e:
            print(f"WARNING: Failed to read queue log for recovery: {e}")
            return []

        # Filter
        pending = []
        stale_count = 0
        for entry in entries:
            if entry.get("task_id") in completed_ids:
                continue
            queued_at = entry.get("queued_at", "")
            if queued_at:
                try:
                    task_time = datetime.fromisoformat(queued_at)
                    if task_time.tzinfo is None:
                        task_time = task_time.replace(tzinfo=timezone.utc)
                    if task_time < cutoff:
                        stale_count += 1
                        continue
                except Exception:
                    pass
            pending.append(entry)

        if stale_count > 0:
            print(f"Discarded {stale_count} stale tasks (>{max_age_seconds}s old)")

        return pending


def camera_worker(controller: CameraController, task_queue: CameraTaskQueue):
    """
    Background worker thread that processes camera tasks sequentially.

    This worker runs in a background thread and processes camera tasks one at a time:
    1. Move camera to preset position
    2. Wait for camera to settle (8 seconds)
    3. Capture frame via RTSP
    4. Save image to disk

    Supports delayed tasks via the ``scheduled_at`` field: tasks scheduled for the
    future are held in a local delay list and processed when their time arrives.
    This replaces the old threading.Timer pattern for challan rechecks and
    eliminates the camera race condition.

    Args:
        controller: CameraController instance
        task_queue: CameraTaskQueue instance
    """
    print("Camera worker thread started")
    _delayed_tasks = []  # tasks with a future scheduled_at

    while not task_queue.shutdown_event.is_set():
        task = None
        now = datetime.now(timezone.utc)

        # 1. Check if any delayed tasks are now due
        ready_idx = None
        for i, dt in enumerate(_delayed_tasks):
            try:
                scheduled = datetime.fromisoformat(dt["scheduled_at"])
                if scheduled.tzinfo is None:
                    scheduled = scheduled.replace(tzinfo=timezone.utc)
                if scheduled <= now:
                    ready_idx = i
                    break
            except Exception:
                ready_idx = i  # can't parse → process immediately
                break

        if ready_idx is not None:
            task = _delayed_tasks.pop(ready_idx)
        else:
            # 2. Get next task from queue (blocking with timeout)
            task = task_queue.get_task(timeout=1.0)

            if task is None:
                continue

            # 3. If task is scheduled for the future, defer it
            scheduled_at = task.get("scheduled_at")
            if scheduled_at:
                try:
                    scheduled = datetime.fromisoformat(scheduled_at)
                    if scheduled.tzinfo is None:
                        scheduled = scheduled.replace(tzinfo=timezone.utc)
                    if scheduled > now:
                        _delayed_tasks.append(task)
                        continue
                except Exception:
                    pass  # can't parse → process immediately

        # Extract task details
        task_id = task.get("task_id")
        slot_id = task.get("slot_id")
        slot_name = task.get("slot_name", str(slot_id))
        zone = task.get("zone", "")
        preset = task.get("preset")
        task_type = task.get("task_type", "camera_capture")

        # Resolve timestamp for filename
        ts_raw = task.get("timestamp")
        if isinstance(ts_raw, datetime):
            timestamp = ts_raw
        elif isinstance(ts_raw, str):
            try:
                timestamp = datetime.fromisoformat(ts_raw)
            except Exception:
                timestamp = datetime.now(timezone.utc)
        else:
            timestamp = datetime.now(timezone.utc)

        print(f"\n{'='*50}")
        print(f"Processing {task_type} task for slot {slot_name} (Zone {zone})")
        print(f"{'='*50}")

        success = False
        image_path = None
        error_msg = None

        try:
            # Step 1: Move camera to preset
            if controller.move_to_preset(preset):
                # Step 2: Wait for camera to settle
                print(f"Waiting {CAMERA_SETTLE_TIME}s for camera to settle...")
                time.sleep(CAMERA_SETTLE_TIME)

                # Step 3: Capture frame
                prefix = "challan" if task_type == "challan_recheck" else "slot"
                filename = f"{prefix}_{slot_id}_{timestamp.strftime('%Y%m%d_%H%M%S')}.jpg"
                output_path = CAMERA_SNAPSHOTS_DIR / filename

                if controller.capture_frame(output_path):
                    image_path = str(output_path)
                    success = True
                    print(f"Camera task completed successfully for slot {slot_name}")
                else:
                    error_msg = "Frame capture failed"
                    print(f"Camera task failed: {error_msg}")
            else:
                error_msg = "Camera movement failed"
                print(f"Camera task failed: {error_msg}")

        except Exception as e:
            error_msg = str(e)
            print(f"Camera task exception for slot {slot_name}: {e}")

        # Mark task as completed in the persistent log
        if task_id:
            task_queue.complete_task(task_id)

        # Execute callback if provided
        callback = task.get("callback")
        if callback and callable(callback):
            try:
                callback(success, image_path, error_msg)
            except Exception as e:
                print(f"Callback error for slot {slot_name}: {e}")

    print("Camera worker thread stopped")
