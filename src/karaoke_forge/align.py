from __future__ import annotations

import copy
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import pairwise
from statistics import median

from .models import KaraokeToken, LyricLine, LyricsDocument
from .text import DisplayUnit, split_display_units


class AlignmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecognizedWord:
    text: str
    start: float
    end: float
    confidence: float | None = None


@dataclass(frozen=True)
class AlignmentReport:
    target_units: int
    recognized_units: int
    matched_units: int
    exact_units: int
    coverage: float
    mean_similarity: float
    unmatched_line_indexes: tuple[int, ...] = ()
    trusted_timing_units: int = 0
    timing_anchor_lines: int = 0
    timing_median_shift: float = 0.0
    timing_max_shift: float = 0.0
    forced_alignment_attempted_lines: int = 0
    forced_alignment_aligned_lines: int = 0
    forced_alignment_accepted_lines: int = 0


@dataclass(frozen=True)
class _TargetUnit:
    line_index: int
    unit: DisplayUnit
    unit_index: int = 0
    line_unit_count: int = 1
    source_start: float | None = None
    source_end: float | None = None


@dataclass(frozen=True)
class _RecognizedUnit:
    key: str
    start: float
    end: float
    confidence: float | None


@dataclass(frozen=True)
class _TimelineAnchor:
    line_index: int
    source: float
    detected: float
    matched_units: int

    @property
    def shift(self) -> float:
        return self.detected - self.source


def _similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    if left in right or right in left:
        return min(len(left), len(right)) / max(len(left), len(right))
    return SequenceMatcher(None, left, right).ratio()


def _match_score(similarity: float, confidence: float | None = None) -> float:
    if similarity == 1.0:
        score = 3.0
    elif similarity >= 0.8:
        score = 1.4
    elif similarity >= 0.6:
        score = 0.35
    else:
        return -1.5
    if confidence is not None:
        # Prompt-induced hallucinations often contain the exact lyric text but
        # have weak probability. Let them participate in text recovery without
        # giving them the same authority as a confident acoustic match.
        score *= 0.55 + 0.45 * min(1.0, max(0.0, confidence))
    return score


def _expand_recognized(words: list[RecognizedWord]) -> list[_RecognizedUnit]:
    expanded: list[_RecognizedUnit] = []
    recognized_cursor = 0.0
    for word in words:
        units = split_display_units(word.text)
        if not units:
            continue
        # Whisper can occasionally return overlapping or backwards word spans.
        # Keep the decoder order, but make its timing monotonic before those
        # spans become lyric anchors.
        word_start = max(0.0, recognized_cursor, word.start)
        word_end = max(word_start + 0.02, word.end)
        duration = word_end - word_start
        weights = [max(1, len(unit.key)) for unit in units]
        total_weight = sum(weights)
        cursor = word_start
        for index, (unit, weight) in enumerate(zip(units, weights)):
            if index + 1 == len(units):
                end = word_end
            else:
                end = cursor + duration * weight / total_weight
            expanded.append(
                _RecognizedUnit(
                    key=unit.key,
                    start=max(0.0, cursor),
                    end=max(cursor + 0.01, end),
                    confidence=word.confidence,
                )
            )
            cursor = end
            total_weight -= weight
            duration = max(0.01, word_end - cursor)
        recognized_cursor = word_end
    return expanded


def _flatten_target(document: LyricsDocument) -> list[_TargetUnit]:
    target: list[_TargetUnit] = []
    for line_index, line in enumerate(document.lines):
        units = split_display_units(line.text)
        for unit_index, unit in enumerate(units):
            source_start: float | None = None
            source_end: float | None = None
            if len(line.tokens) == len(units):
                source_start = line.tokens[unit_index].start
                source_end = line.tokens[unit_index].end
            elif line.start is not None and line.end is not None and units:
                step = max(0.01, line.end - line.start) / len(units)
                source_start = line.start + step * unit_index
                source_end = line.start + step * (unit_index + 1)
            target.append(
                _TargetUnit(
                    line_index=line_index,
                    unit=unit,
                    unit_index=unit_index,
                    line_unit_count=len(units),
                    source_start=source_start,
                    source_end=source_end,
                )
            )
    return target


