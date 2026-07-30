from __future__ import annotations

import re

_LRC_TIME_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:[.,]\d+)?)")


def parse_clock(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    raise ValueError(f"Invalid timecode: {value!r}")


def parse_lrc_clock(value: str) -> float:
    match = _LRC_TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Invalid LRC timecode: {value!r}")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = float(match.group(3).replace(",", "."))
    return hours * 3600 + minutes * 60 + seconds


def lrc_clock(seconds: float, precision: int = 2) -> str:
    seconds = max(0.0, seconds)
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    width = 3 + precision
    return f"{minutes:02d}:{remainder:0{width}.{precision}f}"


def srt_clock(seconds: float) -> str:
    milliseconds = round(max(0.0, seconds) * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def vtt_clock(seconds: float) -> str:
    return srt_clock(seconds).replace(",", ".")


def ass_clock(seconds: float) -> str:
    centiseconds = round(max(0.0, seconds) * 100)
    hours, centiseconds = divmod(centiseconds, 360_000)
    minutes, centiseconds = divmod(centiseconds, 6_000)
    secs, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"
