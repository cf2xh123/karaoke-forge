from __future__ import annotations

import copy
import html
import json
from collections.abc import Iterable, Sequence
from difflib import SequenceMatcher
from itertools import pairwise
from typing import Any

from .formats import parse_json
from .models import KaraokeToken, LyricLine, LyricsDocument, PronunciationSpan
from .pronunciation import generate_pronunciation
from .text import split_display_units

LINE_STATUS_VISIBLE = "显示"
LINE_STATUS_HIDDEN = "隐藏"
LINE_STATUS_DELETED = "删除"
LINE_STATUSES = (LINE_STATUS_VISIBLE, LINE_STATUS_HIDDEN, LINE_STATUS_DELETED)


def document_from_payload(payload: dict[str, Any]) -> LyricsDocument:
    return parse_json(json.dumps(payload, ensure_ascii=False))


def document_to_editor_rows(document: LyricsDocument) -> list[list[object]]:
    return [
        [
            index,
            LINE_STATUS_HIDDEN if line.hidden else LINE_STATUS_VISIBLE,
            line.start,
            line.end,
            line.text,
            line.translation or "",
        ]
        for index, line in enumerate(document.lines, 1)
    ]


def _table_rows(value: object) -> list[list[object]]:
    if value is None:
        return []
    if isinstance(value, dict) and isinstance(value.get("data"), list):
        value = value["data"]
    if hasattr(value, "values") and hasattr(value.values, "tolist"):
        return list(value.values.tolist())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [list(row) for row in value if isinstance(row, Sequence)]
    raise ValueError("编辑表格格式无效，请重新载入歌词项目。")


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _synthetic_tokens(text: str, start: float, end: float) -> list[KaraokeToken]:
    units = split_display_units(text)
    if not units:
        return []
    duration = max(0.01, end - start)
    weights = [max(1, len(unit.key)) for unit in units]
    total = sum(weights)
    cursor = start
    tokens: list[KaraokeToken] = []
    remaining_duration = duration
    remaining_weight = total
    for index, (unit, weight) in enumerate(zip(units, weights)):
        if index + 1 == len(units):
            token_end = end
        else:
            token_end = cursor + remaining_duration * weight / remaining_weight
        tokens.append(KaraokeToken(unit.text, cursor, token_end))
        remaining_duration = max(0.01, end - token_end)
        remaining_weight -= weight
        cursor = token_end
    return tokens


def _retime_existing_tokens(
    tokens: Iterable[KaraokeToken],
    old_start: float,
    old_end: float,
    new_start: float,
    new_end: float,
) -> list[KaraokeToken]:
    old_duration = max(0.01, old_end - old_start)
    new_duration = max(0.01, new_end - new_start)
    result: list[KaraokeToken] = []
    for token in tokens:
        relative_start = (token.start - old_start) / old_duration
        relative_end = (token.end - old_start) / old_duration
        result.append(
            KaraokeToken(
                text=token.text,
                start=new_start + max(0.0, relative_start) * new_duration,
                end=new_start + min(1.0, max(relative_start, relative_end)) * new_duration,
                confidence=token.confidence,
            )
        )
    return result


def apply_editor_rows(
    document: LyricsDocument,
    table: object,
) -> LyricsDocument:
    """Apply line edits while keeping all per-line fields together."""

    rows = _table_rows(table)
    source_by_id = {index: line for index, line in enumerate(document.lines, 1)}
    edited_lines: list[LyricLine] = []
    generated_timing = False

    for position, row in enumerate(rows, 1):
        padded = [*row, None, None, None, None, None, None][:6]
        try:
            line_id = int(float(padded[0])) if padded[0] not in (None, "") else position
        except (TypeError, ValueError):
            line_id = position
        status = str(padded[1] or LINE_STATUS_VISIBLE).strip()
        if status not in LINE_STATUSES:
            raise ValueError(f"第 {position} 行状态无效：{status}")
        if status == LINE_STATUS_DELETED:
            continue

        source = source_by_id.get(line_id)
        text = str(padded[4] or "").strip()
        if not text:
            raise ValueError(f"第 {position} 行原文不能为空；如需移除请选择“删除”。")
        translation = str(padded[5] or "").strip() or None
        start = _optional_float(padded[2])
        end = _optional_float(padded[3])
        if (start is None) != (end is None):
            raise ValueError(f"第 {position} 行的开始和结束时间必须同时填写。")
        if start is not None and end is not None and end <= start:
            raise ValueError(f"第 {position} 行结束时间必须晚于开始时间。")

        text_changed = source is None or source.text != text
        if start is not None and end is not None:
            if (
                source is not None
                and not text_changed
                and source.tokens
                and source.start is not None
                and source.end is not None
            ):
                tokens = _retime_existing_tokens(
                    source.tokens,
                    source.start,
                    source.end,
                    start,
                    end,
                )
            else:
                tokens = _synthetic_tokens(text, start, end)
                generated_timing = True
        else:
            tokens = []

        edited_lines.append(
            LyricLine(
                text=text,
                start=start,
                end=end,
                tokens=tokens,
                translation=translation,
                pronunciation=None if text_changed else (source.pronunciation if source else None),
                pronunciation_units=(
                    []
                    if text_changed
                    else copy.deepcopy(source.pronunciation_units if source else [])
                ),
                hidden=status == LINE_STATUS_HIDDEN,
            )
        )

    if not edited_lines:
        raise ValueError("编辑结果中没有歌词行。")
    metadata = dict(document.metadata)
    if generated_timing:
        metadata["word_timing"] = "synthetic"
    return LyricsDocument(
        lines=edited_lines,
        metadata=metadata,
        source_format="json",
    )