def _sequence_alignment(
    target: list[_TargetUnit],
    recognized: list[_RecognizedUnit],
    *,
    timing_prior: float = 0.0,
) -> tuple[dict[int, tuple[int, float]], int]:
    rows = len(target) + 1
    columns = len(recognized) + 1
    gap = -1.1

    previous = [column * gap for column in range(columns)]
    trace = [bytearray(columns) for _ in range(rows)]
    target_progress: list[float] | None = None
    recognized_progress: list[float] | None = None
    timed_target = [
        ((unit.source_start or 0.0) + (unit.source_end or unit.source_start or 0.0)) / 2
        for unit in target
        if unit.source_start is not None
    ]
    if timing_prior > 0 and len(timed_target) == len(target) and target and recognized:
        target_span = timed_target[-1] - timed_target[0]
        recognized_midpoints = [(unit.start + unit.end) / 2 for unit in recognized]
        recognized_span = recognized_midpoints[-1] - recognized_midpoints[0]
        if target_span > 0.1 and recognized_span > 0.1:
            target_progress = [(value - timed_target[0]) / target_span for value in timed_target]
            recognized_progress = [
                (value - recognized_midpoints[0]) / recognized_span
                for value in recognized_midpoints
            ]
    for column in range(1, columns):
        trace[0][column] = 2
    for row in range(1, rows):
        trace[row][0] = 1
        current = [row * gap] + [0.0] * (columns - 1)
        target_key = target[row - 1].unit.key
        for column in range(1, columns):
            recognized_unit = recognized[column - 1]
            similarity = _similarity(target_key, recognized_unit.key)
            match_score = _match_score(similarity, recognized_unit.confidence)
            if target_progress is not None and recognized_progress is not None:
                match_score -= timing_prior * abs(
                    target_progress[row - 1] - recognized_progress[column - 1]
                )
            diagonal = previous[column - 1] + match_score
            delete_target = previous[column] + gap
            insert_asr = current[column - 1] + gap
            if diagonal >= delete_target and diagonal >= insert_asr:
                current[column] = diagonal
                trace[row][column] = 0
            elif delete_target >= insert_asr:
                current[column] = delete_target
                trace[row][column] = 1
            else:
                current[column] = insert_asr
                trace[row][column] = 2
        previous = current

    mapping: dict[int, tuple[int, float]] = {}
    exact = 0
    row, column = len(target), len(recognized)
    while row > 0 or column > 0:
        direction = trace[row][column]
        if row > 0 and column > 0 and direction == 0:
            similarity = _similarity(target[row - 1].unit.key, recognized[column - 1].key)
            if similarity >= 0.6:
                mapping[row - 1] = (column - 1, similarity)
                if similarity == 1.0:
                    exact += 1
            row -= 1
            column -= 1
        elif row > 0 and (column == 0 or direction == 1):
            row -= 1
        else:
            column -= 1
    return mapping, exact


def _estimate_default_duration(recognized: list[_RecognizedUnit]) -> float:
    durations = [unit.end - unit.start for unit in recognized if unit.end > unit.start]
    if not durations:
        return 0.35
    return min(0.8, max(0.08, median(durations)))


def _interpolate_timings(
    count: int,
    mapping: dict[int, tuple[int, float]],
    recognized: list[_RecognizedUnit],
) -> list[tuple[float, float, float | None]]:
    if not mapping:
        raise AlignmentError("No lyric words could be matched to the speech recognition result.")

    values: list[tuple[float, float, float | None] | None] = [None] * count
    for target_index, (recognized_index, _similarity_value) in mapping.items():
        unit = recognized[recognized_index]
        values[target_index] = (unit.start, unit.end, unit.confidence)

    anchors = sorted(mapping)
    default_duration = _estimate_default_duration(recognized)

    first = anchors[0]
    first_start = values[first][0]  # type: ignore[index]
    cursor = max(0.0, first_start - default_duration * first)
    for index in range(first):
        remaining = first - index
        step = max(0.03, (first_start - cursor) / remaining)
        values[index] = (cursor, min(first_start, cursor + step), None)
        cursor += step

    for left_index, right_index in pairwise(anchors):
        missing = right_index - left_index - 1
        if missing <= 0:
            continue
        left_start = values[left_index][0]  # type: ignore[index]
        right_start = values[right_index][0]  # type: ignore[index]
        span = max(0.03 * (missing + 1), right_start - left_start)
        step = span / (missing + 1)
        for offset in range(1, missing + 1):
            start = min(right_start - 0.01, left_start + step * offset)
            values[left_index + offset] = (max(0.0, start), max(0.01, start + step), None)

    last = anchors[-1]
    cursor = values[last][1]  # type: ignore[index]
    for index in range(last + 1, count):
        values[index] = (cursor, cursor + default_duration, None)
        cursor += default_duration

    return [value for value in values if value is not None]


