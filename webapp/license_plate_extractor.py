"""
License plate extraction module with selectable OpenAI and EasyOCR backends.
"""

import base64
import cv2
import json
import logging
import numpy as np
import os
import re
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# ── Configuration (override via environment variables) ────────────────────────
PLATE_REGEX_PATTERN = os.getenv(
    "PLATE_REGEX_PATTERN",
    r"^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4}$",
)

# Common Indian state codes for validation
_INDIAN_STATE_CODES = {
    "AN", "AP", "AR", "AS", "BR", "CG", "CH", "DD", "DL", "GA",
    "GJ", "HP", "HR", "JH", "JK", "KA", "KL", "LA", "LD", "MH",
    "ML", "MN", "MP", "MZ", "NL", "OD", "PB", "PY", "RJ", "SK",
    "TN", "TR", "TS", "UK", "UP", "WB", "BH",  # BH = Bharat series
}

# ── OpenAI configuration ─────────────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_LPR_MODEL", "gpt-5.2")
OPENAI_MAX_COMPLETION_TOKENS = int(os.getenv("OPENAI_LPR_MAX_TOKENS", "300"))
LPR_BACKEND = os.getenv("LPR_BACKEND", "auto").strip().lower()
LPR_EASYOCR_LANGS = [
    lang.strip().lower()
    for lang in os.getenv("LPR_EASYOCR_LANGS", "en").split(",")
    if lang.strip()
]
LPR_EASYOCR_DOWNLOAD = os.getenv("LPR_EASYOCR_DOWNLOAD", "0").strip().lower() in {"1", "true", "yes"}
LPR_PREPROCESS = os.getenv("LPR_PREPROCESS", "1").strip().lower() not in {"0", "false", "no"}
LPR_EASYOCR_GPU = os.getenv("LPR_EASYOCR_GPU", "auto").strip().lower()

# Downscale large images before OCR to save time & memory.
# Frames wider than this are resized (aspect-ratio preserved).
LPR_MAX_OCR_WIDTH = int(os.getenv("LPR_MAX_OCR_WIDTH", "1024"))

# Minimum confidence to keep a plate (high=1.0, medium=0.7, low=0.3)
PLATE_MIN_CONFIDENCE = float(os.getenv("PLATE_MIN_CONFIDENCE", "0.65"))
_CONFIDENCE_SCORES: dict = {"high": 1.0, "medium": 0.7, "low": 0.3}

# ── Singleton ─────────────────────────────────────────────────────────────────
_openai_client = None  # openai.OpenAI
_easyocr_reader = None
_active_backend_logged = False

# ── System prompt for OpenAI vision ──────────────────────────────────────────
_SYSTEM_PROMPT = (
    "### ROLE\n"
    "You are an expert Indian vehicle license plate recognition system analyzing parking lot camera feeds.\n\n"
    "### CORE TASKS\n"
    "1. Detect if any vehicle (car, truck, bus, auto-rickshaw) is visible.\n"
    "2. Extract clearly readable license plate numbers.\n"
    "3. Ignore non-plate text like phone numbers, fleet numbers, vehicle model names, or background signage.\n\n"
    "### REJECTION CRITERIA (WHEN TO SKIP A PLATE)\n"
    "Do NOT guess. Return NO plate if it meets ANY of these conditions:\n"
    "- Too small, distant, blurry, or washed out by glare.\n"
    "- At an extreme angle making characters ambiguous.\n"
    "- Partially obscured, dirty, or faded, making characters illegible.\n"
    "- CROPPED or CUT OFF at the edge of the image boundary.\n"
    "- Fewer than half the characters are confidently legible.\n\n"
    "### INDIAN LICENSE PLATE RULES\n"
    "1. Standard Format: SS DD XX NNNN\n"
    "   - SS: State code (2 letters, e.g., MH, DL, KA, TS, AP, UP).\n"
    "   - DD: District/RTO code (ALWAYS 2 digits, e.g., '01', '09'). MUST include leading zeros.\n"
    "   - XX: Series (1-3 letters).\n"
    "   - NNNN: Number (ALWAYS 4 digits, e.g., '0045', '0500'). MUST include leading zeros.\n"
    "2. Bharat Series: BH DD YYYY XXNNNN (e.g., BH02AA1234).\n"
    "3. Multi-line Plates: Plates on motorcycles and commercial vehicles are often split across "
    "two lines. Read the top line first, then the bottom line, and combine into a single string.\n"
    "4. Formatting: UPPERCASE only. Remove all spaces, dashes, dots, or bullets (•). "
    "Do not hallucinate characters.\n\n"
    "### OCR ERROR CORRECTION\n"
    "- Watch for common visual confusions: 0↔O, 1↔I, 8↔B, 5↔S, 2↔Z.\n"
    "- Use the strict SS-DD-XX-NNNN structure to resolve ambiguities "
    "(e.g., if the state code looks like 'M8', it is 'MH').\n\n"
    "### CONFIDENCE SCORING\n"
    "- 'high': Large, perfectly clear, directly facing the camera.\n"
    "- 'medium': Readable, but smaller, slightly angled, or in dim lighting.\n"
    "- 'low': Barely legible (usually omit these).\n\n"
    "Respond with ONLY valid JSON (no markdown, no code fences):\n"
    '{"vehicle_detected": true/false, "plates": [{"plate_text": "KA01MR0045", "confidence": "high"}]}\n'
    "If no vehicle is visible or no plates are clearly readable, return:\n"
    '{"vehicle_detected": false, "plates": []}'
)


