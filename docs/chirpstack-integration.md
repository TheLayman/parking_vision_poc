# ChirpStack Integration Guide

## Overview

The system integrates with ChirpStack v4 via two channels:
1. **MQTT** -- Receives sensor uplink messages (occupancy state changes)
2. **gRPC API** -- Fetches device list, sends downlink commands (calibration)

**ChirpStack runs on the management server**, separate from the application server.
This ensures sensor events continue to be received even if the application server
goes down. See [Server Architecture](deployment.md#server-architecture) for details.

## MQTT Setup

### 1. ChirpStack MQTT Broker

ChirpStack publishes device events to its built-in MQTT broker on the **management server**. The parking application (on the application server) subscribes over the network.

**Topic pattern:** `application/+/device/+/event/up`

This matches all devices across all applications. The `+` wildcard captures the application ID and device EUI.

### 2. Configure Connection

In `.env.production` on the **application server**:
```bash
ENABLE_MQTT=1
MQTT_BROKER=MANAGEMENT_SERVER_IP   # Management server IP (NOT localhost)
MQTT_PORT=1883                     # Default MQTT port
MQTT_TOPIC=application/+/device/+/event/up
```

If ChirpStack uses authentication for MQTT:
```bash
MQTT_USERNAME=parking
MQTT_PASSWORD=your_mqtt_password
```

> **Resilience:** When the application server restarts, MQTT workers reconnect to
> the management server's broker automatically. Any events published while the app
> server was down are available if MQTT QoS 1 with persistent sessions is configured
> on the broker (recommended).

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

1. Open ChirpStack web UI on the management server: `http://MANAGEMENT_SERVER_IP:8080`
2. Go to **API Keys** (top-right menu)
3. Click **Create** and give it a name (e.g., "parking-system")
4. Copy the token

### 2. Configure

In `.env.production` on the **application server**:
```bash
CHIRPSTACK_HOST=MANAGEMENT_SERVER_IP   # Management server (NOT localhost)
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
ChirpStack Network Server          ← Management Server
    | (MQTT publish)                ← crosses network boundary
API Server (on_mqtt_message)        ← Application Server
    | (XADD to Redis Stream)
parking:mqtt:events
    | (XREADGROUP)
MQTT Workers (4x)
    |-> Atomic slot state transition (Lua CAS)
    |-> Insert occupancy_events (PostgreSQL)
    |-> Publish SSE event (Redis pub/sub)
    +-> Enqueue camera task (if FREE->OCCUPIED)
```

> The network boundary between management and application server is the MQTT
> connection. If the application server goes down, ChirpStack continues receiving
> sensor data and the MQTT broker queues messages for when the app reconnects.

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
