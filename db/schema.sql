-- PostgreSQL schema for Smart Parking Dashboard POC
-- Run once: psql -U postgres -d parking < db/schema.sql

CREATE TABLE IF NOT EXISTS occupancy_events (
    id          BIGSERIAL PRIMARY KEY,
    slot_id     INT NOT NULL,
    event_type  VARCHAR(32) NOT NULL CHECK (event_type IN ('FREE', 'OCCUPIED', 'calibration', 'battery_low', 'temperature_high')),
    device_eui  VARCHAR(32),
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload     JSONB
);
CREATE INDEX IF NOT EXISTS occupancy_events_slot_ts ON occupancy_events (slot_id, ts DESC);
CREATE INDEX IF NOT EXISTS occupancy_events_ts ON occupancy_events (ts DESC);
