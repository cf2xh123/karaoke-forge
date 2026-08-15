from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .models import LyricLine, LyricsDocument
from .pronunciation import (
    PronunciationLine,
    PronunciationUnit,
    contains_english_word,
    generate_pronunciation,
)
from .text import split_edge_whitespace
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
    show_countdown: bool = True
    countdown_gap_threshold: float = 8.0
    countdown_lead_in: float = 3.0
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
        units = tuple(
            PronunciationUnit(
                source=unit.source,
                reading=unit.reading,
                start=unit.start,
                end=unit.end,
            )
            for unit in line.pronunciation_units
            if unit.reading.strip()
            and (auto_english_pronunciation or not contains_english_word(unit.source))
        )
        return PronunciationLine(units, separator="") if units else None
    if line.pronunciation:
        # Older projects can contain a whole-line katakana reading generated
        # before this switch existed. Suppress it when the source line is
        # English-only, while retaining whole-line Japanese readings.
        if (
            not auto_english_pronunciation
            and contains_english_word(line.text)
            and not any(
                "\u3040" <= char <= "\u30ff" or "\u3400" <= char <= "\u9fff" for char in line.text
            )
        ):
            return None
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


def _karaoke_text(line: LyricLine, render_end: float | None = None) -> str:
    """Build ASS karaoke tags without collapsing pauses between tokens."""

    assert line.start is not None
    assert line.end is not None
    scale = 1.0
    if render_end is not None and render_end < line.end - 0.01:
        source_duration = max(0.01, line.end - line.start)
        scale = max(0.01, render_end - line.start) / source_duration

    def rendered_time(value: float) -> float:
        return line.start + (value - line.start) * scale

    line_start_cs = round(line.start * 100)
    cursor_cs = 0
    parts: list[str] = []
    for token in line.tokens:
        token_start_cs = max(cursor_cs, round(rendered_time(token.start) * 100) - line_start_cs)
        token_end_cs = max(
            token_start_cs + 1,
            round(rendered_time(token.end) * 100) - line_start_cs,
        )
        gap_cs = token_start_cs - cursor_cs
        if gap_cs:
            # An empty \k syllable advances libass' karaoke clock while leaving
            # the visible text unchanged. Without it, every detected pause is
            # removed and all later words sweep progressively too early.
            parts.append(r"{\k" + str(gap_cs) + "}")
        leading, core, trailing = split_edge_whitespace(token.text)
        if leading:
            parts.append(r"{\k0}")
            parts.append(_escape_ass_text(leading))
        parts.append(r"{\kf" + str(token_end_cs - token_start_cs) + "}")
        parts.append(_escape_ass_text(core))
        if trailing:
            # Keep display spacing, but do not spend the sung word's duration
            # sweeping through invisible whitespace.
            parts.append(r"{\k0}")
            parts.append(_escape_ass_text(trailing))
        cursor_cs = token_end_cs
    return "".join(parts)


def _estimated_display_end(
    line: LyricLine,
    following: LyricLine | None,
    gap_threshold: float,
) -> float:
    """Return the point after which the line should leave an instrumental gap.

    Source word timing normally gives us the real sung end. Plain line-timed LRC
    instead stretches a line to just before the next timestamp, including a long
    instrumental break. In that one recognisable case, cap the display using a
    conservative text-length estimate so the break can still be presented cleanly.
    """

    assert line.start is not None and line.end is not None
    end = line.end
    if following is None or following.start is None:
        return max(line.start + 0.01, end)

    next_start = following.start
    if line.tokens:
        token_end = min(end, max(token.end for token in line.tokens))
        if next_start - token_end >= gap_threshold:
            end = token_end
    fills_whole_interval = end >= next_start - 0.25
    if fills_whole_interval:
        visible_characters = sum(not char.isspace() for char in line.text)
        estimated_duration = max(3.0, min(8.0, 1.5 + visible_characters * 0.42))
        inferred_end = min(end, line.start + estimated_duration)
        if next_start - inferred_end >= gap_threshold:
            end = inferred_end
    return max(line.start + 0.01, end)


def _inactive_display_windows(
    lines: list[LyricLine],
    render_ends: list[float],
    break_before: dict[int, tuple[float, float]],
    index: int,
    lead_in: float,
) -> list[tuple[float, float]]:
    """Split an upcoming-line preview around a long instrumental break."""

    line = lines[index]
    assert line.start is not None
    previous = lines[index - 1] if index else None
    following = lines[index + 1] if index + 1 < len(lines) else None
    display_start = previous.start if previous is not None else line.start
    display_end = following.start if following is not None else render_ends[index]
    assert display_start is not None and display_end is not None

    after_break = break_before.get(index)
    before_break = break_before.get(index + 1)
    windows: list[tuple[float, float]] = []
    if after_break is not None:
        # Keep the normal next-line preview while the preceding line is being
        # sung, clear it during the instrumental, then bring it back for the cue.
        if previous is not None:
            windows.append((display_start, min(render_ends[index - 1], line.start)))
        cue_start = max(after_break[0], line.start - lead_in)
        windows.append((cue_start, display_end))
    else:
        windows.append((display_start, display_end))

    if before_break is not None:
        windows = [(start, min(end, render_ends[index])) for start, end in windows]
    return [(start, end) for start, end in windows if end >= start + 0.01]


