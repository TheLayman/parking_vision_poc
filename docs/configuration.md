# Configuration Reference (POC)

## Environment Variables

### Infrastructure

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://parking:parking@localhost/parking` | PostgreSQL connection string |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection string |

### MQTT / ChirpStack

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_MQTT` | `1` | Enable MQTT listener (`0` to disable for local dev) |
| `MQTT_BROKER` | `localhost` | MQTT broker hostname |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_TOPIC` | `application/+/device/+/event/up` | Subscription topic pattern |

### Workers

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKER_ID` | `worker-{pid}` | Consumer group member ID (set by systemd or manually) |

## Slot Metadata

**File:** `config/slot_meta.yaml`

```yaml
- id: 1
  name: A1          # Must match ChirpStack device name exactly
  zone: A
  lat: 17.385044    # GPS latitude (WGS84)
  lng: 78.486671    # GPS longitude (WGS84)
- id: 2
  name: A2
  zone: A
  lat: 17.385094
  lng: 78.486751
```

The `name` field is how the MQTT worker maps incoming sensor messages to slots. It must exactly match the device name in ChirpStack.

## Redis Keys

| Key | Type | Description |
|-----|------|-------------|
| `parking:slot:state` | Hash | Current occupancy (slot_id -> FREE/OCCUPIED) |
| `parking:slot:since` | Hash | Last state change timestamp per slot |
| `parking:sensor:lastseen` | Hash | Last uplink timestamp per slot |
| `parking:sensor:rssi` | Hash | Best RSSI per slot |
| `parking:sensor:alerts` | Hash | Active device alerts (battery_low / temperature_high) |
| `parking:mqtt:events` | Stream | Raw MQTT uplinks, consumed by mqtt_workers |
| `parking:events:live` | Pub/Sub | Real-time SSE events |

## Redis Stream

| Stream | Producer | Consumer Group | Workers | Purpose |
|--------|----------|----------------|---------|---------|
| `parking:mqtt:events` | API server (on_mqtt_message) | `mqtt-processors` | 2 | Raw MQTT uplinks |

## Database Schema

### occupancy_events

Single table tracking every state transition and device health alert.

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL | Primary key |
| `slot_id` | INT | Parking slot ID |
| `event_type` | VARCHAR(32) | FREE, OCCUPIED, calibration, battery_low, temperature_high |
| `device_eui` | VARCHAR(32) | LoRaWAN device EUI |
| `ts` | TIMESTAMPTZ | Event timestamp |
| `payload` | JSONB | Metadata (slot_name, zone, prev_state, new_state) |

See `db/schema.sql` for full DDL.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard page |
| `/health` | GET | Service health (Redis, Postgres, MQTT, sensor stats) |
| `/state` | GET | All slot states + zone stats + sensor health data |
| `/events` | GET | SSE stream (slot_state_changed, device_alert) |
| `/state-changes` | GET | Paginated occupancy transitions from Postgres |
| `/analytics/summary` | GET | Occupancy events, dwell, turnover, heatmap, peak |

### /analytics/summary query params

| Param | Values | Default |
|-------|--------|---------|
| `range` | `1h`, `6h`, `24h`, `7d`, `all` | `24h` |
| `zone` | Zone letter (e.g., `A`, `B`) or empty for all | all |

## Port Reference

| Port | Service | Bind |
|------|---------|------|
| 80 | Nginx (reverse proxy) | 0.0.0.0 |
| 8000 | API (uvicorn/gunicorn) | 127.0.0.1 |
| 6379 | Redis | 127.0.0.1 |
| 5432 | PostgreSQL | 127.0.0.1 |
| 1883 | MQTT Broker (Mosquitto) | localhost |
| 8080 | ChirpStack | localhost |
