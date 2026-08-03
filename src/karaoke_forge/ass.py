from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .models import LyricLine, LyricsDocument
from .pronunciation import PronunciationLine, PronunciationUnit, generate_pronunciation
from .timecode import ass_clock


@dataclass(frozen=True)
class AssStyle:
    font: str = "Microsoft YaHei"
    font_size: int = 58
    text_color: str = "#FFFFFF"
    highlight_color: str = "#FFD54A"
    outline_color: str = "#111111"
    outline: float = 3.0
    shadow: float = 1.2
    margin_v: int = 72
    resolution: tuple[int, int] = (1920, 1080)
    show_translation: bool = True
    translation_font_size: int = 38
    translation_color: str = "#EAF4FF"
    translation_margin_v: int = 54
    karaoke_row_gap: int = 72
    karaoke_margin_h: int = 100
    show_pronunciation: bool = True
    auto_pronunciation: bool = True
    auto_english_pronunciation: bool = True
    pronunciation_font_size: int = 26
    pronunciation_color: str = "#FFFFFF"
    pronunciation_gap: int = 4


def _ass_color(value: str, alpha: int = 0) -> str:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    if len(value) != 6:
        raise ValueError(f"Expected an RGB hex color, got: {value!r}")
    red, green, blue = value[0:2], value[2:4], value[4:6]
    return f"&H{alpha:02X}{blue}{green}{red}".upper()


def _escape_ass_text(value: str) -> str:
    return value.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def _line_pronunciation(
    line: LyricLine,
    *,
    auto_pronunciation: bool = True,
    auto_english_pronunciation: bool = True,
) -> PronunciationLine | None:
    if line.pronunciation_units:
        return PronunciationLine(
            tuple(
                PronunciationUnit(
                    source=unit.source,
                    reading=unit.reading,
                    start=unit.start,
                    end=unit.end,
                )
                for unit in line.pronunciation_units
                if unit.reading.strip()
            ),
            separator="",
        )
    if line.pronunciation:
        return PronunciationLine(
            (
                PronunciationUnit(
                    source=line.text,
                    reading=line.pronunciation,
                    start=0,
                    end=len(line.text),
                ),
            ),
            separator="",
        )
    if not auto_pronunciation:
        return None
    return generate_pronunciation(
        line.text,
        include_english=auto_english_pronunciation,
    )


@lru_cache(maxsize=32)
def _load_measurement_font(font_name: str, font_size: int) -> Any | None:
    try:
        from PIL import ImageFont
    except ImportError:
        return None

    windows_fonts = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts"
    candidates = {
        "microsoft yahei": ("msyhbd.ttc", "msyh.ttc"),
        "arial": ("arialbd.ttf", "arial.ttf"),
    }.get(font_name.strip().lower(), ())
    for filename in candidates:
        path = windows_fonts / filename
        if path.is_file():
            try:
                # libass/ASS font sizes behave like points. At a 96-DPI
                # desktop, measuring at 72/96 of the style size matches the
                # rendered glyph advance closely.
                return ImageFont.truetype(str(path), max(1, round(font_size * 0.75)))
            except OSError:
                continue
    return None


def _fallback_text_width(text: str, font_size: int) -> float:
    width = 0.0
    for char in text:
        if char.isspace():
            factor = 0.2
        elif unicodedata.east_asian_width(char) in {"W", "F"}:
            factor = 0.75
        elif char.isupper():
            factor = 0.5
        elif char.islower() or char.isdigit():
            factor = 0.4
        else:
            factor = 0.25
        width += font_size * factor
    return width


def _text_width(text: str, style: AssStyle) -> float:
    font = _load_measurement_font(style.font, style.font_size)
    if font is not None:
        return float(font.getlength(text))
    return _fallback_text_width(text, style.font_size)


