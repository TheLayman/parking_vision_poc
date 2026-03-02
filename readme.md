# 🅿️ Parking Occupancy Detection System

Real-time parking slot occupancy monitoring using **LoRaWAN magnetometer sensors** with PTZ camera integration for visual verification.

---

## Features

- **LoRaWAN Sensor Integration** — Receives occupancy status via MQTT from ChirpStack
- **Simple Status Decoding** — Payload-based occupancy: `00` (Free), `01` (Occupied), `cd` (Calibration Done)
- **Device Calibration** — Send calibration commands to sensors via ChirpStack gRPC or MQTT downlink
- **PTZ Camera Control** — Automatic camera positioning and image capture on state changes
- **License Plate Recognition** — EasyOCR-based extraction with Indian plate pattern matching
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

1. **LoRaWAN Sensor** transmits occupancy status (`00` = Free, `01` = Occupied, `cd` = Calibration Done)
2. **ChirpStack** forwards data to MQTT broker on topic `application/+/device/+/event/up`
3. **Server** subscribes to MQTT, decodes base64 payload hex, maps device name to slot ID
4. **State Changes** trigger event logging and optional camera capture
5. **Dashboard** receives real-time updates via Server-Sent Events

### Payload Format

LoRaWAN uplink payload (base64-encoded hex string):
- `00` — Slot is **Free**
- `01` — Slot is **Occupied**
- `cd` — Device completed **Calibration**

Example MQTT message from ChirpStack:
```json
{
  "deviceInfo": {
    "deviceName": "B7",
    "applicationId": "...",
    "devEui": "..."
  },
  "data": "AA=="
}
```

The `data` field is base64-decoded to its hex representation (e.g. `AA==` → `00` → Free).

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
1. Sends a `CC` hex command to the device via ChirpStack gRPC (or MQTT fallback)
2. Device performs on-board calibration
3. Device responds with `cd` payload confirming calibration is complete

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
5. Extract license plate using EasyOCR (returns "UNKNOWN" if not detected)
6. Save image to `data/camera_snapshots/slot_<id>_<timestamp>.jpg`
7. Log capture event with license plate to `data/occupancy_events.jsonl`

**Processing time:** ~15 seconds per event (2s move + 8s settle + 2s capture + 3s OCR)

**Testing without hardware:** Set `ENABLE_CAMERA_CONTROL=false` — alerts will show without images.

### License Plate Recognition

Automatically extracts license plates from captured images:

- **OCR Engine:** EasyOCR with smart error correction (O→0, I→1, S→5, Z→2)
- **Pattern Matching:** Indian plate formats (e.g., TS07ES2598, KA01A1234)
- **Fallback:** Returns "UNKNOWN" if detection fails or plate not visible
- **Formats Supported:**
  - Standard: `LL DD LL DDDD` (TS07ES2598)
  - Variant: `LL DD L DDDD` (KA01A1234)

License plates are stored in event log and displayed in dashboard alerts.

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
  "image_path": "data/camera_snapshots/slot_1_20260130_100005.jpg",
  "license_plate": "TS07ES2598"
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
│   └── slot_meta.yaml              # Slot configuration and device mapping
├── data/
│   ├── occupancy_events.jsonl      # Event log with license plates (auto-generated)
│   ├── snapshot.yaml               # Current state with baselines (auto-generated)
│   └── camera_snapshots/           # Captured images (auto-generated)
├── webapp/
│   ├── server.py                   # FastAPI server with MQTT client
│   ├── camera_controller.py        # PTZ camera control module
│   ├── license_plate_extractor.py  # OCR and pattern matching
│   └── static/                     # Dashboard frontend (HTML/JS/CSS)
├── .env                            # Environment configuration
├── .env.example                    # Configuration template
├── requirements.txt                # Python dependencies (includes easyocr)
└── readme.md                       # This file
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

### Calibration issues
- Check device name in `slot_meta.yaml` matches ChirpStack device name exactly
- Verify sensor is transmitting (check ChirpStack dashboard)
- Ensure device map is populated (device must have sent at least one uplink)

### Camera not capturing
- Verify `ENABLE_CAMERA_CONTROL=true` in `.env`
- Check camera IP/credentials are correct
- Test RTSP URL: `ffplay rtsp://admin:password@192.168.1.100/stream1`
- Check camera presets are configured correctly (test manually via camera UI)

### False positives/negatives
- Re-calibrate slots when parking lot is empty
- Check for nearby metal objects affecting sensor readings
