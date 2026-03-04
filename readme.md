# 🅿️ Unauthorized Parking Detection System

Real-time unauthorized parking detection using **LoRaWAN magnetometer sensors** with PTZ camera integration, selectable **OpenAI Vision / EasyOCR license plate recognition**, and automated **challan (violation) tracking**.

---

## Features

- **LoRaWAN Sensor Integration** — Receives occupancy status via MQTT from ChirpStack
- **Simple Status Decoding** — Payload-based occupancy: `00` (Free), `01` (Occupied), `cd` (Calibration Done)
- **Device Calibration & Threshold** — Send calibration/threshold commands via ChirpStack gRPC (MQTT fallback)
- **PTZ Camera Control** — Automatic camera positioning and image capture on state changes
- **Dual LPR Backends** — GPT-5.2 OpenAI Vision or local EasyOCR (cost-saving backup)
- **Challan Tracking** — Automated violation detection with timed rechecks and deduplication
- **Fuzzy Plate Matching** — Handles OCR misreads with configurable similarity threshold
- **Live Dashboard** — Real-time visualization with Server-Sent Events and 4 tabs
- **Challan Dashboard** — Dedicated page for viewing/filtering violation records
- **Analytics** — Occupancy trends, dwell distribution, hourly incidents, and challan summary
- **Persistent Camera Queue** — JSONL-backed task queue with crash recovery
- **Thread-Safe** — Concurrent MQTT message handling with proper locking

---

## Requirements

- Python 3.9+
- MQTT Broker (Mosquitto, ChirpStack, etc.)
- LoRaWAN sensors with 3-axis magnetometer (sending via ChirpStack)
- PTZ Camera with preset support (optional)
- OpenAI API key (optional, only when using OpenAI LPR backend)

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
CAMERA_SETTLE_TIME=8
CAMERA_CAPTURE_TIMEOUT=10
CAMERA_QUEUE_MAXSIZE=50

# Inference pipeline (OpenAI post-processing queue)
INFERENCE_QUEUE_MAXSIZE=200

# ChirpStack gRPC API (for downlink commands)
CHIRPSTACK_HOST=localhost
CHIRPSTACK_GRPC_PORT=8080
CHIRPSTACK_API_TOKEN=your_api_token
CHIRPSTACK_APP_ID=your_application_uuid

# OpenAI Vision LPR
OPENAI_API_KEY=your_openai_api_key
OPENAI_LPR_MODEL=gpt-5.2
OPENAI_LPR_MAX_TOKENS=300

# LPR backend selection
# auto      -> use OpenAI when OPENAI_API_KEY is set, else EasyOCR
# openai    -> force OpenAI Vision
# easyocr   -> force local EasyOCR
LPR_BACKEND=auto
LPR_EASYOCR_LANGS=en
LPR_EASYOCR_DOWNLOAD=0
LPR_PREPROCESS=1

PLATE_MIN_CONFIDENCE=0.65
PLATE_REGEX_PATTERN=^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4}$

# Challan (violation) tracking
CHALLAN_RECHECK_INTERVAL=70    # Seconds before re-checking a plate
CHALLAN_DEDUP_WINDOW=600       # Skip plate if processed within this window (seconds)
```

### 2. Slot Metadata

Edit `config/slot_meta.yaml` to define parking slots:

```yaml
- id: 1
  name: "B7"
  zone: "B"
  preset: 1   # PTZ camera preset (1-256)

- id: 2
  name: "B60"
  zone: "B"
  preset: 1
