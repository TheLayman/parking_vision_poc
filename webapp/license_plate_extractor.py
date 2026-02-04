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


# Initialize EasyOCR reader (lazy initialization)
_reader = None


def get_reader():
    """Get or create the EasyOCR reader instance (singleton pattern)."""
    global _reader
    if _reader is None:
        print("Initializing EasyOCR reader...")
        # Using English language for license plates
        # gpu=False for CPU usage (change to True if GPU available)
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
    # Convert to grayscale
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Apply Gaussian blur to reduce noise
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    # Apply adaptive thresholding to enhance text
    thresh = cv2.adaptiveThreshold(
        blurred, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        11, 2
    )

    return thresh


def clean_license_plate_text(text: str) -> str:
    """
    Clean and normalize detected license plate text.

    Args:
        text: Raw OCR text

    Returns:
        Cleaned license plate text (uppercase, alphanumeric with hyphens)
    """
    if not text:
        return text

    # Convert to uppercase
    text = text.upper()

    # Remove common prefixes/suffixes that OCR might add
    # Remove leading # or other special characters
    text = re.sub(r'^[#@*]+', '', text)
    text = re.sub(r'[#@*]+$', '', text)

    # Remove common OCR misreads and special characters
    # Keep only alphanumeric characters, spaces, and hyphens
    text = re.sub(r'[^A-Z0-9\s\-]', '', text)

    # Remove extra whitespace
    text = ' '.join(text.split())

    # Smart OCR error correction for license plates
    # Indian plates typically follow patterns like: XX00XX0000 or XX-00-XX-0000
    # Apply context-aware replacements
    corrected = []
    chars = list(text.replace(' ', '').replace('-', ''))

    for i, char in enumerate(chars):
        # Get context
        prev_is_digit = i > 0 and chars[i-1].isdigit()
        next_is_digit = i < len(chars)-1 and chars[i+1].isdigit()

        # Replace O with 0 if surrounded by digits or in numeric section
        if char == 'O' and (prev_is_digit or next_is_digit or i >= 4):
            corrected.append('0')
        # Replace I/l with 1 if in numeric context
        elif char in ['I', 'L'] and (prev_is_digit or next_is_digit):
            corrected.append('1')
        # Replace S with 5 if strongly in numeric context
        elif char == 'S' and prev_is_digit and next_is_digit:
            corrected.append('5')
        # Replace Z with 2 if in numeric context
        elif char == 'Z' and (prev_is_digit or next_is_digit):
            corrected.append('2')
        else:
            corrected.append(char)

    result = ''.join(corrected).strip()

    # Indian license plate pattern matching
    # Format: LL DD LL DDDD (2 letters, 2 digits, 1-2 letters, 4 digits)
    # Try to find and extract this pattern from the text
    if len(result) >= 9:
        # Try first few starting positions to find the valid pattern
        max_start = min(3, len(result) - 9)  # Try up to 3 positions, as long as we have 9+ chars left
        for start in range(max_start + 1):  # +1 to include the max_start position
            substr = result[start:]

            # Check if it matches 10-char Indian plate pattern: LL DD LL DDDD
            if (len(substr) >= 10 and
                substr[0:2].isalpha() and  # State code (2 letters)
                substr[2:4].isdigit() and  # District code (2 digits)
                substr[4:6].isalpha() and  # Series (2 letters)
                substr[6:10].isdigit()):   # Unique number (4 digits)
                return substr[:10]

            # Check if it matches 9-char pattern: LL DD L DDDD
            if (len(substr) >= 9 and
                substr[0:2].isalpha() and  # State code
                substr[2:4].isdigit() and  # District code
                substr[4].isalpha() and    # Series (1 letter)
                substr[5:9].isdigit()):    # Unique number
                return substr[:9]

    return result


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

    # Must contain at least one letter and one number
    has_letter = any(c.isalpha() for c in text)
    has_number = any(c.isdigit() for c in text)

    if not (has_letter and has_number):
        return False

    # Length check (most plates are between 3-12 characters)
    # Relaxed from 10 to 12 to handle variations
    text_no_spaces = text.replace(' ', '').replace('-', '')
    if not (3 <= len(text_no_spaces) <= 12):
        return False

    # Reject if too many letters or too many numbers only
    letter_count = sum(1 for c in text_no_spaces if c.isalpha())
    number_count = sum(1 for c in text_no_spaces if c.isdigit())

    # Plates should have a reasonable mix (not all letters, not all numbers)
    if letter_count < 2 or number_count < 2:
        return False

    return True


def extract_license_plate(image_path: str | Path) -> str:
    """
    Extract license plate number from an image.

    Args:
        image_path: Path to the image file

    Returns:
        License plate number as string, or "UNKNOWN" if extraction fails
    """
    try:
        # Load image
        image = cv2.imread(str(image_path))

        if image is None:
            print(f"Failed to load image: {image_path}")
            return "UNKNOWN"

        # Get EasyOCR reader
        reader = get_reader()

        # Try detection on original image first
        results = reader.readtext(image)

        # Also try on preprocessed image for better results
        preprocessed = preprocess_image_for_plate_detection(image)
        results_preprocessed = reader.readtext(preprocessed)

        # Combine results from both approaches
        all_results = results + results_preprocessed

        # Filter and rank detected texts
        candidates = []

        for detection in all_results:
            # EasyOCR returns (bbox, text, confidence)
            bbox, text, confidence = detection

            # Clean the text
            cleaned_text = clean_license_plate_text(text)

            # Check if it looks like a license plate
            if is_valid_license_plate(cleaned_text):
                candidates.append({
                    'text': cleaned_text,
                    'confidence': confidence,
                    'length': len(cleaned_text.replace(' ', '').replace('-', ''))
                })

        if not candidates:
            print(f"No valid license plate detected in {image_path}")
            return "UNKNOWN"

        # Sort by confidence first, then prefer typical plate lengths (8-10 for Indian plates)
        candidates.sort(key=lambda x: (
            -x['confidence'],  # Higher confidence is better (primary)
            abs(x['length'] - 10),  # Prefer length around 10 (Indian plates like TS07ES2598)
        ))

        best_match = candidates[0]
        print(f"License plate detected: {best_match['text']} (confidence: {best_match['confidence']:.2f})")

        return best_match['text']

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

        # Get EasyOCR reader
        reader = get_reader()

        # Try detection on original image
        results = reader.readtext(image)

        # Also try on preprocessed image
        preprocessed = preprocess_image_for_plate_detection(image)
        results_preprocessed = reader.readtext(preprocessed)

        # Combine results
        all_results = results + results_preprocessed

        # Filter and rank detected texts
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
            print("No valid license plate detected")
            return "UNKNOWN"

        # Sort and select best match
        candidates.sort(key=lambda x: (
            -x['confidence'],  # Higher confidence is better (primary)
            abs(x['length'] - 10),  # Prefer length around 10 (Indian plates)
        ))

        best_match = candidates[0]
        print(f"License plate detected: {best_match['text']} (confidence: {best_match['confidence']:.2f})")

        return best_match['text']

    except Exception as e:
        print(f"Error extracting license plate: {e}")
        return "UNKNOWN"
