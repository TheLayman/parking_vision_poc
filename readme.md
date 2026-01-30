# 🅿️ Parking Occupancy Detection System

Real-time parking slot occupancy monitoring using **LoRaWAN magnetometer sensors** with PTZ camera integration for visual verification.

---

## Features

- **LoRaWAN Sensor Integration** — Receives magnetic field data via MQTT from ChirpStack
- **Magnetic Field Detection** — Euclidean distance-based occupancy detection with hysteresis
- **Smart Calibration** — MQTT-based calibration with automatic baseline learning
- **PTZ Camera Control** — Automatic camera positioning and image capture on state changes
- **Live Dashboard** — Real-time visualization with Server-Sent Events
- **Analytics** — Occupancy trends, dwell time analysis, and predictions
- **Thread-Safe** — Concurrent MQTT message handling with proper locking

---

## Requirements

- Python 3.9+
- MQTT Broker (Mosquitto, ChirpStack, etc.)
- LoRaWAN sensors with 3-axis magnetometer (sending via ChirpStack)
- PTZ Camera with preset support (optional)

### Installation

```bash
pip install -r requirements.txt
```

---

## Configuration

### 1. Environment Variables

Create a `.env` file in the project root:

```bash
# MQTT Configuration
MQTT_BROKER=localhost
MQTT_PORT=1883
MQTT_TOPIC=application/+/device/+/event/up
ENABLE_MQTT=1

# Camera Configuration (optional)
ENABLE_CAMERA_CONTROL=false
CAMERA_IP=192.168.1.100
CAMERA_USER=admin
CAMERA_PASS=your_password
CAMERA_RTSP_URL=rtsp://admin:password@192.168.1.100/stream1
```

### 2. Slot Metadata

Edit `config/slot_meta.yaml` to map device names to parking slots:

```yaml
- id: 1
  name: "A01"
  zone: "A"
  device_name: "sensor-001"  # Must match ChirpStack device name
  preset: 1                   # PTZ camera preset (1-256) - optional

- id: 2
  name: "A02"
  zone: "A"
  device_name: "sensor-002"
  preset: 2
```

**Key fields:**
- `id` — Unique slot identifier (integer)
- `name` — Human-readable slot name
- `zone` — Zone grouping (e.g., "A", "B", "VIP")
- `device_name` — LoRaWAN device name from ChirpStack (used for MQTT mapping)
- `preset` — Camera preset position (optional, requires camera enabled)

---

## How It Works

### Sensor Data Flow

1. **LoRaWAN Sensor** transmits 3-axis magnetometer data (X, Y, Z)
2. **ChirpStack** forwards data to MQTT broker on topic `application/+/device/+/event/up`
3. **Server** subscribes to MQTT, decodes base64 payload, maps device to slot ID
4. **Detection Algorithm** calculates distance from baseline and determines occupancy
5. **State Changes** trigger event logging and optional camera capture
6. **Dashboard** receives real-time updates via Server-Sent Events

### Payload Format

LoRaWAN uplink payload (8 bytes, base64 encoded):
```
Bytes 0-1: X magnetometer (signed int16, divide by 100)
Bytes 2-3: Y magnetometer (signed int16, divide by 100)
Bytes 4-5: Z magnetometer (signed int16, divide by 100)
Bytes 6-7: Temperature (signed int16) - unused
```

Example MQTT message from ChirpStack:
```json
{
  "deviceInfo": {
    "deviceName": "sensor-001"
  },
  "data": "AH4BCgDy/+w="  // base64 encoded sensor data
}
```

### Detection Logic

**Distance Calculation:**
```
distance = sqrt((X - baseline_x)² + (Y - baseline_y)² + (Z - baseline_z)²)
```

**State Determination:**
- **distance > 7.5**: Increment consecutive occupancy counter (max 3)
- **distance ≤ 6.75** (90% of threshold): Reset counter to 0
- **Consecutive counter ≥ 3**: Slot marked **OCCUPIED**
- **Consecutive counter = 0**: Slot marked **FREE**

**Hysteresis Zone (6.75 to 7.5):** Counter unchanged to prevent oscillation from nearby vehicles.

**Baseline Learning:**
- Baseline auto-updates when slot is FREE using exponential moving average (α=0.01)
- Adapts to environmental magnetic field drift over time

---

## Running the System

### 1. Start MQTT Broker (if not already running)

```bash
# Using Mosquitto
mosquitto -c /path/to/mosquitto.conf

# Or use existing ChirpStack MQTT broker
```

### 2. Start Web Server

```bash
python -m uvicorn webapp.server:app --reload --port 8080
```