```

**Key fields:**
- `id` — Unique slot identifier (integer)
- `name` — Human-readable slot name (matches ChirpStack device name for MQTT mapping)
- `zone` — Zone grouping (e.g., "A", "B", "VIP")
- `preset` — Camera preset position (optional, requires camera enabled)

---

## How It Works

### Sensor Data Flow

1. **LoRaWAN Sensor** transmits occupancy status (`00` = Free, `01` = Occupied, `cd` = Calibration Done)
2. **ChirpStack** forwards data to MQTT broker on topic `application/+/device/+/event/up`
3. **Server** subscribes to MQTT, decodes base64 payload hex, maps device name to slot ID
4. **State Changes** trigger event logging and camera capture (if enabled)
5. **Camera captures** are analyzed by the configured LPR backend (OpenAI Vision or EasyOCR)
6. **Challan rechecks** are scheduled — if the same plate is detected again after the interval, a violation is confirmed
7. **Dashboard** receives real-time updates via Server-Sent Events

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
- Fetch device list from ChirpStack gRPC API (populates device map)
- Initialize camera controller and persistent task queue (if enabled)
- Start background camera worker thread (handles captures and challan rechecks)
- Recover any pending camera tasks from the queue log
- Serve dashboard at [http://127.0.0.1:8080](http://127.0.0.1:8080)
- Serve challan dashboard at [http://127.0.0.1:8080/challan-dashboard](http://127.0.0.1:8080/challan-dashboard)

### 3. Calibrate Slots

**Ensure slot is EMPTY** before calibrating:

```bash
curl -X POST http://127.0.0.1:8080/calibrate/1
```

**Calibration process:**
1. Sends a `CC` hex command to the device via ChirpStack gRPC (or MQTT fallback)
2. Device performs on-board calibration
3. Device responds with `cd` payload confirming calibration is complete

### 4. Set Threshold

```bash
curl -X POST http://127.0.0.1:8080/setThreshold/1/500
```

Sends a `DD` + uint16 big-endian threshold value to the device.

---

## Camera Integration

### Setup

**Requirements:**
- PTZ camera with HTTP API support (Tyco Illustra compatible)
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
  name: "B7"
  preset: 5  # Camera moves to preset 5 when slot 1 changes state
```

### Operation

**On State Change (FREE → OCCUPIED):**
1. Task added to persistent camera queue (JSONL-backed, survives restarts)
2. Camera moves to preset position (HTTP command)
3. Wait for camera to settle
4. Capture frame via RTSP stream
5. Save image to `data/camera_snapshots/slot_<id>_<timestamp>.jpg`
6. Enqueue inference job (camera worker remains non-blocking)
7. Inference worker extracts all license plates using OpenAI Vision API
8. Log capture event with plates to `data/occupancy_events.jsonl`
9. Schedule challan recheck after `CHALLAN_RECHECK_INTERVAL` seconds

**Challan Recheck:**
1. Camera recaptures the same slot after the configured interval
2. Recheck image is processed by inference worker (single OpenAI call for all plates)
3. If the same plate is still present → **challan confirmed**
4. If the plate is gone → **challan cleared**
5. Dedup window prevents re-processing the same plate within `CHALLAN_DEDUP_WINDOW`

**Testing without hardware:** Set `ENABLE_CAMERA_CONTROL=false` — alerts will show without images.

### License Plate Recognition

Automatically extracts license plates from captured images using **OpenAI Vision (GPT-5.2)**:

- **Engine:** Single OpenAI Vision API call per image (no local ML models required)
- **Structured Output:** JSON schema-enforced response format
- **Indian Plate Rules:** Detailed system prompt with state codes, format rules, multi-line plate handling
- **OCR Error Correction:** Automatic confusable character fix (O↔0, I↔1, 8↔B, S↔5, Z↔2)
- **Confidence Scoring:** high (1.0), medium (0.7), low (0.3) — minimum threshold configurable via `PLATE_MIN_CONFIDENCE`
- **Fuzzy Matching:** `SequenceMatcher`-based comparison (0.85 threshold) for dedup and recheck matching
- **Formats Supported:**
  - Standard: `SS DD XX NNNN` (TS07ES2598)
  - Variant: `SS DD X NNNN` (KA01A1234)
  - Bharat Series: `BH DD YYYY XXNNNN` (BH02AA1234)