def _pronunciation_position(
    line: LyricLine,
    unit: PronunciationUnit,
    row: int,
    style: AssStyle,
) -> tuple[float, float]:
    text = line.text
    start = max(0, min(len(text), unit.start))
    end = max(start, min(len(text), unit.end or start + len(unit.source)))
    total_width = _text_width(text, style)
    prefix_width = _text_width(text[:start], style)
    source_width = _text_width(text[start:end], style)
    width, height = style.resolution
    if row == 0:
        line_left = float(style.karaoke_margin_h)
        row_margin = style.margin_v + style.font_size + style.karaoke_row_gap
    else:
        line_left = width - style.karaoke_margin_h - total_width
        row_margin = style.margin_v
    x = line_left + prefix_width + (source_width / 2)
    y = height - row_margin - style.font_size - style.pronunciation_gap
    return max(10.0, min(width - 10.0, x)), max(10.0, y)


def _pronunciation_karaoke(
    unit: PronunciationUnit,
    line: LyricLine,
    style: AssStyle,
) -> str:
    assert line.start is not None and line.end is not None
    text = line.text
    start = max(0, min(len(text), unit.start))
    end = max(start, min(len(text), unit.end or start + len(unit.source)))
    total_width = max(1.0, _text_width(text, style))
    start_ratio = _text_width(text[:start], style) / total_width
    end_ratio = _text_width(text[:end], style) / total_width
    total_duration = max(0.01, line.end - line.start)
    delay = max(0, round(total_duration * start_ratio * 100))
    sweep = max(1, round(total_duration * max(0.01, end_ratio - start_ratio) * 100))
    delay_tag = r"{\k" + str(delay) + "}" if delay else ""
    return delay_tag + r"{\kf" + str(sweep) + "}" + _escape_ass_text(unit.reading)