def _pronunciation_source_timing(
    unit: PronunciationUnit,
    line: LyricLine,
) -> tuple[float, float] | None:
    """Map a pronunciation source span onto the matching lyric-token timing."""

    if not line.tokens:
        return None
    source_start = max(0, min(len(line.text), unit.start))
    source_end = max(
        source_start,
        min(len(line.text), unit.end or source_start + len(unit.source)),
    )
    if source_end <= source_start:
        return None

    mapped: list[tuple[float, float]] = []
    search_from = 0
    for token in line.tokens:
        character_start = line.text.find(token.text, search_from)
        if character_start < 0:
            # Generated display units normally concatenate back to line.text.
            # Keep a monotonic fallback for hand-edited projects that do not.
            character_start = search_from
        character_end = min(len(line.text), character_start + len(token.text))
        search_from = max(search_from, character_end)
        leading, core, _trailing = split_edge_whitespace(token.text)
        timed_start = min(character_end, character_start + len(leading))
        timed_end = min(character_end, timed_start + len(core))
        overlap_start = max(source_start, timed_start)
        overlap_end = min(source_end, timed_end)
        if overlap_end <= overlap_start or timed_end <= timed_start:
            continue
        duration = max(0.01, token.end - token.start)
        span = timed_end - timed_start
        mapped.append(
            (
                token.start + duration * (overlap_start - timed_start) / span,
                token.start + duration * (overlap_end - timed_start) / span,
            )
        )
    if not mapped:
        return None
    return mapped[0][0], mapped[-1][1]


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
    source_timing = _pronunciation_source_timing(unit, line)
    if source_timing is not None:
        line_start_cs = round(line.start * 100)
        source_start_cs = max(0, round(source_timing[0] * 100) - line_start_cs)
        source_end_cs = max(source_start_cs + 1, round(source_timing[1] * 100) - line_start_cs)
        delay = source_start_cs
        sweep = source_end_cs - source_start_cs
    else:
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
    countdown_muted = _ass_color(style.text_color, alpha=0x88)
    upper_margin = style.margin_v + style.font_size + style.karaoke_row_gap
    pronunciation_outline = max(1.0, min(style.outline, 2.0))
    countdown_size = max(28, round(style.font_size * 0.55))
    countdown_y = max(
        40,
        height
        - upper_margin
        - style.font_size
        - (style.pronunciation_font_size if style.show_pronunciation else 0)
        - 24,
    )
    gap_threshold = max(1.0, float(style.countdown_gap_threshold))
    lead_in = max(0.5, float(style.countdown_lead_in))
    render_ends = [
        _estimated_display_end(
            line,
            lines[index + 1] if index + 1 < len(lines) else None,
            gap_threshold,
        )
        for index, line in enumerate(lines)
    ]
    break_before: dict[int, tuple[float, float]] = {}
    for index, line in enumerate(lines):
        assert line.start is not None
        gap_start = render_ends[index - 1] if index else 0.0
        if line.start - gap_start >= gap_threshold:
            break_before[index] = (gap_start, line.start)

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
Style: Countdown,{style.font},{countdown_size},{primary},{primary},{outline},&H80000000,-1,0,0,0,100,100,0,0,1,{pronunciation_outline},{style.shadow},2,0,0,0,1

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
    # time. Long instrumental breaks are split into two preview windows so the
    # middle remains visually clean and the upcoming line returns for its cue.
    for index, line in enumerate(lines):
        assert line.start is not None and line.end is not None
        row = index % 2
        inactive_style = "KaraokeInactive" if row == 0 else "KaraokeLowerInactive"
        pronunciation = pronunciations[index]
        for display_start, display_end in _inactive_display_windows(
            lines,
            render_ends,
            break_before,
            index,
            lead_in,
        ):
            events.append(
                "Dialogue: 0,"
                f"{ass_clock(display_start)},{ass_clock(display_end)},"
                f"{inactive_style},,0,0,0,,"
                f"{{\\fad(120,180)}}{_escape_ass_text(line.text)}"
            )
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

    if style.show_countdown:
        for index, (gap_start, line_start) in break_before.items():
            cue_start = max(gap_start, line_start - lead_in)
            cue_duration = line_start - cue_start
            if cue_duration < 0.03:
                continue
            stage_duration = cue_duration / 3
            for stage in range(3):
                start = cue_start + stage * stage_duration
                end = line_start if stage == 2 else cue_start + (stage + 1) * stage_duration
                dots = []
                for dot in range(3):
                    color = primary if dot <= stage else countdown_muted
                    dots.append(f"{{\\1c{color}}}●")
                events.append(
                    "Dialogue: 4,"
                    f"{ass_clock(start)},{ass_clock(end)},Countdown,,0,0,0,,"
                    f"{{\\an2\\pos({width / 2:.1f},{countdown_y:.1f})"
                    "\\fad(100,120)\\fscx92\\fscy92"
                    "\\t(0,260,\\fscx116\\fscy116)"
                    "\\t(260,700,\\fscx100\\fscy100)}}"
                    + r"\h\h".join(dots)
                )

    for index, line in enumerate(lines):
        assert line.start is not None and line.end is not None
        render_end = render_ends[index]
        if style.show_translation and line.translation:
            events.append(
                "Dialogue: 3,"
                f"{ass_clock(line.start)},{ass_clock(render_end)},"
                f"Translation,,0,0,0,,{{\\fad(120,180)}}"
                f"{_escape_ass_text(line.translation)}"
            )
        if line.tokens:
            lyric_text = _karaoke_text(line, render_end)
        else:
            lyric_text = _escape_ass_text(line.text)
        karaoke_style = "Karaoke" if index % 2 == 0 else "KaraokeLower"
        events.append(
            "Dialogue: 1,"
            f"{ass_clock(line.start)},{ass_clock(render_end)},"
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
                    f"{ass_clock(line.start)},{ass_clock(render_end)},"
                    "Pronunciation,,0,0,0,,"
                    f"{{\\an2\\pos({x:.1f},{y:.1f})\\fad(120,180)}}"
                    f"{_pronunciation_karaoke(unit, line, style)}"
                )
    return header + "\n".join(events) + "\n"