def pronunciation_to_editor_rows(line: LyricLine) -> list[list[object]]:
    units: Iterable[object]
    if line.pronunciation_units:
        units = line.pronunciation_units
    elif line.pronunciation:
        units = ()
    else:
        generated = generate_pronunciation(line.text)
        units = generated.units if generated else ()
    return [
        [unit.source, unit.reading, unit.start, unit.end]
        for unit in units
        if str(unit.reading).strip()
    ]


def apply_pronunciation_rows(
    document: LyricsDocument,
    line_number: int,
    table: object,
    whole_line: str | None = None,
) -> LyricsDocument:
    result = copy.deepcopy(document)
    index = int(line_number) - 1
    if index < 0 or index >= len(result.lines):
        raise ValueError("请选择有效的歌词行号。")
    line = result.lines[index]
    spans: list[PronunciationSpan] = []
    for row_number, row in enumerate(_table_rows(table), 1):
        padded = [*row, None, None, None][:4]
        reading = str(padded[1] or "").strip()
        if not reading:
            continue
        try:
            start = int(float(padded[2]))
            end = int(float(padded[3]))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"逐词注音第 {row_number} 行缺少有效字符范围。") from exc
        if start < 0 or end <= start or end > len(line.text):
            raise ValueError(f"逐词注音第 {row_number} 行范围 {start}:{end} 超出原文长度。")
        spans.append(
            PronunciationSpan(
                source=line.text[start:end],
                reading=reading,
                start=start,
                end=end,
            )
        )
    spans.sort(key=lambda unit: (unit.start, unit.end))
    for left, right in pairwise(spans):
        if right.start < left.end:
            raise ValueError("逐词注音的字符范围不能重叠。")
    line.pronunciation = (whole_line or "").strip() or None
    line.pronunciation_units = spans
    return result


def token_timing_to_json(line: LyricLine) -> str:
    tokens = line.tokens
    if not tokens and line.start is not None and line.end is not None:
        tokens = _synthetic_tokens(line.text, line.start, line.end)
    return json.dumps(
        [
            {
                "text": token.text,
                "start": round(token.start, 3),
                "end": round(token.end, 3),
            }
            for token in tokens
        ],
        ensure_ascii=False,
    )


def _remap_pronunciation_after_text_edit(
    line: LyricLine,
    old_text: str,
    new_text: str,
) -> None:
    if old_text == new_text:
        return
    equal_ranges = [
        (old_start, old_end, new_start)
        for tag, old_start, old_end, new_start, _new_end in SequenceMatcher(
            None,
            old_text,
            new_text,
            autojunk=False,
        ).get_opcodes()
        if tag == "equal"
    ]
    remapped: list[PronunciationSpan] = []
    for span in line.pronunciation_units:
        for old_start, old_end, new_start in equal_ranges:
            if span.start < old_start or span.end > old_end:
                continue
            shift = new_start - old_start
            start = span.start + shift
            end = span.end + shift
            remapped.append(
                PronunciationSpan(
                    source=new_text[start:end],
                    reading=span.reading,
                    start=start,
                    end=end,
                )
            )
            break
    line.pronunciation_units = remapped
    if "".join(old_text.split()) != "".join(new_text.split()):
        line.pronunciation = None


