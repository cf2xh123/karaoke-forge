from __future__ import annotations

import copy
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


@dataclass(frozen=True)
class _TargetUnit:
    line_index: int
    unit: DisplayUnit


@dataclass(frozen=True)
class _RecognizedUnit:
    key: str
    start: float
    end: float
    confidence: float | None


def _similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    if left in right or right in left:
        return min(len(left), len(right)) / max(len(left), len(right))
    return SequenceMatcher(None, left, right).ratio()


def _match_score(similarity: float) -> float:
    if similarity == 1.0:
        return 3.0
    if similarity >= 0.8:
        return 1.4
    if similarity >= 0.6:
        return 0.35
    return -1.5


def _expand_recognized(words: list[RecognizedWord]) -> list[_RecognizedUnit]:
    expanded: list[_RecognizedUnit] = []
    for word in words:
        units = split_display_units(word.text)
        if not units:
            continue
        duration = max(0.02, word.end - word.start)
        weights = [max(1, len(unit.key)) for unit in units]
        total_weight = sum(weights)
        cursor = word.start
        for index, (unit, weight) in enumerate(zip(units, weights)):
            if index + 1 == len(units):
                end = word.end
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
            duration = max(0.01, word.end - cursor)
    return expanded


def _flatten_target(document: LyricsDocument) -> list[_TargetUnit]:
    target: list[_TargetUnit] = []
    for line_index, line in enumerate(document.lines):
        for unit in split_display_units(line.text):
            target.append(_TargetUnit(line_index=line_index, unit=unit))
    return target


def _sequence_alignment(
    target: list[_TargetUnit], recognized: list[_RecognizedUnit]
) -> tuple[dict[int, tuple[int, float]], int]:
    rows = len(target) + 1
    columns = len(recognized) + 1
    gap = -1.1

    previous = [column * gap for column in range(columns)]
    trace = [bytearray(columns) for _ in range(rows)]
    for column in range(1, columns):
        trace[0][column] = 2
    for row in range(1, rows):
        trace[row][0] = 1
        current = [row * gap] + [0.0] * (columns - 1)
        target_key = target[row - 1].unit.key
        for column in range(1, columns):
            similarity = _similarity(target_key, recognized[column - 1].key)
            diagonal = previous[column - 1] + _match_score(similarity)
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


def align_document(
    lyrics: LyricsDocument,
    recognized_words: list[RecognizedWord],
    *,
    minimum_coverage: float = 0.2,
) -> tuple[LyricsDocument, AlignmentReport]:
    """Force-align the user's exact lyrics to timestamped ASR words."""

    target = _flatten_target(lyrics)
    recognized = _expand_recognized(recognized_words)
    if not target:
        raise AlignmentError("The lyrics contain no alignable words.")
    if not recognized:
        raise AlignmentError("Speech recognition returned no timestamped words.")

    mapping, exact = _sequence_alignment(target, recognized)
    coverage = len(mapping) / len(target)
    if coverage < minimum_coverage:
        raise AlignmentError(
            f"Alignment coverage is only {coverage:.1%}; expected at least "
            f"{minimum_coverage:.1%}. Check the lyrics, language, or use vocal separation."
        )

    timings = _interpolate_timings(len(target), mapping, recognized)
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
    )
    return (
        LyricsDocument(
            lines=aligned_lines,
            metadata=dict(lyrics.metadata),
            source_format="aligned",
        ),
        report,
    )


def refine_timed_document(
    lyrics: LyricsDocument,
    recognized_words: list[RecognizedWord],
    *,
    minimum_coverage: float = 0.2,
    protect_existing_word_timing: bool = False,
) -> tuple[LyricsDocument, AlignmentReport]:
    """Refine inside each source line without moving its trusted boundaries."""

    lyrics.require_timed()
    target = _flatten_target(lyrics)
    recognized = _expand_recognized(recognized_words)
    if not target:
        raise AlignmentError("The lyrics contain no alignable words.")
    if not recognized:
        raise AlignmentError("Speech recognition returned no timestamped words.")

    global_mapping, global_exact = _sequence_alignment(target, recognized)
    global_coverage = len(global_mapping) / len(target)
    global_similarities = [value[1] for value in global_mapping.values()]
    report = AlignmentReport(
        target_units=len(target),
        recognized_units=len(recognized),
        matched_units=len(global_mapping),
        exact_units=global_exact,
        coverage=global_coverage,
        mean_similarity=(
            sum(global_similarities) / len(global_similarities) if global_similarities else 0.0
        ),
    )
    if global_coverage < minimum_coverage and not protect_existing_word_timing:
        raise AlignmentError(
            f"Alignment coverage is only {global_coverage:.1%}; expected at least "
            f"{minimum_coverage:.1%}. Check the lyrics, language, or use vocal separation."
        )

    refined_lines: list[LyricLine] = []
    refined_count = 0
    for source in lyrics.lines:
        assert source.start is not None and source.end is not None
        display_units = split_display_units(source.text)
        if not display_units:
            # Timed LRC files commonly use an empty timestamped row to mark an
            # instrumental break. It has no words to align, but its boundary is
            # still part of the source timeline and must survive refinement.
            refined_lines.append(copy.deepcopy(source))
            continue

        # Restrict recognition to this known line window. This prevents repeated
        # choruses from being matched against an identical phrase elsewhere.
        window_margin = min(0.35, max(0.08, (source.end - source.start) * 0.08))
        candidates = [
            unit
            for unit in recognized
            if unit.end >= source.start - window_margin and unit.start <= source.end + window_margin
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
        line_coverage = len(line_mapping) / len(line_target)
        mean_similarity = sum(line_similarities) / len(line_similarities)
        exact_coverage = line_exact / len(line_target)
        reliable = (
            line_coverage >= max(0.70, minimum_coverage)
            and mean_similarity >= 0.86
            and (exact_coverage >= 0.50 or line_coverage >= 0.90)
        )
        if protect_existing_word_timing and not reliable:
            refined_lines.append(copy.deepcopy(source))
            continue

        timings = _interpolate_timings(len(line_target), line_mapping, candidates)
        tokens: list[KaraokeToken] = []
        cursor = source.start
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

    metadata = dict(lyrics.metadata)
    if refined_count:
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
