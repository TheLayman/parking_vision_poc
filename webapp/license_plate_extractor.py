"""
License plate extraction module using EasyOCR.

This module provides functionality to extract license plate numbers from images
captured by the parking camera system.
"""

import cv2
import easyocr
import re
from pathlib import Path
from typing import Optional


_reader = None


def get_reader():
    """Get or create the EasyOCR reader instance (singleton pattern)."""
    global _reader
    if _reader is None:
        print("Initializing EasyOCR reader...")
        _reader = easyocr.Reader(['en'], gpu=False)
        print("EasyOCR reader initialized")
    return _reader


def preprocess_image_for_plate_detection(image):
    """
    Preprocess image to improve license plate detection.

    Args:
        image: OpenCV image (BGR format)

    Returns:
        Preprocessed grayscale image
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    thresh = cv2.adaptiveThreshold(
        blurred, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        11, 2
    )
    return thresh


def _normalize_text(text: str) -> str:
    """Normalize text: uppercase, remove special chars, trim whitespace."""
    if not text:
        return text

    text = text.upper()
    text = re.sub(r'^[#@*]+', '', text)
    text = re.sub(r'[#@*]+$', '', text)
    text = re.sub(r'[^A-Z0-9\s\-]', '', text)
    text = ' '.join(text.split())

    return text


def _apply_ocr_corrections(text: str) -> str:
    """Apply context-aware OCR error corrections for Indian license plates."""
    chars = list(text.replace(' ', '').replace('-', ''))
    corrected = []

    for i, char in enumerate(chars):
        prev_is_digit = i > 0 and chars[i-1].isdigit()
        next_is_digit = i < len(chars)-1 and chars[i+1].isdigit()

        # Context-aware character corrections
        if char == 'O' and (prev_is_digit or next_is_digit or i >= 4):
            corrected.append('0')
        elif char in ['I', 'L'] and (prev_is_digit or next_is_digit):
            corrected.append('1')
        elif char == 'S' and prev_is_digit and next_is_digit:
            corrected.append('5')
        elif char == 'Z' and (prev_is_digit or next_is_digit):
            corrected.append('2')
        else:
            corrected.append(char)

    return ''.join(corrected)


def _extract_indian_plate_pattern(text: str) -> str:
    """
    Extract Indian license plate pattern from text.
    Patterns: LL DD LL DDDD (10 chars) or LL DD L DDDD (9 chars)
    """
    if len(text) < 9:
        return text

    # Try first few positions to find valid pattern
    max_start = min(3, len(text) - 9)
    for start in range(max_start + 1):
        substr = text[start:]

        # Pattern: LL DD LL DDDD
        if (len(substr) >= 10 and
            substr[0:2].isalpha() and
            substr[2:4].isdigit() and
            substr[4:6].isalpha() and
            substr[6:10].isdigit()):
            return substr[:10]

        # Pattern: LL DD L DDDD
        if (len(substr) >= 9 and
            substr[0:2].isalpha() and
            substr[2:4].isdigit() and
            substr[4].isalpha() and
            substr[5:9].isdigit()):
            return substr[:9]

    return text


def clean_license_plate_text(text: str) -> str:
    """
    Clean and normalize detected license plate text.

    Args:
        text: Raw OCR text

    Returns:
        Cleaned license plate text (uppercase, alphanumeric with hyphens)
    """
    text = _normalize_text(text)
    text = _apply_ocr_corrections(text)
    text = _extract_indian_plate_pattern(text)
    return text.strip()


def is_valid_license_plate(text: str) -> bool:
    """
    Check if extracted text looks like a valid license plate.

    Args:
        text: Cleaned text string

    Returns:
        True if text appears to be a valid license plate
    """
    if not text or len(text) < 3:
        return False

    has_letter = any(c.isalpha() for c in text)
    has_number = any(c.isdigit() for c in text)

    if not (has_letter and has_number):
        return False

    text_no_spaces = text.replace(' ', '').replace('-', '')
    if not (3 <= len(text_no_spaces) <= 12):
        return False

    letter_count = sum(1 for c in text_no_spaces if c.isalpha())
    number_count = sum(1 for c in text_no_spaces if c.isdigit())

    # Plates should have reasonable mix (not all letters or all numbers)
    if letter_count < 2 or number_count < 2:
        return False

    return True


def _process_ocr_results(image, preprocessed_image) -> str:
    """
    Process OCR results from both original and preprocessed images.
    Returns best matching license plate or "UNKNOWN".
    """
    reader = get_reader()

    results = reader.readtext(image)
    results_preprocessed = reader.readtext(preprocessed_image)
    all_results = results + results_preprocessed

    candidates = []

    for detection in all_results:
        bbox, text, confidence = detection
        cleaned_text = clean_license_plate_text(text)

        if is_valid_license_plate(cleaned_text):
            candidates.append({
                'text': cleaned_text,
                'confidence': confidence,
                'length': len(cleaned_text.replace(' ', '').replace('-', ''))
            })

    if not candidates:
        return "UNKNOWN"

    # Sort by confidence, then prefer typical plate lengths (around 10 for Indian plates)
    candidates.sort(key=lambda x: (
        -x['confidence'],
        abs(x['length'] - 10),
    ))

    best_match = candidates[0]
    print(f"License plate detected: {best_match['text']} (confidence: {best_match['confidence']:.2f})")

    return best_match['text']


def extract_license_plate(image_path: str | Path) -> str:
    """
    Extract license plate number from an image.

    Args:
        image_path: Path to the image file

    Returns:
        License plate number as string, or "UNKNOWN" if extraction fails
    """
    try:
        image = cv2.imread(str(image_path))

        if image is None:
            print(f"Failed to load image: {image_path}")
            return "UNKNOWN"

        preprocessed = preprocess_image_for_plate_detection(image)
        return _process_ocr_results(image, preprocessed)

    except Exception as e:
        print(f"Error extracting license plate from {image_path}: {e}")
        return "UNKNOWN"


def extract_license_plate_from_cv2_image(image) -> str:
    """
    Extract license plate number from an OpenCV image object.

    Args:
        image: OpenCV image (BGR format)

    Returns:
        License plate number as string, or "UNKNOWN" if extraction fails
    """
    try:
        if image is None:
            print("Invalid image provided")
            return "UNKNOWN"

        preprocessed = preprocess_image_for_plate_detection(image)
        return _process_ocr_results(image, preprocessed)

    except Exception as e:
        print(f"Error extracting license plate: {e}")
        return "UNKNOWN"