def apply_token_timing(
    document: LyricsDocument,
    table: object,
    line_number: int,
    token_timing_json: str,
) -> LyricsDocument:
    """Apply a visually edited per-token timeline to one lyric line."""

    result = apply_editor_rows(document, table)
    index = int(line_number) - 1
    if index < 0 or index >= len(result.lines):
        raise ValueError(f"行号应在 1 到 {len(result.lines)} 之间。")
    line = result.lines[index]
    try:
        entries = json.loads(token_timing_json or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("逐词时间数据无效，请重新载入当前行。") from exc
    if not isinstance(entries, list) or not entries:
        raise ValueError("当前行没有可保存的逐词时间。")

    tokens: list[KaraokeToken] = []
    previous_end: float | None = None
    for position, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise TypeError(f"第 {position} 个词的时间数据无效。")
        text = str(entry.get("text") or "")
        if not text:
            raise ValueError(f"第 {position} 个词的文本为空。")
        try:
            start = float(entry["start"])
            end = float(entry["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"第 {position} 个词缺少有效开始/结束时间。") from exc
        if end <= start:
            raise ValueError(f"第 {position} 个词结束时间必须晚于开始时间。")
        if previous_end is not None and start < previous_end - 0.001:
            raise ValueError(f"第 {position} 个词与前一个词的时间发生重叠。")
        tokens.append(KaraokeToken(text=text, start=start, end=end))
        previous_end = end

    old_text = line.text
    new_text = "".join(token.text for token in tokens)
    _remap_pronunciation_after_text_edit(line, old_text, new_text)
    line.tokens = tokens
    line.text = new_text
    line.start = tokens[0].start
    line.end = tokens[-1].end
    result.metadata["word_timing"] = "manual"
    return result


def nudge_editor_line_timing(
    document: LyricsDocument,
    table: object,
    line_number: int,
    *,
    start_delta: float = 0.0,
    end_delta: float = 0.0,
) -> LyricsDocument:
    """Apply table edits, then nudge one line while rescaling its word timing."""

    current = apply_editor_rows(document, table)
    index = int(line_number) - 1
    if index < 0 or index >= len(current.lines):
        raise ValueError(f"行号应在 1 到 {len(current.lines)} 之间。")
    line = current.lines[index]
    if line.start is None or line.end is None:
        raise ValueError("当前歌词行没有完整时间，无法微调。")
    rows = document_to_editor_rows(current)
    rows[index][2] = max(0.0, line.start + float(start_delta))
    rows[index][3] = line.end + float(end_delta)
    if float(rows[index][3]) <= float(rows[index][2]):
        raise ValueError("微调后结束时间必须晚于开始时间。")
    return apply_editor_rows(current, rows)


def _line_ruby_html(line: LyricLine) -> str:
    units = pronunciation_to_editor_rows(line)
    if not units and line.pronunciation:
        units = [[line.text, line.pronunciation, 0, len(line.text)]]
    parts: list[str] = []
    cursor = 0
    for _source, reading, start_value, end_value in units:
        start, end = int(start_value), int(end_value)
        parts.append(html.escape(line.text[cursor:start]))
        parts.append(
            '<ruby style="color:inherit !important;text-decoration-color:inherit !important;">'
            '<span style="color:inherit !important;">'
            f"{html.escape(line.text[start:end])}</span>"
            '<rt style="color:inherit !important;opacity:.88;">'
            f"{html.escape(str(reading))}</rt></ruby>"
        )
        cursor = end
    parts.append(html.escape(line.text[cursor:]))
    return "".join(parts)


def editor_token_timeline_html(document: LyricsDocument, line_number: int) -> str:
    index = int(line_number) - 1
    if index < 0 or index >= len(document.lines):
        return '<div class="kf-tip">请选择有效的歌词行。</div>'
    line = document.lines[index]
    if line.start is None or line.end is None:
        return '<div class="kf-tip">当前行没有完整时间，无法显示逐词时间条。</div>'
    tokens = line.tokens or _synthetic_tokens(line.text, line.start, line.end)
    if not tokens:
        return '<div class="kf-tip">当前行没有可调整的词语。</div>'

    clip_start = max(0.0, line.start - 1.0)
    clip_end = line.end + 1.0
    duration = max(0.01, clip_end - clip_start)
    minimum_width = max(760, len(tokens) * 76)

    blocks: list[str] = []
    for token_index, token in enumerate(tokens):
        start = token.start
        end = token.end
        left = (start - clip_start) / duration * 100
        width = max(0.35, (end - start) / duration * 100)
        blocks.append(
            '<div class="kf-token-block" role="button" tabindex="0" '
            f'data-token-index="{token_index}" '
            f'data-token="{html.escape(token.text, quote=True)}" '
            f'data-start="{start:.3f}" data-end="{end:.3f}" '
            f'style="left:{left:.5f}%;width:{width:.5f}%;" '
            'title="点击空白处试听；可直接修改文字；清空后保存即可删除">'
            '<input class="kf-token-text" type="text" '
            f'value="{html.escape(token.text, quote=True)}" '
            f'aria-label="第 {token_index + 1} 个词的文字" placeholder="删除" '
            'title="直接修改；清空后保存即可删除这个词，其他词时间不变">'
            f'<span class="kf-token-time">{start:.2f}–{end:.2f}s</span>'
            "</div>"
        )

    handles: list[str] = []
    for token_index, token in enumerate(tokens):
        for edge_index, (edge, value) in enumerate((("start", token.start), ("end", token.end))):
            boundary_index = token_index * 2 + edge_index
            edge_label = "开始" if edge == "start" else "结束"
            handles.append(
                '<input class="kf-token-boundary" type="range" '
                f'data-boundary-index="{boundary_index}" '
                f'data-token-index="{token_index}" data-edge="{edge}" '
                f'min="{clip_start:.3f}" max="{clip_end:.3f}" step="0.01" '
                f'value="{value:.3f}" '
                f'aria-label="第 {token_index + 1} 个词{edge_label}时间">'
            )

    return (
        '<div class="kf-token-editor" '
        f'data-line-number="{int(line_number)}" '
        f'data-line-start="{line.start:.3f}" data-line-end="{line.end:.3f}" '
        f'data-clip-start="{clip_start:.3f}" data-clip-end="{clip_end:.3f}">'
        '<div class="kf-token-toolbar"><div class="kf-token-help"><b>逐词时间：</b>'
        "直接修改词块文字；右键词块可立即移除，其他词时间不变。"
        "点击词块空白处可试听，拖动黄色竖线调整词块；拖动红线可跳到对应时间；"
        "放大后可按住时间轴空白处左右拖动。</div>"
        '<div class="kf-token-actions">'
        '<button type="button" class="kf-token-zoom-out">− 缩小</button>'
        '<button type="button" class="kf-token-zoom-fit">适应全句</button>'
        '<button type="button" class="kf-token-zoom-in">＋ 放大</button>'
        '<button type="button" class="kf-token-page-left">◀ 前一段</button>'
        '<button type="button" class="kf-token-page-right">后一段 ▶</button>'
        '<button type="button" class="kf-token-undo">↶ 撤销拖动</button>'
        '<button type="button" class="kf-token-redo">↷ 重做</button>'
        "</div></div>"
        '<div class="kf-token-scroll">'
        f'<div class="kf-token-canvas" data-base-width="{minimum_width}" '
        f'style="min-width:{minimum_width}px">'
        '<div class="kf-token-ruler">'
        f'<span>{clip_start:.2f}s</span><b class="kf-token-playtime">'
        f"{clip_start:.2f}s</b><span>{clip_end:.2f}s</span></div>"
        '<div class="kf-token-track">'
        '<div class="kf-token-playhead" style="left:0%" role="slider" '
        'aria-label="当前播放时间；可左右拖动定位" tabindex="0"></div>'
        f"{''.join(blocks)}{''.join(handles)}"
        "</div></div></div></div>"
    )


def editor_preview_html(document: LyricsDocument, line_number: int) -> str:
    index = int(line_number) - 1
    if index < 0 or index >= len(document.lines):
        return '<div class="kf-tip">请选择有效的歌词行号。</div>'
    line = document.lines[index]
    state = "暂时隐藏" if line.hidden else "显示"
    translation = (
        f'<div class="kf-editor-preview-translation">{html.escape(line.translation)}</div>'
        if line.translation
        else ""
    )
    visible_lines = document.visible_lines
    visible_index = next(
        (position for position, candidate in enumerate(visible_lines) if candidate is line),
        None,
    )
    following = (
        visible_lines[visible_index + 1]
        if visible_index is not None and visible_index + 1 < len(visible_lines)
        else None
    )
    if line.hidden:
        active = f'<div style="color:#94a3b8">{_line_ruby_html(line)}</div>'
    else:
        active = (
            '<div class="kf-live-karaoke-current" '
            f'data-line-start="{line.start or 0.0:.3f}" '
            f'data-line-end="{line.end or 0.01:.3f}" '
            'style="position:relative;display:inline-block;color:white;">'
            f'<div class="kf-live-karaoke-base">{_line_ruby_html(line)}</div>'
            '<div class="kf-live-karaoke-fill" '
            'style="position:absolute;inset:0;color:#ffd54a;'
            'clip-path:inset(0 100% 0 0);">'
            f"{_line_ruby_html(line)}</div></div>"
        )
    upcoming = f'<div style="color:white">{_line_ruby_html(following)}</div>' if following else ""
    if visible_index is not None and visible_index % 2:
        upper, lower = upcoming, active
    else:
        upper, lower = active, upcoming
    return (
        '<div class="kf-editor-preview-stage" '
        f'data-line-start="{line.start or 0.0:.3f}" '
        f'data-line-end="{line.end or 0.01:.3f}" '
        f'data-line-number="{line_number}" data-line-count="{len(document.lines)}" '
        'style="--kf-preview-font-size:28px">'
        '<div class="kf-editor-preview-info">'
        f"第 {line_number} 行 · {state} · 当前句黄色 / 下一句白色</div>"
        f"{translation}"
        '<div class="kf-editor-preview-row kf-editor-preview-upper">'
        f"{upper}</div>"
        '<div class="kf-editor-preview-row kf-editor-preview-lower">'
        f"{lower}</div></div>"
    )