def _uniform_fallback_timings(
    count: int,
    recognized: list[_RecognizedUnit],
) -> list[tuple[float, float, float | None]]:
    """Spread unmatched lyrics across the detected singing span for manual recovery."""

    if count <= 0:
        return []
    first_start = max(0.0, recognized[0].start)
    detected_end = max(unit.end for unit in recognized)
    end = max(detected_end, first_start + count * 0.08)
    step = (end - first_start) / count
    return [
        (first_start + index * step, first_start + (index + 1) * step, None)
        for index in range(count)
    ]


def _unmatched_line_indexes(
    lyrics: LyricsDocument,
    target: list[_TargetUnit],
    mapping: dict[int, tuple[int, float]],
) -> tuple[int, ...]:
    alignable = {unit.line_index for unit in target}
    matched = {target[index].line_index for index in mapping}
    return tuple(
        index for index, _line in enumerate(lyrics.lines) if index in alignable - matched
    )


def _trusted_timing_mapping(
    mapping: dict[int, tuple[int, float]],
    recognized: list[_RecognizedUnit],
) -> dict[int, tuple[int, float]]:
    """Keep acoustically credible, context-supported matches as timing anchors."""

    trusted: dict[int, tuple[int, float]] = {}
    typical_duration = _estimate_default_duration(recognized)
    maximum_anchor_duration = max(1.5, typical_duration * 6.0)
    for target_index, (recognized_index, similarity) in mapping.items():
        if similarity < 0.8:
            continue
        unit = recognized[recognized_index]
        if unit.confidence is not None and unit.confidence < 0.35:
            continue
        if unit.end - unit.start > maximum_anchor_duration:
            continue
        supported = any(
            neighbor in mapping
            and mapping[neighbor][0] == recognized_index + (neighbor - target_index)
            and mapping[neighbor][1] >= 0.8
            for neighbor in (target_index - 1, target_index + 1)
        )
        strong_isolated = similarity == 1.0 and (
            unit.confidence is None or unit.confidence >= 0.72
        )
        if supported or strong_isolated:
            trusted[target_index] = (recognized_index, similarity)
    return trusted


def _representative_timing_mapping(
    count: int,
    mapping: dict[int, tuple[int, float]],
    trusted: dict[int, tuple[int, float]],
    recognized: list[_RecognizedUnit],
) -> dict[int, tuple[int, float]]:
    """Use only trusted anchors when they represent most of the detected song."""

    if len(trusted) < 2:
        return mapping
    trusted_indexes = sorted(trusted)
    target_span = trusted_indexes[-1] - trusted_indexes[0]
    if count > 1 and target_span / (count - 1) < 0.60:
        return mapping

    mapped_indexes = sorted(mapping)
    mapped_start = recognized[mapping[mapped_indexes[0]][0]].start
    mapped_end = recognized[mapping[mapped_indexes[-1]][0]].end
    trusted_start = recognized[trusted[trusted_indexes[0]][0]].start
    trusted_end = recognized[trusted[trusted_indexes[-1]][0]].end
    mapped_span = mapped_end - mapped_start
    if mapped_span > 0.1 and (trusted_end - trusted_start) / mapped_span < 0.60:
        return mapping
    return trusted


def _plausible_anchor_chain(anchors: list[_TimelineAnchor]) -> list[_TimelineAnchor]:
    """Find the longest anchor subsequence without implausible local time stretching."""

    if not anchors:
        return []
    best_scores: list[tuple[int, int, float]] = []
    previous: list[int | None] = []
    for index, anchor in enumerate(anchors):
        score = (1, anchor.matched_units, 0.0)
        predecessor: int | None = None
        for candidate_index, candidate in enumerate(anchors[:index]):
            source_delta = anchor.source - candidate.source
            detected_delta = anchor.detected - candidate.detected
            if source_delta <= 0.02:
                continue
            slope = detected_delta / source_delta
            if not 0.65 <= slope <= 1.35:
                continue
            candidate_score = (
                best_scores[candidate_index][0] + 1,
                best_scores[candidate_index][1] + anchor.matched_units,
                anchor.source - anchors[_chain_start(previous, candidate_index)].source,
            )
            if candidate_score > score:
                score = candidate_score
                predecessor = candidate_index
        best_scores.append(score)
        previous.append(predecessor)

    end = max(range(len(anchors)), key=lambda index: best_scores[index])
    chain: list[_TimelineAnchor] = []
    while end is not None:
        chain.append(anchors[end])
        end = previous[end]
    return list(reversed(chain))


