"""
License plate extraction module using YOLO detection + EasyOCR pipeline.

Pipeline (adapted from NumberPlateDetection project):
  1. Preprocess (CLAHE + denoise + unsharp mask)
  2. YOLO detection (plate or vehicle bounding boxes)
  3. Perspective correction (deskew skewed plates)
  4. EasyOCR (text extraction with character allowlist)
  5. Post-processing (zone-based character corrections + regex validation)
"""

import cv2
import easyocr
import itertools
import logging
import numpy as np
import os
import re
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# ── Configuration (override via environment variables) ────────────────────────
APP_ROOT = Path(__file__).resolve().parent
REPO_ROOT = APP_ROOT.parent

# Resolve YOLO model path: check repo-local weights/ first, then fall back to
# the NumberPlateDetection project weights, then to yolov8n.pt (auto-download).
_default_model = "yolov8n.pt"
_repo_weights = REPO_ROOT / "weights" / "license_plate_detector.pt"
_sibling_weights = Path(os.getenv("SIBLING_WEIGHTS_PATH", ""))
if _repo_weights.exists():
    _default_model = str(_repo_weights)
elif _sibling_weights.name and _sibling_weights.exists():
    _default_model = str(_sibling_weights)

YOLO_MODEL_PATH = os.getenv("YOLO_MODEL_PATH", _default_model)
YOLO_PLATE_CLASSES = ["license_plate"]
YOLO_CONF_THRESHOLD = float(os.getenv("YOLO_CONF_THRESHOLD", "0.35"))
OCR_LANGUAGES = ["en"]
OCR_USE_GPU = os.getenv("OCR_USE_GPU", "false").lower() == "true"
OCR_MIN_CHAR_CONFIDENCE = float(os.getenv("OCR_MIN_CHAR_CONFIDENCE", "0.3"))
CLAHE_CLIP_LIMIT = float(os.getenv("CLAHE_CLIP_LIMIT", "3.0"))
CLAHE_TILE_GRID_SIZE = (8, 8)
UNSHARP_KERNEL_SIZE = int(os.getenv("UNSHARP_KERNEL_SIZE", "5"))
UNSHARP_STRENGTH = float(os.getenv("UNSHARP_STRENGTH", "1.5"))
PERSPECTIVE_SKEW_THRESHOLD_DEG = float(os.getenv("PERSPECTIVE_SKEW_THRESHOLD_DEG", "5.0"))
PLATE_REGEX_PATTERN = os.getenv(
    "PLATE_REGEX_PATTERN",
    r"^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4}$",
)
_VEHICLE_FALLBACK_CLASSES = {"car", "truck", "bus", "motorcycle"}
_PLATE_ALLOWLIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

# ── Singletons ────────────────────────────────────────────────────────────────
_reader_instance: Optional[easyocr.Reader] = None
_detector_instance = None  # ultralytics.YOLO
_target_class_ids_cache: Optional[tuple] = None  # (class_ids, is_vehicle_fallback)
_clahe_instance = None  # cv2.CLAHE singleton

# ── OCR character confusion maps ──────────────────────────────────────────────
_LETTER_TO_DIGIT = {"O": "0", "I": "1", "S": "5", "B": "8", "G": "6", "Z": "2"}
_DIGIT_TO_LETTER = {v: k for k, v in _LETTER_TO_DIGIT.items()}


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Preprocessing
# ═══════════════════════════════════════════════════════════════════════════════

def _get_clahe():
    """Get or create the CLAHE singleton."""
    global _clahe_instance
    if _clahe_instance is None:
        _clahe_instance = cv2.createCLAHE(clipLimit=CLAHE_CLIP_LIMIT, tileGridSize=CLAHE_TILE_GRID_SIZE)
    return _clahe_instance


def _apply_clahe(gray: np.ndarray) -> np.ndarray:
    """CLAHE contrast enhancement for dark / low-light images."""
    return _get_clahe().apply(gray)


def _denoise(gray: np.ndarray) -> np.ndarray:
    """Non-local means denoising — preserves edges better than Gaussian blur."""
    return cv2.fastNlMeansDenoising(gray, h=10, templateWindowSize=7, searchWindowSize=21)


