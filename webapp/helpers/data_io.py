"""Generic I/O helpers for JSONL and YAML files."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


# ── JSONL helpers ─────────────────────────────────────────────────────────────

def append_jsonl(path: Path, record: dict, lock: threading.Lock | None = None):
    """Append a single JSON record to a JSONL file (thread-safe when *lock* given)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record) + "\n"
    if lock:
        with lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
    else:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()


def append_jsonl_batch(path: Path, records: list[dict],
                       lock: threading.Lock | None = None):
    """Append multiple JSON records in one write (thread-safe when *lock* given)."""
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = "".join(json.dumps(r) + "\n" for r in records)
    if lock:
        with lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(blob)
                f.flush()
    else:
        with open(path, "a", encoding="utf-8") as f:
            f.write(blob)
            f.flush()


def load_jsonl_records(path: Path, max_records: int) -> list[dict]:
    """Load JSONL records from *path*, keeping at most the last *max_records*.

    Uses a collections.deque to cap memory usage instead of reading all
    records then trimming.
    """
    from collections import deque
    ring: deque[dict] = deque(maxlen=max_records)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ring.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        log.error("Error loading records from %s: %s", path, e)
    return list(ring)


# ── YAML helpers ──────────────────────────────────────────────────────────────

def load_yaml(path: Path):
    """Load and return YAML content from *path*, or ``None`` if absent."""
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_yaml(path: Path, data, **kwargs):
    """Write *data* as YAML to *path*, creating parent dirs if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, **kwargs)
    except (IOError, OSError, yaml.YAMLError) as exc:
        log.error("Error writing YAML to %s: %s", path, exc)


# ── Log rotation ─────────────────────────────────────────────────────────────

def rotate_log_if_needed(event_log_path: Path, max_size_mb: int = 50):
    """Rotate event log if it exceeds *max_size_mb* to prevent disk exhaustion."""
    if not event_log_path.exists():
        return
    try:
        file_size_mb = event_log_path.stat().st_size / (1024 * 1024)
        if file_size_mb > max_size_mb:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_path = event_log_path.with_stem(f"{event_log_path.stem}_{timestamp}")
            event_log_path.rename(backup_path)
            log.info("Rotated event log to %s (size: %.1f MB)", backup_path.name, file_size_mb)
    except Exception as e:
        log.error("Error rotating event log: %s", e)