def _chain_start(previous: list[int | None], index: int) -> int:
    while previous[index] is not None:
        index = previous[index]  # type: ignore[assignment]
    return index


def _timeline_anchors(
    target: list[_TargetUnit],
    mapping: dict[int, tuple[int, float]],
    recognized: list[_RecognizedUnit],
) -> list[_TimelineAnchor]:
    grouped: dict[int, list[tuple[_TargetUnit, _RecognizedUnit]]] = {}
    line_sizes: dict[int, int] = {}
    for target_index, (recognized_index, _similarity_value) in mapping.items():
        target_unit = target[target_index]
        if target_unit.source_start is None or target_unit.source_end is None:
            continue
        detected_unit = recognized[recognized_index]
        grouped.setdefault(target_unit.line_index, []).append(
            (target_unit, detected_unit)
        )
        line_sizes[target_unit.line_index] = target_unit.line_unit_count

    anchors: list[_TimelineAnchor] = []
    for line_index, pairs in sorted(grouped.items()):
        line_size = line_sizes[line_index]
        coverage = len(pairs) / max(1, line_size)
        if len(pairs) < 2 and not (line_size == 1 and coverage == 1.0):
            continue
        if coverage < 0.4:
            continue
        first_unit_match = next(
            (
                (target_unit, detected_unit)
                for target_unit, detected_unit in pairs
                if target_unit.unit_index == 0 and target_unit.source_start is not None
            ),
            None,
        )
        if first_unit_match is None:
            continue
        source_unit, detected_unit = first_unit_match
        assert source_unit.source_start is not None
        anchors.append(
            _TimelineAnchor(
                line_index=line_index,
                source=source_unit.source_start,
                detected=detected_unit.start,
                matched_units=len(pairs),
            )
        )

    # Collapse duplicate source timestamps (commonly original + translation
    # rows in LRC) and retain a strictly monotonic anchor chain.
    monotonic: list[_TimelineAnchor] = []
    for anchor in anchors:
        if monotonic and anchor.source <= monotonic[-1].source + 0.02:
            continue
        if monotonic and anchor.detected <= monotonic[-1].detected + 0.02:
            continue
        monotonic.append(anchor)
    monotonic = _plausible_anchor_chain(monotonic)
    if len(monotonic) < 3 or monotonic[-1].source - monotonic[0].source < 3.0:
        return []

    slopes = [
        (right.detected - left.detected) / (right.source - left.source)
        for left_index, left in enumerate(monotonic)
        for right in monotonic[left_index + 1 :]
        if right.source - left.source >= 2.0
    ]
    baseline_slope = min(1.35, max(0.65, median(slopes) if slopes else 1.0))
    baseline_offset = median(
        anchor.detected - baseline_slope * anchor.source for anchor in monotonic
    )
    residuals = [
        anchor.detected - (baseline_slope * anchor.source + baseline_offset)
        for anchor in monotonic
    ]
    residual_center = median(residuals)
    residual_mad = median(abs(value - residual_center) for value in residuals)
    residual_limit = max(0.75, residual_mad * 4.0 + 0.15)
    filtered = [
        anchor
        for anchor, residual in zip(monotonic, residuals)
        if abs(residual - residual_center) <= residual_limit
    ]
    if len(filtered) < 3:
        return []

    smoothed: list[_TimelineAnchor] = []
    for index, anchor in enumerate(filtered):
        if 0 < index < len(filtered) - 1:
            shift = median(item.shift for item in filtered[index - 1 : index + 2])
            detected = anchor.source + shift
        else:
            detected = anchor.detected
        if smoothed:
            source_delta = anchor.source - smoothed[-1].source
            detected = min(
                smoothed[-1].detected + source_delta * 1.35,
                max(smoothed[-1].detected + source_delta * 0.65, detected),
            )
        smoothed.append(
            _TimelineAnchor(
                line_index=anchor.line_index,
                source=anchor.source,
                detected=detected,
                matched_units=anchor.matched_units,
            )
        )
    return smoothed