# ═══════════════════════════════════════════════════════════════════════════════
# OpenAI client & image helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _get_openai_client():
    """Get or create the OpenAI client singleton."""
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        api_key = OPENAI_API_KEY
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY environment variable is not set. "
                "Set it to use OpenAI vision for license plate reading."
            )
        _openai_client = OpenAI(
            api_key=api_key,
            timeout=60.0,       # 60 s per-request timeout (image uploads)
            max_retries=2,      # automatic retry on transient failures
        )
        log.info("OpenAI client initialised (model: %s)", OPENAI_MODEL)
    return _openai_client


def _resolve_backend() -> str:
    backend = LPR_BACKEND
    if backend not in {"auto", "openai", "easyocr"}:
        log.warning("Invalid LPR_BACKEND=%s; falling back to auto", backend)
        backend = "auto"
    if backend == "auto":
        return "openai" if OPENAI_API_KEY else "easyocr"
    return backend


def _detect_gpu() -> bool:
    """Return True if CUDA is available and user hasn't explicitly disabled GPU."""
    if LPR_EASYOCR_GPU in {"0", "false", "no", "off", "cpu"}:
        return False
    if LPR_EASYOCR_GPU in {"1", "true", "yes", "on"}:
        return True
    # auto-detect
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False


def _get_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr

        langs = LPR_EASYOCR_LANGS or ["en"]
        use_gpu = _detect_gpu()
        _easyocr_reader = easyocr.Reader(
            langs,
            gpu=use_gpu,
            verbose=False,
            download_enabled=LPR_EASYOCR_DOWNLOAD,
        )
        log.info(
            "EasyOCR initialised (langs: %s, gpu: %s, download_enabled: %s)",
            ",".join(langs),
            use_gpu,
            LPR_EASYOCR_DOWNLOAD,
        )
    return _easyocr_reader


def warm_up():
    """Pre-load the active OCR backend so the first real inference job is not slow.

    Call this once at server startup (e.g. from the inference worker thread before
    entering the main loop).  For EasyOCR this triggers PyTorch model loading from
    disk (~5–10 s); subsequent calls reuse the in-memory singleton.
    """
    backend = _resolve_backend()
    if backend == "easyocr":
        log.info("Pre-warming EasyOCR reader (this may take a few seconds)…")
        _get_easyocr_reader()
        log.info("EasyOCR reader ready")


def _encode_image_to_base64(image: np.ndarray) -> str:
    """Encode an OpenCV image (BGR numpy array) to a base64 JPEG string."""
    success, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not success:
        raise ValueError("Failed to encode image to JPEG")
    return base64.b64encode(buffer).decode("utf-8")


