# Parking Vision POC

Unauthorized parking enforcement: LoRaWAN sensors -> ChirpStack MQTT -> Redis Streams -> Workers -> PostgreSQL -> FastAPI dashboard.

3,000 sensors, ~40 PTZ cameras, 2 servers (app + management). Production branch: `prod_sizin`.

## Commands

```bash
# Run tests (all)
python3 -m pytest tests/ -v

# Run single test file (preferred during dev)
python3 -m pytest tests/test_mqtt_worker.py -v

# Dev server (no MQTT, no cameras)
ENABLE_MQTT=0 DATABASE_URL=postgresql://localhost/parking \
  python3 -m uvicorn webapp.server:app --reload --port 8000

# Seed emulator data (3000 slots, 48h history, dummy images)
DATABASE_URL=postgresql://localhost/parking python3 scripts/emulate.py --seed-only

# Live simulation
DATABASE_URL=postgresql://localhost/parking python3 scripts/emulate.py --live-only
```

## Architecture (critical file paths)

| Component | File | Role |
|-----------|------|------|
| API server | `webapp/server.py` | FastAPI, SSE, /state, /challans, /events endpoints |
| Slot metadata | `webapp/helpers/slot_meta.py` | Loads config/slot_meta.yaml, zone stats, state builder |
| Analytics | `webapp/helpers/analytics.py` | Dwell times, challan summary, hourly incidents |
| OCR extractor | `webapp/license_plate_extractor.py` | OpenAI Vision plate extraction |
| MQTT worker | `workers/mqtt_worker.py` | Redis Streams consumer, Lua CAS for atomic slot state |
| Camera worker | `workers/camera_worker.py` | Per-camera process, PTZ move + RTSP capture |
| Inference worker | `workers/inference_worker.py` | OpenAI OCR, challan recheck logic, dead-letter handling |
| DB layer | `db/client.py` | psycopg3 connection pool, insert/query functions |
| Schema | `db/schema.sql` | 3 tables: occupancy_events, challan_events, camera_captures |
| Slot config | `config/slot_meta.yaml` | Slot ID, name, zone, preset, lat, lng |
| Camera config | `config/cameras.yaml` | Camera IP, credentials, slot-to-preset mapping |
| Emulator | `scripts/emulate.py` | Generates test data, dummy images, live simulation |
| Frontend | `webapp/static/` | Dashboard HTML/JS, challan.html |
| Systemd | `config/systemd/` | Service templates for workers |
| Docs | `docs/` | deployment.md, configuration.md, chirpstack-integration.md |

## Critical constraints

- **Redis eviction**: MUST be `noeviction` with 4GB maxmemory. `allkeys-lru` would silently drop slot state.
- **psycopg3 only**: Never use psycopg2. All DB code uses `psycopg` (v3) with connection pooling.
- **Slot state atomicity**: Slot state transitions use a Lua CAS script in `mqtt_worker.py`. Never write to `parking:slot:state` directly from Python.
- **RAID is not backup**: Both servers use RAID 1. Nightly rsync of snapshots + pg_dump to management server is the actual backup.
- **ChirpStack on management server**: MQTT_BROKER and CHIRPSTACK_HOST point to the management server IP, not localhost.

## Non-obvious env vars

| Variable | Default | Why it matters |
|----------|---------|---------------|
| `ENABLE_MQTT` | `1` | Set to `0` for local dev without ChirpStack |
| `ENABLE_CAMERA_CONTROL` | `false` | Set to `false` without physical cameras |
| `CHALLAN_RECHECK_INTERVAL` | `70` | Seconds between 1st and 2nd OCR capture. Production: use 180+ |
| `LPR_BACKEND` | `openai` | OCR backend. `easyocr` available as offline fallback |
| `PLATE_REGEX_PATTERN` | Indian format | Regex for plate validation |

## Code style

- Python 3.11+, type hints on function signatures
- Workers are sync processes; API endpoints are async (FastAPI)
- Tests use `fakeredis` for Redis, `unittest.mock` for Postgres
- Config files are YAML in `config/`, NOT JSON
- Challan metadata stored in JSONB column (no schema migration for new fields)

## Test file mapping

| Source | Test |
|--------|------|
| `webapp/server.py` | `tests/test_api.py` |
| `workers/mqtt_worker.py` | `tests/test_mqtt_worker.py` |
| `workers/camera_worker.py` | `tests/test_camera_worker.py` |
| `workers/inference_worker.py` | `tests/test_inference_worker.py` |
| `db/client.py` | `tests/test_db_client.py` |
| Full pipeline | `tests/test_e2e.py` |
