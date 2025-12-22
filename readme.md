# 🅿️ Parking Occupancy Dashboard POC

A real-time dashboard solution for visualizing parking slot occupancy and analytics. This system consumes external occupancy data (simulated via file input) and provides a live web interface for monitoring and historical analysis.

## ✨ Features

- **Live Dashboard** — Real-time visualization of parking slot status (🔴 Occupied / 🟢 Free) using Server-Sent Events (SSE).
- **Analytics Module** — Insights into occupancy trends, average dwell times, and peak usage hours.
- **Data Simulation** — Simple integration via `data.txt` to simulate external sensor inputs.
- **Event Logging** — Tracks all state changes and periodic snapshots in `data/occupancy_events.jsonl`.
- **Configurable** — Slot names and zones defined in `config/slot_meta.yaml`.

---

## 📋 Requirements

| Component | Requirement |
|-----------|-------------|
| **Python** | 3.9+ |
| **OS** | Windows, Linux, or macOS |

### Installation

```bash
pip install -r requirements.txt
```

---

## 📁 Project Structure

```
parking_vision_poc/
├── config/
│   └── slot_meta.yaml        # Slot names + zone labels for dashboard
├── data/
│   └── occupancy_events.jsonl # (Generated) History of state changes
├── webapp/
│   ├── server.py             # FastAPI backend (SSE + Analytics)
│   └── static/               # Frontend (HTML/JS/CSS)
├── data.txt                  # Input file for simulating sensor data
└── readme.md
```

---

## 🚀 Quick Start

### Step 1: Start the Server

```bash
python -m uvicorn webapp.server:app --reload --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

### Step 2: Simulate Data

Edit `data.txt` to update the status of parking slots. The server polls this file every 10 seconds.

**Format:**
```json
[
    {"id": 4, "unique_id": "slot_1", "status": "{\"r\":1,\"y\":1,\"b\":1}"},
    {"id": 5, "unique_id": "slot_2", "status": "{\"r\":0,\"y\":0,\"b\":0}"}
]
```
- `r:1, y:1, b:1` = **Occupied**
- `r:0, y:0, b:0` = **Free**

The dashboard will update automatically when the file is saved and the server processes the change.

---

## 📊 Analytics

The dashboard includes an analytics view (`/analytics/summary` endpoint) providing:
- **Occupancy Trends**: Occupancy % over time (1h, 6h, 24h).
- **Dwell Time**: Average time vehicles spend in slots per zone.
- **Zone Stats**: Current usage per zone (e.g., Zone A, Zone B).

---

## ⚙️ Configuration

**`config/slot_meta.yaml`**
Map internal IDs to human-readable names and zones:

```yaml
1:
  name: "A-01"
  zone: "Zone A"
4:
  name: "B-01"
  zone: "Zone B"
```

---

## 🧾 Event Log

Changes are recorded in `data/occupancy_events.jsonl`:

```json
{"event": "slot_state_changed", "ts": "2025-12-22T10:00:00+00:00", "slot_id": 4, "new_state": "OCCUPIED", ...}
```