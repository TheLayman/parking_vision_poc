# Configuration Reference

## Environment Variables

### Infrastructure

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://parking:parking@localhost/parking` | PostgreSQL connection string |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection string |
| `SNAPSHOTS_DIR` | `/data/snapshots` | Camera capture storage directory |

### MQTT / ChirpStack (on Management Server)

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_MQTT` | `1` | Enable MQTT listener (`0` to disable) |
| `MQTT_BROKER` | `localhost` | MQTT broker hostname — **set to management server IP in production** |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_TOPIC` | `application/+/device/+/event/up` | Subscription topic pattern |
| `CHIRPSTACK_HOST` | `localhost` | ChirpStack gRPC server — **set to management server IP in production** |
| `CHIRPSTACK_GRPC_PORT` | `8080` | ChirpStack gRPC port |
| `CHIRPSTACK_API_TOKEN` | _(required)_ | API token from ChirpStack UI |
| `CHIRPSTACK_APP_ID` | _(required)_ | Application UUID |

### Camera Control

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_CAMERA_CONTROL` | `false` | Enable PTZ camera control |
| `CAMERA_IP` | `192.168.1.100` | Default camera IP (overridden by cameras.yaml) |
| `CAMERA_USER` | `admin` | Camera auth username |
| `CAMERA_PASS` | `admin` | Camera auth password |

### License Plate Recognition

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | _(optional)_ | OpenAI API key for Vision OCR |
| `OPENAI_LPR_MODEL` | `gpt-4o` | Vision model for plate recognition |
| `OPENAI_LPR_MAX_TOKENS` | `300` | Max response tokens |
| `PLATE_MIN_CONFIDENCE` | `0.65` | Minimum OCR confidence threshold |
| `PLATE_REGEX_PATTERN` | `^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4}$` | Indian plate format regex |

### Workers

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKER_ID` | _(set by systemd)_ | Consumer group member ID |
| `CHALLAN_RECHECK_INTERVAL` | `70` | Seconds between 1st and 2nd capture for challan verification. Minimum recommended: 180 (3 minutes) for production. |

## Camera Configuration

**File:** `config/cameras.yaml`

```yaml
cameras:
  CAM_01:
    ip: "192.168.1.100"
    user: admin
    password: "CHANGE_ME"
    settle_time: 8.0        # seconds after PTZ move
    capture_timeout: 10.0   # RTSP capture timeout
    slot_presets:            # slot_id: preset_number
      1: 1
      2: 1
      3: 2
      4: 2
  CAM_02:
    ip: "192.168.1.101"
    user: admin
    password: "CHANGE_ME"
    slot_presets:
      5: 1
      6: 1
```

## Slot Metadata

**File:** `config/slot_meta.yaml`

```yaml
- id: 1
  name: B7        # Must match ChirpStack device name
  zone: B
  preset: 1       # PTZ preset for this slot's camera
  lat: 17.385044  # GPS latitude (WGS84)
  lng: 78.486671  # GPS longitude (WGS84)
- id: 2
  name: B60
  zone: B
  preset: 1
  lat: 17.385094
  lng: 78.486751
```

GPS coordinates (`lat`, `lng`) are included in challan records and embedded on
the 2nd capture image for confirmed violations. They appear as a clickable
Google Maps link in the challan dashboard.

## Redis Streams

| Stream | Producer | Consumer Group | Workers | Purpose |
|--------|----------|----------------|---------|---------|
| `parking:mqtt:events` | API server | `mqtt-processors` | 4 | Raw MQTT uplinks |
| `parking:camera:tasks:{CAM_ID}` | mqtt_worker | `cam-{CAM_ID}` | 1 per camera | PTZ + capture tasks |
| `parking:inference:jobs` | camera_worker | `inference-workers` | 6 | OCR analysis tasks |
| `parking:inference:deadletter` | inference_worker | -- | -- | Failed after 3 retries |

## Redis Keys

| Key | Type | Description |
|-----|------|-------------|
| `parking:slot:state` | Hash | Current occupancy (slot_id -> FREE/OCCUPIED) |
| `parking:slot:since` | Hash | Last state change time per slot |
| `parking:device:map` | Hash | ChirpStack device -> slot mapping |
| `parking:challan:pending:{slot_id}` | String (TTL) | Pending recheck info |
| `parking:events:live` | Pub/Sub | Real-time SSE events |

## Database Schema

### occupancy_events
Tracks every FREE/OCCUPIED state transition.

### camera_captures
Stores image metadata and OCR results per capture.

### challan_events
Parking violation records with plate, confidence, and status (confirmed/cleared/pending).

See `db/schema.sql` for full DDL with indexes and constraints.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service health (Redis, Postgres, MQTT) |
| `/state` | GET | All slot states with zone stats |
| `/events` | GET | SSE stream for real-time updates |
| `/analytics/summary` | GET | Incidents, dwell times, challans |
| `/alerts` | GET | Recent occupancy alerts with images |
| `/challans` | GET | Challan records with filters |
| `/challans/pending` | GET | Active recheck list |
| `/calibrate/{slot_id}` | POST | Trigger sensor calibration |
| `/challan-dashboard` | GET | Challan dashboard page |

## Port Reference

### Application Server

| Port | Service | Bind | External |
|------|---------|------|----------|
| 80 | nginx | 0.0.0.0 | Yes |
| 8000 | API (gunicorn) | 127.0.0.1 | No |
| 6379 | Redis | 127.0.0.1 | No |
| 5432 | PostgreSQL | 127.0.0.1 | No |

### Management Server

| Port | Service | Bind | External |
|------|---------|------|----------|
| 1883 | MQTT Broker (ChirpStack) | 0.0.0.0 | Yes (app server + gateways) |
| 8080 | ChirpStack gRPC API | 0.0.0.0 | Yes (app server) |
| 22 | SSH (rsync backups) | 0.0.0.0 | Yes (app server only) |
