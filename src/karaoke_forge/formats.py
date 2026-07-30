from __future__ import annotations

import html
import json
import re
from pathlib import Path

from .models import KaraokeToken, LyricLine, LyricsDocument
from .text import split_display_units, strip_section_label
from .timecode import lrc_clock, parse_clock, parse_lrc_clock, srt_clock, vtt_clock


class LyricsFormatError(ValueError):
    pass


_LRC_LINE_TIME = re.compile(r"\[((?:\d+:)?\d{1,2}:\d{1,2}(?:[.,]\d+)?)\]")
_ELRC_WORD_TIME = re.compile(r"<((?:\d+:)?\d{1,2}:\d{1,2}(?:[.,]\d+)?)>")
_LRC_METADATA = re.compile(r"^\[([A-Za-z][A-Za-z0-9_-]*):(.*)]$")
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
            parsed.append(LyricLine(text=clean_text, start=start, tokens=tokens))

    if not parsed:
        raise LyricsFormatError("No lyric lines were found in the LRC file.")

    parsed.sort(key=lambda line: line.start if line.start is not None else float("inf"))
    _fill_line_ends(parsed)
    for line in parsed:
        _finish_token_ends(line)
    _hydrate_missing_tokens(parsed)
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
    return LyricsDocument(lines=lines, source_format="srt")


def parse_vtt(text: str) -> LyricsDocument:
    normalized = text.replace("\r\n", "\n").lstrip("\ufeff")
    if normalized.startswith("WEBVTT"):
        normalized = normalized.split("\n", 1)[1] if "\n" in normalized else ""
    return LyricsDocument(lines=parse_srt(normalized).lines, source_format="vtt")


def parse_ass(text: str) -> LyricsDocument:
    lines: list[LyricLine] = []
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
        end = parse_clock(fields[2])
        value = _ASS_TAG.sub("", fields[9]).replace(r"\N", "\n").replace(r"\n", "\n")
        value = value.replace(r"\h", " ").strip()
        if value:
            lines.append(LyricLine(text=value, start=start, end=max(end, start + 0.01)))
    if not lines:
        raise LyricsFormatError("No Dialogue events were found in the ASS file.")
    _hydrate_missing_tokens(lines)
    return LyricsDocument(lines=lines, source_format="ass")


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
    for line in document.lines:
        assert line.start is not None
        line_tag = f"[{lrc_clock(line.start, 2)}]"
        if enhanced and line.tokens:
            content = "".join(f"<{lrc_clock(token.start, 3)}>{token.text}" for token in line.tokens)
        else:
            content = line.text
        output.append(f"{line_tag}{content}")
    return "\n".join(output) + "\n"


def write_srt(document: LyricsDocument) -> str:
    document.require_timed()
    blocks: list[str] = []
    for index, line in enumerate(document.lines, 1):
        assert line.start is not None and line.end is not None
        blocks.append(f"{index}\n{srt_clock(line.start)} --> {srt_clock(line.end)}\n{line.text}")
    return "\n\n".join(blocks) + "\n"


def write_vtt(document: LyricsDocument) -> str:
    document.require_timed()
    blocks = ["WEBVTT"]
    for line in document.lines:
        assert line.start is not None and line.end is not None
        blocks.append(f"{vtt_clock(line.start)} --> {vtt_clock(line.end)}\n{line.text}")
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
