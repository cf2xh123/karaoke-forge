from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_UNIT_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
    r"|[\u3040-\u30ff\u31f0-\u31ff]"
    r"|[\uac00-\ud7af]"
    r"|[^\W_]+(?:['’][^\W_]+)*",
    re.UNICODE,
)


@dataclass(frozen=True)
class DisplayUnit:
    text: str
    key: str


def alignment_key(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(char for char in normalized if char.isalnum())


def split_display_units(text: str) -> list[DisplayUnit]:
    """Split a line into alignable units while preserving every display character."""

    matches = list(_UNIT_RE.finditer(text))
    if not matches:
        return []

    units: list[DisplayUnit] = []
    for index, match in enumerate(matches):
        start = 0 if index == 0 else match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        chunk = text[start:end]
        key = alignment_key(match.group(0))
        if key:
            units.append(DisplayUnit(chunk, key))
    return units


def split_edge_whitespace(text: str) -> tuple[str, str, str]:
    """Return leading whitespace, timed text, and trailing whitespace."""

    leading_length = len(text) - len(text.lstrip())
    trailing_start = max(len(text.rstrip()), leading_length)
    return (
        text[:leading_length],
        text[leading_length:trailing_start],
        text[trailing_start:],
    )


def strip_section_label(line: str) -> bool:
    """Return True for common non-sung labels such as [Verse] or 【副歌】."""

    value = line.strip()
    if len(value) < 3:
        return False
    return (value.startswith("[") and value.endswith("]")) or (
        value.startswith("【") and value.endswith("】")
    )
