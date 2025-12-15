from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

APP_ROOT = Path(__file__).resolve().parent
REPO_ROOT = APP_ROOT.parent

PARKING_SLOTS_PATH = REPO_ROOT / "config" / "parking_slots.yaml"
SLOT_META_PATH = REPO_ROOT / "config" / "slot_meta.yaml"
EVENT_LOG_PATH = REPO_ROOT / "data" / "occupancy_events.jsonl"

app = FastAPI(title="Parking Vision Dashboard")
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")


def _load_yaml(path: Path):
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_slot_ids() -> List[int]:
    data = _load_yaml(PARKING_SLOTS_PATH) or []
    slot_ids: List[int] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "id" in item:
                try:
                    slot_ids.append(int(item["id"]))
                except Exception:
                    pass
    return sorted(set(slot_ids))


def load_slot_meta_by_id() -> Dict[int, dict]:
    data = _load_yaml(SLOT_META_PATH)
    if not data:
        return {}

    meta: Dict[int, dict] = {}

    if isinstance(data, dict):
        for k, v in data.items():
            try:
                slot_id = int(k)
            except Exception:
                continue
            if isinstance(v, dict):
                meta[slot_id] = v
        return meta

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict) or "id" not in item:
                continue
            try:
                slot_id = int(item["id"])
            except Exception:
                continue
            meta[slot_id] = item

    return meta


def describe_slot(slot_id: int, meta_by_id: Dict[int, dict]) -> dict:
    meta = meta_by_id.get(slot_id, {})
    name = meta.get("name") or str(slot_id)
    zone = meta.get("zone") or "A"
    return {"id": slot_id, "name": name, "zone": zone}


def build_state_from_log(
    slot_ids: List[int],
    meta_by_id: Dict[int, dict],
    max_events: int = 200,
) -> dict:
    state_by_id: Dict[int, str] = {slot_id: "FREE" for slot_id in slot_ids}
    last_events: List[dict] = []

    if EVENT_LOG_PATH.exists():
        with open(EVENT_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                event_type = obj.get("event")
                if event_type == "snapshot":
                    occupied_ids = set(obj.get("occupied_ids") or [])
                    for slot_id in slot_ids:
                        state_by_id[slot_id] = "OCCUPIED" if slot_id in occupied_ids else "FREE"
                elif event_type == "slot_state_changed":
                    try:
                        slot_id = int(obj.get("slot_id"))
                    except Exception:
                        continue
                    if slot_id in state_by_id and obj.get("new_state") in ("FREE", "OCCUPIED"):
                        state_by_id[slot_id] = obj["new_state"]

                if event_type in ("slot_state_changed",):
                    last_events.append(obj)

    if len(last_events) > max_events:
        last_events = last_events[-max_events:]

    slots = [describe_slot(slot_id, meta_by_id) for slot_id in slot_ids]

    zones: Dict[str, dict] = {}
    for slot in slots:
        zone = slot["zone"]
        if zone not in zones:
            zones[zone] = {"total": 0, "free": 0, "occupied": 0}
        zones[zone]["total"] += 1
        if state_by_id[slot["id"]] == "OCCUPIED":
            zones[zone]["occupied"] += 1
        else:
            zones[zone]["free"] += 1

    free_count = sum(1 for s in state_by_id.values() if s == "FREE")

    return {
        "slots": slots,
        "state_by_id": state_by_id,
        "zones": zones,
        "free_count": free_count,
        "total_count": len(slot_ids),
        "recent_events": last_events,
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(str(APP_ROOT / "static" / "index.html"))


@app.get("/state")
def state():
    slot_ids = load_slot_ids()
    meta_by_id = load_slot_meta_by_id()
    return build_state_from_log(slot_ids=slot_ids, meta_by_id=meta_by_id)


@app.get("/events")
def events():
    def gen():
        EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVENT_LOG_PATH.touch(exist_ok=True)

        with open(EVENT_LOG_PATH, "r", encoding="utf-8") as f:
            f.seek(0, 2)  # tail from end
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.25)
                    continue
                line = line.strip()
                if not line:
                    continue
                yield f"data: {line}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")