License plates are stored in event logs and displayed in dashboard alerts and challan records.

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard UI |
| `/challan-dashboard` | GET | Dedicated challan dashboard page |
| `/state` | GET | Current slot states (30s cache) |
| `/snapshot` | GET | Current in-memory snapshot data |
| `/events` | GET | SSE stream for real-time updates |
| `/analytics/summary` | GET | Unauthorized parking analytics (`?range=1h/6h/24h/7d/all&zone=X`) |
| `/calibrate/{slot_id}` | POST | Calibrate slot (sends `CC` via gRPC/MQTT) |
| `/setThreshold/{slot_id}/{threshold}` | POST | Set sensor threshold (sends `DD` + uint16) |
| `/alerts` | GET | Recent FREE→OCCUPIED changes with images (`?limit=50&offset=0`) |
| `/challans` | GET | Challan records (`?limit=100&offset=0&challan_only=false&zone=X&since=ISO8601`) |
| `/challans/pending` | GET | Currently pending challan rechecks with countdown |
| `/snapshots/{filename}` | GET | Serve captured camera images (1-year cache) |
| `/camera/status` | GET | Camera system status |

---

## Event Logging

### Main Event Log

**File:** `data/occupancy_events.jsonl` (auto-rotates at 50 MB)

#### State Change
```json
{
  "event": "slot_state_changed",
  "ts": "2026-01-30T10:00:00+00:00",
  "slot_id": 1,
  "slot_name": "B7",
  "zone": "B",
  "prev_state": "FREE",
  "new_state": "OCCUPIED"
}
```

#### Camera Capture
```json
{
  "event": "camera_capture",
  "ts": "2026-01-30T10:00:05+00:00",
  "slot_id": 1,
  "slot_name": "B7",
  "zone": "B",
  "image_path": "data/camera_snapshots/slot_1_20260130_100005.jpg",
  "license_plates": ["TS07ES2598", "KA01MR0045"],
  "vehicle_detected": true,
  "capture_session_id": "abc123",
  "mqtt_event_ts": "2026-01-30T10:00:00+00:00"
}
```

#### Challan Completed
```json
{
  "event": "challan_completed",
  "ts": "2026-01-30T10:02:15+00:00",
  "plate_text": "TS07ES2598",
  "slot_id": 1,
  "slot_name": "B7",
  "zone": "B",
  "challan": true
}
```

#### Device Calibration
```json
{
  "event": "device_calibration",
  "ts": "2026-01-30T10:05:00+00:00",
  "slot_id": 1,
  "slot_name": "B7"
}
```

#### Snapshot (on state change or periodic, deduplicated)
```json
{
  "event": "snapshot",
  "ts": "2026-01-30T10:00:00+00:00",
  "occupied_ids": [1, 4, 7],
  "zones": {
    "B": {"total": 10, "free": 7, "occupied": 3}
  },
  "total_count": 10,
  "free_count": 7
}
```

### Challan Event Log

**File:** `data/challan_events.jsonl`

```json
{
  "plate_text": "TS07ES2598",
  "slot_id": 1,
  "slot_name": "B7",
  "zone": "B",
  "challan": true,
  "first_image": "data/camera_snapshots/slot_1_20260130_100005.jpg",
  "first_time": "2026-01-30T10:00:05+00:00",
  "second_image": "data/camera_snapshots/challan_1_20260130_100215.jpg",
  "second_time": "2026-01-30T10:02:15+00:00",
  "first_plates": ["TS07ES2598"],
  "second_plates": ["TS07ES2598"],
  "capture_session_id": "abc123"
}
```

---

## Project Structure

```
parking_vision_poc/
├── config/
│   └── slot_meta.yaml              # Slot config: id, name, zone, preset
├── data/
│   ├── occupancy_events.jsonl      # Main event log (auto-rotates at 50 MB)
│   ├── challan_events.jsonl        # Challan violation records
│   ├── camera_queue.jsonl          # Camera task queue persistence
│   ├── snapshot.yaml               # Current state + device map (auto-generated)
│   └── camera_snapshots/           # Captured images (slot_*.jpg, challan_*.jpg)
├── webapp/
│   ├── server.py                   # FastAPI server: MQTT, routes, challan tracking, SSE
│   ├── camera_controller.py        # PTZ camera control, task queue with JSONL persistence
│   ├── license_plate_extractor.py  # OpenAI Vision-based license plate recognition
│   ├── helpers/
│   │   ├── __init__.py
│   │   ├── analytics.py            # Dwell distribution, hourly incidents, challan summary
│   │   ├── data_io.py              # JSONL/YAML I/O, log rotation
│   │   └── slot_meta.py            # Slot metadata, zone stats, state reconstruction
│   └── static/
│       ├── index.html              # Main dashboard (tabs: Dashboard, Analytics, Alerts, Challans)
│       ├── challan.html            # Dedicated challan dashboard page
│       ├── app.js                  # Frontend logic (tabs, charts, SSE, calibration)
│       └── styles.css              # Dark theme UI styles
├── .env                            # Environment configuration (gitignored)
├── .env.example                    # Configuration template with all variables
├── requirements.txt                # Python dependencies
└── readme.md                       # This file
```

