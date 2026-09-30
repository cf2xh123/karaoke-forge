"""Validated, per-user UI preferences independent of lyric projects."""

from __future__ import annotations

import json
import math
import os
import re
import threading
from pathlib import Path
from uuid import uuid4

from .network import settings_file_path

_LOCK = threading.Lock()
_RANGES = {
    "font_size": (32, 88),
    "margin_v": (30, 180),
    "translation_font_size": (24, 58),
    "translation_margin_v": (16, 760),
    "pronunciation_font_size": (18, 40),
    "countdown_gap_threshold": (5, 20),
    "playback_rate": (0.5, 2),
    "global_zoom": (0, 8),
    "token_zoom": (0, 8),
    "preview_font_size": (16, 56),
    "overview_font_size": (10, 24),
}
_BOOLEANS = {
    "show_translation",
    "show_pronunciation",
    "auto_english_pronunciation",
    "show_countdown",
    "ripple_following",
    "snap_enabled",
    "follow_playback",
}
_COLORS = {"text_color", "highlight_color", "translation_color", "pronunciation_color"}


def _validated(values: object) -> dict[str, object]:
    if not isinstance(values, dict):
        return {}
    result: dict[str, object] = {}
    for key, value in values.items():
        if key in _BOOLEANS and isinstance(value, bool):
            result[key] = value
        elif key in _RANGES and isinstance(value, (int, float)) and not isinstance(value, bool):
            low, high = _RANGES[key]
            if math.isfinite(value):
                result[key] = min(high, max(low, value))
        elif key in _COLORS and isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            result[key] = value
        elif key == "font" and isinstance(value, str) and 0 < len(value.strip()) <= 160:
            result[key] = value.strip()
        elif key == "timing_mode" and value in ("line", "global"):
            result[key] = value
    return result


def preferences_path() -> Path:
    return settings_file_path().with_name("editor-preferences.json")


def load_preferences() -> dict[str, object]:
    try:
        return _validated(json.loads(preferences_path().read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return {}


def save_preferences(patch: object) -> dict[str, object]:
    """Merge only supplied preference fields, then replace the file atomically."""
    updates = _validated(patch)
    with _LOCK:
        values = load_preferences()
        if not updates:
            return values
        values.update(updates)
        destination = preferences_path()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return values
