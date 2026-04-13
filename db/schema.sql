-- PostgreSQL schema for Smart Parking Enforcement (production)
-- Run once: psql -U postgres -d parking < db/schema.sql

CREATE TABLE IF NOT EXISTS occupancy_events (
    id          BIGSERIAL PRIMARY KEY,
    slot_id     INT NOT NULL,
    event_type  VARCHAR(32) NOT NULL CHECK (event_type IN ('FREE', 'OCCUPIED', 'calibration')),
    device_eui  VARCHAR(32),
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload     JSONB
);
CREATE INDEX IF NOT EXISTS occupancy_events_slot_ts ON occupancy_events (slot_id, ts DESC);
CREATE INDEX IF NOT EXISTS occupancy_events_ts ON occupancy_events (ts DESC);

CREATE TABLE IF NOT EXISTS challan_events (
    id            BIGSERIAL PRIMARY KEY,
    challan_id    VARCHAR(64) UNIQUE NOT NULL,
    slot_id       INT NOT NULL,
    license_plate VARCHAR(32),
    confidence    FLOAT,
    status        VARCHAR(32) CHECK (status IN ('confirmed', 'cleared', 'pending')),
    ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata      JSONB
);
CREATE INDEX IF NOT EXISTS challan_events_slot_ts ON challan_events (slot_id, ts DESC);
CREATE INDEX IF NOT EXISTS challan_events_status ON challan_events (status);
CREATE INDEX IF NOT EXISTS challan_events_plate ON challan_events (license_plate);

CREATE TABLE IF NOT EXISTS camera_captures (
    id          BIGSERIAL PRIMARY KEY,
    slot_id     INT,
    camera_id   VARCHAR(64),
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    image_path  TEXT,
    ocr_result  JSONB,
    backend     VARCHAR(32)
);
CREATE INDEX IF NOT EXISTS camera_captures_slot_ts ON camera_captures (slot_id, ts DESC);
CREATE INDEX IF NOT EXISTS camera_captures_ts ON camera_captures (ts DESC);