---

## Analytics

The `/analytics/summary` endpoint provides:

- **Occupancy Series** — Zone occupancy % over time
- **Dwell Time** — Average parking duration per zone
- **Dwell Distribution** — Cumulative buckets for >15m, >30m, >45m, >1h parking
- **Hourly Incidents** — FREE→OCCUPIED transitions grouped by hour with zone breakdown
- **Challan Summary** — Total/confirmed/cleared counts with per-zone breakdown
- **Predictions** — Moving average forecast of next occupancy %
- **Current Occupancy** — Real-time zone statistics

Query parameters: `?range=1h|6h|24h|7d|all&zone=X` (default: range=24h)

---

## Dashboard

### Main Dashboard (`/`)

Four tabs accessible from the main page:

- **Dashboard** — Real-time slot grid with occupancy status, detected plates, and pending recheck indicators
- **Analytics** — Stat cards (incidents, avg duration, challans, dwell distribution), hourly incidents bar chart (Chart.js), zone and time range filters
- **Alerts** — Card-based layout of FREE→OCCUPIED events with camera snapshots, license plate badges, and image lightbox (filters out false-positive captures where no vehicle detected)
- **Challans** — Link to the dedicated challan dashboard page

Tab visibility is configurable via the gear icon and persisted in localStorage.

### Challan Dashboard (`/challan-dashboard`)

- Stats row: Unique Plates, Challans Issued, Cleared
- Filterable table with zone, date range, and challan/cleared filters
- Image lightbox for 1st and 2nd detection photos
- SSE-driven near-real-time updates with 60s fallback polling

---

## Performance

- **Response Caching** — `/state` cached for 30 seconds
- **Metadata Caching** — Config file cached with modification time tracking
- **Thread-Safe Operations** — Locks for snapshot, event log, challan data, and camera queue
- **Connection Limits** — Max 50 concurrent SSE streams
- **Log Rotation** — Event log auto-rotates at 50 MB
- **Queue Persistence** — Camera tasks survive restarts via JSONL log with compaction
- **Ring Buffers** — In-memory alert (500) and challan (1000) buffers cap memory usage
- **Smart Snapshots** — Periodic snapshots suppressed when occupancy unchanged

---

## Troubleshooting

### No MQTT messages received
- Check MQTT broker is running: `mosquitto_sub -h localhost -t '#' -v`
- Verify `MQTT_TOPIC` matches ChirpStack configuration
- Ensure sensors are transmitting and joined to LoRaWAN network

### Calibration issues
- Check slot name in `slot_meta.yaml` matches ChirpStack device name exactly
- Verify sensor is transmitting (check ChirpStack dashboard)
- Ensure device map is populated (device must have sent at least one uplink)

### Camera not capturing
- Verify `ENABLE_CAMERA_CONTROL=true` in `.env`
- Check camera IP/credentials are correct
- Test RTSP URL: `ffplay rtsp://admin:password@192.168.1.100/stream1`
- Check camera presets are configured correctly (test manually via camera UI)

### License plate not detected
- Verify `OPENAI_API_KEY` is set and valid
- Check `PLATE_MIN_CONFIDENCE` — lower it to capture more plates (may increase false positives)
- Review camera image quality and angle — ensure plates are visible and not cropped

### Challan not triggering
- Verify `CHALLAN_RECHECK_INTERVAL` is appropriate for your use case
- Check `/challans/pending` for in-progress rechecks
- Ensure camera is successfully capturing on rechecks (check `/camera/status`)

### False positives/negatives
- Re-calibrate slots when parking lot is empty
- Check for nearby metal objects affecting sensor readings