def write_ass(document: LyricsDocument, style: AssStyle | None = None) -> str:
    document.require_timed()
    lines = document.visible_lines
    style = style or AssStyle()
    width, height = style.resolution
    primary = _ass_color(style.highlight_color)
    secondary = _ass_color(style.text_color)
    outline = _ass_color(style.outline_color)
    translation = _ass_color(style.translation_color)
    pronunciation_color = _ass_color(style.pronunciation_color)
    upper_margin = style.margin_v + style.font_size + style.karaoke_row_gap
    pronunciation_outline = max(1.0, min(style.outline, 2.0))

    header = f"""[Script Info]
; Generated by Karaoke Forge
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,{style.font},{style.font_size},{primary},{secondary},{outline},&H80000000,-1,0,0,0,100,100,0,0,1,{style.outline},{style.shadow},1,{style.karaoke_margin_h},{style.karaoke_margin_h},{upper_margin},1
Style: KaraokeLower,{style.font},{style.font_size},{primary},{secondary},{outline},&H80000000,-1,0,0,0,100,100,0,0,1,{style.outline},{style.shadow},3,{style.karaoke_margin_h},{style.karaoke_margin_h},{style.margin_v},1
Style: KaraokeInactive,{style.font},{style.font_size},{secondary},{secondary},{outline},&H80000000,-1,0,0,0,100,100,0,0,1,{style.outline},{style.shadow},1,{style.karaoke_margin_h},{style.karaoke_margin_h},{upper_margin},1
Style: KaraokeLowerInactive,{style.font},{style.font_size},{secondary},{secondary},{outline},&H80000000,-1,0,0,0,100,100,0,0,1,{style.outline},{style.shadow},3,{style.karaoke_margin_h},{style.karaoke_margin_h},{style.margin_v},1
Style: Pronunciation,{style.font},{style.pronunciation_font_size},{primary},{pronunciation_color},{outline},&H80000000,-1,0,0,0,100,100,0,0,1,{pronunciation_outline},{style.shadow},2,0,0,0,1
Style: PronunciationInactive,{style.font},{style.pronunciation_font_size},{pronunciation_color},{pronunciation_color},{outline},&H80000000,-1,0,0,0,100,100,0,0,1,{pronunciation_outline},{style.shadow},2,0,0,0,1
Style: Translation,{style.font},{style.translation_font_size},{translation},{translation},{outline},&H80000000,-1,0,0,0,100,100,0,0,1,{style.outline},{style.shadow},8,60,60,{style.translation_margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    pronunciations = [
        (
            _line_pronunciation(
                line,
                auto_pronunciation=style.auto_pronunciation,
                auto_english_pronunciation=style.auto_english_pronunciation,
            )
            if style.show_pronunciation
            else None
        )
        for line in lines
    ]

    # Keep the current line and the next line visible, then roll one row at a
    # time. While B is active on the lower row, A has already been replaced by
    # the C preview on the upper row; the layout never waits for an A/B page to
    # finish before revealing C/D.
    for index, line in enumerate(lines):
        assert line.start is not None and line.end is not None
        previous = lines[index - 1] if index else None
        following = lines[index + 1] if index + 1 < len(lines) else None
        display_start = previous.start if previous is not None else line.start
        display_end = following.start if following is not None else line.end
        assert display_start is not None and display_end is not None
        if display_end <= display_start:
            display_end = max(line.end, display_start + 0.01)
        row = index % 2
        inactive_style = "KaraokeInactive" if row == 0 else "KaraokeLowerInactive"
        events.append(
            "Dialogue: 0,"
            f"{ass_clock(display_start)},{ass_clock(display_end)},"
            f"{inactive_style},,0,0,0,,"
            f"{{\\fad(120,180)}}{_escape_ass_text(line.text)}"
        )
        pronunciation = pronunciations[index]
        if pronunciation is not None:
            for unit in pronunciation.units:
                if not unit.reading.strip():
                    continue
                x, y = _pronunciation_position(line, unit, row, style)
                events.append(
                    "Dialogue: 0,"
                    f"{ass_clock(display_start)},{ass_clock(display_end)},"
                    "PronunciationInactive,,0,0,0,,"
                    f"{{\\an2\\pos({x:.1f},{y:.1f})\\fad(120,180)}}"
                    f"{_escape_ass_text(unit.reading)}"
                )

    for index, line in enumerate(lines):
        assert line.start is not None and line.end is not None
        if style.show_translation and line.translation:
            events.append(
                "Dialogue: 3,"
                f"{ass_clock(line.start)},{ass_clock(line.end)},"
                f"Translation,,0,0,0,,{{\\fad(120,180)}}"
                f"{_escape_ass_text(line.translation)}"
            )
        if line.tokens:
            karaoke_parts: list[str] = []
            for token in line.tokens:
                duration = max(1, round((token.end - token.start) * 100))
                karaoke_parts.append(r"{\kf" + str(duration) + "}" + _escape_ass_text(token.text))
            lyric_text = "".join(karaoke_parts)
        else:
            lyric_text = _escape_ass_text(line.text)
        karaoke_style = "Karaoke" if index % 2 == 0 else "KaraokeLower"
        events.append(
            "Dialogue: 1,"
            f"{ass_clock(line.start)},{ass_clock(line.end)},"
            f"{karaoke_style},,0,0,0,,{{\\fad(120,180)}}{lyric_text}"
        )
        pronunciation = pronunciations[index]
        if pronunciation is not None:
            row = index % 2
            for unit in pronunciation.units:
                if not unit.reading.strip():
                    continue
                x, y = _pronunciation_position(line, unit, row, style)
                events.append(
                    "Dialogue: 2,"
                    f"{ass_clock(line.start)},{ass_clock(line.end)},"
                    "Pronunciation,,0,0,0,,"
                    f"{{\\an2\\pos({x:.1f},{y:.1f})\\fad(120,180)}}"
                    f"{_pronunciation_karaoke(unit, line, style)}"
                )
    return header + "\n".join(events) + "\n"
