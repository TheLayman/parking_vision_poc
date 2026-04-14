# ChirpStack Integration Guide (POC)

## POC Setup

Single device runs everything: ChirpStack, PostgreSQL, Redis, and the parking dashboard application. 150 BMM350 magnetometer sensors, 1 LoRa gateway, ~50m radius.

```
┌──────────────────────────────────────────────────┐
│  POC Device (Raspberry Pi / Mini PC / Server)    │
│                                                  │
│  ┌─────────────┐   MQTT    ┌──────────────────┐  │
│  │ ChirpStack  │ ────────> │ Parking Dashboard │  │
│  │  (port 8080)│ localhost │  (port 8000)      │  │
│  └──────┬──────┘           └────────┬─────────┘  │
│         │                           │             │
│  ┌──────┴──────┐           ┌────────┴─────────┐  │
│  │ Mosquitto   │           │ Redis + Postgres  │  │
│  │ (port 1883) │           │ (6379 / 5432)     │  │
│  └─────────────┘           └──────────────────┘  │
└──────────────────────────────────────────────────┘
          ▲
          │ UDP (Semtech packet forwarder)
    ┌─────┴─────┐
    │ LoRa GW   │
    │ (1 unit)  │
    └─────┬─────┘
          │ radio (IN865, ~50m)
    ┌─────┴──────────────────────────────────┐
    │  150 x BMM350 Parking Sensors          │
    │  STM32WLE5CC, Class A, OTAA            │
    └────────────────────────────────────────┘
```

## Step 1: Install ChirpStack

Follow the official guide for your platform. For Debian/Ubuntu:

```bash
# Add ChirpStack repo
sudo apt-get install -y apt-transport-https
sudo sh -c 'echo "deb https://artifacts.chirpstack.io/packages/4.x/deb stable main" > /etc/apt/sources.list.d/chirpstack.list'
curl -fsSL https://artifacts.chirpstack.io/packages/4.x/deb/pool/main/c/chirpstack/chirpstack_4.10.2_linux_arm64.deb -o /tmp/chirpstack.deb
# or download from https://www.chirpstack.io/docs/getting-started/

sudo apt-get update
sudo apt-get install -y chirpstack mosquitto mosquitto-clients
```

### ChirpStack config (`/etc/chirpstack/chirpstack.toml`)

Key settings for the POC:

```toml
[network]
  enabled_regions = ["in865_867"]    # India 865 MHz

[integration]
  [integration.mqtt]
    server = "tcp://localhost:1883"  # Local Mosquitto
    json = true                      # JSON encoding (not Protobuf)
```

Restart:
```bash
sudo systemctl restart chirpstack
sudo systemctl enable chirpstack
```

Verify: open `http://localhost:8080` in a browser. Default login: `admin` / `admin`.

## Step 2: Configure the Gateway

### In ChirpStack UI:

1. Go to **Gateways** > **Add gateway**
2. Enter the gateway EUI (from gateway label or config)
3. Set region to **IN865**

### On the gateway itself:

Configure the packet forwarder to point to your POC device:

```json
{
  "server_address": "localhost",
  "serv_port_up": 1700,
  "serv_port_down": 1700
}
```

If the gateway is a separate physical device, replace `localhost` with the POC device's IP.

Verify: the gateway should appear as "online" (green dot) in ChirpStack within ~30 seconds.

## Step 3: Create Device Profile

In ChirpStack UI:

1. Go to **Device profiles** > **Add device profile**
2. Settings:

| Field | Value | Why |
|-------|-------|-----|
| Name | `BMM350-Parking-Sensor` | |
| Region | IN865 | India 865 MHz band |
| MAC version | LoRaWAN 1.0.3 | Matches firmware (STM32WLE5 HAL) |
| Regional params revision | RP002-1.0.1 | |
| ADR algorithm | Default ADR | Firmware has ADR enabled |
| Expected uplink interval | 5400 (seconds) | Heartbeat every ~90 min |
| Device-status request frequency | 0 | Sensors report status in payload |
| Supports Class-B | No | Firmware defaults to Class A |
| Supports Class-C | No | |

3. Under **Codec** tab:
   - Choose **JavaScript functions**
   - Paste the decoder:

```javascript
function decodeUplink(input) {
  var status = input.bytes[0];
  var result = {};

  if (status === 0x00) {
    result.state = "FREE";
  } else if (status === 0x01) {
    result.state = "OCCUPIED";
  } else if (status === 0x09) {
    result.state = "BATTERY_LOW";
  } else if (status === 0x0A) {
    result.state = "TEMPERATURE_HIGH";
  } else if (status === 0xCD) {
    result.state = "CALIBRATION_DONE";
  }

  return { data: result };
}

function encodeDownlink(input) {
  if (input.data.command === "calibrate") {
    return { bytes: [0xCC], fPort: 2 };
  }
  if (input.data.command === "set_threshold") {
    var n = Math.round(input.data.threshold * 2);
    return { bytes: [0xDD, n], fPort: 2 };
  }
  return { bytes: [], fPort: 2 };
}
```