def _downscale(image: np.ndarray, max_width: int = LPR_MAX_OCR_WIDTH) -> np.ndarray:
    """Resize image so its width <= *max_width* (aspect-ratio preserved)."""
    h, w = image.shape[:2]
    if w <= max_width:
        return image
    scale = max_width / w
    return cv2.resize(image, (max_width, int(h * scale)), interpolation=cv2.INTER_AREA)


def _preprocess_for_ocr(image: np.ndarray) -> list:
    """Return at most 2 variants (original + CLAHE-enhanced) to limit OCR passes."""
    # Downscale first to speed up both preprocessing and OCR inference.
    image = _downscale(image)
    variants = [image]
    if not LPR_PREPROCESS:
        return variants

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # CLAHE + light denoise (d=5 is much faster than d=9, still effective)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    denoised = cv2.bilateralFilter(enhanced, d=5, sigmaColor=50, sigmaSpace=50)
    variants.append(cv2.cvtColor(denoised, cv2.COLOR_GRAY2BGR))

    # Dropped adaptive-threshold variant — it rarely helps and adds a full
    # extra OCR pass (~30-40 s on CPU).  The CLAHE variant already handles
    # poor-contrast plates well.

    return variants


def _resize_for_ocr(image: np.ndarray) -> np.ndarray:
    """Downscale image so its longest edge ≤ _OCR_MAX_DIM (aspect ratio preserved).

    EasyOCR's CRAFT detector is O(W×H); skipping this on a 1080p frame means
    ~45 s per readtext() call on CPU.  At 1280 px the same call takes ~10–15 s.
    """
    h, w = image.shape[:2]
    max_dim = max(h, w)
    if max_dim <= _OCR_MAX_DIM:
        return image
    scale = _OCR_MAX_DIM / max_dim
    new_w, new_h = int(w * scale), int(h * scale)
    return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)


# ═══════════════════════════════════════════════════════════════════════════════
# Core: single OpenAI vision call (Structured Outputs)
# ═══════════════════════════════════════════════════════════════════════════════

# Strict JSON schema — guarantees the model returns exactly this structure.
_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "license_plate_extraction",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "vehicle_detected": {"type": "boolean"},
                "plates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "plate_text": {"type": "string"},
                            "confidence": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                        },
                        "required": ["plate_text", "confidence"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["vehicle_detected", "plates"],
            "additionalProperties": False,
        },
    },
}


def _call_openai_vision(image: np.ndarray) -> dict:
    """
    Send the full scene image to OpenAI vision.

    Returns parsed dict: {"vehicle_detected": bool, "plates": [str, ...]}
    Falls back to {"vehicle_detected": False, "plates": []} on any error.
    """
    b64_image = _encode_image_to_base64(image)

    try:
        client = _get_openai_client()
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            max_completion_tokens=OPENAI_MAX_COMPLETION_TOKENS,
            response_format=_RESPONSE_SCHEMA,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64_image}",
                                "detail": "high",
                            },
                        },
                        {
                            "type": "text",
                            "text": "Detect vehicles and read all license plates in this image.",
                        },
                    ],
                },
            ],
        )

        raw = response.choices[0].message.content.strip()
        parsed = json.loads(raw)

        vehicle = bool(parsed.get("vehicle_detected", False))
        plates_raw = parsed.get("plates", [])
        if not isinstance(plates_raw, list):
            plates_raw = []

        # Map confidence labels to numeric scores and filter by threshold
        plates_out: list = []
        for entry in plates_raw:
            text = entry.get("plate_text", "")
            conf_label = entry.get("confidence", "medium")
            conf_score = _CONFIDENCE_SCORES.get(conf_label, _CONFIDENCE_SCORES["low"])
            if conf_score >= PLATE_MIN_CONFIDENCE:
                plates_out.append({"plate_text": text, "confidence": conf_score})
            else:
                log.info("Dropping low-confidence plate: %s (%.1f < %.1f)",
                         text, conf_score, PLATE_MIN_CONFIDENCE)

        return {
            "vehicle_detected": vehicle,
            "plates": [p["plate_text"] for p in plates_out],
            "plates_detail": plates_out,
        }

    except json.JSONDecodeError as e:
        log.error("Failed to parse LLM JSON response: %s", e)
        return {"vehicle_detected": False, "plates": [], "plates_detail": []}
    except Exception as e:
        log.error("OpenAI vision API call failed: %s", e)
        return {"vehicle_detected": False, "plates": [], "plates_detail": []}


