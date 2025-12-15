import cv2
import yaml
import numpy as np

# Global variables
drawing = False
current_points = []
all_polygons = []
video_path = 'data/easy1.mp4' 
config_path = 'config/parking_slots.yaml'

def draw_polygon(event, x, y, flags, param):
    global current_points, drawing
    
    if event == cv2.EVENT_LBUTTONDOWN:
        drawing = True
        current_points.append((x, y))
        
    elif event == cv2.EVENT_MOUSEMOVE:
        if drawing:
            pass # We just visualize lines in the main loop

def main():
    global current_points, all_polygons

    # 1. Capture the first frame
    cap = cv2.VideoCapture(video_path)
    success, frame = cap.read()
    if not success:
        print("Failed to read video. Check path.")
        return
    cap.release()

    cv2.namedWindow("Frame")
    cv2.setMouseCallback("Frame", draw_polygon)

    print("--- INSTRUCTIONS ---")
    print("1. Click 4 points to define a parking spot.")
    print("2. Press 's' to save the current polygon and start a new one.")
    print("3. Press 'q' to quit and generate the YAML file.")
    print("--------------------")

    while True:
        img_copy = frame.copy()

        # Draw already saved polygons (Green)
        for poly in all_polygons:
            pts = np.array(poly['points'], np.int32)
            cv2.polylines(img_copy, [pts], True, (0, 255, 0), 2)

        # Draw current drawing polygon (Red)
        if len(current_points) > 0:
            pts = np.array(current_points, np.int32)
            cv2.polylines(img_copy, [pts], False, (0, 0, 255), 2)
            # Draw points
            for pt in current_points:
                cv2.circle(img_copy, pt, 3, (0, 0, 255), -1)

        cv2.imshow("Frame", img_copy)
        key = cv2.waitKey(1) & 0xFF

        if key == ord('s'):
            if len(current_points) >= 3: # Allow triangles or more
                # Save - convert tuples to lists for YAML compatibility
                new_id = len(all_polygons) + 1
                points_as_lists = [list(pt) for pt in current_points]
                all_polygons.append({'id': new_id, 'points': points_as_lists})
                print(f"Saved Spot #{new_id}")
                current_points = [] # Reset
            else:
                print("Click at least 3 points before saving!")

        elif key == ord('q'):
            break

    # Save to YAML
    with open(config_path, 'w') as f:
        yaml.dump(all_polygons, f)
    
    print(f"Configuration saved to {config_path}")
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()