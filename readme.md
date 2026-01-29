# 🅿️ Parking Occupancy Dashboard POC

Real-time dashboard for parking slot occupancy using **3-axis magnetometer detection**. Detects vehicle presence by measuring magnetic field distortions and provides live monitoring with analytics.

## 📝 Recent Updates

**Camera Control & Alerts System (Jan 2026)**
- 📸 PTZ camera integration with automatic preset positioning on state changes
- 🚨 New Alerts dashboard tab with real-time state changes and camera snapshots
- 🔄 Sequential task queue to prevent concurrent camera commands
- 🎛️ Environment variable control: `ENABLE_CAMERA_CONTROL` for easy testing without hardware
- 🖼️ Image capture via RTSP with 8-second camera settle time
- 🧵 Thread-safe camera worker with graceful error handling

**Code Refactoring (Jan 2026)**
- Reduced [server.py](webapp/server.py) from 857 → 754 lines (12% reduction)
- Extracted helper functions: `_process_slot_item()`, `_detect_state_changes()`, `_calculate_zone_stats()`
- Refactored analytics pipeline into modular functions for better maintainability
- All functionality tested: calibration ✓, state transitions ✓, real-time updates ✓

## ✨ Features

- **Magnetic Field Detection** — Uses Euclidean distance from baseline (r,y,b values) with 7.5 threshold and hysteresis to prevent false positives
- **Smart Calibration** — Automatic baseline learning (α=0.01) when slots are free + manual calibration endpoint
- **Live Dashboard** — Real-time visualization (🔴 Occupied / 🟢 Free) via Server-Sent Events with 50-connection limit
- **📸 Camera Control** — PTZ camera automatically moves to preset positions and captures images on state changes
- **🚨 Alerts System** — New dashboard tab showing real-time alerts with captured images and state transition history
- **Analytics Module** — Occupancy trends, dwell times, peak hours with 30s response caching
- **Auto Log Rotation** — Events rotate at 50 MB to prevent disk exhaustion
- **Thread-Safe** — Snapshot and event log locks prevent data corruption
- **Production Ready** — Configurable API polling with Bearer token auth

---

## 📋 Requirements

| Component | Requirement |
|-----------|-------------|
| **Python** | 3.9+ |
| **OS** | Windows, Linux, or macOS |

### Installation

```bash
pip install -r requirements.txt
```

---

## 📁 Project Structure

```
parking_vision_poc/
├── config/
│   └── slot_meta.yaml        # Slot names, zones, and camera presets
├── data/
│   ├── occupancy_events.jsonl # (Generated) History of state changes
│   └── camera_snapshots/     # (Generated) Captured images from camera
├── webapp/
│   ├── server.py             # FastAPI backend (SSE + Analytics + Camera API)
│   ├── camera_controller.py  # Camera control module (PTZ + RTSP)
│   └── static/               # Frontend (HTML/JS/CSS)
├── data.txt                  # Input file for simulating sensor data
├── .env.example              # Environment configuration template
└── readme.md
```

---

## 🚀 Quick Start

### Option 1: Test Mode (Local File Simulation)

**Start Server:**
```bash
python -m uvicorn webapp.server:app --reload --port 8080
```

**Edit `data.txt` with test values:**
```json
[
  {"id":1,"unique_id":"2","status":"{\"r\":45,\"y\":30,\"b\":-20}","timestamp":1766561204011},
  {"id":2,"unique_id":"1","status":"{\"r\":-50,\"y\":25,\"b\":40}","timestamp":1766561205011}
]
```