> Note: The codec is optional — the parking dashboard decodes the raw base64 payload directly. But having it in ChirpStack makes the device data readable in the ChirpStack UI and enables the Events tab to show human-readable values.

## Step 4: Create Application and Register Devices

### Create Application

1. Go to **Applications** > **Add application**
2. Name: `parking-poc`
3. Note the **Application ID** (UUID) — you'll need it later

### Register Devices (150 sensors)

For each sensor, you need its **DevEUI** and **AppKey** (printed on the sensor or in the manufacturer's provisioning sheet).

**One at a time (UI):**
1. Go to your application > **Add device**
2. Name: must match `config/slot_meta.yaml` entries (e.g., `A1`, `A2`, `B31`, etc.)
3. Device EUI: from the sensor
4. Device profile: `BMM350-Parking-Sensor`
5. Under **OTAA keys** tab, enter the **AppKey**

**Bulk registration (CLI, recommended for 150 devices):**

```bash
# Create a CSV: device_name,dev_eui,app_key
# Example:
# A1,0011223344556677,00112233445566778899aabbccddeeff
# A2,0011223344556688,00112233445566778899aabbccddeef0
# ...

# Use chirpstack CLI or API to bulk-register:
while IFS=, read -r name eui key; do
  grpcurl -plaintext \
    -H "Authorization: Bearer YOUR_API_TOKEN" \
    -d "{
      \"device\": {
        \"devEui\": \"$eui\",
        \"name\": \"$name\",
        \"applicationId\": \"YOUR_APP_ID\",
        \"deviceProfileId\": \"YOUR_PROFILE_ID\",
        \"isDisabled\": false
      }
    }" \
    localhost:8080 api.DeviceService/Create

  # Set OTAA keys
  grpcurl -plaintext \
    -H "Authorization: Bearer YOUR_API_TOKEN" \
    -d "{
      \"deviceKeys\": {
        \"devEui\": \"$eui\",
        \"nwkKey\": \"$key\"
      }
    }" \
    localhost:8080 api.DeviceService/CreateKeys

  echo "Registered $name ($eui)"
done < devices.csv
```

### Critical: Device Names Must Match slot_meta.yaml

The dashboard maps ChirpStack device names to parking slots via `config/slot_meta.yaml`. Every `deviceInfo.deviceName` in the MQTT message must match a `name` field in the YAML:

```yaml
# config/slot_meta.yaml
- id: 1
  name: A1          # <-- must match ChirpStack device name exactly
  zone: A
  lat: 17.3850
  lng: 78.4867
- id: 2
  name: A2
  zone: A
  lat: 17.3851
  lng: 78.4868
# ... 150 entries
```

If a device name doesn't match, the MQTT worker logs a warning and discards the event.

## Step 5: Configure the Dashboard

### Environment variables

Since everything runs on the same device, all hosts are `localhost`:

```bash
# .env (or export in your shell)
ENABLE_MQTT=1
MQTT_BROKER=localhost
MQTT_PORT=1883
MQTT_TOPIC=application/+/device/+/event/up
REDIS_URL=redis://localhost:6379
DATABASE_URL=postgresql://parking:parking@localhost/parking
```

### Start the services

```bash
# 1. Ensure Redis and Postgres are running
sudo systemctl start redis-server postgresql

# 2. Apply the schema
psql -U parking -d parking -f db/schema.sql

# 3. Start MQTT workers (2 is enough for 150 sensors)
WORKER_ID=w1 python3 -m workers.mqtt_worker &
WORKER_ID=w2 python3 -m workers.mqtt_worker &

# 4. Start the API server
python3 -m uvicorn webapp.server:app --host 0.0.0.0 --port 8000
```

Or use the systemd services:
```bash
sudo systemctl start parking-mqtt-worker@1 parking-mqtt-worker@2
sudo systemctl start parking-api
```

## Step 6: Verify End-to-End

### 1. Check MQTT messages are flowing

```bash
mosquitto_sub -h localhost -t 'application/+/device/+/event/up' -v
```

You should see JSON messages when sensors fire. Heartbeats arrive every ~90 minutes. State changes arrive within 20-30 seconds of a vehicle arriving/departing (firmware debounce).

### 2. Check the MQTT worker is processing

```bash
# Stream depth (should be near 0 if workers are keeping up)
redis-cli XLEN parking:mqtt:events

# Slot states
redis-cli HGETALL parking:slot:state

# Sensor lastseen timestamps
redis-cli HGETALL parking:sensor:lastseen
```

### 3. Check the dashboard

Open `http://localhost:8000` (or the device's IP if accessing remotely). You should see:
- Slots appearing with FREE/OCCUPIED states
- KPI cards populating (Total Slots, Occupancy %, Sensors Online)
- Change Log entries when state transitions happen
- Analytics tab with data after ~1 hour of events

### 4. Inject a test event (without waiting for a real sensor)

```bash
python3 -c "
import redis, json, base64
r = redis.Redis()
payload = {
    'deviceInfo': {'deviceName': 'A1', 'devEui': 'test123', 'applicationId': 'test'},
    'data': base64.b64encode(bytes.fromhex('01')).decode()
}
r.xadd('parking:mqtt:events', {'payload': json.dumps(payload)})
print('Injected OCCUPIED event for A1')
"
```

Check the dashboard — slot A1 should turn red.

## Sensor Payload Reference

### Uplink (sensor to server)

| Hex Byte | Meaning | Dashboard Effect |
|----------|---------|-----------------|
| `0x00` | Vehicle departed | Slot turns FREE (green) |
| `0x01` | Vehicle detected | Slot turns OCCUPIED (red) |
| `0x09` | Battery low (<1.5V) | Device alert icon on slot tile |
| `0x0A` | Temperature high (>45C) | Device alert icon on slot tile |
| `0xCD` | Calibration complete | Logged, no state change |

Payload is always **1 byte** on **fPort 2**.

### Downlink (server to sensor)

| Command | Bytes | Effect |
|---------|-------|--------|
| Recalibrate | `0xCC` | Sensor collects 5 new baseline samples (~5 min) |
| Set threshold | `0xDD <N>` | Threshold = N * 0.5 uT. Default: `0xDD 0x09` = 4.5 uT |

### Sensor Timing

| Event | Frequency |
|-------|-----------|
| Heartbeat | Every ~90 min (current state, same 1-byte format) |
| State change | Immediate after 20-30s firmware confirmation |
| Retransmit | 15s after each state change (reliability) |
| Sensing cycle | Every ~60s (+/- 20s jitter) |
| Calibration | 5 samples x ~60s = ~5 min total |

### Firmware Detection Algorithm

The sensor handles all occupancy detection internally:

1. **Baseline**: 5-sample average of magnetic field (x/y/z), stored in flash
2. **Detection**: Euclidean distance from baseline > 4.5 uT threshold
3. **Hysteresis**: Occupied at 4.5 uT, vacant at 4.05 uT (10% band)
4. **Confirmation**: 2-3 consecutive readings 10s apart must agree
5. **Drift correction**: EMA baseline update (alpha=0.01) on vacancy

The server does NOT need to debounce or filter. Trust the firmware's state determination.

## Capacity Planning for 150 Sensors

### MQTT throughput

Worst case: all 150 sensors change state simultaneously.
- 150 uplinks + 150 retransmits (15s later) = 300 messages in ~30s
- Each message is ~200 bytes JSON
- Total: ~60 KB — trivial for local MQTT

Steady state: ~150 heartbeats per 90 min = ~1.7 messages/min. Nearly idle.

### Redis

- 150 slot state entries in `parking:slot:state` hash: ~5 KB
- Stream depth: stays near 0 with 2 workers
- Memory: <10 MB total

### PostgreSQL

- ~300 events/day (2 state changes per slot per day average)
- ~150 heartbeats logged per 90 min cycle (if logging heartbeats)
- At this rate, 1 year = ~110K rows. Trivially small.

### Workers

2 MQTT workers are more than sufficient for 150 sensors. Each worker processes messages in <10ms.

## Troubleshooting

### Sensors not appearing on dashboard

1. Check gateway is online in ChirpStack UI
2. Check device has joined (OTAA): ChirpStack > Application > Device > Events tab
3. Verify device name matches `slot_meta.yaml`:
   ```bash
   # What ChirpStack calls the device
   mosquitto_sub -h localhost -t 'application/+/device/+/event/up' -C 1 | python3 -c "import sys,json; print(json.load(sys.stdin)['deviceInfo']['deviceName'])"

   # What slot_meta.yaml expects
   grep -o 'name: .*' config/slot_meta.yaml | head
   ```

### Sensor shows OCCUPIED but slot is empty (or vice versa)

The sensor's magnetic baseline may be wrong. Recalibrate:

```bash
# Via Redis inject (direct)
python3 -c "
import redis, json, base64
r = redis.Redis()
# Find the device EUI for the slot
dev_map = r.hgetall('parking:device:map')
print('Device map:', {k.decode(): json.loads(v) for k,v in dev_map.items()})
"

# Via ChirpStack UI: Device > Enqueue downlink > fPort 2 > data: zA== (0xCC base64)
```

After sending `0xCC`, the sensor enters calibration mode for ~5 minutes. Ensure the slot is **empty** during calibration.

### No MQTT messages at all

```bash
# 1. Is Mosquitto running?
sudo systemctl status mosquitto

# 2. Is ChirpStack publishing?
mosquitto_sub -h localhost -t '#' -v -C 5

# 3. Is the gateway forwarding?
# Check ChirpStack > Gateways > your gateway > "Last seen"

# 4. Is the MQTT topic correct?
echo "Expected: application/+/device/+/event/up"
echo "Configured: $MQTT_TOPIC"
```

### Dashboard shows stale data

Sensors send heartbeats every ~90 minutes. If a sensor hasn't been heard from in >100 minutes, the dashboard marks it as "Sensor offline" (grey, dashed border). This is normal during initial deployment while sensors are still joining the network.

Check sensor health:
```bash
redis-cli HGETALL parking:sensor:lastseen
```
