
# 🚗 Vision-Based Parking Occupancy POC

A real-time computer vision solution for detecting parking slot occupancy using **YOLOv8** object detection and **Shapely** geometric logic.

![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)
![OpenCV](https://img.shields.io/badge/OpenCV-4.x-green.svg)
![YOLOv8](https://img.shields.io/badge/YOLOv8-Ultralytics-purple.svg)

## ✨ Features

- **Custom Zone Mapping** — Draw parking slots on any camera feed
- **Real-time Detection** — YOLOv8 Nano for fast CPU inference
- **Smart Occupancy Logic** — IoU-based detection distinguishes parked vs. passing vehicles
- **Visual Feedback** — Color-coded overlays (🔴 Occupied / 🟢 Available)
- **Event Logging (POC)** — Debounced occupancy change events written to `data/occupancy_events.jsonl`
- **Centralized Dashboard (POC)** — Live web UI reads the log and shows slot + zone status

---

## 📋 Requirements

| Component | Requirement |
|-----------|-------------|
| **Python** | 3.9+ |
| **CPU** | Modern multi-core (Intel i5/i7 or Apple Silicon) |
| **GPU** | Optional (NVIDIA CUDA for faster FPS) |
| **OS** | Windows, Linux, or macOS |

### Installation

```bash
pip install -r requirements.txt
```

---

## 📁 Project Structure

```
parking_vision_poc/
├── data/
│   └── parking_lot_sample.mp4    # Your source video
│   └── occupancy_events.jsonl     # (Generated) occupancy change + snapshot events
├── config/
│   └── parking_slots.yaml        # Auto-generated zone config
│   └── slot_meta.yaml            # Slot names + zone labels for dashboard
├── src/
│   ├── setup_zones.py            # Zone drawing tool
│   └── main_detection.py         # Detection engine
├── webapp/
│   ├── server.py                 # FastAPI dashboard (SSE)
│   └── static/                   # Single-page UI
└── readme.md
```

---

## 🚀 Quick Start

### Step 1: Configure Parking Zones

Define where parking spots are located in your video feed:

```bash
python src/setup_zones.py
```

**Controls:**
| Action | Description |
|--------|-------------|
| `Left Click` | Place 4 points to outline a parking spot |
| `S` | Save the current polygon (turns green) |
| `Q` | Quit and save to `config/parking_slots.yaml` |

### Step 2: Run Detection

```bash
python src/main_detection.py
```

Press `Q` to stop the program.

---

## 🖥️ Live Dashboard (POC)

The vision loop appends debounced occupancy events to `data/occupancy_events.jsonl`.
The dashboard tails that file and updates the UI live.

### Terminal 1: Run the vision loop

```bash
python src/main_detection.py
```

### Terminal 2: Run the dashboard

```bash
python -m uvicorn webapp.server:app --reload --port 8000
```

Open `http://127.0.0.1:8000`.

---

## 🧾 Event Log Format

Events are written as JSON Lines (one JSON object per line).

### `slot_state_changed`

- Emitted when a slot changes debounced state
- Includes `overlap_ratio` as informational

Example:

```json
{"ts":"2025-12-15T18:41:12.402Z","event":"slot_state_changed","slot_id":7,"slot_name":"X07","zone":"B","prev_state":"FREE","new_state":"OCCUPIED","overlap_ratio":0.53}
```

### `snapshot` (optional)

- Emitted periodically to allow fast dashboard resync

Example:

```json
{"ts":"2025-12-15T18:41:42.402Z","event":"snapshot","occupied_ids":[1,3,7],"free_count":8,"total_count":11,"zones":{"B":{"total":3,"free":2,"occupied":1}}}
```

---

## 🧩 Slot Names + Zones

Edit `config/slot_meta.yaml` to name slots (e.g., `X12`) and assign zones (e.g., `A`, `B`).
The vision code and dashboard will default to `name=str(id)` and `zone='A'` if metadata is missing.

---

## 🔁 Debounce Semantics (POC)

Debounce is applied on the **periodic checks** (every `CHECK_INTERVAL_SEC`), not per-frame.

- `DEBOUNCE_COUNT = 2` means a slot must be detected occupied/free for **2 consecutive checks** to change state.
- Tunables live in `src/main_detection.py`:
   - `CHECK_INTERVAL_SEC`
   - `IOU_THRESHOLD`
   - `DEBOUNCE_COUNT`
   - `LOG_SNAPSHOTS` / `SNAPSHOT_EVERY_CHECKS`

---

## 🔧 How It Works

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│  Frame Extract  │───▶│  YOLO Inference │───▶│  IoU Calculation│
│  (Video Loop)   │    │  (Cars/Trucks)  │    │  (Overlap %)    │
└─────────────────┘    └─────────────────┘    └─────────────────┘
                                                      │
                       ┌─────────────────┐            ▼
                       │  Visual Output  │◀───────────────────────
                       │  (Color Overlay)│    Threshold: 40%
                       └─────────────────┘    Debounce: 3 frames
```

**Detection Pipeline:**

1. **Frame Extraction** — Reads video frames in a loop
2. **YOLO Inference** — Detects COCO class IDs `2` (car) and `7` (truck)
3. **IoU Calculation** — Compares bounding boxes against parking polygons
   - Formula: `Overlap Area / Slot Area`
4. **Thresholding** — Slot marked occupied if overlap > 40%
5. **Debouncing** — Status must hold for 3 frames to prevent flickering

---

## ❓ Troubleshooting

| Issue | Solution |
|-------|----------|
| `Failed to read video` | Verify `data/parking_lot_sample.mp4` exists and path is correct |
| Slow/laggy video | Implement frame skipping in `main_detection.py` (process every 3rd-5th frame) |
| Cars detected but spots stay green | Lower IoU threshold to `0.3` if polygons are larger than vehicles |

---

## 📄 License

This POC is for educational and testing purposes.