Server polls `/slots` endpoint (returns `data.txt` contents) every 5 seconds. Dashboard updates live at [http://127.0.0.1:8080](http://127.0.0.1:8080).

### Option 2: Production Mode (External API)

**Configure Environment:**
```bash
# In webapp/server.py, set:
API_URL = "https://your-api.com/parking/sensors"
API_TOKEN = "your_bearer_token_here"  # Optional
ENABLE_POLLING = 1
```

Server polls external API every 5 seconds with `Authorization: Bearer {token}` header. Expects JSON array:
```json
[{"id": 1, "unique_id": "slot_1", "status": "{\"r\":30,\"y\":20,\"b\":-35}", "timestamp": 1234567890}]
```

---

## 📸 Camera Control & Alerts

### Overview

The system supports optional PTZ camera integration that automatically:
1. **Detects** state changes (FREE ↔ OCCUPIED)
2. **Moves** camera to preset position (configured in `slot_meta.yaml`)
3. **Waits** 8 seconds for camera to settle
4. **Captures** image via RTSP stream
5. **Displays** alert with image in dashboard

### Setup

**1. Copy environment template:**
```bash
cp .env.example .env
```

**2. Configure camera settings in `.env`:**
```bash
# Enable camera control
ENABLE_CAMERA_CONTROL=true

# Camera connection
CAMERA_IP=192.168.1.100
CAMERA_USER=admin
CAMERA_PASS=your_password

# RTSP stream (optional - auto-generated if not set)
CAMERA_RTSP_URL=rtsp://admin:your_password@192.168.1.100:554/stream1
```

**3. Configure camera presets in `config/slot_meta.yaml`:**
```yaml
- id: 1
  name: A01
  zone: A
  preset: 1    # Camera preset position (1-256)
```

**4. Start server:**
```bash
# Windows
set ENABLE_CAMERA_CONTROL=true
set CAMERA_IP=192.168.1.100
python -m uvicorn webapp.server:app --reload --port 8080

# Linux/Mac
export ENABLE_CAMERA_CONTROL=true
export CAMERA_IP=192.168.1.100
python -m uvicorn webapp.server:app --reload --port 8080
```

### Testing Without Camera

Camera control is **disabled by default**, allowing full alerts functionality without hardware:

```bash
# No environment variables needed
python -m uvicorn webapp.server:app --reload --port 8080
```

1. Navigate to **Alerts** tab in dashboard
2. Trigger state change (magnetic sensor or API)
3. Alert appears with "No image" placeholder
4. All metadata displayed: time, slot, zone, state transition

### Camera API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/camera/status` | GET | Camera system status and queue size |
| `/snapshots/{filename}` | GET | Serve captured images |
| `/alerts` | GET | Recent alerts with pagination (`?limit=50&offset=0`) |

**Check camera status:**
```bash
curl http://127.0.0.1:8080/camera/status
```

**Response:**
```json
{
  "enabled": true,
  "available": true,
  "queue_size": 2,
  "worker_active": true,
  "camera_ip": "192.168.1.100"
}
```

### How It Works

**Sequential Processing:**
- Multiple state changes queued automatically
- Processed one at a time (prevents concurrent camera commands)
- Queue limit: 50 tasks (drops oldest if full)
- Processing time: ~12 seconds per slot (2s move + 8s settle + 2s capture)

**Error Handling:**
- Camera unreachable → Alert created without image
- RTSP timeout (10s) → Retry once, then skip
- Queue overflow → Log warning, drop oldest task
- All errors logged, server continues running

**Production Considerations:**
- Images stored in `data/camera_snapshots/` (~200KB each)
- Recommend retention policy (e.g., delete after 30 days)
- For multiple cameras: extend queue system to support parallel processing

---

## 🧲 How Detection Works

**Magnetic Field Baseline:**
- Each slot stores calibrated baseline (r, y, b) values representing empty state
- Distance calculated: `sqrt((r-baseline_r)² + (y-baseline_y)² + (z-baseline_z)²)`

**Occupancy Logic:**
- **Distance > 7.5**: Increment consecutive count (max 3)
- **Distance ≤ 6.75** (0.9 × threshold): Reset consecutive count to 0
- **Consecutive ≥ 3**: Slot marked OCCUPIED (hysteresis prevents false positives from nearby cars)
- **Free Slots**: Baseline auto-updates with α=0.01 learning rate

**State Transitions:**
- Logged only when state changes (FREE ↔ OCCUPIED)
- Snapshots logged on change OR every 1 minute

---

## 🎯 Calibration & Testing

### Manual Calibration

**Calibrate slot baseline (must be empty):**
```bash
curl -X POST http://127.0.0.1:8080/calibrate/1
```

Takes 10 samples over 50 seconds, validates:
- Minimum 5 samples required
- Rejects near-zero values (sensor error)
- Rejects unconfigured slot IDs

Baseline saved to `data/snapshot.yaml`:
```yaml
slots:
  '1':
    baseline_x: 30.0
    baseline_y: 20.0
    baseline_z: -35.0
    calibrated_at: '2025-12-24T10:33:33+00:00'
```

### Testing Occupancy Detection

**Test 1: Empty Slot (Distance < 7.5)**
```json
{"id":1,"unique_id":"1","status":"{\"r\":32,\"y\":21,\"b\":-34}","timestamp":1234567891}
```
Distance ≈ 2.45 → FREE

**Test 2: Occupied Slot (Distance > 7.5)**
```json
{"id":1,"unique_id":"1","status":"{\"r\":50,\"y\":40,\"b\":-10}","timestamp":1234567892}
```
Distance ≈ 31.62 → After 3 consecutive reads → OCCUPIED

**Test 3: Mixed States**
```json
[
  {"id":1,"unique_id":"1","status":"{\"r\":32,\"y\":21,\"b\":-34}","timestamp":1234567893},
  {"id":2,"unique_id":"2","status":"{\"r\":60,\"y\":50,\"b\":20}","timestamp":1234567894}
]
```
Slot 1: FREE, Slot 2: OCCUPIED

---

## 📊 Analytics

The dashboard includes an analytics view (`/analytics/summary` endpoint) providing:
- **Occupancy Trends**: Occupancy % over time (1h, 6h, 24h).
- **Dwell Time**: Average time vehicles spend in slots per zone.
- **Zone Stats**: Current usage per zone (e.g., Zone A, Zone B).

---

## ⚙️ Configuration

**`config/slot_meta.yaml`**
Map internal IDs to human-readable names, zones, and camera presets:

```yaml
- id: 1
  name: "A01"
  zone: "A"
  preset: 1    # PTZ camera preset position (1-256)
- id: 2
  name: "A02"
  zone: "A"
  preset: 2
```

**Environment Variables (`.env`)**
See [`.env.example`](.env.example) for all configuration options:
- `ENABLE_CAMERA_CONTROL` - Enable/disable camera (default: false)
- `CAMERA_IP` - Camera IP address
- `CAMERA_USER` / `CAMERA_PASS` - Authentication credentials
- `CAMERA_RTSP_URL` - Custom RTSP stream URL (optional)

---

## 🧾 Event Logging System

**File:** `data/occupancy_events.jsonl` (auto-rotates at 50 MB)

**Event Types:**

**State Change:**
```json
{
  "event": "slot_state_changed",
  "ts": "2025-12-24T10:00:00+00:00",
  "slot_id": 1,
  "slot_name": "A-01",
  "zone": "Zone A",
  "prev_state": "FREE",
  "new_state": "OCCUPIED"
}
```

**Snapshot (every state change OR 1 min):**
```json
{
  "event": "snapshot",
  "ts": "2025-12-24T10:00:00+00:00",
  "occupied_ids": [1, 4],
  "zone_stats": {"Zone A": {"total": 2, "free": 0, "occupied": 2}},
  "total_count": 10,
  "free_count": 8
}
```

**Log Rotation:**
- Rotates when file exceeds 50 MB
- Backup named: `occupancy_events_YYYYMMDD_HHMMSS.jsonl`
- Prevents disk exhaustion in long-running deployments

---

## 🔌 API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard UI |
| `/state` | GET | Current slot states (cached 30s) |
| `/slots` | GET | Raw sensor data (from data.txt or API) |
| `/events` | GET | SSE stream of real-time events (max 50 connections) |
| `/analytics/summary` | GET | Occupancy trends (1h/6h/24h) |
| `/calibrate/{slot_id}` | POST | Calibrate slot baseline |
| `/alerts` | GET | Recent alerts with pagination (`?limit=50&offset=0`) |
| `/snapshots/{filename}` | GET | Serve captured camera images |
| `/camera/status` | GET | Camera system status (enabled, available, queue size) |

---

## ⚡ Performance Features

- **Response Caching**: `/state` cached for 30s (reduces log reads by 90%)
- **Metadata Caching**: Config file parsed only on modification
- **Set-Based Lookups**: O(1) occupancy checks (was O(n))
- **Thread-Safe**: Snapshot and event log locks prevent corruption
- **Connection Limits**: Max 50 SSE streams to prevent FD exhaustion
- **Exponential Backoff**: SSE clients back off 0.1s → 1.0s when idle