def _unsharp_mask(image: np.ndarray, kernel_size: int, strength: float) -> np.ndarray:
    """Sharpen edges lost to camera blur via unsharp masking."""
    blurred = cv2.GaussianBlur(image, (kernel_size, kernel_size), sigmaX=0)
    return cv2.addWeighted(image, 1 + strength, blurred, -strength, 0)


def _preprocess_image(colour_bgr: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """
    Full preprocessing pipeline: grayscale -> CLAHE -> denoise -> unsharp mask.

    Returns:
        (enhanced_colour, enhanced_gray)
    """
    gray = cv2.cvtColor(colour_bgr, cv2.COLOR_BGR2GRAY)
    gray = _apply_clahe(gray)
    gray = _denoise(gray)

    kernel = UNSHARP_KERNEL_SIZE
    if kernel % 2 == 0:
        kernel += 1
    gray = _unsharp_mask(gray, kernel_size=kernel, strength=UNSHARP_STRENGTH)
    enhanced_colour = _unsharp_mask(colour_bgr, kernel_size=kernel, strength=UNSHARP_STRENGTH)

    return enhanced_colour, gray


# ═══════════════════════════════════════════════════════════════════════════════
# 2. YOLO Detection
# ═══════════════════════════════════════════════════════════════════════════════

def _find_plate_in_crop(
    full_image: np.ndarray,
    veh_x1: int, veh_y1: int, veh_x2: int, veh_y2: int,
    lenient: bool = False,
) -> Optional[Tuple[int, int, int, int]]:
    """
    Locate a licence plate within a vehicle bounding box using morphological
    OpenCV analysis (Sobel edges -> threshold -> contour filtering by aspect ratio).

    Returns (x1, y1, x2, y2) in full-image coordinates, or None.
    """
    ih, iw = full_image.shape[:2]
    vx1, vy1 = max(0, veh_x1), max(0, veh_y1)
    vx2, vy2 = min(iw, veh_x2), min(ih, veh_y2)

    crop = full_image[vy1:vy2, vx1:vx2]
    if crop.size == 0:
        return None

    ch, cw = crop.shape[:2]

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    blur = cv2.bilateralFilter(gray, 9, 75, 75)

    sobelx = cv2.Sobel(blur, cv2.CV_64F, 1, 0, ksize=3)
    sobely = cv2.Sobel(blur, cv2.CV_64F, 0, 1, ksize=3)
    edges = cv2.magnitude(sobelx, sobely)
    edges = np.uint8(np.clip(edges, 0, 255))

    _, thresh = cv2.threshold(edges, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    dyn_kw = max(25, int(cw * 0.08)) if lenient else max(15, int(cw * 0.05))
    dyn_kh = max(5, int(ch * 0.03)) if lenient else max(3, int(ch * 0.02))
    kw, kh = min(dyn_kw, 80), min(dyn_kh, 20)

    morph_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kw, kh))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, morph_kernel)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best = None
    best_area = 0

    for cnt in contours:
        rx, ry, rw, rh = cv2.boundingRect(cnt)
        rect = cv2.minAreaRect(cnt)
        _, (rect_w, rect_h), _ = rect
        true_w, true_h = max(rect_w, rect_h), min(rect_w, rect_h)

        if true_h == 0:
            continue
        if true_w < cw * (0.05 if lenient else 0.10) or true_h < 10:
            continue

        real_aspect = true_w / true_h
        min_asp = 1.2 if lenient else 1.5
        max_asp = 10.0 if lenient else 8.0
        if not (min_asp <= real_aspect <= max_asp):
            continue

        max_h_frac = 0.80 if lenient else 0.35
        if true_h > ch * max_h_frac:
            continue

        area = rw * rh
        if area > best_area:
            best_area = area
            best = (rx, ry, rw, rh)

    if best is None:
        return None

    rx, ry, rw, rh = best
    pad_x, pad_y = int(rw * 0.05), int(rh * 0.10)
    px1 = max(0, rx - pad_x) + vx1
    py1 = max(0, ry - pad_y) + vy1
    px2 = min(iw, rx + rw + pad_x + vx1)
    py2 = min(ih, ry + rh + pad_y + vy1)

    return px1, py1, px2, py2