def _warp_time(value: float, anchors: list[_TimelineAnchor]) -> float:
    sources = [anchor.source for anchor in anchors]
    position = bisect_right(sources, value)
    if position == 0:
        return max(0.0, value + anchors[0].shift)
    if position == len(anchors):
        return max(0.0, value + anchors[-1].shift)
    left = anchors[position - 1]
    right = anchors[position]
    ratio = (value - left.source) / (right.source - left.source)
    detected = left.detected + ratio * (right.detected - left.detected)
    return max(0.0, detected)


def _correct_timeline_drift(
    lyrics: LyricsDocument,
    anchors: list[_TimelineAnchor],
) -> LyricsDocument:
    corrected = copy.deepcopy(lyrics)
    for line in corrected.lines:
        if line.start is None or line.end is None:
            continue
        line.start = _warp_time(line.start, anchors)
        line.end = max(line.start + 0.01, _warp_time(line.end, anchors))
        for token in line.tokens:
            token.start = _warp_time(token.start, anchors)
            token.end = max(token.start + 0.01, _warp_time(token.end, anchors))
    corrected.metadata["timeline_correction"] = "piecewise-audio-drift"
    corrected.metadata["timeline_anchor_lines"] = str(len(anchors))
    corrected.metadata["timeline_median_shift"] = f"{median(a.shift for a in anchors):.3f}"
    corrected.metadata["timeline_max_shift"] = f"{max(abs(a.shift) for a in anchors):.3f}"
    return corrected


def align_document(
    lyrics: LyricsDocument,
    recognized_words: list[RecognizedWord],
    *,
    minimum_coverage: float = 0.2,
    allow_low_coverage: bool = False,
) -> tuple[LyricsDocument, AlignmentReport]:
    """Force-align the user's exact lyrics to timestamped ASR words."""

    target = _flatten_target(lyrics)
    recognized = _expand_recognized(recognized_words)
    if not target:
        raise AlignmentError("The lyrics contain no alignable words.")
    if not recognized:
        raise AlignmentError("Speech recognition returned no timestamped words.")

    mapping, exact = _sequence_alignment(target, recognized)
    trusted_mapping = _trusted_timing_mapping(mapping, recognized)
    coverage = len(mapping) / len(target)
    if coverage < minimum_coverage and not allow_low_coverage:
        raise AlignmentError(
            f"Alignment coverage is only {coverage:.1%}; expected at least "
            f"{minimum_coverage:.1%}. Check the lyrics, language, or use vocal separation."
        )

    timing_mapping = _representative_timing_mapping(
        len(target), mapping, trusted_mapping, recognized
    )
    timings = (
        _interpolate_timings(len(target), timing_mapping, recognized)
        if timing_mapping
        else _uniform_fallback_timings(len(target), recognized)
    )
    line_tokens: list[list[KaraokeToken]] = [[] for _ in lyrics.lines]
    similarities: list[float] = []
    for index, (target_unit, timing) in enumerate(zip(target, timings)):
        start, end, confidence = timing
        line_tokens[target_unit.line_index].append(
            KaraokeToken(
                text=target_unit.unit.text,
                start=start,
                end=end,
                confidence=confidence,
            )
        )
        if index in mapping:
            similarities.append(mapping[index][1])

    aligned_lines: list[LyricLine] = []
    for line_index, source_line in enumerate(lyrics.lines):
        tokens = line_tokens[line_index]
        if not tokens:
            continue
        for token_index in range(len(tokens) - 1):
            tokens[token_index].end = max(
                tokens[token_index].start + 0.01, tokens[token_index + 1].start
            )
        aligned_lines.append(
            LyricLine(
                text=source_line.text,
                start=tokens[0].start,
                end=max(tokens[-1].end + 0.35, tokens[0].start + 0.5),
                tokens=tokens,
                translation=source_line.translation,
                pronunciation=source_line.pronunciation,
                pronunciation_units=copy.deepcopy(source_line.pronunciation_units),
                hidden=source_line.hidden,
            )
        )

    for index, line in enumerate(aligned_lines[:-1]):
        next_start = aligned_lines[index + 1].start
        assert next_start is not None and line.start is not None and line.end is not None
        if next_start > line.start + 0.1:
            line.end = min(line.end, max(line.start + 0.1, next_start - 0.02))
            if line.tokens:
                line.tokens[-1].end = max(
                    line.tokens[-1].start + 0.01,
                    min(line.tokens[-1].end, line.end),
                )

    report = AlignmentReport(
        target_units=len(target),
        recognized_units=len(recognized),
        matched_units=len(mapping),
        exact_units=exact,
        coverage=coverage,
        mean_similarity=sum(similarities) / len(similarities) if similarities else 0.0,
        unmatched_line_indexes=_unmatched_line_indexes(lyrics, target, mapping),
        trusted_timing_units=len(trusted_mapping),
    )
    metadata = dict(lyrics.metadata)
    metadata["alignment_trusted_timing_units"] = str(len(trusted_mapping))
    return (
        LyricsDocument(lines=aligned_lines, metadata=metadata, source_format="aligned"),
        report,
    )


