from __future__ import annotations

import html
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

from .models import KaraokeToken, LyricLine, LyricsDocument, PronunciationSpan
from .text import alignment_key, split_display_units, strip_section_label
from .timecode import lrc_clock, parse_clock, parse_lrc_clock, srt_clock, vtt_clock


class LyricsFormatError(ValueError):
    pass


_ASS_KARAOKE_TAG = re.compile(r"\{\\(?:kf|ko|k)(\d+)[^}]*\}", re.IGNORECASE)
_LRC_LINE_TIME = re.compile(r"\[((?:\d+:)?\d{1,2}:\d{1,2}(?:[.,]\d+)?)\]")
_ELRC_WORD_TIME = re.compile(r"<((?:\d+:)?\d{1,2}:\d{1,2}(?:[.,]\d+)?)>")
_LRC_METADATA = re.compile(r"^\[([A-Za-z][A-Za-z0-9_-]*):(.*)]$")
_YRC_LINE = re.compile(r"^\[(\d+),(\d+)](.*)$")
_YRC_WORD = re.compile(r"\((\d+),(\d+),\d+\)")
_ASS_TAG = re.compile(r"\{[^}]*}")
_HTML_TAG = re.compile(r"<[^>]+>")


def read_lyrics(path: str | Path, format_name: str | None = None) -> LyricsDocument:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Lyrics file not found: {source}")
    text = source.read_text(encoding="utf-8-sig")
    fmt = (format_name or source.suffix.lstrip(".") or "txt").lower()
    aliases = {"enhanced-lrc": "elrc", "enhanced_lrc": "elrc", "webvtt": "vtt"}
    fmt = aliases.get(fmt, fmt)

    if fmt in {"txt", "lyrics"}:
        return parse_plain(text)
    if fmt in {"lrc", "elrc"}:
        return parse_lrc(text)
    if fmt == "yrc":
        return parse_yrc(text)
    if fmt == "srt":
        return parse_srt(text)
    if fmt == "vtt":
        return parse_vtt(text)
    if fmt == "ass":
        return parse_ass(text)
    if fmt == "json":
        return parse_json(text)
    raise LyricsFormatError(f"Unsupported lyrics format: {fmt}")


def parse_plain(text: str) -> LyricsDocument:
    lines: list[LyricLine] = []
    for raw in text.splitlines():
        value = raw.strip()
        if not value or value.startswith("#") or strip_section_label(value):
            continue
        lines.append(LyricLine(text=value))
    if not lines:
        raise LyricsFormatError("No lyric lines were found in the text file.")
    return LyricsDocument(lines=lines, source_format="txt")


def parse_lrc(text: str) -> LyricsDocument:
    metadata: dict[str, str] = {}
    parsed: list[LyricLine] = []
    timestamp_lines = 0
    word_timed_lines = 0

    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        metadata_match = _LRC_METADATA.fullmatch(raw)
        if metadata_match and not _LRC_LINE_TIME.match(raw):
            metadata[metadata_match.group(1).lower()] = metadata_match.group(2).strip()
            continue

        timestamps = list(_LRC_LINE_TIME.finditer(raw))
        if not timestamps:
            value = _ELRC_WORD_TIME.sub("", raw).strip()
            if value and not strip_section_label(value):
                parsed.append(LyricLine(text=value))
            continue

        content = raw[timestamps[-1].end() :]
        for stamp in timestamps:
            start = parse_lrc_clock(stamp.group(1))
            tokens, clean_text = _parse_elrc_content(content)
            timestamp_lines += 1
            if tokens:
                word_timed_lines += 1
            parsed.append(LyricLine(text=clean_text, start=start, tokens=tokens))

    if not parsed:
        raise LyricsFormatError("No lyric lines were found in the LRC file.")

    parsed.sort(key=lambda line: line.start if line.start is not None else float("inf"))
    _fill_line_ends(parsed)
    for line in parsed:
        _finish_token_ends(line)
    _hydrate_missing_tokens(parsed)
    metadata.setdefault(
        "word_timing",
        "source" if timestamp_lines and word_timed_lines == timestamp_lines else "synthetic",
    )
    return LyricsDocument(lines=parsed, metadata=metadata, source_format="lrc")


