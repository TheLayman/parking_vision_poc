# Smart Parking Dashboard POC

Dashboard-only POC: LoRaWAN sensors -> ChirpStack MQTT -> Redis Streams -> MQTT Worker -> PostgreSQL -> FastAPI dashboard.

No cameras, no OCR, no challans. Focus: accuracy (sensor health monitoring) and dashboard features (live state, analytics, state changes).

Firmware (BMM350 magnetometer) handles all occupancy detection: 3-axis Euclidean distance, hysteresis, 20-30s confirmation. Server trusts the device's 1-byte status (0x00=FREE, 0x01=OCCUPIED, 0x09=battery_low, 0x0A=temp_high).

## Commands

```bash
# Run tests (all)
python3 -m pytest tests/ -v

# Run single test file (preferred during dev)
python3 -m pytest tests/test_mqtt_worker.py -v

# Dev server (no MQTT)
ENABLE_MQTT=0 DATABASE_URL=postgresql://localhost/parking \
  python3 -m uvicorn webapp.server:app --reload --port 8000

# Seed emulator data
DATABASE_URL=postgresql://localhost/parking python3 scripts/emulate.py --seed-only

# Live simulation
DATABASE_URL=postgresql://localhost/parking python3 scripts/emulate.py --live-only
```

## Architecture

| Component | File | Role |
|-----------|------|------|
| API server | `webapp/server.py` | FastAPI, SSE, /state, /events, /analytics, /state-changes |
| Slot metadata | `webapp/helpers/slot_meta.py` | Loads config/slot_meta.yaml, zone stats, state builder |
| Analytics | `webapp/helpers/analytics.py` | Dwell times, hourly occupancy events |
| MQTT worker | `workers/mqtt_worker.py` | Redis Streams consumer, Lua CAS, device health tracking |
| Worker base | `workers/base.py` | Stream consumer loop, DB reconnect |
| DB layer | `db/client.py` | psycopg3 connection pool, insert/query functions |
| Schema | `db/schema.sql` | 1 table: occupancy_events |
| Slot config | `config/slot_meta.yaml` | Slot ID, name, zone, lat, lng |
| Emulator | `scripts/emulate.py` | Generates test data, live simulation |
| Frontend | `webapp/static/` | Dashboard HTML/JS (index.html, app.js, styles.css) |
| Systemd | `config/systemd/` | Service templates (API + MQTT workers) |
| Docs | `docs/` | deployment.md, configuration.md, chirpstack-integration.md |

## Redis keys

| Key | Type | Purpose |
|-----|------|---------|
| `parking:slot:state` | Hash | Current state per slot (FREE/OCCUPIED) |
| `parking:slot:since` | Hash | Timestamp of last state transition |
| `parking:sensor:lastseen` | Hash | Last uplink timestamp per slot |
| `parking:sensor:rssi` | Hash | Best RSSI per slot |
| `parking:sensor:alerts` | Hash | Active device alerts (battery_low, temperature_high) |
| `parking:mqtt:events` | Stream | Raw MQTT uplinks for mqtt_worker |
| `parking:events:live` | Pub/Sub | SSE broadcast channel |

## Critical constraints

- **Redis eviction**: MUST be `noeviction` with 4GB maxmemory.
- **psycopg3 only**: Never use psycopg2. All DB code uses `psycopg` (v3).
- **Slot state atomicity**: Lua CAS script in `mqtt_worker.py`. Never write `parking:slot:state` directly.
- **Trust the firmware**: Device handles detection, debouncing, calibration. Server does NOT add extra debouncing.

## Env vars

| Variable | Default | Why it matters |
|----------|---------|---------------|
| `ENABLE_MQTT` | `1` | Set to `0` for local dev without ChirpStack |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection |
| `DATABASE_URL` | `postgresql://parking:parking@localhost/parking` | Postgres connection |

## Code style

- Python 3.11+, type hints on function signatures
- Workers are sync processes; API endpoints are async (FastAPI)
- Tests use `fakeredis` for Redis, `unittest.mock` for Postgres
- Config files are YAML in `config/`, NOT JSON

## Test file mapping

| Source | Test |
|--------|------|
| `webapp/server.py` | `tests/test_api.py` |
| `workers/mqtt_worker.py` | `tests/test_mqtt_worker.py` |
| `db/client.py` | `tests/test_db_client.py` |
