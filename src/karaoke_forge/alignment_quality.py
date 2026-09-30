"""Conservative review hints, not an estimate of timestamp accuracy."""

from __future__ import annotations

import json
from itertools import pairwise

from .models import LyricsDocument
from .text import alignment_key


def line_indices(value: str) -> set[int]:
    """Read the one-based line indices stored in project metadata."""
    return {int(item) for item in str(value).split(",") if item.strip().isdigit() and int(item) > 0}


def review_reasons(document: LyricsDocument) -> dict[int, list[str]]:
    try:
        raw = json.loads(document.metadata.get("alignment_review_reasons", "{}"))
    except (TypeError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    reasons = {
        int(key): [reason for reason in reasons if isinstance(reason, str)]
        for key, reasons in raw.items()
        if str(key).isdigit() and 0 < int(key) <= len(document.lines) and isinstance(reasons, list)
    }
    explicit = "alignment_review_lines" in document.metadata
    key = "alignment_review_lines" if explicit else "unmatched_lyric_lines"
    indices = line_indices(document.metadata.get(key, ""))
    if not explicit and not indices and document.metadata.get("alignment_status") == "low_coverage_recovery":
        indices = set(range(1, len(document.lines) + 1))
    for index in indices:
        if 0 < index <= len(document.lines):
            reasons.setdefault(index, ["之前标记为待复核，尚未取得可靠修正"])
    return reasons


def store_review_reasons(document: LyricsDocument, reasons: dict[int, list[str]]) -> None:
    reasons = {index: values for index, values in sorted(reasons.items()) if values}
    document.metadata["alignment_review_lines"] = ",".join(map(str, reasons))
    document.metadata["alignment_review_reasons"] = json.dumps(reasons, ensure_ascii=False)


def annotate_alignment_review(
    document: LyricsDocument,
    *,
    estimated_source: bool,
    new_alignment: bool = False,
) -> None:
    """Flag observable uncertainty without treating model probability as accuracy.

    Imported word timing need not carry ASR confidence. Missing confidence is
    therefore a review reason only for newly estimated timing. A rejected AI
    replacement of trusted source timing is not itself a defect in that source.
    """
    refined = line_indices(document.metadata.get("audio_refined_line_indices", ""))
    forced = line_indices(document.metadata.get("forced_alignment_accepted_line_indices", ""))
    previous = {} if new_alignment else review_reasons(document)
    reasons: dict[int, list[str]] = {}
    for index, line in enumerate(document.lines, 1):
        if line.hidden or not alignment_key(line.text):
            continue
        tokens = [token for token in line.tokens if alignment_key(token.text)]
        warnings = list(previous.get(index, [])) if index not in refined | forced else []
        if not tokens:
            warnings.append("缺少逐字时间")
        elif estimated_source or index in refined | forced:
            if not new_alignment and index not in refined | forced:
                warnings.append("未取得可靠演唱锚点，保留估算逐字时间")
            elif any(token.confidence is None for token in tokens):
                warnings.append("部分逐字时间由插值估算")
        if any(token.confidence is not None and token.confidence < 0.35 for token in tokens):
            warnings.append("部分演唱锚点置信度较低")
        if any(token.end - token.start <= 0.020001 for token in tokens):
            warnings.append("存在不超过 20 毫秒的扫色，请试听核对")
        if any(right.start < left.end - 0.001 for left, right in pairwise(tokens)):
            warnings.append("句内逐字时间重叠")
        if warnings:
            reasons[index] = list(dict.fromkeys(warnings))
    store_review_reasons(document, reasons)