def _parse_elrc_content(content: str) -> tuple[list[KaraokeToken], str]:
    matches = list(_ELRC_WORD_TIME.finditer(content))
    if not matches:
        return [], content.strip()

    tokens: list[KaraokeToken] = []
    visible_parts: list[str] = []
    prefix = content[: matches[0].start()]
    if prefix:
        visible_parts.append(prefix)

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        token_text = content[match.end() : end]
        if index == 0 and prefix:
            token_text = prefix + token_text
            visible_parts.clear()
        visible_parts.append(token_text)
        if token_text:
            start = parse_lrc_clock(match.group(1))
            tokens.append(KaraokeToken(text=token_text, start=start, end=start + 0.1))
    return tokens, "".join(visible_parts).strip()


def parse_yrc(text: str) -> LyricsDocument:
    """Parse NetEase YRC millisecond line and word timing."""

    lines: list[LyricLine] = []
    for raw in text.splitlines():
        line_match = _YRC_LINE.match(raw.strip())
        if not line_match:
            continue
        line_start_ms = int(line_match.group(1))
        line_duration_ms = int(line_match.group(2))
        content = line_match.group(3)
        matches = list(_YRC_WORD.finditer(content))
        if not matches:
            continue

        pieces: list[str] = []
        token_data: list[tuple[str, float, float]] = []
        for index, match in enumerate(matches):
            text_end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
            value = content[match.end() : text_end]
            if index == 0:
                value = value.lstrip()
            if index + 1 == len(matches):
                value = value.rstrip()
            if not value:
                continue
            start = int(match.group(1)) / 1000
            duration = max(0.01, int(match.group(2)) / 1000)
            pieces.append(value)
            token_data.append((value, start, start + duration))
        line_text = "".join(pieces)
        if not line_text:
            continue
        line_start = line_start_ms / 1000
        line_end = max(line_start + 0.01, (line_start_ms + line_duration_ms) / 1000)
        tokens = [
            KaraokeToken(text=value, start=start, end=min(line_end, end))
            for value, start, end in token_data
        ]
        lines.append(
            LyricLine(
                text=line_text,
                start=line_start,
                end=line_end,
                tokens=tokens,
            )
        )
    if not lines:
        raise LyricsFormatError("No timed lyric lines were found in the YRC file.")
    return LyricsDocument(
        lines=lines,
        metadata={"word_timing": "source"},
        source_format="yrc",
    )


def parse_srt(text: str) -> LyricsDocument:
    normalized = text.replace("\r\n", "\n").strip()
    lines: list[LyricLine] = []
    for block in re.split(r"\n{2,}", normalized):
        parts = [part.strip("\ufeff") for part in block.splitlines()]
        time_index = next((i for i, part in enumerate(parts) if "-->" in part), None)
        if time_index is None:
            continue
        timing = parts[time_index].split("-->", 1)
        start = parse_clock(timing[0])
        end = parse_clock(timing[1].split()[0])
        value = "\n".join(parts[time_index + 1 :]).strip()
        value = html.unescape(_HTML_TAG.sub("", value))
        if value:
            lines.append(LyricLine(text=value, start=start, end=max(end, start + 0.01)))
    if not lines:
        raise LyricsFormatError("No subtitle cues were found in the SRT file.")
    _hydrate_missing_tokens(lines)
    return LyricsDocument(
        lines=lines,
        metadata={"word_timing": "synthetic"},
        source_format="srt",
    )


def parse_vtt(text: str) -> LyricsDocument:
    normalized = text.replace("\r\n", "\n").lstrip("\ufeff")
    if normalized.startswith("WEBVTT"):
        normalized = normalized.split("\n", 1)[1] if "\n" in normalized else ""
    return LyricsDocument(
        lines=parse_srt(normalized).lines,
        metadata={"word_timing": "synthetic"},
        source_format="vtt",
    )


def _ass_visible_text(value: str) -> str:
    return (
        _ASS_TAG.sub("", value)
        .replace(r"\N", "\n")
        .replace(r"\n", "\n")
        .replace(r"\h", " ")
    )


