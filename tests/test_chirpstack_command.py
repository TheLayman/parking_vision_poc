
import unittest
from unittest.mock import MagicMock, patch
import json
import base64
import sys
import importlib
from fastapi.testclient import TestClient

class TestChirpStackCommand(unittest.TestCase):

    def setUp(self):
        # We need to make sure webapp.server is clean for each test
        # We also need to mock its dependencies before importing it
        
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

    def test_queue_command_flow(self):
        """
        Test the full flow:
        1. Receive uplink message to populate device map
        2. Call queue_command
        3. Verify MQTT publish
        """
        # Manual mock for MQTT client on the loaded module
        mock_mqtt = MagicMock()
        self.server_module._mqtt_client = mock_mqtt
        
        # 1. Simulate Uplink Message
        uplink_payload = {
            "deviceInfo": {
                "deviceName": "A1",
                "devEui": "aabbccddeeff0011",
                "applicationId": "123"
            },
            "data": "AA==" # "00" -> Free
        }
        
        msg = MagicMock()
        msg.payload = json.dumps(uplink_payload).encode('utf-8')
        
        # Mock helpers on the loaded module
        with patch.object(self.server_module, 'load_slot_meta_by_id') as mock_load_meta, \
             patch.object(self.server_module, 'load_snapshot_data', return_value={"slots":{}}), \
             patch.object(self.server_module, 'save_snapshot_data'), \
             patch.object(self.server_module, '_log_events_to_file'):
            
            mock_load_meta.return_value = {
                1: {"id": 1, "name": "A01", "device_name": "A1"}
            }
            
            self.server_module.on_mqtt_message(None, None, msg)
        
        # Verify device map
        self.assertIn(1, self.server_module._device_map)
        self.assertEqual(self.server_module._device_map[1]['devEui'], "aabbccddeeff0011")
        
        # 2. Call queue_command
        success = self.server_module.queue_command(1, "01")
        self.assertTrue(success)
        
        # 3. Verify MQTT publish
        expected_topic = "application/123/device/aabbccddeeff0011/command/down"
        expected_payload = {
            "confirmed": False,
            "fPort": 1,
            "data": "AQ==" 
        }
        
        mock_mqtt.publish.assert_called_once()
        args, _ = mock_mqtt.publish.call_args
        self.assertEqual(args[0], expected_topic)
        self.assertEqual(json.loads(args[1]), expected_payload)

    def test_api_endpoint(self):
        """Test the API endpoint triggers the command queue."""
        # Pre-populate device map on the loaded module
        self.server_module._device_map[1] = {
            "applicationId": "app-1",
            "devEui": "dev-1"
        }
        
        mock_mqtt = MagicMock()
        self.server_module._mqtt_client = mock_mqtt
        
        response = self.client.post("/slots/1/command", json={"command": "ff"})
        
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], "queued")
        
        mock_mqtt.publish.assert_called()

if __name__ == '__main__':
    unittest.main()
