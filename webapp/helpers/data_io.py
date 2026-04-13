"""I/O helpers — YAML config reads only.

JSONL functions (append_jsonl, load_jsonl_records, rotate_log_if_needed) have
been removed. All persistent storage now uses PostgreSQL (events/challans) and
Redis (slot state). This module keeps only YAML utilities needed by slot_meta
and camera config loading.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


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