def _parse_ass_karaoke(
    value: str,
    line_start: float,
    line_end: float,
) -> tuple[str, list[KaraokeToken]]:
    matches = list(_ASS_KARAOKE_TAG.finditer(value))
    if not matches:
        return _ass_visible_text(value).strip(), []

    prefix = _ass_visible_text(value[: matches[0].start()])
    tokens: list[KaraokeToken] = []
    pending_text = ""
    cursor = line_start
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        token_text = _ass_visible_text(value[match.end() : next_start])
        if index == 0 and prefix:
            token_text = prefix + token_text
        duration_cs = int(match.group(1))
        if duration_cs == 0:
            if token_text:
                if tokens:
                    tokens[-1].text += token_text
                else:
                    pending_text += token_text
            continue
        duration = max(0.01, duration_cs / 100)
        token_end = min(line_end, cursor + duration)
        if token_text:
            tokens.append(
                KaraokeToken(
                    text=pending_text + token_text,
                    start=cursor,
                    end=max(cursor + 0.01, token_end),
                )
            )
            pending_text = ""
        cursor = token_end
    if pending_text and tokens:
        tokens[-1].text += pending_text
    return "".join(token.text for token in tokens).strip(), tokens


def parse_ass(text: str) -> LyricsDocument:
    events: list[tuple[float, float, str, str, list[KaraokeToken]]] = []
    in_events = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.lower() == "[events]":
            in_events = True
            continue
        if not in_events or not stripped.lower().startswith("dialogue:"):
            continue
        fields = stripped.split(":", 1)[1].lstrip().split(",", 9)
        if len(fields) != 10:
            continue
        start = parse_clock(fields[1])
        end = max(parse_clock(fields[2]), start + 0.01)
        style = fields[3].strip()
        value, tokens = _parse_ass_karaoke(fields[9], start, end)
        if value:
            events.append((start, end, style, value, tokens))
    generated_by_karaoke_forge = "; Generated by Karaoke Forge" in text
    lyric_events = (
        [event for event in events if event[2] in {"Karaoke", "KaraokeLower"}]
        if generated_by_karaoke_forge
        else events
    )
    lines = [
        LyricLine(text=value, start=start, end=end, tokens=tokens)
        for start, end, _style, value, tokens in lyric_events
    ]
    if not lines:
        raise LyricsFormatError("No Dialogue events were found in the ASS file.")
    if generated_by_karaoke_forge:
        for line in lines:
            translations = [
                value
                for start, end, style, value, _tokens in events
                if style == "Translation"
                and abs(start - (line.start or 0.0)) <= 0.02
                and abs(end - (line.end or 0.0)) <= 0.02
            ]
            readings = [
                value
                for start, end, style, value, _tokens in events
                if style == "Pronunciation"
                and abs(start - (line.start or 0.0)) <= 0.02
                and abs(end - (line.end or 0.0)) <= 0.02
            ]
            if translations:
                line.translation = translations[0]
            if readings:
                line.pronunciation = " ".join(readings)
    source_word_timing = bool(lines) and all(line.tokens for line in lines)
    _hydrate_missing_tokens(lines)
    return LyricsDocument(
        lines=lines,
        metadata={"word_timing": "source" if source_word_timing else "synthetic"},
        source_format="ass",
    )


