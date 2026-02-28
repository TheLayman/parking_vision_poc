
import unittest
from unittest.mock import MagicMock, patch
import sys
import importlib
from fastapi.testclient import TestClient
import json

class TestCalibration(unittest.TestCase):

    def setUp(self):
        # Create mocks for dependencies
        self.mock_camera = MagicMock()
        self.mock_extractor = MagicMock()
        
        # Patch sys.modules to provide mocks for dependencies
        self.module_patcher = patch.dict('sys.modules', {
            'webapp.camera_controller': self.mock_camera,
            'webapp.license_plate_extractor': self.mock_extractor
        })
        self.module_patcher.start()
        
        # Import server module (fresh or reloaded)
        if 'webapp.server' in sys.modules:
            import webapp.server
            importlib.reload(webapp.server)
        else:
            import webapp.server
            
        self.server_module = sys.modules['webapp.server']
        self.app = self.server_module.app
        self.client = TestClient(self.app)
        
        # Reset device map in the loaded module
        self.server_module._device_map.clear()
        
    def tearDown(self):
        self.module_patcher.stop()

    def test_calibration_endpoint_success(self):
        """Test the calibration endpoint triggers the command queue with '02'."""
        # Pre-populate device map on the loaded module
        self.server_module._device_map[1] = {
            "applicationId": "app-1",
            "devEui": "dev-1"
        }
        
        mock_mqtt = MagicMock()
        self.server_module._mqtt_client = mock_mqtt
        
        response = self.client.post("/calibrate/1")
        
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['success'], True)
        
        # Verify MQTT publish was called with correct topic and payload
        mock_mqtt.publish.assert_called_once()
        args, _ = mock_mqtt.publish.call_args
        topic = args[0]
        payload = json.loads(args[1])
        
        self.assertEqual(topic, "application/app-1/device/dev-1/command/down")
        # '02' in base64 is 'Ag=='
        self.assertEqual(payload['data'], 'Ag==')

    def test_calibration_endpoint_no_device(self):
        """Test calibration fails if device not mapped."""
        mock_mqtt = MagicMock()
        self.server_module._mqtt_client = mock_mqtt
        
        response = self.client.post("/calibrate/99")
        
        self.assertEqual(response.status_code, 404)

if __name__ == '__main__':
    unittest.main()
