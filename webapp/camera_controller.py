"""Camera control module for PTZ camera operations and RTSP frame capture.

Provides CameraController for use by workers/camera_worker.py.
The CameraTaskQueue and camera_worker function from the POC have been removed;
the production worker is workers/camera_worker.py (a standalone process).
"""

import time
import cv2
import requests
from pathlib import Path
from requests.auth import HTTPDigestAuth


# Camera operation parameters (overridden per-camera via cameras.yaml)
_DEFAULT_SETTLE_TIME = 8.0
_DEFAULT_CAPTURE_TIMEOUT = 10.0


class CameraController:
    """Handles PTZ camera movement and RTSP frame capture."""

    def __init__(self, ip: str, user: str, password: str, rtsp_url: str,
                 settle_time: float = _DEFAULT_SETTLE_TIME,
                 capture_timeout: float = _DEFAULT_CAPTURE_TIMEOUT):
        self.ip = ip
        self.user = user
        self.password = password
        self.rtsp_url = rtsp_url
        self.settle_time = settle_time
        self.capture_timeout = capture_timeout
        self.auth = HTTPDigestAuth(user, password)
        self.session = requests.Session()
        self.session.auth = self.auth

    def move_to_preset(self, preset_number: int) -> bool:
        """Move camera to preset position (Tyco Illustra ISAPI).

        Returns True if command accepted, False on error.
        """
        if not 1 <= preset_number <= 256:
            print(f"Invalid preset number: {preset_number} (must be 1-256)")
            return False

        try:
            url = f"http://{self.ip}/ISAPI/PTZCtrl/channels/1/presets/{preset_number}/goto"
            response = self.session.put(url, timeout=5)
            success = 200 <= response.status_code < 300
            if success:
                print(f"Camera movement accepted (preset {preset_number})")
            else:
                print(f"Camera movement failed (preset {preset_number}): HTTP {response.status_code}")
            return success
        except requests.exceptions.Timeout:
            print(f"Camera connection timeout (preset {preset_number})")
            return False
        except Exception as e:
            print(f"Camera movement error (preset {preset_number}): {e}")
            return False

    def capture_frame(self, output_path: Path) -> bool:
        """Capture a frame from the RTSP stream and save to *output_path*.

        Returns True on success, False on failure.
        """
        cap = None
        try:
            print("Capturing frame from RTSP stream...")
            cap = cv2.VideoCapture(self.rtsp_url)

            if not cap.isOpened():
                print("Failed to open RTSP stream")
                return False

            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            for _ in range(10):
                cap.read()

            start_time = time.time()
            ret = False
            frame = None

            while time.time() - start_time < self.capture_timeout:
                ret, frame = cap.read()
                if ret:
                    break
                time.sleep(0.1)

            if not ret or frame is None:
                print(f"Failed to capture frame within {self.capture_timeout}s")
                return False

            output_path.parent.mkdir(parents=True, exist_ok=True)
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
        """Check if the camera is reachable."""
        try:
            response = self.session.get(f"http://{self.ip}/", timeout=3)
            return response.status_code in (200, 401)
        except Exception:
            return False

    def close(self):
        """Release HTTP session resources."""
        try:
            self.session.close()
        except Exception:
            pass
