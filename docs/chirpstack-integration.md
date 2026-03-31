# ChirpStack Integration Guide

## Overview

The system integrates with ChirpStack v4 via two channels:
1. **MQTT** -- Receives sensor uplink messages (occupancy state changes)
2. **gRPC API** -- Fetches device list, sends downlink commands (calibration)

## MQTT Setup

### 1. ChirpStack MQTT Broker

ChirpStack publishes device events to its built-in MQTT broker. The parking system subscribes to uplink events.

**Topic pattern:** `application/+/device/+/event/up`

This matches all devices across all applications. The `+` wildcard captures the application ID and device EUI.

### 2. Configure Connection

In `.env.production`:
```bash
ENABLE_MQTT=1
MQTT_BROKER=192.168.1.50       # ChirpStack server IP
MQTT_PORT=1883                  # Default MQTT port
MQTT_TOPIC=application/+/device/+/event/up
```

If ChirpStack uses authentication for MQTT:
```bash
MQTT_USERNAME=parking
MQTT_PASSWORD=your_mqtt_password
```

### 3. Message Format

ChirpStack publishes JSON messages like:
```json
{
  "deviceInfo": {
    "deviceName": "B7",
    "devEui": "aabbccdd11223344",
    "applicationId": "app-uuid-here"
  },
  "data": "AQ=="
}
```

- `data` is base64-encoded LoRa payload
- `AQ==` decodes to `0x01` = OCCUPIED
- `AA==` decodes to `0x00` = FREE
- `zQ==` decodes to `0xCD` = Calibration Done

### 4. Device Name Mapping

The `deviceInfo.deviceName` field must match entries in `config/slot_meta.yaml`:

```yaml
- id: 1
  name: B7       # Must match ChirpStack device name
  zone: B
  preset: 1
- id: 2
  name: B60
  zone: B
  preset: 1
```

If a device name doesn't match any slot, the event is logged and discarded.

## gRPC API Setup

### 1. Generate API Token

1. Open ChirpStack web UI: `http://YOUR_CHIRPSTACK_IP:8080`
2. Go to **API Keys** (top-right menu)
3. Click **Create** and give it a name (e.g., "parking-system")
4. Copy the token

### 2. Configure

In `.env.production`:
```bash
CHIRPSTACK_HOST=192.168.1.50
CHIRPSTACK_GRPC_PORT=8080
CHIRPSTACK_API_TOKEN=eyJ0eXAi...your_token_here
CHIRPSTACK_APP_ID=your-application-uuid
```

### 3. What gRPC is Used For

**Device Enumeration (startup):**
- On API server start, fetches all devices from ChirpStack
- Maps device names to slot IDs using `slot_meta.yaml`
- Stores mapping in Redis: `parking:device:map`

**Downlink Commands (calibration):**
- When a user clicks "Calibrate" on a slot tile
- Sends a downlink to the LoRa sensor via ChirpStack
- Falls back to MQTT publish if gRPC is unavailable

## Data Flow

```
LoRa Sensor
    | (radio)
LoRa Gateway
    | (UDP/TCP)
ChirpStack Network Server
    | (MQTT publish)
API Server (on_mqtt_message)
    | (XADD to Redis Stream)
parking:mqtt:events
    | (XREADGROUP)
MQTT Workers (4x)
    |-> Atomic slot state transition (Lua CAS)
    |-> Insert occupancy_events (PostgreSQL)
    |-> Publish SSE event (Redis pub/sub)
    +-> Enqueue camera task (if FREE->OCCUPIED)
```

## Verifying the Integration

### Test MQTT connectivity

```bash
# Subscribe to all ChirpStack events
mosquitto_sub -h YOUR_CHIRPSTACK_IP -t '#' -v

# You should see messages when sensors fire
```

### Test without ChirpStack (inject directly)

```bash
# Inject a test event into the Redis stream
python3 -c "
import redis, json, base64
r = redis.Redis.from_url('redis://localhost:6379')
payload = {
    'deviceInfo': {'deviceName': 'B7', 'devEui': 'test123', 'applicationId': 'test'},
    'data': base64.b64encode(b'\x01').decode()
}
r.xadd('parking:mqtt:events', {'payload': json.dumps(payload)})
print('Injected test OCCUPIED event for device B7')
"
```

### Check device mapping

```bash
redis-cli HGETALL parking:device:map
```

## Sensor Configuration

### LoRa Payload Format

| Hex | Meaning | Dashboard State |
|-----|---------|-----------------|
| `00` | Vehicle left | FREE |
| `01` | Vehicle detected | OCCUPIED |
| `CD` | Calibration done | (logged, no state change) |

### Recommended Sensor Settings

- **Uplink interval:** Event-driven (not periodic)
- **Confirmed uplinks:** Enabled (for reliability)
- **fPort:** 1 (default for uplinks)
- **ADR:** Enabled (adaptive data rate)
