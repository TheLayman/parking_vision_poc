"""
License plate extraction module using OpenAI GPT-4o vision.

Pipeline (single API call per image):
  1. Load image & encode to base64 JPEG
  2. Send to GPT-4o vision — detect vehicles + read all plates in one call
  3. Post-process (normalisation + regex validation)

No local ML models (YOLO) or heavy OpenCV preprocessing required.
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
OPENAI_MODEL = os.getenv("OPENAI_LPR_MODEL", "gpt-4o")
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_LPR_MAX_TOKENS", "300"))

# Minimum confidence to keep a plate (high=1.0, medium=0.7, low=0.3)
PLATE_MIN_CONFIDENCE = float(os.getenv("PLATE_MIN_CONFIDENCE", "0.65"))

# ── Singleton ─────────────────────────────────────────────────────────────────
_openai_client = None  # openai.OpenAI

# ── System prompt for GPT-4o vision ──────────────────────────────────────────
_SYSTEM_PROMPT = (
    "You are an expert Indian vehicle license plate recognition system. "
    "You will receive a parking lot camera image from India.\n\n"
    "Your tasks:\n"
    "1. Determine whether any vehicle (car, truck, bus, motorcycle, auto-rickshaw) is visible.\n"
    "2. If vehicles are visible, read license plate numbers that are CLEARLY READABLE.\n\n"
    "IMPORTANT — Only return plates you can read with confidence:\n"
    "- Do NOT guess at plates that are too small, too far away, blurry, or at sharp angles.\n"
    "- Do NOT attempt to read plates where fewer than half the characters are legible.\n"
    "- If a plate is partially obscured and you cannot confidently read it, SKIP it entirely.\n"
    "- Only include plates where you can clearly make out the characters.\n\n"
    "Indian license plate format:\n"
    "- Standard format: SS DD XX NNNN\n"
    "  - SS = State code (2 letters, e.g. MH, DL, KA, TN, AP, GJ, RJ, UP, WB, HR, TS)\n"
    "  - DD = District/RTO code (1-2 digits)\n"
    "  - XX = Series letters (1-3 letters)\n"
    "  - NNNN = Number (1-4 digits)\n"
    "- Examples: MH12AB1234, DL04CAF5765, KA01MR7189, TN09CE5765, TS08FA9087\n"
    "- Bharat (BH) series: BH DD YYYY XXNNNN (e.g. BH02AA1234)\n"
    "- Plates may have the Indian flag, Ashoka emblem, or state name on top.\n"
    "- Plates can be white (private), yellow (commercial), green (electric), "
    "or red (temporary).\n\n"
    "IMPORTANT — Leading zeros:\n"
    "- The district/RTO code is ALWAYS 2 digits on the physical plate (e.g. 01, 04, 09). "
    "Always include leading zeros: KA01, not KA1.\n"
    "- The trailing number is ALWAYS 4 digits (e.g. 0045, 0500). "
    "Always include leading zeros: MR0045, not MR45 or MR5.\n"
    "- Example: the plate 'KA 01 MR 0045' must be returned as KA01MR0045, "
    "never KA1MR45 or KA1MR5.\n\n"
    "Rules for reading plates:\n"
    "- Read each plate exactly as printed, preserving all leading zeros.\n"
    "- Use UPPERCASE letters and digits only.\n"
    "- Remove all spaces, dashes, dots, bullet separators, and special characters.\n"
    "- Indian plates often use a bullet (•) or dash between groups — ignore those.\n"
    "- Common OCR confusions on Indian plates: 0↔O, 1↔I, 8↔B, 5↔S, 2↔Z. "
    "Use the known Indian plate structure to resolve ambiguity "
    "(state code must be letters, district code must be digits, etc.).\n"
    "- Do NOT invent or hallucinate plate numbers.\n\n"
    "For each plate, rate your confidence:\n"
    "- \"high\"  = plate is large, clear, fully visible\n"
    "- \"medium\" = plate is readable but small or slightly angled\n"
    "- \"low\"   = plate is distant, blurry, or partially obscured "
    "(you should usually omit these)\n\n"
    "Respond with ONLY valid JSON (no markdown, no code fences):\n"
    '{"vehicle_detected": true/false, "plates": [{"plate_text": "PLATE1", "confidence": "high"}, ...]}\n\n'
    "If no vehicle is visible, respond:\n"
    '{"vehicle_detected": false, "plates": []}\n\n'
    "If vehicles are visible but no plates are clearly readable, respond:\n"
    '{"vehicle_detected": true, "plates": []}'
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
                "Set it to use GPT-4o for license plate reading."
            )
        _openai_client = OpenAI(api_key=api_key)
        log.info("OpenAI client initialised (model: %s)", OPENAI_MODEL)
    return _openai_client


def _encode_image_to_base64(image: np.ndarray) -> str:
    """Encode an OpenCV image (BGR numpy array) to a base64 JPEG string."""
    success, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not success:
        raise ValueError("Failed to encode image to JPEG")
    return base64.b64encode(buffer).decode("utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# Core: single GPT-4o vision call
# ═══════════════════════════════════════════════════════════════════════════════

def _call_openai_vision(image: np.ndarray) -> dict:
    """
    Send the full scene image to GPT-4o vision.

    Returns parsed dict: {"vehicle_detected": bool, "plates": [str, ...]}
    Falls back to {"vehicle_detected": False, "plates": []} on any error.
    """
    b64_image = _encode_image_to_base64(image)

    try:
        client = _get_openai_client()
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            max_tokens=OPENAI_MAX_TOKENS,
            response_format={"type": "json_object"},
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

        # Normalise: accept both old format (list of strings) and new format (list of dicts)
        confidence_map = {"high": 1.0, "medium": 0.7, "low": 0.3}
        plates_out = []
        for entry in plates_raw:
            if isinstance(entry, str):
                plates_out.append({"plate_text": entry, "confidence": 1.0})
            elif isinstance(entry, dict):
                text = entry.get("plate_text", "")
                conf_label = entry.get("confidence", "medium")
                conf_score = confidence_map.get(conf_label, 0.5)
                plates_out.append({"plate_text": text, "confidence": conf_score})

        # Filter by confidence threshold
        dropped = [p for p in plates_out if p["confidence"] < PLATE_MIN_CONFIDENCE]
        for d in dropped:
            log.info("Dropping low-confidence plate: %s (%.1f < %.1f)",
                     d["plate_text"], d["confidence"], PLATE_MIN_CONFIDENCE)
        plates_out = [p for p in plates_out if p["confidence"] >= PLATE_MIN_CONFIDENCE]

        return {
            "vehicle_detected": vehicle,
            "plates": [p["plate_text"] for p in plates_out],
            "plates_detail": plates_out,
        }

    except json.JSONDecodeError as e:
        log.error("Failed to parse GPT-4o JSON response: %s", e)
        return {"vehicle_detected": False, "plates": [], "plates_detail": []}
    except Exception as e:
        log.error("OpenAI vision API call failed: %s", e)
        return {"vehicle_detected": False, "plates": [], "plates_detail": []}


# ═══════════════════════════════════════════════════════════════════════════════
# Post-processing
# ═══════════════════════════════════════════════════════════════════════════════

def _normalise(raw: str) -> str:
    """Strip whitespace, remove non-alphanumeric, uppercase."""
    cleaned = raw.strip().upper().replace(" ", "").replace("-", "")
    return "".join(ch for ch in cleaned if ch.isalnum())


def _postprocess_plate_text(raw_ocr_text: str) -> Tuple[str, Optional[bool]]:
    """
    Post-processing: normalise -> regex validation.
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
            # Try to extract a valid plate as a substring
            search_pattern = PLATE_REGEX_PATTERN.lstrip("^").rstrip("$")
            found = re.search(search_pattern, normalised)
            if found:
                normalised = found.group(0)
                matched = True
            else:
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

        vision = _call_openai_vision(image)
        result = _empty_result()
        result["vehicle_detected"] = vision["vehicle_detected"]

        if not vision["plates"]:
            return result

        # Take the first (best) plate and post-process
        plate_text, _matched = _postprocess_plate_text(vision["plates"][0])
        result["plate_text"] = plate_text if plate_text else "UNKNOWN"
        result["confidence"] = 0.95 if plate_text else 0.0

        log.info("License plate detected: %s", result["plate_text"])
        return result

    except Exception as e:
        log.error("Error extracting license plate from %s: %s", image_path, e)
        return _empty_result()


def extract_all_license_plates(image_path) -> dict:
    """
    Extract ALL license plates from an image file using a single GPT-4o call.

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

        vision = _call_openai_vision(image)

        plates = []
        for raw_plate in vision["plates"]:
            plate_text, _matched = _postprocess_plate_text(raw_plate)
            if not plate_text:
                continue
            plates.append({
                "plate_text": plate_text,
                "confidence": 0.95,
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