def _get_detector():
    """Get or create the YOLO detector singleton and cache class IDs."""
    global _detector_instance, _target_class_ids_cache
    if _detector_instance is None:
        from ultralytics import YOLO

        log.info("Loading YOLO model from: %s", YOLO_MODEL_PATH)
        _detector_instance = YOLO(YOLO_MODEL_PATH)
        log.info("YOLO model loaded. Classes: %s", list(_detector_instance.names.values()))
        # Cache class IDs once at model load time
        _target_class_ids_cache = _resolve_target_class_ids(_detector_instance)
    return _detector_instance


def _resolve_target_class_ids(model) -> Tuple[Optional[set], bool]:
    """
    Match configured plate classes against the loaded model's classes.
    Returns (class_ids, is_vehicle_fallback).
    """
    model_names_lower = {v.lower(): k for k, v in model.names.items()}
    target_names = {n.lower() for n in YOLO_PLATE_CLASSES}

    plate_ids = {model_names_lower[n] for n in target_names if n in model_names_lower}
    if plate_ids:
        return plate_ids, False

    vehicle_ids = {
        model_names_lower[n] for n in _VEHICLE_FALLBACK_CLASSES if n in model_names_lower
    }
    if vehicle_ids:
        log.info("No dedicated plate class in model. Using vehicle classes + OpenCV plate localisation.")
        return vehicle_ids, True

    log.warning("No matching classes found. Returning all detections.")
    return None, False


def _detect_plates(colour_bgr: np.ndarray) -> list:
    """
    Run YOLO inference to detect licence plates (or vehicles as fallback).

    Returns list of detection dicts: {x1, y1, x2, y2, confidence, class_name}.
    Returns empty list if nothing detected — this signals "no vehicle found".
    """
    model = _get_detector()
    plate_class_ids, is_vehicle_fallback = _target_class_ids_cache

    results = model(colour_bgr, conf=YOLO_CONF_THRESHOLD, verbose=False)
    boxes = []

    for result in results:
        for box in result.boxes:
            class_id = int(box.cls[0])
            conf = float(box.conf[0])

            if plate_class_ids is not None and class_id not in plate_class_ids:
                continue

            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            class_name = model.names.get(class_id, "unknown")

            if is_vehicle_fallback:
                plate_coords = _find_plate_in_crop(colour_bgr, x1, y1, x2, y2)
                if plate_coords and (plate_coords[2] - plate_coords[0]) >= 30 and (plate_coords[3] - plate_coords[1]) >= 25:
                    px1, py1, px2, py2 = plate_coords
                    boxes.append({
                        "x1": px1, "y1": py1, "x2": px2, "y2": py2,
                        "confidence": conf, "class_name": "license_plate(cv)",
                    })
                else:
                    # Fall back to lower-third of vehicle crop
                    h_third = (y2 - y1) // 3
                    fb_y1 = y1 + (2 * h_third)
                    boxes.append({
                        "x1": x1, "y1": fb_y1, "x2": x2, "y2": y2,
                        "confidence": conf * 0.5, "class_name": "vehicle_lower_third",
                    })
            else:
                boxes.append({
                    "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "confidence": conf, "class_name": class_name,
                })

    # Full-image OpenCV fallback when YOLO finds nothing
    if not boxes:
        ih, iw = colour_bgr.shape[:2]
        total_area = ih * iw
        plate_coords = _find_plate_in_crop(colour_bgr, 0, 0, iw, ih, lenient=True)
        if plate_coords and (plate_coords[2] - plate_coords[0]) * (plate_coords[3] - plate_coords[1]) >= total_area * 0.05:
            px1, py1, px2, py2 = plate_coords
            boxes.append({
                "x1": px1, "y1": py1, "x2": px2, "y2": py2,
                "confidence": 0.5, "class_name": "license_plate(cv_full)",
            })

    boxes.sort(key=lambda b: b["confidence"], reverse=True)
    return boxes


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Perspective Correction
# ═══════════════════════════════════════════════════════════════════════════════

def _order_points(pts: np.ndarray) -> np.ndarray:
    """Order 4 corner points: TL, TR, BR, BL."""
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect


def _four_point_transform(image: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Perspective warp to straighten a quadrilateral region."""
    (tl, tr, br, bl) = pts
    max_width = max(int(np.linalg.norm(tr - tl)), int(np.linalg.norm(br - bl)))
    max_height = max(int(np.linalg.norm(bl - tl)), int(np.linalg.norm(br - tr)))

    dst = np.array([
        [0, 0], [max_width - 1, 0],
        [max_width - 1, max_height - 1], [0, max_height - 1],
    ], dtype="float32")

    M = cv2.getPerspectiveTransform(pts, dst)
    return cv2.warpPerspective(image, M, (max_width, max_height))


def _correct_perspective(image: np.ndarray, bbox: dict) -> Tuple[np.ndarray, bool]:
    """
    Crop the plate from image using bbox, then deskew if needed.

    Returns:
        (crop, was_corrected)
    """
    h_img, w_img = image.shape[:2]
    x1, y1 = max(0, bbox["x1"]), max(0, bbox["y1"])
    x2, y2 = min(w_img, bbox["x2"]), min(h_img, bbox["y2"])

    crop = image[y1:y2, x1:x2]
    if crop.size == 0:
        return crop, False

    gray_crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop.copy()
    binary = cv2.adaptiveThreshold(
        gray_crop, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, blockSize=15, C=5,
    )

    h_crop, w_crop = crop.shape[:2]
    kw = min(max(5, int(w_crop * 0.03)), 30)
    kh = min(max(2, int(h_crop * 0.02)), 10)
    morph_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kw, kh))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, morph_kernel)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return crop, False

    largest = max(contours, key=cv2.contourArea)
    rect = cv2.minAreaRect(largest)
    angle = rect[2]
    if angle < -45:
        angle = 90 + angle

    if abs(angle) < PERSPECTIVE_SKEW_THRESHOLD_DEG:
        return crop, False

    box_pts = np.array(cv2.boxPoints(rect), dtype="float32")
    ordered = _order_points(box_pts)
    corrected = _four_point_transform(crop, ordered)
    return corrected, True


# ═══════════════════════════════════════════════════════════════════════════════
# 4. OCR
# ═══════════════════════════════════════════════════════════════════════════════

def _get_ocr_reader() -> easyocr.Reader:
    """Get or create the EasyOCR reader singleton."""
    global _reader_instance
    if _reader_instance is None:
        log.info("Initializing EasyOCR reader (languages: %s, GPU: %s)", OCR_LANGUAGES, OCR_USE_GPU)
        _reader_instance = easyocr.Reader(lang_list=OCR_LANGUAGES, gpu=OCR_USE_GPU)
        log.info("EasyOCR reader initialized")
    return _reader_instance


def _scale_up_if_small(image: np.ndarray, min_height: int = 64) -> np.ndarray:
    """Upscale tiny crops so EasyOCR's LSTM doesn't miss characters."""
    h, w = image.shape[:2]
    if h < min_height:
        scale = min_height / h
        new_w = max(1, int(w * scale))
        image = cv2.resize(image, (new_w, min_height), interpolation=cv2.INTER_CUBIC)
    return image


def _extract_text_from_crop(plate_crop: np.ndarray) -> Tuple[str, float]:
    """
    Run EasyOCR on a plate crop with character allowlist.
    Returns (text, avg_confidence).
    """
    reader = _get_ocr_reader()
    crop = _scale_up_if_small(plate_crop)

    raw_results = reader.readtext(
        crop, detail=1, paragraph=False,
        allowlist=_PLATE_ALLOWLIST, beamWidth=5, batch_size=1,
    )

    if not raw_results:
        return "", 0.0

    accepted = [
        (text, conf) for _, text, conf in raw_results
        if conf >= OCR_MIN_CHAR_CONFIDENCE
    ]

    if not accepted:
        # Fall back to best result regardless of confidence
        _, best_text, best_conf = max(raw_results, key=lambda r: r[2])
        return best_text.strip(), best_conf

    combined_text = " ".join(t for t, _ in accepted).strip()
    avg_conf = sum(c for _, c in accepted) / len(accepted)
    return combined_text, avg_conf


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Post-processing
# ═══════════════════════════════════════════════════════════════════════════════

def _normalise(raw: str) -> str:
    """Strip whitespace, remove non-alphanumeric, uppercase."""
    cleaned = raw.strip().upper().replace(" ", "").replace("-", "")
    return "".join(ch for ch in cleaned if ch.isalnum())


def _positional_correction(text: str) -> str:
    """
    Zone-based character correction for common OCR confusions.
    Groups consecutive chars by type (alpha/digit) and corrects look-alikes
    based on the dominant type in each group.
    """
    if not text:
        return text

    def char_type(c: str) -> str:
        if c.isalpha():
            return "alpha"
        if c.isdigit():
            return "digit"
        return "other"

    result = []
    for _, group in itertools.groupby(text, key=char_type):
        segment = list(group)
        alpha_cnt = sum(1 for c in segment if c.isalpha())
        digit_cnt = sum(1 for c in segment if c.isdigit())
        total = len(segment)

        if total == 0:
            continue

        if alpha_cnt / total >= 0.6:
            for c in segment:
                result.append(_DIGIT_TO_LETTER.get(c, c) if c.isdigit() else c)
        elif digit_cnt / total >= 0.6:
            for c in segment:
                result.append(_LETTER_TO_DIGIT.get(c, c) if c.isalpha() else c)
        else:
            result.extend(segment)

    return "".join(result)


def _postprocess_plate_text(raw_ocr_text: str) -> Tuple[str, Optional[bool]]:
    """
    Full post-processing: normalise -> character correction -> regex validation.
    Returns (cleaned_plate_text, matched_regex).
    """
    if not raw_ocr_text:
        return "", None

    normalised = _normalise(raw_ocr_text)
    corrected = _positional_correction(normalised)

    matched = None
    if PLATE_REGEX_PATTERN:
        if re.match(PLATE_REGEX_PATTERN, corrected):
            matched = True
        else:
            # Try to extract a valid plate as a substring
            search_pattern = PLATE_REGEX_PATTERN.lstrip("^").rstrip("$")
            found = re.search(search_pattern, corrected)
            if found:
                corrected = found.group(0)
                matched = True
            else:
                matched = False

    return corrected, matched


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def extract_license_plate(image_path) -> dict:
    """
    Extract license plate from an image file using the full YOLO + OCR pipeline.

    Args:
        image_path: Path to the image file.

    Returns:
        dict with keys:
            plate_text          - cleaned plate string or "UNKNOWN"
            vehicle_detected    - True if YOLO found a vehicle/plate in the image
            confidence          - OCR confidence [0, 1]
            detection_confidence - YOLO detection confidence [0, 1]
    """
    try:
        image = cv2.imread(str(image_path))
        if image is None:
            log.warning("Failed to load image: %s", image_path)
            return _empty_result()

        return _run_pipeline(image)

    except Exception as e:
        log.error("Error extracting license plate from %s: %s", image_path, e)
        return _empty_result()


def _empty_result() -> dict:
    return {
        "plate_text": "UNKNOWN",
        "vehicle_detected": False,
        "confidence": 0.0,
        "detection_confidence": 0.0,
    }


def _run_pipeline(image: np.ndarray) -> dict:
    """Execute the full detection + OCR pipeline on an image array."""
    result = _empty_result()

    # 1. Preprocess
    enhanced_colour, _enhanced_gray = _preprocess_image(image)

    # 2. YOLO Detection
    detections = _detect_plates(enhanced_colour)
    if not detections:
        log.debug("No vehicle or licence plate detected in image")
        return result

    result["vehicle_detected"] = True
    best = detections[0]
    result["detection_confidence"] = best["confidence"]

    # 3. Perspective Correction
    try:
        plate_crop, _was_corrected = _correct_perspective(enhanced_colour, best)
    except Exception:
        y1 = max(0, best["y1"])
        y2 = min(enhanced_colour.shape[0], best["y2"])
        x1 = max(0, best["x1"])
        x2 = min(enhanced_colour.shape[1], best["x2"])
        plate_crop = enhanced_colour[y1:y2, x1:x2]

    if plate_crop.size == 0:
        return result

    # 4. OCR
    raw_text, ocr_confidence = _extract_text_from_crop(plate_crop)
    if not raw_text:
        return result

    # 5. Post-process
    plate_text, _matched_regex = _postprocess_plate_text(raw_text)
    result["plate_text"] = plate_text if plate_text else "UNKNOWN"
    result["confidence"] = ocr_confidence

    log.info(
        "License plate detected: %s (OCR conf: %.2f, det conf: %.2f)",
        result['plate_text'], ocr_confidence, best['confidence']
    )
    return result