def parse_json(text: str) -> LyricsDocument:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LyricsFormatError(f"Invalid JSON lyrics: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("lines"), list):
        raise LyricsFormatError("JSON lyrics must contain a `lines` array.")

    lines: list[LyricLine] = []
    for item in payload["lines"]:
        tokens = [
            KaraokeToken(
                text=token["text"],
                start=token["start"],
                end=token["end"],
                confidence=token.get("confidence"),
            )
            for token in item.get("tokens", [])
        ]
        lines.append(
            LyricLine(
                text=item["text"],
                start=item.get("start"),
                end=item.get("end"),
                tokens=tokens,
                translation=item.get("translation"),
                pronunciation=item.get("pronunciation"),
                pronunciation_units=[
                    PronunciationSpan(
                        source=str(unit.get("source") or ""),
                        reading=str(unit.get("reading") or ""),
                        start=unit.get("start", 0),
                        end=unit.get("end", 0),
                    )
                    for unit in item.get("pronunciation_units", [])
                    if isinstance(unit, dict)
                ],
                hidden=bool(item.get("hidden", False)),
            )
        )
    document = LyricsDocument(
        lines=lines,
        metadata={str(k): str(v) for k, v in payload.get("metadata", {}).items()},
        source_format="json",
    )
    if document.is_timed:
        _hydrate_missing_tokens(document.lines)
    return document


def attach_lrc_translation(
    document: LyricsDocument,
    translated_lrc: str | None,
    *,
    tolerance: float = 0.5,
) -> int:
    """Attach translated LRC lines to the nearest original line timestamp."""

    if not translated_lrc or not document.is_timed:
        return 0
    translated = parse_lrc(translated_lrc)
    candidates = [line for line in translated.lines if line.start is not None and line.text.strip()]
    if not candidates:
        return 0

    attached = 0
    cursor = 0
    for line in document.lines:
        if line.start is None:
            continue
        while (
            cursor + 1 < len(candidates)
            and candidates[cursor + 1].start is not None
            and abs(candidates[cursor + 1].start - line.start)
            <= abs((candidates[cursor].start or 0.0) - line.start)
        ):
            cursor += 1
        candidate = candidates[cursor]
        if (
            candidate.start is not None
            and abs(candidate.start - line.start) <= tolerance
            and candidate.text.strip() != line.text.strip()
        ):
            line.translation = candidate.text.strip()
            attached += 1
    return attached


def attach_reference_translation(
    document: LyricsDocument,
    original_lrc: str | None,
    translated_lrc: str | None,
) -> int:
    """Attach translations by fuzzy-aligning YRC text to the original LRC text."""

    if not original_lrc or not translated_lrc:
        return attach_lrc_translation(document, translated_lrc)
    reference = parse_lrc(original_lrc)
    attach_lrc_translation(reference, translated_lrc)
    target_lines = [line for line in document.lines if alignment_key(line.text)]
    reference_lines = [line for line in reference.lines if alignment_key(line.text)]
    if not target_lines or not reference_lines:
        return 0

    rows = len(target_lines) + 1
    columns = len(reference_lines) + 1
    gap = -0.35
    previous = [column * gap for column in range(columns)]
    trace = [bytearray(columns) for _ in range(rows)]
    for column in range(1, columns):
        trace[0][column] = 2
    similarities: dict[tuple[int, int], float] = {}
    for row in range(1, rows):
        trace[row][0] = 1
        current = [row * gap] + [0.0] * (columns - 1)
        target = target_lines[row - 1]
        target_key = alignment_key(target.text)
        for column in range(1, columns):
            source = reference_lines[column - 1]
            source_key = alignment_key(source.text)
            similarity = SequenceMatcher(None, target_key, source_key).ratio()
            if target.start is not None and source.start is not None:
                similarity += max(0.0, 1.0 - abs(target.start - source.start) / 3.0) * 0.15
            similarities[(row - 1, column - 1)] = similarity
            diagonal = previous[column - 1] + similarity
            skip_target = previous[column] + gap
            skip_reference = current[column - 1] + gap
            if diagonal >= skip_target and diagonal >= skip_reference:
                current[column] = diagonal
                trace[row][column] = 0
            elif skip_target >= skip_reference:
                current[column] = skip_target
                trace[row][column] = 1
            else:
                current[column] = skip_reference
                trace[row][column] = 2
        previous = current

    mapping: dict[int, int] = {}
    row, column = len(target_lines), len(reference_lines)
    while row > 0 or column > 0:
        direction = trace[row][column]
        if row > 0 and column > 0 and direction == 0:
            if similarities[(row - 1, column - 1)] >= 0.45:
                mapping[row - 1] = column - 1
            row -= 1
            column -= 1
        elif row > 0 and (column == 0 or direction == 1):
            row -= 1
        else:
            column -= 1

    attached = 0
    for target_index, reference_index in mapping.items():
        translation = reference_lines[reference_index].translation
        if translation:
            target_lines[target_index].translation = translation
            attached += 1
    return attached


def _fill_line_ends(lines: list[LyricLine]) -> None:
    timed = [line for line in lines if line.start is not None]
    for index, line in enumerate(timed):
        next_start = timed[index + 1].start if index + 1 < len(timed) else None
        if line.end is None:
            line.end = max(
                line.start + 0.5, (next_start - 0.02) if next_start else line.start + 4.0
            )


def _finish_token_ends(line: LyricLine) -> None:
    if not line.tokens:
        return
    line_end = line.end if line.end is not None else line.tokens[-1].start + 0.5
    for index, token in enumerate(line.tokens):
        end = line.tokens[index + 1].start if index + 1 < len(line.tokens) else line_end
        token.end = max(token.start + 0.01, end)


def _hydrate_missing_tokens(lines: list[LyricLine]) -> None:
    for line in lines:
        if line.tokens or line.start is None or line.end is None:
            continue
        units = split_display_units(line.text)
        if not units:
            continue
        duration = max(0.01, line.end - line.start)
        step = duration / len(units)
        line.tokens = [
            KaraokeToken(
                text=unit.text,
                start=line.start + index * step,
                end=line.start + (index + 1) * step,
            )
            for index, unit in enumerate(units)
        ]


def write_lrc(document: LyricsDocument, enhanced: bool = False) -> str:
    document.require_timed()
    output: list[str] = []
    for key in ("ar", "ti", "al", "by"):
        if value := document.metadata.get(key):
            output.append(f"[{key}:{value}]")
    for line in document.visible_lines:
        assert line.start is not None
        line_tag = f"[{lrc_clock(line.start, 2)}]"
        if enhanced and line.tokens:
            content = "".join(f"<{lrc_clock(token.start, 3)}>{token.text}" for token in line.tokens)
        else:
            content = line.text
        output.append(f"{line_tag}{content}")
        if line.translation:
            output.append(f"{line_tag}{line.translation}")
    return "\n".join(output) + "\n"


def write_srt(document: LyricsDocument) -> str:
    document.require_timed()
    blocks: list[str] = []
    for index, line in enumerate(document.visible_lines, 1):
        assert line.start is not None and line.end is not None
        text = f"{line.translation}\n{line.text}" if line.translation else line.text
        blocks.append(f"{index}\n{srt_clock(line.start)} --> {srt_clock(line.end)}\n{text}")
    return "\n\n".join(blocks) + "\n"


def write_vtt(document: LyricsDocument) -> str:
    document.require_timed()
    blocks = ["WEBVTT"]
    for line in document.visible_lines:
        assert line.start is not None and line.end is not None
        text = f"{line.translation}\n{line.text}" if line.translation else line.text
        blocks.append(f"{vtt_clock(line.start)} --> {vtt_clock(line.end)}\n{text}")
    return "\n\n".join(blocks) + "\n"


def write_json(document: LyricsDocument) -> str:
    return json.dumps(document.to_dict(), ensure_ascii=False, indent=2) + "\n"


def write_format(document: LyricsDocument, format_name: str, *, ass_style: object = None) -> str:
    fmt = format_name.lower().lstrip(".")
    if fmt == "lrc":
        return write_lrc(document)
    if fmt in {"elrc", "enhanced-lrc"}:
        return write_lrc(document, enhanced=True)
    if fmt == "srt":
        return write_srt(document)
    if fmt == "vtt":
        return write_vtt(document)
    if fmt == "json":
        return write_json(document)
    if fmt == "ass":
        from .ass import AssStyle, write_ass

        style = ass_style if isinstance(ass_style, AssStyle) else AssStyle()
        return write_ass(document, style)
    raise LyricsFormatError(f"Unsupported output format: {format_name}")


def export_formats(
    document: LyricsDocument,
    output_dir: str | Path,
    basename: str,
    formats: list[str],
    *,
    ass_style: object = None,
) -> dict[str, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for requested in formats:
        fmt = requested.strip().lower()
        if not fmt:
            continue
        suffix = "lrc" if fmt == "elrc" else fmt
        label = "enhanced" if fmt == "elrc" else fmt
        filename = f"{basename}.{label}.{suffix}" if fmt == "elrc" else f"{basename}.{suffix}"
        target = directory / filename
        target.write_text(write_format(document, fmt, ass_style=ass_style), encoding="utf-8")
        result[fmt] = target
    return result
