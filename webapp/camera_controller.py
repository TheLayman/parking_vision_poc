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
import threading
import cv2
import requests
from datetime import datetime
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
CAMERA_SETTLE_TIME = 8  # Seconds to wait after camera movement
CAMERA_CAPTURE_TIMEOUT = 10  # Seconds to wait for RTSP frame
MAX_QUEUE_SIZE = 50  # Maximum number of pending camera tasks

# Storage paths
REPO_ROOT = Path(__file__).parent.parent
CAMERA_SNAPSHOTS_DIR = REPO_ROOT / "data" / "camera_snapshots"


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

    def move_to_preset(self, preset_number: int) -> bool:
        """
        Move camera to preset position (1-256).

        Args:
            preset_number: Preset position number (1-256)

        Returns:
            True if command succeeded, False otherwise
        """
        if not 1 <= preset_number <= 256:
            print(f"Invalid preset number: {preset_number} (must be 1-256)")
            return False

        try:
            # Camera API command format
            payload = f"0x8000062{preset_number-1:02X}081"
            params = {
                'command': '0x09A5',
                'type': 'P_OCTET',
                'direction': 'WRITE',
                'num': '1',
                'payload': payload
            }

            print(f"Moving camera to preset {preset_number}...")
            response = requests.get(
                f"http://{self.ip}/rcp.xml",
                params=params,
                auth=self.auth,
                timeout=5
            )

            # Check if command was accepted
            success = "<str>" in response.text and "<err>" not in response.text

            if success:
                print(f"Camera movement command accepted (preset {preset_number})")
            else:
                print(f"Camera movement command failed (preset {preset_number})")

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
            response = requests.get(
                f"http://{self.ip}/",
                auth=self.auth,
                timeout=3
            )
            return response.status_code in (200, 401)  # 401 means auth required but reachable
        except Exception:
            return False


class CameraTaskQueue:
    """Thread-safe queue for camera tasks."""

    def __init__(self):
        """Initialize the camera task queue."""
        self.queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self.shutdown_event = threading.Event()

    def add_task(self, task: dict):
        """
        Add a camera task to the queue.

        Args:
            task: Dictionary containing task details (slot_id, preset, timestamp, etc.)
        """
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


def camera_worker(controller: CameraController, task_queue: CameraTaskQueue):
    """
    Background worker thread that processes camera tasks sequentially.

    This worker runs in a background thread and processes camera tasks one at a time:
    1. Move camera to preset position
    2. Wait for camera to settle (8 seconds)
    3. Capture frame via RTSP
    4. Save image to disk

    Args:
        controller: CameraController instance
        task_queue: CameraTaskQueue instance
    """
    print("Camera worker thread started")

    while not task_queue.shutdown_event.is_set():
        # Get next task (blocking with timeout)
        task = task_queue.get_task(timeout=1.0)

        if task is None:
            continue

        # Extract task details
        slot_id = task.get("slot_id")
        slot_name = task.get("slot_name", str(slot_id))
        zone = task.get("zone", "")
        preset = task.get("preset")
        timestamp = task.get("timestamp", datetime.now())

        print(f"\n{'='*50}")
        print(f"Processing camera task for slot {slot_name} (Zone {zone})")
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
                filename = f"slot_{slot_id}_{timestamp.strftime('%Y%m%d_%H%M%S')}.jpg"
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

        # Optional: Execute callback if provided
        callback = task.get("callback")
        if callback and callable(callback):
            try:
                callback(success, image_path, error_msg)
            except Exception as e:
                print(f"Callback error for slot {slot_name}: {e}")

    print("Camera worker thread stopped")