def refine_timed_document(
    lyrics: LyricsDocument,
    recognized_words: list[RecognizedWord],
    *,
    minimum_coverage: float = 0.2,
    protect_existing_word_timing: bool = False,
) -> tuple[LyricsDocument, AlignmentReport]:
    """Refine word timing and correct drift in synthetic line-level timelines."""

    lyrics.require_timed()
    target = _flatten_target(lyrics)
    recognized = _expand_recognized(recognized_words)
    if not target:
        raise AlignmentError("The lyrics contain no alignable words.")
    if not recognized:
        raise AlignmentError("Speech recognition returned no timestamped words.")

    global_mapping, global_exact = _sequence_alignment(
        target,
        recognized,
        timing_prior=0.45,
    )
    trusted_global_mapping = _trusted_timing_mapping(global_mapping, recognized)
    global_coverage = len(global_mapping) / len(target)
    global_similarities = [value[1] for value in global_mapping.values()]
    anchors = (
        []
        if protect_existing_word_timing
        else _timeline_anchors(target, trusted_global_mapping, recognized)
    )
    working_lyrics = _correct_timeline_drift(lyrics, anchors) if anchors else lyrics
    report = AlignmentReport(
        target_units=len(target),
        recognized_units=len(recognized),
        matched_units=len(global_mapping),
        exact_units=global_exact,
        coverage=global_coverage,
        mean_similarity=(
            sum(global_similarities) / len(global_similarities) if global_similarities else 0.0
        ),
        unmatched_line_indexes=_unmatched_line_indexes(lyrics, target, global_mapping),
        trusted_timing_units=len(trusted_global_mapping),
        timing_anchor_lines=len(anchors),
        timing_median_shift=(median(anchor.shift for anchor in anchors) if anchors else 0.0),
        timing_max_shift=(max(abs(anchor.shift) for anchor in anchors) if anchors else 0.0),
    )
    if global_coverage < minimum_coverage and not protect_existing_word_timing:
        raise AlignmentError(
            f"Alignment coverage is only {global_coverage:.1%}; expected at least "
            f"{minimum_coverage:.1%}. Check the lyrics, language, or use vocal separation."
        )

    recognized_by_line: dict[int, list[int]] = {}
    for target_index, (recognized_index, _similarity_value) in trusted_global_mapping.items():
        recognized_by_line.setdefault(target[target_index].line_index, []).append(recognized_index)

    refined_lines: list[LyricLine] = []
    refined_count = 0
    for line_index, source in enumerate(working_lyrics.lines):
        assert source.start is not None and source.end is not None
        display_units = split_display_units(source.text)
        if not display_units:
            # Timed LRC files commonly use an empty timestamped row to mark an
            # instrumental break. It has no words to align, but its boundary is
            # still part of the source timeline and must survive refinement.
            refined_lines.append(copy.deepcopy(source))
            continue

        # Use the drift-corrected source window plus the globally matched
        # context. The latter lets a line recover even after the source LRC has
        # drifted beyond the old fixed ±0.35 second search window.
        window_margin = min(1.25, max(0.15, (source.end - source.start) * 0.12))
        window_start = source.start - window_margin
        window_end = source.end + window_margin
        global_indexes = recognized_by_line.get(line_index, [])
        if global_indexes:
            window_start = min(window_start, recognized[min(global_indexes)].start - 0.45)
            window_end = max(window_end, recognized[max(global_indexes)].end + 0.45)
        candidates = [
            unit
            for unit in recognized
            if unit.end >= window_start and unit.start <= window_end
        ]
        if not candidates:
            refined_lines.append(copy.deepcopy(source))
            continue

        line_target = [_TargetUnit(line_index=0, unit=unit) for unit in display_units]
        line_mapping, line_exact = _sequence_alignment(line_target, candidates)
        if not line_mapping:
            refined_lines.append(copy.deepcopy(source))
            continue
        line_similarities = [value[1] for value in line_mapping.values()]
        trusted_line_mapping = _trusted_timing_mapping(line_mapping, candidates)
        line_coverage = len(line_mapping) / len(line_target)
        trusted_line_coverage = len(trusted_line_mapping) / len(line_target)
        mean_similarity = sum(line_similarities) / len(line_similarities)
        exact_coverage = line_exact / len(line_target)
        if protect_existing_word_timing:
            reliable = (
                line_coverage >= max(0.70, minimum_coverage)
                and trusted_line_coverage >= 0.50
                and mean_similarity >= 0.86
                and (exact_coverage >= 0.50 or line_coverage >= 0.90)
            )
        else:
            reliable = (
                line_coverage >= max(0.50, minimum_coverage)
                and trusted_line_coverage >= 0.35
                and mean_similarity >= 0.80
                and (exact_coverage >= 0.25 or line_coverage >= 0.85)
            )
        if not reliable:
            refined_lines.append(copy.deepcopy(source))
            continue

        timings = _interpolate_timings(
            len(line_target),
            trusted_line_mapping or line_mapping,
            candidates,
        )
        tokens: list[KaraokeToken] = []
        cursor = source.start
        clamped_boundaries = 0
        minimum_token_duration = 0.01
        if source.end - source.start < len(display_units) * minimum_token_duration:
            refined_lines.append(copy.deepcopy(source))
            continue
        for token_index, (display_unit, timing) in enumerate(zip(display_units, timings)):
            raw_start, raw_end, confidence = timing
            remaining = len(display_units) - token_index
            latest_start = source.end - remaining * minimum_token_duration
            latest_end = source.end - (remaining - 1) * minimum_token_duration
            start = min(latest_start, max(cursor, source.start, raw_start))
            end = min(latest_end, max(start + minimum_token_duration, raw_end))
            if abs(start - raw_start) > 0.05 or abs(end - raw_end) > 0.05:
                clamped_boundaries += 1
            tokens.append(
                KaraokeToken(
                    text=display_unit.text,
                    start=start,
                    end=end,
                    confidence=confidence,
                )
            )
            cursor = end

        if any(right.start < left.end - 1e-9 for left, right in pairwise(tokens)):
            refined_lines.append(copy.deepcopy(source))
            continue
        if source.tokens and clamped_boundaries > max(1, len(tokens) // 4):
            refined_lines.append(copy.deepcopy(source))
            continue

        if protect_existing_word_timing and source.tokens:
            same_tokenization = len(source.tokens) == len(tokens) and all(
                original.text == candidate.text
                for original, candidate in zip(source.tokens, tokens)
            )
            if not same_tokenization:
                refined_lines.append(copy.deepcopy(source))
                continue
            boundary_shifts = [
                abs(original.start - candidate.start)
                for original, candidate in zip(source.tokens, tokens)
            ] + [
                abs(original.end - candidate.end)
                for original, candidate in zip(source.tokens, tokens)
            ]
            # One badly displaced word can crush every remaining token even
            # when the median shift looks harmless. Trusted/manual timing wins
            # unless every proposed boundary stays inside the safe envelope.
            if max(boundary_shifts, default=0.0) > min(
                0.25,
                (source.end - source.start) * 0.12,
            ):
                refined_lines.append(copy.deepcopy(source))
                continue

        refined_lines.append(
            LyricLine(
                text=source.text,
                start=source.start,
                end=source.end,
                tokens=tokens,
                translation=source.translation,
                pronunciation=source.pronunciation,
                pronunciation_units=copy.deepcopy(source.pronunciation_units),
                hidden=source.hidden,
            )
        )
        refined_count += 1

    metadata = dict(working_lyrics.metadata)
    if refined_count or anchors:
        metadata["word_timing"] = "audio-refined"
    metadata["audio_refined_lines"] = str(refined_count)
    metadata["audio_preserved_lines"] = str(len(lyrics.lines) - refined_count)
    return (
        LyricsDocument(
            lines=refined_lines,
            metadata=metadata,
            source_format=lyrics.source_format,
        ),
        report,
    )


def apply_forced_line_alignments(
    lyrics: LyricsDocument,
    recognized_by_line: Mapping[int, list[RecognizedWord] | tuple[RecognizedWord, ...]],
    *,
    minimum_coverage: float = 0.2,
    protect_existing_word_timing: bool = False,
) -> tuple[LyricsDocument, int]:
    """Safely apply exact-text acoustic timings without a second global alignment.

    Each line is evaluated independently. This is important for duet/overlapping
    lyrics: a global monotonic cursor would otherwise push the second voice behind
    the first even though they are meant to be sung at the same time.
    """

    lyrics.require_timed()
    output = copy.deepcopy(lyrics)
    accepted_lines = 0
    for line_index, recognized_words in recognized_by_line.items():
        if not 0 <= line_index < len(output.lines):
            continue
        source = output.lines[line_index]
        if source.hidden or source.start is None or source.end is None:
            continue
        display_units = split_display_units(source.text)
        recognized = _expand_recognized(list(recognized_words))
        if not display_units or not recognized:
            continue

        line_target = [_TargetUnit(line_index=0, unit=unit) for unit in display_units]
        mapping, exact = _sequence_alignment(line_target, recognized)
        if not mapping:
            continue
        trusted_mapping = _trusted_timing_mapping(mapping, recognized)
        similarities = [value[1] for value in mapping.values()]
        coverage = len(mapping) / len(line_target)
        trusted_coverage = len(trusted_mapping) / len(line_target)
        exact_coverage = exact / len(line_target)
        mean_similarity = sum(similarities) / len(similarities)
        confidences = [
            unit.confidence for unit in recognized if unit.confidence is not None
        ]
        mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
        typical_duration = _estimate_default_duration(recognized)
        abnormal_duration = any(
            unit.end - unit.start > max(1.5, typical_duration * 6.0)
            for unit in recognized
        )
        reliable = (
            coverage >= max(0.75, minimum_coverage)
            and trusted_coverage >= 0.60
            and exact_coverage >= 0.60
            and mean_similarity >= 0.90
            and mean_confidence >= 0.40
            and not abnormal_duration
        )
        if not reliable:
            continue

        timings = _interpolate_timings(len(line_target), mapping, recognized)
        if source.end - source.start < len(display_units) * 0.01:
            continue
        tokens: list[KaraokeToken] = []
        cursor = source.start
        clamped_boundaries = 0
        for token_index, (display_unit, timing) in enumerate(zip(display_units, timings)):
            raw_start, raw_end, confidence = timing
            remaining = len(display_units) - token_index
            latest_start = source.end - remaining * 0.01
            latest_end = source.end - (remaining - 1) * 0.01
            start = min(latest_start, max(cursor, source.start, raw_start))
            end = min(latest_end, max(start + 0.01, raw_end))
            if abs(start - raw_start) > 0.05 or abs(end - raw_end) > 0.05:
                clamped_boundaries += 1
            tokens.append(
                KaraokeToken(
                    text=display_unit.text,
                    start=start,
                    end=end,
                    confidence=confidence,
                )
            )
            cursor = end

        if any(right.start < left.end - 1e-9 for left, right in pairwise(tokens)):
            continue
        if clamped_boundaries > max(1, len(tokens) // 4):
            continue
        if protect_existing_word_timing and source.tokens:
            same_tokenization = len(source.tokens) == len(tokens) and all(
                original.text == candidate.text
                for original, candidate in zip(source.tokens, tokens)
            )
            if not same_tokenization:
                continue
            boundary_shifts = [
                abs(original.start - candidate.start)
                for original, candidate in zip(source.tokens, tokens)
            ] + [
                abs(original.end - candidate.end)
                for original, candidate in zip(source.tokens, tokens)
            ]
            if max(boundary_shifts, default=0.0) > min(
                0.25,
                (source.end - source.start) * 0.12,
            ):
                continue

        output.lines[line_index] = LyricLine(
            text=source.text,
            start=source.start,
            end=source.end,
            tokens=tokens,
            translation=source.translation,
            pronunciation=source.pronunciation,
            pronunciation_units=copy.deepcopy(source.pronunciation_units),
            hidden=source.hidden,
        )
        accepted_lines += 1

    output.metadata["forced_alignment_accepted_lines"] = str(accepted_lines)
    if accepted_lines:
        output.metadata["word_timing"] = "audio-forced"
    return output, accepted_lines