**Server will:**
- Connect to MQTT broker and subscribe to sensor data
- Initialize camera controller (if enabled)
- Start background camera worker thread
- Serve dashboard at [http://127.0.0.1:8080](http://127.0.0.1:8080)

### 3. Calibrate Slots

**Ensure slot is EMPTY** before calibrating:

```bash
curl -X POST http://127.0.0.1:8080/calibrate/1
```

**Calibration process:**
1. Waits for 5 MQTT sensor readings (timeout: 120 seconds)
2. Calculates average baseline (X, Y, Z)
3. Validates readings (rejects near-zero values)
4. Saves baseline to `data/snapshot.yaml`

Repeat for each slot before deployment.

---

## Camera Integration

### Setup

**Requirements:**
- PTZ camera with HTTP API support
- RTSP stream access
- Camera presets configured (1-256)

**Enable in `.env`:**
```bash
ENABLE_CAMERA_CONTROL=true
CAMERA_IP=192.168.1.100
CAMERA_USER=admin
CAMERA_PASS=your_password
```

**Configure presets in `config/slot_meta.yaml`:**
```yaml
- id: 1
  name: "A01"
  preset: 5  # Camera moves to preset 5 when slot 1 changes state
```

### Operation

**On State Change (FREE ↔ OCCUPIED):**
1. Task added to camera queue (max 50 tasks)
2. Camera moves to preset position (HTTP command)
3. Wait 8 seconds for camera to settle
4. Capture frame via RTSP stream
5. Save image to `data/camera_snapshots/slot_<id>_<timestamp>.jpg`
6. Log capture event to `data/occupancy_events.jsonl`

**Processing time:** ~12 seconds per event (2s move + 8s settle + 2s capture)

**Testing without hardware:** Set `ENABLE_CAMERA_CONTROL=false` — alerts will show without images.

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard UI |
| `/state` | GET | Current slot states (30s cache) |
| `/snapshot` | GET | Current snapshot with baseline values |
| `/events` | GET | SSE stream for real-time updates |
| `/analytics/summary` | GET | Occupancy trends (`?range=1h/6h/24h/7d/all`) |
| `/calibrate/{slot_id}` | POST | Calibrate slot baseline (MQTT-based) |
| `/alerts` | GET | Recent state changes with images (`?limit=50&offset=0`) |
| `/snapshots/{filename}` | GET | Serve captured camera images |
| `/camera/status` | GET | Camera system status |

---

## Event Logging

**File:** `data/occupancy_events.jsonl` (auto-rotates at 50 MB)

**Event Types:**

### State Change
```json
{
  "event": "slot_state_changed",
  "ts": "2026-01-30T10:00:00+00:00",
  "slot_id": 1,
  "slot_name": "A01",
  "zone": "A",
  "prev_state": "FREE",
  "new_state": "OCCUPIED"
}
```

### Camera Capture
```json
{
  "event": "camera_capture",
  "ts": "2026-01-30T10:00:05+00:00",
  "slot_id": 1,
  "slot_name": "A01",
  "zone": "A",
  "image_path": "data/camera_snapshots/slot_1_20260130_100005.jpg"
}
```

### Snapshot (every state change OR 1 minute)
```json
{
  "event": "snapshot",
  "ts": "2026-01-30T10:00:00+00:00",
  "occupied_ids": [1, 4, 7],
  "zone_stats": {
    "A": {"total": 10, "free": 7, "occupied": 3}
  },
  "total_count": 10,
  "free_count": 7
}
```

---

## Project Structure

```
parking_vision_poc/
├── config/
│   └── slot_meta.yaml           # Slot configuration and device mapping
├── data/
│   ├── occupancy_events.jsonl   # Event log (auto-generated)
│   ├── snapshot.yaml             # Current state with baselines (auto-generated)
│   └── camera_snapshots/         # Captured images (auto-generated)
├── webapp/
│   ├── server.py                 # FastAPI server with MQTT client
│   ├── camera_controller.py      # PTZ camera control module
│   └── static/                   # Dashboard frontend (HTML/JS/CSS)
├── .env                          # Environment configuration
├── .env.example                  # Configuration template
├── requirements.txt              # Python dependencies
└── readme.md                     # This file
```

---

## Analytics

The `/analytics/summary` endpoint provides:

- **Occupancy Series** — Zone occupancy % over time
- **Dwell Time** — Average parking duration per zone
- **Predictions** — Moving average forecast of next occupancy %
- **Current Occupancy** — Real-time zone statistics

Query parameters: `?range=1h|6h|24h|7d|all` (default: 24h)

---

## Performance

- **Response Caching** — `/state` cached for 30 seconds
- **Metadata Caching** — Config file cached with modification time tracking
- **Thread-Safe Operations** — Locks for snapshot, event log, and calibration data
- **Connection Limits** — Max 50 concurrent SSE streams
- **Log Rotation** — Event log auto-rotates at 50 MB

---

## Troubleshooting

### No MQTT messages received
- Check MQTT broker is running: `mosquitto_sub -h localhost -t '#' -v`
- Verify `MQTT_TOPIC` matches ChirpStack configuration
- Ensure sensors are transmitting and joined to LoRaWAN network

### Calibration timeout
- Check device name in `slot_meta.yaml` matches ChirpStack device name exactly
- Verify sensor is transmitting (check ChirpStack dashboard)
- Increase timeout in calibration endpoint if needed

### Camera not capturing
- Verify `ENABLE_CAMERA_CONTROL=true` in `.env`
- Check camera IP/credentials are correct
- Test RTSP URL: `ffplay rtsp://admin:password@192.168.1.100/stream1`
- Check camera presets are configured correctly (test manually via camera UI)

### False positives/negatives
- Re-calibrate slots when parking lot is empty
- Adjust `DISTANCE_THRESHOLD` (default: 7.5) in `server.py` if needed
- Check for nearby metal objects affecting baseline readings
