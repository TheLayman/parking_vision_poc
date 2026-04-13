"""License plate extraction via OpenAI Vision API.

EasyOCR backend has been removed. OpenAI Vision is the sole OCR backend
for production — it's the only permitted external dependency on this
closed-network deployment.
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

# ── Configuration ─────────────────────────────────────────────────────────────
PLATE_REGEX_PATTERN = os.getenv(
    "PLATE_REGEX_PATTERN",
    r"^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4}$",
)

_INDIAN_STATE_CODES = {
    "AN", "AP", "AR", "AS", "BR", "CG", "CH", "DD", "DL", "GA",
    "GJ", "HP", "HR", "JH", "JK", "KA", "KL", "LA", "LD", "MH",
    "ML", "MN", "MP", "MZ", "NL", "OD", "PB", "PY", "RJ", "SK",
    "TN", "TR", "TS", "UK", "UP", "WB", "BH",
}

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_LPR_MODEL", "gpt-4o")
OPENAI_MAX_COMPLETION_TOKENS = int(os.getenv("OPENAI_LPR_MAX_TOKENS", "300"))
LPR_PREPROCESS = os.getenv("LPR_PREPROCESS", "1").strip().lower() not in {"0", "false", "no"}
LPR_MAX_OCR_WIDTH = int(os.getenv("LPR_MAX_OCR_WIDTH", "1024"))
PLATE_MIN_CONFIDENCE = float(os.getenv("PLATE_MIN_CONFIDENCE", "0.65"))
_CONFIDENCE_SCORES: dict = {"high": 1.0, "medium": 0.7, "low": 0.3}

_openai_client = None

# ── System prompt ─────────────────────────────────────────────────────────────
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
    "3. Multi-line Plates: Read top line first, then bottom, combine into single string.\n"
    "4. Formatting: UPPERCASE only. Remove all spaces, dashes, dots, or bullets (•). "
    "Do not hallucinate characters.\n\n"
    "### OCR ERROR CORRECTION\n"
    "- Watch for common visual confusions: 0↔O, 1↔I, 8↔B, 5↔S, 2↔Z.\n"
    "- Use the strict SS-DD-XX-NNNN structure to resolve ambiguities.\n\n"
    "### CONFIDENCE SCORING\n"
    "- 'high': Large, perfectly clear, directly facing the camera.\n"
    "- 'medium': Readable, but smaller, slightly angled, or in dim lighting.\n"
    "- 'low': Barely legible (usually omit these).\n\n"
    "Respond with ONLY valid JSON (no markdown, no code fences):\n"
    '{"vehicle_detected": true/false, "plates": [{"plate_text": "KA01MR0045", "confidence": "high"}]}\n'
    "If no vehicle is visible or no plates are clearly readable, return:\n"
    '{"vehicle_detected": false, "plates": []}'
)

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


# ── OpenAI client ─────────────────────────────────────────────────────────────

def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        if not OPENAI_API_KEY:
            raise RuntimeError(
                "OPENAI_API_KEY environment variable is not set."
            )
        _openai_client = OpenAI(api_key=OPENAI_API_KEY, timeout=60.0, max_retries=2)
        log.info("OpenAI client initialised (model: %s)", OPENAI_MODEL)
    return _openai_client


# ── Image helpers ─────────────────────────────────────────────────────────────

def _encode_image_to_base64(image: np.ndarray) -> str:
    success, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not success:
        raise ValueError("Failed to encode image to JPEG")
    return base64.b64encode(buffer).decode("utf-8")


def _downscale(image: np.ndarray, max_width: int = LPR_MAX_OCR_WIDTH) -> np.ndarray:
    h, w = image.shape[:2]
    if w <= max_width:
        return image
    scale = max_width / w
    return cv2.resize(image, (max_width, int(h * scale)), interpolation=cv2.INTER_AREA)


def _preprocess_for_ocr(image: np.ndarray) -> list:
    """Return at most 2 variants (original + CLAHE-enhanced)."""
    image = _downscale(image)
    variants = [image]
    if not LPR_PREPROCESS:
        return variants

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    denoised = cv2.bilateralFilter(enhanced, d=5, sigmaColor=50, sigmaSpace=50)
    variants.append(cv2.cvtColor(denoised, cv2.COLOR_GRAY2BGR))
    return variants


# ── OpenAI Vision call ────────────────────────────────────────────────────────

def _call_openai_vision(image: np.ndarray) -> dict:
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
        log.error("Failed to parse OpenAI JSON response: %s", e)
        return {"vehicle_detected": False, "plates": [], "plates_detail": []}
    except Exception as e:
        log.error("OpenAI vision API call failed: %s", e)
        raise  # let caller handle retries


# ── Post-processing ──────────────────────────────────────────────────────────

def _normalise(raw: str) -> str:
    cleaned = raw.strip().upper().replace(" ", "").replace("-", "")
    return "".join(ch for ch in cleaned if ch.isalnum())


_DIGIT_TO_LETTER = str.maketrans("02581", "OZSBI")
_LETTER_TO_DIGIT = str.maketrans("OZSBI", "02581")


def _fix_confusables(plate: str) -> Optional[str]:
    for d_len in (2, 1):
        for s_len in (2, 1, 3):
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


def _postprocess_plate_text(raw_ocr_text: str, trusted: bool = True) -> Tuple[str, Optional[bool]]:
    """Normalise and validate a plate string from OpenAI output.

    *trusted* is always True since we only use OpenAI — skip _fix_confusables
    to avoid corrupting the model's already-corrected output.
    """
    if not raw_ocr_text:
        return "", None

    normalised = _normalise(raw_ocr_text)

    matched = None
    if PLATE_REGEX_PATTERN:
        if re.match(PLATE_REGEX_PATTERN, normalised):
            matched = True
        else:
            log.info("OpenAI plate %s does not match regex; keeping as-is", normalised)
            matched = False

    return normalised, matched


# ── Public API ────────────────────────────────────────────────────────────────

def extract_license_plate(image_path) -> dict:
    """Extract the best license plate from an image file."""
    try:
        image = cv2.imread(str(image_path))
        if image is None:
            log.warning("Failed to load image: %s", image_path)
            return _empty_result()

        vision = _call_openai_vision(image)
        result = _empty_result()
        result["vehicle_detected"] = vision["vehicle_detected"]

        if not vision["plates_detail"]:
            return result

        best = vision["plates_detail"][0]
        plate_text, _ = _postprocess_plate_text(best["plate_text"])
        result["plate_text"] = plate_text if plate_text else "UNKNOWN"
        result["confidence"] = best["confidence"] if plate_text else 0.0
        log.info("License plate detected: %s", result["plate_text"])
        return result

    except Exception as e:
        log.error("Error extracting license plate from %s: %s", image_path, e)
        raise


def extract_all_license_plates(image_path) -> dict:
    """Extract ALL license plates from an image file via a single OpenAI Vision call."""
    try:
        image = cv2.imread(str(image_path))
        if image is None:
            log.warning("Failed to load image: %s", image_path)
            return {"plates": [], "vehicle_detected": False}

        vision = _call_openai_vision(image)

        plates = []
        for detail in vision["plates_detail"]:
            plate_text, _ = _postprocess_plate_text(detail["plate_text"])
            if not plate_text:
                continue
            plates.append({
                "plate_text": plate_text,
                "confidence": detail["confidence"],
                "detection_confidence": 0.0,
            })
            log.info("License plate detected: %s", plate_text)

        return {"plates": plates, "vehicle_detected": vision["vehicle_detected"]}

    except Exception as e:
        log.error("Error extracting license plates from %s: %s", image_path, e)
        raise


def _empty_result() -> dict:
    return {
        "plate_text": "UNKNOWN",
        "vehicle_detected": False,
        "confidence": 0.0,
        "detection_confidence": 0.0,
    }
