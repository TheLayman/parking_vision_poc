#!/usr/bin/env python3
"""
Production-scale parking emulator (3000 slots).

Generates slot_meta.yaml, seeds Redis + Postgres with historical data,
then runs a live simulation loop with realistic occupancy patterns.

Usage:
    # Full setup + live simulation (default):
    python3 scripts/emulate.py

    # Seed only (historical data, no live loop):
    python3 scripts/emulate.py --seed-only

    # Live loop only (assumes seed already ran):
    python3 scripts/emulate.py --live-only

    # Custom scale:
    python3 scripts/emulate.py --slots 500 --zones 3

Requires: Redis running on localhost:6379, Postgres with schema applied.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import string
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
import psycopg
import redis
import yaml

# ── Configuration ────────────────────────────────────────────────────────────

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost/parking")

REPO_ROOT = Path(__file__).resolve().parent.parent
SLOT_META_PATH = REPO_ROOT / "config" / "slot_meta.yaml"
SNAPSHOTS_DIR = os.environ.get("SNAPSHOTS_DIR", "/data/snapshots")

# Zone layout: name → target slot count (scaled proportionally)
ZONE_LAYOUT = {
    "A": 0.20,
    "B": 0.25,
    "C": 0.20,
    "D": 0.15,
    "E": 0.10,
    "F": 0.05,
    "G": 0.05,
}

# Indian license plate patterns
PLATE_STATES = ["KA", "MH", "DL", "TN", "AP", "TS", "GJ", "RJ", "UP", "WB"]
PLATE_SERIES = ["01", "02", "03", "04", "05", "10", "11", "12", "19", "20", "51", "53"]

# GPS center point for the parking facility (default: Hyderabad)
GPS_CENTER_LAT = 17.385044
GPS_CENTER_LNG = 78.486671
GPS_SPREAD = 0.003  # ~300m spread across the facility

# Simulation parameters
OCCUPANCY_RATE = 0.55          # ~55% occupied at any time
TURNOVER_PER_MINUTE = 0.02     # fraction of slots that change state per minute
CHALLAN_PROBABILITY = 0.15     # 15% of occupied vehicles get a challan
HISTORY_HOURS = 48             # seed 48 hours of historical data
HISTORY_EVENT_RATE = 0.01      # events per slot per minute for history

# Live simulation
LIVE_TICK_SECONDS = 2          # seconds between simulation ticks
EVENTS_PER_TICK_RANGE = (5, 30)  # random events per tick at 3000-slot scale
CHALLAN_RECHECK_MINUTES = 3    # minimum minutes between 1st and 2nd capture


def generate_plate() -> str:
    state = random.choice(PLATE_STATES)
    series = random.choice(PLATE_SERIES)
    alpha = random.choice(string.ascii_uppercase) + random.choice(string.ascii_uppercase)
    num = f"{random.randint(1, 9999):04d}"
    return f"{state}{series}{alpha}{num}"


def generate_dummy_image(
    path: str,
    slot_name: str,
    plate: str,
    ts: str,
    lat: float = None,
    lng: float = None,
    is_second: bool = False,
    is_challan: bool = False,
):
    """Generate a dummy parking snapshot image with overlaid text.

    For confirmed challans on the 2nd image, GPS coordinates are embedded.
    """
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    # Dark gray background with some noise for realism
    img[:] = (40, 40, 40)
    noise = np.random.randint(0, 15, img.shape, dtype=np.uint8)
    img = cv2.add(img, noise)

    # Draw a car-shaped rectangle in the center
    cv2.rectangle(img, (180, 140), (460, 340), (80, 80, 80), -1)
    cv2.rectangle(img, (180, 140), (460, 340), (120, 120, 120), 2)

    # License plate area
    cv2.rectangle(img, (230, 260), (410, 310), (255, 255, 255), -1)
    cv2.putText(img, plate, (240, 298), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 2)

    # Slot name top-left
    cv2.putText(img, f"Slot: {slot_name}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

    # Timestamp top-right
    short_ts = ts[:19] if len(ts) > 19 else ts
    cv2.putText(img, short_ts, (320, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

    # Capture label
    label = "2nd Capture (Recheck)" if is_second else "1st Capture"
    cv2.putText(img, label, (10, 465), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)

    # Embed GPS on the 2nd image of confirmed challans
    if is_second and is_challan and lat is not None and lng is not None:
        gps_text = f"GPS: {lat:.6f}, {lng:.6f}"
        # Red background strip for GPS
        cv2.rectangle(img, (0, 60), (640, 95), (0, 0, 180), -1)
        cv2.putText(img, gps_text, (10, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(path, img)


def _build_challan_data(
    meta: dict, sid: int, ts: datetime, recheck_minutes: int,
) -> dict:
    """Build challan record data shared by seed and live phases.

    Returns a dict with all fields needed to insert capture + challan rows.
    """
    plate = generate_plate()
    recheck_secs = recheck_minutes * 60 + random.randint(0, 120)
    challan_ts = ts + timedelta(seconds=recheck_secs)
    session_id = str(uuid.uuid4())
    challan_id = f"{sid}_{session_id}_{plate}"[:64]
    is_match = random.random() < 0.6
    status = "confirmed" if is_match else "cleared"
    first_img = f"{SNAPSHOTS_DIR}/emu/{session_id}_1.jpg"
    second_img = f"{SNAPSHOTS_DIR}/emu/{session_id}_2.jpg"
    camera_id = f"CAM_{meta['zone']}_01"

    return {
        "plate": plate,
        "challan_ts": challan_ts,
        "session_id": session_id,
        "challan_id": challan_id,
        "is_match": is_match,
        "status": status,
        "first_img": first_img,
        "second_img": second_img,
        "camera_id": camera_id,
        "second_plates": [plate] if is_match else [generate_plate()],
        "metadata": {
            "slot_name": meta["name"],
            "zone": meta["zone"],
            "first_image": first_img,
            "first_time": ts.isoformat(),
            "second_image": second_img,
            "second_time": challan_ts.isoformat(),
            "first_plates": [plate],
            "second_plates": [plate] if is_match else [generate_plate()],
            "capture_session_id": session_id,
            "camera_id": camera_id,
            "lat": meta.get("lat"),
            "lng": meta.get("lng"),
        },
    }


# ── Phase 1: Generate slot_meta.yaml ─────────────────────────────────────────

def generate_slot_meta(total_slots: int, num_zones: int) -> list[dict]:
    zones = list(ZONE_LAYOUT.keys())[:num_zones]
    weights = [ZONE_LAYOUT[z] for z in zones]
    total_weight = sum(weights)
    weights = [w / total_weight for w in weights]

    slots = []
    slot_id = 1
    cam_id_counter = 1

    for i, zone in enumerate(zones):
        zone_count = int(total_slots * weights[i])
        if i == len(zones) - 1:
            zone_count = total_slots - len(slots)

        slots_per_cam = 80  # ~80 slots per camera
        cam_presets = {}

        # Each zone gets a different GPS sub-area within the facility
        zone_lat_offset = (i / max(len(zones) - 1, 1) - 0.5) * GPS_SPREAD
        zone_lng_base = GPS_CENTER_LNG - GPS_SPREAD / 2

        for j in range(zone_count):
            cam_idx = j // slots_per_cam
            cam_key = f"CAM_{zone}_{cam_idx + 1:02d}"
            preset = (j % slots_per_cam) + 1

            if cam_key not in cam_presets:
                cam_presets[cam_key] = []

            # Distribute slots in a grid-like pattern within the zone
            row = j // 20
            col = j % 20
            lat = GPS_CENTER_LAT + zone_lat_offset + row * 0.00005
            lng = zone_lng_base + col * 0.00008

            slots.append({
                "id": slot_id,
                "name": f"{zone}{slot_id}",
                "zone": zone,
                "preset": preset,
                "lat": round(lat, 6),
                "lng": round(lng, 6),
            })
            slot_id += 1

    return slots


def write_slot_meta(slots: list[dict]):
    SLOT_META_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Back up existing
    if SLOT_META_PATH.exists():
        backup = SLOT_META_PATH.with_suffix(".yaml.bak")
        SLOT_META_PATH.rename(backup)
        print(f"  Backed up existing slot_meta.yaml -> {backup.name}")

    with open(SLOT_META_PATH, "w") as f:
        f.write("# Auto-generated by emulate.py for load testing\n")
        yaml.dump(slots, f, default_flow_style=False, sort_keys=False)

    print(f"  Wrote {len(slots)} slots to {SLOT_META_PATH}")


# ── Phase 2: Seed Redis ─────────────────────────────────────────────────────

def seed_redis(r: redis.Redis, slots: list[dict]):
    print("\n[2/5] Seeding Redis slot state...")

    pipe = r.pipeline()

    # Clear existing state
    pipe.delete("parking:slot:state")
    pipe.delete("parking:slot:since")

    # Set initial state
    now = datetime.now(timezone.utc)
    state_map = {}
    since_map = {}

    for slot in slots:
        sid = str(slot["id"])
        occupied = random.random() < OCCUPANCY_RATE
        state_map[sid] = "OCCUPIED" if occupied else "FREE"
        offset = random.randint(0, 7200)
        since_map[sid] = (now - timedelta(seconds=offset)).isoformat()

    pipe.hset("parking:slot:state", mapping=state_map)
    pipe.hset("parking:slot:since", mapping=since_map)
    pipe.execute()

    occupied_count = sum(1 for v in state_map.values() if v == "OCCUPIED")
    print(f"  {len(slots)} slots: {occupied_count} OCCUPIED, {len(slots) - occupied_count} FREE")


# ── Phase 3: Seed Postgres ───────────────────────────────────────────────────

def seed_postgres(conn: psycopg.Connection, slots: list[dict],
                  history_hours: int = HISTORY_HOURS,
                  recheck_minutes: int = CHALLAN_RECHECK_MINUTES):
    print("\n[3/5] Seeding Postgres with historical data...")

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=history_hours)

    # Pre-generate slot lookup
    slot_by_id = {s["id"]: s for s in slots}
    slot_ids = [s["id"] for s in slots]

    # Track current state per slot for realistic transitions
    current_state = {}
    for s in slots:
        current_state[s["id"]] = "FREE"

    occ_rows = []
    challan_rows = []
    capture_rows = []

    # Generate events minute-by-minute
    total_minutes = history_hours * 60
    events_generated = 0
    challans_generated = 0

    print(f"  Generating {history_hours}h of history for {len(slots)} slots...")

    for minute in range(total_minutes):
        ts = start + timedelta(minutes=minute)

        # Hour-of-day occupancy modifier (busier during work hours)
        hour = ts.hour
        if 8 <= hour <= 18:
            rate_mult = 1.5
        elif 6 <= hour <= 22:
            rate_mult = 1.0
        else:
            rate_mult = 0.3

        # Pick slots that will have events this minute
        num_events = int(len(slots) * HISTORY_EVENT_RATE * rate_mult)
        num_events = max(1, min(num_events, len(slots) // 5))
        event_slots = random.sample(slot_ids, min(num_events, len(slot_ids)))

        for sid in event_slots:
            meta = slot_by_id[sid]
            prev = current_state[sid]

            # Flip state
            new = "OCCUPIED" if prev == "FREE" else "FREE"
            current_state[sid] = new

            dev_eui = f"emu{sid:08x}"
            payload = {
                "slot_name": meta["name"],
                "zone": meta["zone"],
                "prev_state": prev,
                "new_state": new,
            }

            occ_rows.append((
                sid, new, dev_eui, ts, json.dumps(payload),
            ))
            events_generated += 1

            # Simulate challan for some OCCUPIED events
            if new == "OCCUPIED" and random.random() < CHALLAN_PROBABILITY:
                ch = _build_challan_data(meta, sid, ts, recheck_minutes)

                capture_rows.append((
                    sid, ch["camera_id"], ts, ch["first_img"],
                    json.dumps({"plates": [ch["plate"]], "confidence": round(random.uniform(0.7, 0.99), 2)}),
                    "emulated",
                ))
                challan_rows.append((
                    ch["challan_id"], sid, ch["plate"],
                    0.9 if ch["is_match"] else 0.0,
                    ch["status"], ch["challan_ts"],
                    json.dumps(ch["metadata"]),
                ))
                challans_generated += 1

    # Bulk insert
    print(f"  Inserting {events_generated} occupancy events...")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO occupancy_events (slot_id, event_type, device_eui, ts, payload) "
            "VALUES (%s, %s, %s, %s, %s)",
            occ_rows,
        )

    print(f"  Inserting {len(capture_rows)} camera captures...")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO camera_captures (slot_id, camera_id, ts, image_path, ocr_result, backend) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            capture_rows,
        )

    print(f"  Inserting {challans_generated} challan events...")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO challan_events (challan_id, slot_id, license_plate, confidence, status, ts, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            challan_rows,
        )

    conn.commit()

    # Generate a single shared placeholder image for seeded challans.
    # The seed phase uses one image instead of generating thousands individually.
    emu_dir = Path(SNAPSHOTS_DIR) / "emu"
    emu_dir.mkdir(parents=True, exist_ok=True)
    placeholder = str(emu_dir / "_seed_placeholder.jpg")
    if not Path(placeholder).exists():
        generate_dummy_image(placeholder, "SEED", "XX00XX0000",
                             now.isoformat(), is_second=False)
        print(f"  Created seed placeholder image: {placeholder}")

    print(f"  Done: {events_generated} occupancy + {len(capture_rows)} captures + {challans_generated} challans")


# ── Phase 4: Live simulation ────────────────────────────────────────────────

def run_live_simulation(r: redis.Redis, conn: psycopg.Connection, slots: list[dict],
                        recheck_minutes: int = CHALLAN_RECHECK_MINUTES):
    print("\n[5/5] Starting live simulation (Ctrl+C to stop)...")
    print(f"  {EVENTS_PER_TICK_RANGE[0]}-{EVENTS_PER_TICK_RANGE[1]} events every {LIVE_TICK_SECONDS}s")
    print(f"  Dashboard: http://localhost:8000")
    print(f"  Challans:  http://localhost:8000/challan-dashboard")
    print()

    slot_by_id = {s["id"]: s for s in slots}
    slot_ids = [s["id"] for s in slots]
    tick = 0

    # Read current state from Redis
    raw_state = r.hgetall("parking:slot:state")
    current_state = {}
    for sid_bytes, state_bytes in raw_state.items():
        sid = sid_bytes.decode() if isinstance(sid_bytes, bytes) else sid_bytes
        st = state_bytes.decode() if isinstance(state_bytes, bytes) else state_bytes
        current_state[int(sid)] = st

    try:
        while True:
            tick += 1
            now = datetime.now(timezone.utc)
            ts_str = now.isoformat()

            # Scale events to slot count
            scale_factor = len(slots) / 3000
            lo = max(1, int(EVENTS_PER_TICK_RANGE[0] * scale_factor))
            hi = max(lo + 1, int(EVENTS_PER_TICK_RANGE[1] * scale_factor))
            num_events = random.randint(lo, hi)

            event_slots = random.sample(slot_ids, min(num_events, len(slot_ids)))

            state_changes = 0
            new_challans = 0

            for sid in event_slots:
                meta = slot_by_id[sid]
                prev = current_state.get(sid, "FREE")
                new = "OCCUPIED" if prev == "FREE" else "FREE"

                # Atomic CAS via Redis
                cas_result = r.eval(
                    """
                    local key = KEYS[1]
                    local field = ARGV[1]
                    local expected = ARGV[2]
                    local new_val = ARGV[3]
                    local current = redis.call('HGET', key, field)
                    if current == false then current = 'FREE' end
                    if current == expected then
                        redis.call('HSET', key, field, new_val)
                        return 1
                    end
                    return 0
                    """,
                    1,
                    "parking:slot:state",
                    str(sid),
                    prev,
                    new,
                )

                if cas_result == 0:
                    continue

                current_state[sid] = new
                state_changes += 1

                # Update since timestamp
                r.hset("parking:slot:since", str(sid), ts_str)

                # Insert occupancy event
                payload = {
                    "slot_name": meta["name"],
                    "zone": meta["zone"],
                    "prev_state": prev,
                    "new_state": new,
                }
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO occupancy_events (slot_id, event_type, device_eui, ts, payload) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (sid, new, f"emu{sid:08x}", now, json.dumps(payload)),
                    )

                # Publish SSE event
                live_event = {
                    "event": "slot_state_changed",
                    "ts": ts_str,
                    "slot_id": sid,
                    "slot_name": meta["name"],
                    "zone": meta["zone"],
                    "prev_state": prev,
                    "new_state": new,
                }
                r.publish("parking:events:live", json.dumps(live_event))

                # Occasional challan
                if new == "OCCUPIED" and random.random() < CHALLAN_PROBABILITY:
                    ch = _build_challan_data(meta, sid, now, recheck_minutes)
                    slot_lat = meta.get("lat")
                    slot_lng = meta.get("lng")

                    # Generate dummy images (use now for overlay, not future second_time)
                    generate_dummy_image(
                        ch["first_img"], meta["name"], ch["plate"], now.isoformat(),
                        lat=slot_lat, lng=slot_lng, is_second=False,
                    )
                    generate_dummy_image(
                        ch["second_img"], meta["name"], ch["plate"], now.isoformat(),
                        lat=slot_lat, lng=slot_lng, is_second=True,
                        is_challan=(ch["status"] == "confirmed"),
                    )

                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO camera_captures (slot_id, camera_id, ts, image_path, ocr_result, backend) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            (sid, ch["camera_id"], now, ch["first_img"],
                             json.dumps({"plates": [ch["plate"]]}), "emulated"),
                        )
                        cur.execute(
                            "INSERT INTO challan_events "
                            "(challan_id, slot_id, license_plate, confidence, status, ts, metadata) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            (ch["challan_id"], sid, ch["plate"],
                             0.9 if ch["is_match"] else 0.0,
                             ch["status"], ch["challan_ts"],
                             json.dumps(ch["metadata"])),
                        )

                    r.publish("parking:events:live", json.dumps({
                        "event": "challan_completed",
                        "ts": ch["challan_ts"].isoformat(),
                        "plate_text": ch["plate"],
                        "slot_id": sid,
                        "slot_name": meta["name"],
                        "zone": meta["zone"],
                        "challan": ch["is_match"],
                        "capture_session_id": ch["session_id"],
                    }))
                    new_challans += 1

            conn.commit()

            occupied = sum(1 for v in current_state.values() if v == "OCCUPIED")
            pct = occupied / len(slots) * 100

            sys.stdout.write(
                f"\r  tick {tick:>5d} | "
                f"{state_changes:>3d} changes | "
                f"{new_challans:>2d} challans | "
                f"occupancy {occupied}/{len(slots)} ({pct:.1f}%)"
            )
            sys.stdout.flush()

            time.sleep(LIVE_TICK_SECONDS)

    except KeyboardInterrupt:
        print("\n\n  Simulation stopped.")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Parking emulator (production scale)")
    parser.add_argument("--slots", type=int, default=3000, help="Total parking slots (default: 3000)")
    parser.add_argument("--zones", type=int, default=7, help="Number of zones (default: 7, max: 7)")
    parser.add_argument("--seed-only", action="store_true", help="Seed data and exit (no live loop)")
    parser.add_argument("--live-only", action="store_true", help="Skip seeding, run live loop only")
    parser.add_argument("--history-hours", type=int, default=HISTORY_HOURS,
                        help=f"Hours of historical data to seed (default: {HISTORY_HOURS})")
    parser.add_argument("--recheck-minutes", type=int, default=CHALLAN_RECHECK_MINUTES,
                        help=f"Min minutes between 1st and 2nd capture (default: {CHALLAN_RECHECK_MINUTES})")
    args = parser.parse_args()

    recheck_minutes = args.recheck_minutes
    num_zones = min(args.zones, len(ZONE_LAYOUT))

    print(f"Parking Emulator — {args.slots} slots, {num_zones} zones")
    print("=" * 55)

    # Connect
    r = redis.Redis.from_url(REDIS_URL, decode_responses=False)
    try:
        r.ping()
        print(f"  Redis: connected ({REDIS_URL})")
    except redis.ConnectionError:
        print(f"  ERROR: Cannot connect to Redis at {REDIS_URL}")
        print("  Start Redis first: brew services start redis")
        sys.exit(1)

    try:
        conn = psycopg.connect(DATABASE_URL, autocommit=False)
        conn.execute("SELECT 1")
        print(f"  Postgres: connected ({DATABASE_URL})")
    except Exception as e:
        print(f"  ERROR: Cannot connect to Postgres: {e}")
        print("  Start Postgres and run: createdb parking && psql parking < db/schema.sql")
        sys.exit(1)

    if not args.live_only:
        # Phase 1: Generate slots
        print(f"\n[1/5] Generating {args.slots} slots across {num_zones} zones...")
        slots = generate_slot_meta(args.slots, num_zones)
        write_slot_meta(slots)

        # Phase 2: Seed Redis
        seed_redis(r, slots)

        # Phase 3: Seed Postgres
        seed_postgres(conn, slots, history_hours=args.history_hours,
                      recheck_minutes=recheck_minutes)

        print(f"\n[4/5] Seed complete.")
    else:
        # Load existing slot meta
        with open(SLOT_META_PATH) as f:
            slots = yaml.safe_load(f)
        print(f"\n  Loaded {len(slots)} slots from {SLOT_META_PATH}")

    if args.seed_only:
        print("\n  --seed-only: skipping live simulation.")
        print(f"  Start the server:  python3 -m uvicorn webapp.server:app --reload --port 8000")
        print(f"  Dashboard:         http://localhost:8000")
    else:
        run_live_simulation(r, conn, slots, recheck_minutes=recheck_minutes)

    conn.close()
    r.close()


if __name__ == "__main__":
    main()
