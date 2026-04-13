---
name: change-map
description: Maps common changes to the exact files that need updating. Use when planning modifications to understand the blast radius.
---

# Change Map

## Add a new field to slot metadata (like GPS was added)
1. `config/slot_meta.yaml` -- add field to slot entries
2. `scripts/emulate.py` > `generate_slot_meta()` -- generate the field for emulated slots
3. `scripts/emulate.py` > seed + live challan sections -- include in challan metadata dict
4. `workers/inference_worker.py` > `_process_challan_recheck()` -- load from `load_slot_meta_by_id()`, add to metadata
5. `webapp/server.py` > `/challans` endpoint -- surface field in API response
6. `webapp/helpers/slot_meta.py` > `build_state_from_log()` -- include in slot objects if needed for /state
7. `webapp/helpers/analytics.py` > `parse_events_from_log()` -- carry through parsed challan events
8. `webapp/static/challan.html` -- add column header + render cell in `renderTable()`
9. `docs/configuration.md` -- document the new field
10. Run: `python3 -m pytest tests/ -v`

## Add a new sensor event type
1. `workers/mqtt_worker.py` -- add handler in the message processing function
2. `db/schema.sql` -- modify CHECK constraint on event_type if needed
3. `db/client.py` -- update insert/query functions
4. `webapp/server.py` -- update /state or SSE events if visible to frontend
5. `tests/test_mqtt_worker.py` -- add test case
6. Run: `python3 -m pytest tests/test_mqtt_worker.py -v`

## Add a new API endpoint
1. `webapp/server.py` -- add FastAPI route
2. `tests/test_api.py` -- add endpoint test
3. `webapp/static/` -- add frontend JS call if UI-facing
4. Run: `python3 -m pytest tests/test_api.py -v`

## Add a new camera
1. `config/cameras.yaml` -- add entry: camera ID, IP, credentials, slot_presets mapping
2. No code changes needed -- systemd template handles it
3. Deploy: `sudo systemctl enable --now parking-camera-worker@NEW_CAM_ID.service`

## Modify OCR / plate recognition logic
1. `webapp/license_plate_extractor.py` -- main OCR logic
2. `workers/inference_worker.py` -- calls extractor, handles challan decisions
3. `tests/test_inference_worker.py` -- update/add tests
4. `.env` > `PLATE_REGEX_PATTERN` -- if plate format changes
5. Run: `python3 -m pytest tests/test_inference_worker.py -v`

## Change database schema
1. `db/schema.sql` -- modify DDL
2. `db/client.py` -- update insert/query SQL
3. `tests/conftest.py` -- update test schema bootstrap
4. `tests/test_db_client.py` -- update assertions
5. Run: `python3 -m pytest tests/test_db_client.py -v`
6. IMPORTANT: Existing data needs migration. Consider using metadata JSONB column instead of new columns.

## Modify Redis slot state logic
1. `workers/mqtt_worker.py` -- Lua CAS script lives here. NEVER bypass it.
2. `webapp/helpers/slot_meta.py` -- reads from Redis HGETALL
3. `webapp/server.py` > `/state` endpoint -- reads Redis via slot_meta helper
4. `config/redis.conf` -- NEVER change eviction policy from noeviction
5. `tests/test_mqtt_worker.py` -- test CAS behavior
6. Run: `python3 -m pytest tests/test_mqtt_worker.py -v`

## Modify the challan workflow (recheck timing, match logic)
1. `workers/inference_worker.py` -- `CHALLAN_RECHECK_DELAY`, `_PLATE_MATCH_THRESHOLD`, `_process_challan_recheck()`
2. `.env` > `CHALLAN_RECHECK_INTERVAL` -- seconds between 1st and 2nd capture
3. `scripts/emulate.py` > `CHALLAN_RECHECK_MINUTES` / `--recheck-minutes` -- emulator equivalent
4. `tests/test_inference_worker.py` -- challan recheck tests
5. Run: `python3 -m pytest tests/test_inference_worker.py -v`

## Add/modify dashboard UI
1. `webapp/static/challan.html` -- challan dashboard (table, filters, SSE)
2. `webapp/static/index.html` -- main parking dashboard (if it exists)
3. `webapp/server.py` -- API endpoints the frontend calls
4. Frontend loads data from `/state`, `/challans`, `/events` (SSE), `/analytics/summary`

## Modify emulator behavior
1. `scripts/emulate.py` -- all emulation logic
2. Config constants at top of file: `OCCUPANCY_RATE`, `CHALLAN_PROBABILITY`, `HISTORY_HOURS`, etc.
3. CLI args: `--slots`, `--zones`, `--history-hours`, `--recheck-minutes`, `--seed-only`, `--live-only`
4. Images generated to `/data/snapshots/emu/`

## Update deployment / infrastructure docs
1. `docs/deployment.md` -- server architecture, systemd, firewall, backup, failure scenarios
2. `docs/chirpstack-integration.md` -- MQTT/gRPC setup, ChirpStack on management server
3. `docs/configuration.md` -- env vars, config files, port reference
4. `readme.md` -- quick start, architecture overview

## Add a worker type
1. Create `workers/new_worker.py` -- follow pattern from `workers/mqtt_worker.py`
2. Create `config/systemd/parking-new-worker@.service` -- copy from existing template
3. `docs/deployment.md` -- add to service startup section
4. `docs/configuration.md` -- add Redis stream to streams table
5. `tests/test_new_worker.py` -- add test file