def _call_local_ocr(image: np.ndarray) -> dict:
    try:
        reader = _get_easyocr_reader()
        image = _resize_for_ocr(image)
        variants = _preprocess_for_ocr(image)
        detected: dict = {}
        saw_text = False

        _allowlist = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        for variant in variants:
            output = reader.readtext(
                variant,
                detail=1,
                paragraph=False,
                decoder="greedy",         # ~2× faster than beamsearch
                allowlist=_allowlist,      # skip non-plate characters
                batch_size=8,             # batch text-recognition crops
            )
            if not output:
                continue

            for line in output:
                if not isinstance(line, (list, tuple)) or len(line) < 3:
                    continue

                raw_text = str(line[1]).strip()
                if not raw_text:
                    continue
                saw_text = True

                try:
                    confidence = float(line[2])
                except Exception:
                    confidence = 0.0

                normalised = _normalise(raw_text)
                if not normalised or not normalised.isalnum() or len(normalised) < 5 or len(normalised) > 15:
                    continue

                adjusted_confidence = confidence * 0.85
                state_prefix_ok = len(normalised) >= 2 and normalised[:2] in _INDIAN_STATE_CODES
                regex_ok = bool(PLATE_REGEX_PATTERN and re.match(PLATE_REGEX_PATTERN, normalised))
                if not state_prefix_ok and not regex_ok:
                    adjusted_confidence *= 0.5

                if adjusted_confidence < PLATE_MIN_CONFIDENCE:
                    continue

                existing = detected.get(normalised)
                if (existing is None) or (adjusted_confidence > existing["confidence"]):
                    detected[normalised] = {
                        "plate_text": normalised,
                        "confidence": adjusted_confidence,
                    }

        plates_detail = sorted(detected.values(), key=lambda item: item["confidence"], reverse=True)
        return {
            "vehicle_detected": bool(plates_detail) or saw_text,
            "plates": [item["plate_text"] for item in plates_detail],
            "plates_detail": plates_detail,
        }
    except Exception as e:
        log.error("EasyOCR call failed: %s", e)
        return {"vehicle_detected": False, "plates": [], "plates_detail": []}


def _extract_from_image(image: np.ndarray) -> dict:
    global _active_backend_logged
    backend = _resolve_backend()
    if not _active_backend_logged:
        log.info("License plate backend selected: %s", backend)
        _active_backend_logged = True
    if backend == "openai":
        return _call_openai_vision(image)
    return _call_local_ocr(image)


# ═══════════════════════════════════════════════════════════════════════════════
# Post-processing
# ═══════════════════════════════════════════════════════════════════════════════

def _normalise(raw: str) -> str:
    """Strip whitespace, remove non-alphanumeric, uppercase."""
    cleaned = raw.strip().upper().replace(" ", "").replace("-", "")
    return "".join(ch for ch in cleaned if ch.isalnum())


# Maps for fixing OCR-confusable characters in the wrong position type.
# Digits that should be letters (e.g. '2' in a letter slot is probably 'Z').
_DIGIT_TO_LETTER = str.maketrans("02581", "OZSBI")
# Letters that should be digits (e.g. 'O' in a digit slot is probably '0').
_LETTER_TO_DIGIT = str.maketrans("OZSBI", "02581")


def _fix_confusables(plate: str) -> Optional[str]:
    """Try to fix OCR-confusable characters based on expected Indian plate structure.

    Indian plates follow:  SS  DD  XX  NNNN
      SS = 2 letters (state), DD = 1-2 digits (district),
      XX = 1-3 letters (series), NNNN = 1-4 digits (number).

    When the model misreads e.g. 'Z' as '2', the character lands in the wrong
    group and the regex fails.  This function tries every valid group-size
    split, translates confusable chars into the expected type, and returns
    the first candidate that satisfies the regex.
    """
    for d_len in (2, 1):          # district: prefer 2-digit
        for s_len in (2, 1, 3):   # series: prefer 2-letter, then 1, then 3
            prefix_len = 2 + d_len + s_len
            num_len = len(plate) - prefix_len
            if num_len < 1 or num_len > 4:
                continue

            state    = plate[:2].translate(_DIGIT_TO_LETTER)
            district = plate[2:2 + d_len].translate(_LETTER_TO_DIGIT)
            series   = plate[2 + d_len:prefix_len].translate(_DIGIT_TO_LETTER)
            number   = plate[prefix_len:].translate(_LETTER_TO_DIGIT)

            candidate = state + district + series + number
            if re.match(PLATE_REGEX_PATTERN, candidate):
                return candidate
    return None


