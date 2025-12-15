import cv2
import yaml
import numpy as np
from ultralytics import YOLO
from shapely.geometry import Polygon, box
from shapely.validation import make_valid
import time
import json
import datetime
from pathlib import Path

# --- CONFIGURATION ---
VIDEO_PATH = 'data/easy1.mp4'
CONFIG_PATH = 'config/parking_slots.yaml'
MODEL_NAME = 'yolov8n.pt'  # Nano model for speed
IOU_THRESHOLD = 0.4        # 40% overlap required to mark as occupied
CHECK_INTERVAL_SEC = 5.0

SLOT_META_PATH = 'config/slot_meta.yaml'

EVENT_LOG_PATH = 'data/occupancy_events.jsonl'
DEBOUNCE_COUNT = 2
LOG_SNAPSHOTS = True
SNAPSHOT_EVERY_CHECKS = 6


def _utc_iso_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _append_jsonl(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_slot_meta(path):
    p = Path(path)
    if not p.exists():
        return {}

    with open(p, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}

    meta_by_id = {}
    if isinstance(data, dict):
        for k, v in data.items():
            try:
                slot_id = int(k)
            except Exception:
                continue
            if isinstance(v, dict):
                meta_by_id[slot_id] = v
        return meta_by_id

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            if 'id' not in item:
                continue
            try:
                slot_id = int(item['id'])
            except Exception:
                continue
            meta_by_id[slot_id] = item
        return meta_by_id

    return {}

def load_parking_slots(path, slot_meta_by_id=None):
    slot_meta_by_id = slot_meta_by_id or {}
    with open(path, 'r') as f:
        data = yaml.safe_load(f)
    
    slots = []
    for entry in data:
        # Convert list of points to a Shapely Polygon
        poly = Polygon(entry['points'])
        # Fix invalid polygons (self-intersecting, etc.)
        if not poly.is_valid:
            poly = make_valid(poly)
        slot_id = entry['id']
        meta = slot_meta_by_id.get(slot_id, {})
        slots.append({
            'id': slot_id,
            'poly': poly,
            'points': np.array(entry['points'], np.int32),
            'name': meta.get('name', str(slot_id)),
            'zone': meta.get('zone', 'A'),
            'status': 'FREE',  # Debounced state used for drawing/logging
            'streak': 0,
            'last_overlap_ratio': 0.0,
        })
    return slots

def main():
    # 1. Load Resources
    print("Loading Model...")
    model = YOLO(MODEL_NAME)
    
    print(f"Loading Map from {CONFIG_PATH}...")
    slot_meta_by_id = load_slot_meta(SLOT_META_PATH)
    slots = load_parking_slots(CONFIG_PATH, slot_meta_by_id=slot_meta_by_id)
    
    cap = cv2.VideoCapture(VIDEO_PATH)
    if not cap.isOpened():
        print(f"Error: Could not open video {VIDEO_PATH}")
        return

    # 2. Main Loop
    cached_occupied_ids = set()
    check_index = 0
    last_check_t = 0.0
    while True:
        success, frame = cap.read()
        
        # Loop video forever for the POC
        if not success:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            continue

        now = time.monotonic()
        if (now - last_check_t) >= CHECK_INTERVAL_SEC:
            last_check_t = now
            check_index += 1

            # 3. Run YOLO Inference
            # classes=[2, 7] filters for Car(2) and Truck(7) only
            results = model(frame, classes=[2, 7], verbose=False) 

            # 4. Check Occupancy
            raw_occupied_ids = set()
            slot_overlaps = {slot['id']: 0.0 for slot in slots}

            # Get bounding boxes of all detected cars
            for box_data in results[0].boxes:
                # Extract coordinates (x1, y1, x2, y2)
                x1, y1, x2, y2 = box_data.xyxy[0].cpu().numpy()
                
                # Create a Shapely Box for the car
                car_poly = box(x1, y1, x2, y2)

                # Check this car against every parking slot
                for slot in slots:
                    # Calculate Intersection over Union (or just Intersection Area)
                    intersection_area = slot['poly'].intersection(car_poly).area
                    slot_area = slot['poly'].area
                    
                    if slot_area > 0:
                        overlap_ratio = intersection_area / slot_area

                        if overlap_ratio > slot_overlaps[slot['id']]:
                            slot_overlaps[slot['id']] = overlap_ratio
                        
                        if overlap_ratio > IOU_THRESHOLD:
                            raw_occupied_ids.add(slot['id'])

            # Debounce raw occupancy into stable per-slot status
            for slot in slots:
                is_raw_occupied = slot['id'] in raw_occupied_ids
                slot['last_overlap_ratio'] = slot_overlaps.get(slot['id'], 0.0)

                if is_raw_occupied:
                    slot['streak'] = slot['streak'] + 1 if slot['streak'] > 0 else 1
                else:
                    slot['streak'] = slot['streak'] - 1 if slot['streak'] < 0 else -1

                prev_status = slot['status']
                if slot['streak'] >= DEBOUNCE_COUNT:
                    slot['status'] = 'OCCUPIED'
                    slot['streak'] = DEBOUNCE_COUNT
                elif slot['streak'] <= -DEBOUNCE_COUNT:
                    slot['status'] = 'FREE'
                    slot['streak'] = -DEBOUNCE_COUNT

                if slot['status'] != prev_status:
                    _append_jsonl(EVENT_LOG_PATH, {
                        'ts': _utc_iso_now(),
                        'event': 'slot_state_changed',
                        'slot_id': slot['id'],
                        'slot_name': slot.get('name', str(slot['id'])),
                        'zone': slot.get('zone', 'A'),
                        'prev_state': prev_status,
                        'new_state': slot['status'],
                        'overlap_ratio': round(float(slot['last_overlap_ratio']), 4),
                        'iou_threshold': IOU_THRESHOLD,
                        'debounce_count': DEBOUNCE_COUNT,
                        'source': VIDEO_PATH,
                        'model': MODEL_NAME,
                    })

            cached_occupied_ids = {slot['id'] for slot in slots if slot['status'] == 'OCCUPIED'}

            if LOG_SNAPSHOTS and (SNAPSHOT_EVERY_CHECKS > 0) and (check_index % SNAPSHOT_EVERY_CHECKS == 0):
                zone_stats = {}
                for slot in slots:
                    zone = slot.get('zone', 'A')
                    if zone not in zone_stats:
                        zone_stats[zone] = {'total': 0, 'free': 0, 'occupied': 0}
                    zone_stats[zone]['total'] += 1
                    if slot['status'] == 'OCCUPIED':
                        zone_stats[zone]['occupied'] += 1
                    else:
                        zone_stats[zone]['free'] += 1

                _append_jsonl(EVENT_LOG_PATH, {
                    'ts': _utc_iso_now(),
                    'event': 'snapshot',
                    'occupied_ids': sorted(cached_occupied_ids),
                    'free_count': sum(1 for s in slots if s['status'] == 'FREE'),
                    'total_count': len(slots),
                    'zones': zone_stats,
                    'source': VIDEO_PATH,
                    'model': MODEL_NAME,
                })

        # use cached debounced result for drawing/status every frame
        current_occupied_ids = cached_occupied_ids

        # 5. Update Slot Status & Draw
        free_spots = 0
        total_spots = len(slots)

        for slot in slots:
            is_occupied = slot['id'] in current_occupied_ids
            
            # Update Logic
            if is_occupied:
                slot['status'] = 'OCCUPIED'
                color = (0, 0, 255) # Red
                thickness = 2
            else:
                slot['status'] = 'FREE'
                free_spots += 1
                color = (0, 255, 0) # Green
                thickness = 2

            # Draw Polygon
            cv2.polylines(frame, [slot['points']], isClosed=True, color=color, thickness=thickness)
            
            # Draw Semi-transparent fill (Optional visual polish)
            overlay = frame.copy()
            cv2.fillPoly(overlay, [slot['points']], color)
            alpha = 0.3
            cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)

            # Draw Text Label (ID)
            # Find center of polygon for text placement
            M = cv2.moments(slot['points'])
            if M['m00'] != 0:
                cx = int(M['m10'] / M['m00'])
                cy = int(M['m01'] / M['m00'])
                cv2.putText(frame, str(slot['id']), (cx - 5, cy + 5), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

        # 6. Draw Dashboard
        cv2.rectangle(frame, (0, 0), (300, 50), (0, 0, 0), -1)
        cv2.putText(frame, f"Free: {free_spots}/{total_spots}", (10, 35), 
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)

        # 7. Display
        cv2.imshow("Parking POC", frame)

        # Press 'q' to quit
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()