def _postprocess_plate_text(raw_ocr_text: str) -> Tuple[str, Optional[bool]]:
    """
    Post-processing: normalise -> regex validation -> confusable fix.
    Returns (cleaned_plate_text, matched_regex).
    """
    if not raw_ocr_text:
        return "", None

    normalised = _normalise(raw_ocr_text)

    matched = None
    if PLATE_REGEX_PATTERN:
        if re.match(PLATE_REGEX_PATTERN, normalised):
            matched = True
        else:
            # Try fixing OCR-confusable characters (Z↔2, O↔0, B↔8, etc.)
            fixed = _fix_confusables(normalised)
            if fixed:
                log.info("Fixed confusable chars: %s -> %s", normalised, fixed)
                normalised = fixed
                matched = True
            else:
                # Keep the normalised string as-is; do NOT extract a
                # substring — that mangles plates by shifting/truncating
                # characters to force a regex match.
                log.warning("Plate %s does not match regex and could not be "
                            "auto-corrected; keeping as-is", normalised)
                matched = False

    return normalised, matched


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def extract_license_plate(image_path) -> dict:
    """
    Extract the best license plate from an image file.

    Args:
        image_path: Path to the image file.

    Returns:
        dict with keys:
            plate_text          - cleaned plate string or "UNKNOWN"
            vehicle_detected    - True if a vehicle is visible in the image
            confidence          - OCR confidence [0, 1]
            detection_confidence - kept for backward compat (always 0.0)
    """
    try:
        image = cv2.imread(str(image_path))
        if image is None:
            log.warning("Failed to load image: %s", image_path)
            return _empty_result()

        vision = _extract_from_image(image)
        result = _empty_result()
        result["vehicle_detected"] = vision["vehicle_detected"]

        if not vision["plates_detail"]:
            return result

        # Take the first (best) plate and post-process
        best = vision["plates_detail"][0]
        plate_text, _matched = _postprocess_plate_text(best["plate_text"])
        result["plate_text"] = plate_text if plate_text else "UNKNOWN"
        result["confidence"] = best["confidence"] if plate_text else 0.0

        log.info("License plate detected: %s", result["plate_text"])
        return result

    except Exception as e:
        log.error("Error extracting license plate from %s: %s", image_path, e)
        return _empty_result()


def extract_all_license_plates(image_path) -> dict:
    """
    Extract ALL license plates from an image file using a single OpenAI vision call.

    Args:
        image_path: Path to the image file.

    Returns:
        dict with keys:
            plates           - list of dicts, each with plate_text, confidence,
                               detection_confidence
            vehicle_detected - True if any vehicle is visible in the image
    """
    try:
        image = cv2.imread(str(image_path))
        if image is None:
            log.warning("Failed to load image: %s", image_path)
            return {"plates": [], "vehicle_detected": False}

        vision = _extract_from_image(image)

        plates = []
        for detail in vision["plates_detail"]:
            plate_text, _matched = _postprocess_plate_text(detail["plate_text"])
            if not plate_text:
                continue
            plates.append({
                "plate_text": plate_text,
                "confidence": detail["confidence"],
                "detection_confidence": 0.0,
            })
            log.info("License plate detected: %s", plate_text)

        return {
            "plates": plates,
            "vehicle_detected": vision["vehicle_detected"],
        }

    except Exception as e:
        log.error("Error extracting license plates from %s: %s", image_path, e)
        return {"plates": [], "vehicle_detected": False}


def _empty_result() -> dict:
    return {
        "plate_text": "UNKNOWN",
        "vehicle_detected": False,
        "confidence": 0.0,
        "detection_confidence": 0.0,
    }
