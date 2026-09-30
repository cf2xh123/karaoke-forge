from itertools import pairwise

import pytest

from karaoke_forge.align import (
    RecognizedWord,
    align_document,
    apply_forced_line_alignments,
    refine_timed_document,
)
from karaoke_forge.formats import parse_lrc, parse_plain, parse_yrc
from karaoke_forge.models import KaraokeToken, LyricLine, LyricsDocument


def test_initial_alignment_preserves_real_word_ends_and_intra_line_pauses():
    lyrics = parse_plain("君と hello world")
    recognized = [
        RecognizedWord("君と", 1, 1.4, 0.99),
        RecognizedWord("hello", 3, 3.5, 0.99),
        RecognizedWord("world", 4, 4.4, 0.99),
    ]
    result, _ = align_document(lyrics, recognized)
    tokens = result.lines[0].tokens
    assert [(word.start, word.end) for word in tokens] == pytest.approx(
        [(1, 1.2), (1.2, 1.4), (3, 3.5), (4, 4.4)]
    )


def test_missing_words_use_the_gap_between_trusted_spans_not_their_sung_duration():
    result, _ = align_document(parse_plain("alpha beta gamma"), [
        RecognizedWord("alpha", 1, 2.8, 0.99),
        RecognizedWord("gamma", 3, 3.5, 0.99),
    ])
    tokens = result.lines[0].tokens
    assert (tokens[0].start, tokens[0].end) == (1, 2.8)
    assert 2.8 <= tokens[1].start < tokens[1].end <= 3
    assert (tokens[2].start, tokens[2].end) == (3, 3.5)


def test_unmatched_prefix_at_zero_remains_monotonic_with_minimal_anchor_displacement():
    result, _ = align_document(parse_plain("missing lost early target"), [
        RecognizedWord("target", 0, 0.5, 0.99),
    ])
    tokens = result.lines[0].tokens
    assert all(right.start >= left.end for left, right in pairwise(tokens))
    assert tokens[-1].start <= 0.03 + 1e-9
    assert tokens[-1].end == 0.5


def test_unmatched_long_asr_outlier_cannot_shift_later_trusted_words():
    result, report = align_document(parse_plain("hello world"), [
        RecognizedWord("noise", 0, 15, 0.05),
        RecognizedWord("hello", 1, 1.4, 0.99),
        RecognizedWord("world", 2, 2.5, 0.99),
    ])
    assert report.trusted_timing_units == 2
    assert [(word.start, word.end) for word in result.lines[0].tokens] == [(1, 1.4), (2, 2.5)]


def test_partial_asr_chorus_keeps_each_repeat_near_its_own_source_start():
    lyrics = parse_lrc("".join(
        f"[{time // 60:02}:{time % 60:02}.00]same words\n" for time in [10, 20, 30, 40, 50, 60]
    ))
    recognized = [
        RecognizedWord(word, time + index * .5, time + index * .5 + .3, .95)
        for time in [10, 20, 30] for index, word in enumerate(["same", "words"])
    ]
    result, _ = refine_timed_document(lyrics, recognized)
    for line_index in range(3):
        start = 10 * (line_index + 1)
        assert [(word.start, word.end) for word in result.lines[line_index].tokens] == pytest.approx(
            [(start, start + .3), (start + .5, start + .8)]
        )
    assert result.lines[3:] == lyrics.lines[3:]


@pytest.mark.parametrize("start,end", [(9.45, 9.95), (12.05, 12.6), (9.6, 10.01)])
def test_forced_alignment_rejects_words_outside_or_almost_entirely_clipped(start, end):
    lyrics = LyricsDocument([LyricLine("hello", 10, 12, [KaraokeToken("hello", 10, 12)])])
    result, accepted = apply_forced_line_alignments(lyrics, {
        0: [RecognizedWord("hello", start, end, .95)],
    })
    assert accepted == 0
    assert result.lines == lyrics.lines


def test_forced_alignment_allows_small_boundary_corrections():
    lyrics = LyricsDocument([LyricLine("hello", 10, 12)])
    result, accepted = apply_forced_line_alignments(lyrics, {
        0: [RecognizedWord("hello", 9.97, 10.5, .95)],
    })
    assert accepted == 1
    assert [(word.start, word.end) for word in result.lines[0].tokens] == [(10, 10.5)]


def test_local_start_prior_preserves_large_legitimate_global_offset_recovery():
    lyrics = parse_lrc(
        "[00:10.00]alpha beta\n[00:20.00]gamma delta\n"
        "[00:30.00]epsilon zeta\n[00:40.00]omega final\n"
    )
    recognized = [
        RecognizedWord(word, 40 + line_index * 10 + word_index * .5,
                       40 + line_index * 10 + word_index * .5 + .3, .99)
        for line_index, words in enumerate([
            ["alpha", "beta"], ["gamma", "delta"], ["epsilon", "zeta"], ["omega", "final"],
        ])
        for word_index, word in enumerate(words)
    ]
    result, report = refine_timed_document(lyrics, recognized)
    assert report.timing_anchor_lines == 4
    assert report.timing_median_shift == 30
    assert [line.start for line in result.lines] == [40, 50, 60, 70]
    assert result.metadata["audio_refined_line_indices"] == "1,2,3,4"


def test_refinement_provenance_replaces_stale_indices_when_no_candidate_is_accepted():
    lyrics = parse_lrc("[00:10.00]hello world\n")
    lyrics.metadata["audio_refined_line_indices"] = "1,2,3"
    result, _ = refine_timed_document(lyrics, [
        RecognizedWord("hello", 10, 10.2, .1), RecognizedWord("world", 11, 11.2, .1),
    ])
    assert result.metadata["audio_refined_line_indices"] == ""
    result.metadata["forced_alignment_accepted_line_indices"] = "1,2,3"
    forced, accepted = apply_forced_line_alignments(result, {})
    assert accepted == 0
    assert forced.metadata["forced_alignment_accepted_line_indices"] == ""


def test_unmatched_line_indices_refer_to_returned_document_after_empty_rows_are_filtered():
    lyrics = LyricsDocument([LyricLine("!!!"), LyricLine("missing lyric"), LyricLine("hello")])
    result, report = align_document(lyrics, [RecognizedWord("hello", 2, 3, .99)])
    assert [line.text for line in result.lines] == ["missing lyric", "hello"]
    assert report.unmatched_line_indexes == (0,)


def test_initial_alignment_preserves_hidden_rows_without_matching_or_timing_them():
    hidden = LyricLine("deleted line with many words that are not sung", hidden=True)
    lyrics = LyricsDocument([
        hidden, LyricLine("hello"), LyricLine("old section", 100, 200, hidden=True), LyricLine("world"),
    ])
    result, report = align_document(lyrics, [
        RecognizedWord("hello", 1, 1.5, .99), RecognizedWord("world", 3, 3.5, .99),
    ])
    assert report.coverage == 1
    assert report.target_units == 2
    assert result.lines[0] == hidden
    assert result.lines[2] == lyrics.lines[2]
    assert result.lines[1].start == 1
    assert result.lines[3].start == 3
    assert result.is_timed
    assert lyrics.lines[1].start is None


def test_refinement_warps_only_visible_lines_and_reports_indices_with_hidden_placeholders():
    visible = parse_lrc(
        "[00:10.00]alpha beta\n[00:20.00]gamma delta\n"
        "[00:30.00]epsilon zeta\n[00:40.00]omega final\n"
    )
    lyrics = LyricsDocument([
        LyricLine("not sung", hidden=True), visible.lines[0],
        LyricLine("old timed part", 1, 2, [KaraokeToken("old timed part", 1, 2)], hidden=True),
        *visible.lines[1:],
    ], metadata=visible.metadata)
    recognized = [
        RecognizedWord(word, 40 + line_index * 10 + word_index * .5,
                       40 + line_index * 10 + word_index * .5 + .3, .99)
        for line_index, words in enumerate([
            ["alpha", "beta"], ["gamma", "delta"], ["epsilon", "zeta"], ["omega", "final"],
        ])
        for word_index, word in enumerate(words)
    ]
    result, report = refine_timed_document(lyrics, recognized)
    assert report.coverage == 1
    assert result.lines[0] == lyrics.lines[0]
    assert result.lines[2] == lyrics.lines[2]
    assert [line.start for line in result.visible_lines] == [40, 50, 60, 70]
    assert result.metadata["audio_refined_line_indices"] == "2,4,5,6"


def test_unmatched_indices_account_for_preserved_hidden_rows_and_removed_punctuation():
    lyrics = LyricsDocument([
        LyricLine("hidden", hidden=True), LyricLine("!!!"), LyricLine("missing lyric"), LyricLine("hello"),
    ])
    result, report = align_document(lyrics, [RecognizedWord("hello", 2, 3, .99)])
    assert [line.text for line in result.lines] == ["hidden", "missing lyric", "hello"]
    assert report.unmatched_line_indexes == (1,)


def test_large_overlap_repairs_are_marked_as_estimated_instead_of_trusted_timing():
    result, _ = align_document(parse_plain("one two"), [
        RecognizedWord("one", 1, 3, .99), RecognizedWord("two", 2, 4, .99),
    ])
    assert result.lines[0].tokens[0].confidence == .99
    assert result.lines[0].tokens[1].start == 3
    assert result.lines[0].tokens[1].confidence is None


def test_new_alignment_does_not_carry_old_refinement_and_drift_claims():
    lyrics = parse_plain("hello")
    lyrics.metadata.update({
        "audio_refined_line_indices": "1,2", "audio_refined_lines": "2",
        "timeline_correction": "piecewise-audio-drift", "timeline_max_shift": "30",
    })
    result, _ = align_document(lyrics, [RecognizedWord("hello", 1, 2, .99)])
    assert "audio_refined_line_indices" not in result.metadata
    assert "audio_refined_lines" not in result.metadata
    assert "timeline_correction" not in result.metadata
    assert "timeline_max_shift" not in result.metadata


def test_alignment_keeps_user_lyrics_and_builds_timeline() -> None:
    lyrics = parse_plain("你好，世界！\nHello world.")
    recognized = [
        RecognizedWord("啦", 0.2, 0.4, 0.5),
        RecognizedWord("你好", 1.0, 1.6, 0.95),
        RecognizedWord("世界", 1.8, 2.4, 0.94),
        RecognizedWord("Hello", 3.0, 3.5, 0.98),
        RecognizedWord("word", 3.6, 4.0, 0.90),
    ]

    document, report = align_document(lyrics, recognized)

    assert [line.text for line in document.lines] == ["你好，世界！", "Hello world."]
    assert document.is_timed
    assert report.matched_units == 6
    assert report.exact_units == 5
    assert report.coverage == 1.0
    assert document.lines[0].start == 1.0
    assert document.lines[1].tokens[-1].text == "world."


def test_alignment_interpolates_a_missing_word() -> None:
    lyrics = parse_plain("one two three")
    recognized = [
        RecognizedWord("one", 1.0, 1.2),
        RecognizedWord("three", 2.0, 2.3),
    ]

    document, report = align_document(lyrics, recognized)

    assert report.coverage == 2 / 3
    starts = [token.start for token in document.lines[0].tokens]
    assert starts == sorted(starts)
    assert 1.0 < starts[1] < 2.0


def test_low_coverage_recovery_builds_an_editable_fallback_timeline() -> None:
    lyrics = parse_plain("completely different\nsecond lyric line")
    recognized = [
        RecognizedWord("无法匹配", 2.0, 3.0, 0.8),
        RecognizedWord("另一种语言", 5.0, 6.0, 0.8),
    ]

    document, report = align_document(
        lyrics,
        recognized,
        minimum_coverage=0.2,
        allow_low_coverage=True,
    )

    assert report.coverage == 0.0
    assert report.unmatched_line_indexes == (0, 1)
    assert document.is_timed
    assert [line.text for line in document.lines] == [
        "completely different",
        "second lyric line",
    ]
    assert document.lines[0].start == 2.0
    assert document.lines[-1].end is not None
    assert document.lines[-1].end >= 6.0


def test_refinement_preserves_line_boundaries_but_follows_singing_speed() -> None:
    lyrics = parse_lrc("[00:01.00]one two three\n[00:03.00]last line\n")
    original_start = lyrics.lines[0].start
    original_end = lyrics.lines[0].end
    recognized = [
        RecognizedWord("one", 1.0, 1.1),
        RecognizedWord("two", 1.2, 1.4),
        RecognizedWord("three", 2.5, 2.9),
        RecognizedWord("last", 3.0, 3.2),
        RecognizedWord("line", 3.5, 3.9),
    ]

    refined, report = refine_timed_document(lyrics, recognized)

    line = refined.lines[0]
    assert line.start == original_start
    assert line.end == original_end
    assert report.coverage == 1.0
    assert line.tokens[2].start - line.tokens[1].start > 0.8
    assert refined.metadata["word_timing"] == "audio-refined"


def test_refinement_preserves_timed_instrumental_breaks() -> None:
    lyrics = parse_lrc("[00:01.00]first line\n[00:02.00]\n[00:03.00]last line\n")
    break_start = lyrics.lines[1].start
    break_end = lyrics.lines[1].end
    recognized = [
        RecognizedWord("first", 1.0, 1.3),
        RecognizedWord("line", 1.4, 1.8),
        RecognizedWord("last", 3.0, 3.3),
        RecognizedWord("line", 3.4, 3.8),
    ]

    refined, report = refine_timed_document(lyrics, recognized)

    assert report.coverage == 1.0
    assert len(refined.lines) == len(lyrics.lines)
    assert refined.lines[1].text == ""
    assert refined.lines[1].start == break_start
    assert refined.lines[1].end == break_end
    assert refined.lines[1].tokens == []


def test_protected_refinement_keeps_source_timing_on_large_disagreement() -> None:
    lyrics = parse_yrc("[1000,2000](1000,500,0)one (1500,1500,0)two\n")
    original = [(token.start, token.end) for token in lyrics.lines[0].tokens]
    recognized = [
        RecognizedWord("one", 1.70, 2.00, 0.99),
        RecognizedWord("two", 2.40, 2.80, 0.99),
    ]

    refined, report = refine_timed_document(
        lyrics,
        recognized,
        protect_existing_word_timing=True,
    )

    assert report.coverage == 1.0
    assert [(token.start, token.end) for token in refined.lines[0].tokens] == original
    assert refined.metadata["word_timing"] == "source"
    assert refined.metadata["audio_refined_lines"] == "0"


def test_refinement_matches_repeated_lines_inside_their_own_windows() -> None:
    lyrics = parse_lrc("[00:01.00]same words\n[00:05.00]same words\n")
    recognized = [
        RecognizedWord("same", 1.20, 1.50, 0.95),
        RecognizedWord("words", 1.60, 2.00, 0.95),
        RecognizedWord("same", 5.30, 5.60, 0.95),
        RecognizedWord("words", 5.80, 6.20, 0.95),
    ]

    refined, _report = refine_timed_document(lyrics, recognized)

    assert refined.lines[0].tokens[0].start == 1.20
    assert refined.lines[1].tokens[0].start == 5.30


def test_refinement_keeps_crowded_tokens_strictly_monotonic() -> None:
    lyrics = LyricsDocument(
        lines=[LyricLine(text="one two three", start=0.0, end=1.0)],
        metadata={"word_timing": "synthetic"},
        source_format="lrc",
    )
    recognized = [
        RecognizedWord("one", 0.95, 1.00, 0.95),
        RecognizedWord("two", 0.97, 1.00, 0.95),
        RecognizedWord("three", 0.99, 1.00, 0.95),
    ]

    refined, _report = refine_timed_document(lyrics, recognized)
    tokens = refined.lines[0].tokens

    assert len(tokens) == 3
    assert all(right.start >= left.end for left, right in pairwise(tokens))
    assert tokens[-1].end <= 1.0


def test_protected_refinement_keeps_source_when_tokenization_differs() -> None:
    lyrics = parse_yrc("[1000,1000](1000,1000,0)Hello world\n")
    original = [(token.text, token.start, token.end) for token in lyrics.lines[0].tokens]
    recognized = [RecognizedWord("Hello world", 1.05, 1.95, 0.99)]

    refined, _report = refine_timed_document(
        lyrics,
        recognized,
        protect_existing_word_timing=True,
    )

    assert [(token.text, token.start, token.end) for token in refined.lines[0].tokens] == original
    assert refined.metadata["word_timing"] == "source"


def test_protected_refinement_rejects_one_locally_crushed_tail() -> None:
    pieces = "".join(
        f"({index * 100},{100},0){character}" for index, character in enumerate("abcdefghij")
    )
    lyrics = parse_yrc(f"[0,1000]{pieces}\n")
    original = [(token.start, token.end) for token in lyrics.lines[0].tokens]
    recognized = [
        RecognizedWord(character, index * 0.1, (index + 1) * 0.1, 0.99)
        for index, character in enumerate("abcdef")
    ] + [RecognizedWord("g", 0.6, 0.95, 0.99)]

    refined, _report = refine_timed_document(
        lyrics,
        recognized,
        protect_existing_word_timing=True,
    )

    assert [(token.start, token.end) for token in refined.lines[0].tokens] == original
    assert refined.metadata["word_timing"] == "source"
    assert refined.metadata["audio_refined_lines"] == "0"


def test_refinement_corrects_piecewise_drift_in_synthetic_line_timing() -> None:
    lyrics = parse_lrc(
        "[00:10.00]alpha beta\n"
        "[00:20.00]gamma delta\n"
        "[00:30.00]epsilon zeta\n"
        "[00:40.00]omega final\n"
    )
    recognized = [
        RecognizedWord("alpha", 10.0, 10.3, 0.98),
        RecognizedWord("beta", 10.5, 10.8, 0.98),
        RecognizedWord("gamma", 20.5, 20.8, 0.98),
        RecognizedWord("delta", 21.0, 21.3, 0.98),
        RecognizedWord("epsilon", 31.2, 31.5, 0.98),
        RecognizedWord("zeta", 31.8, 32.1, 0.98),
        RecognizedWord("omega", 42.0, 42.3, 0.98),
        RecognizedWord("final", 42.6, 42.9, 0.98),
    ]

    refined, report = refine_timed_document(lyrics, recognized)

    assert [round(line.start or 0.0, 1) for line in refined.lines] == [10.0, 20.5, 31.2, 42.0]
    assert report.timing_anchor_lines == 4
    assert report.timing_max_shift == 2.0
    assert refined.metadata["timeline_correction"] == "piecewise-audio-drift"
    assert refined.metadata["timeline_anchor_lines"] == "4"


def test_synthetic_refinement_preserves_a_line_with_only_one_matched_word() -> None:
    lyrics = parse_lrc(
        "[00:01.00]one two three four\n"
        "[00:05.00]five six seven eight\n"
    )
    original = [(token.start, token.end) for token in lyrics.lines[0].tokens]
    recognized = [
        RecognizedWord("one", 1.3, 1.6, 0.98),
        RecognizedWord("five", 5.2, 5.5, 0.98),
        RecognizedWord("six", 5.6, 5.9, 0.98),
        RecognizedWord("seven", 6.0, 6.3, 0.98),
        RecognizedWord("eight", 6.4, 6.7, 0.98),
    ]

    refined, report = refine_timed_document(lyrics, recognized)

    assert report.coverage == 5 / 8
    assert [(token.start, token.end) for token in refined.lines[0].tokens] == original
    assert refined.metadata["audio_refined_lines"] == "1"
    assert refined.metadata["audio_preserved_lines"] == "1"


def test_sparse_trusted_matches_do_not_collapse_a_complete_detected_timeline() -> None:
    words = [f"word{index}" for index in range(20)]
    lyrics = parse_plain(" ".join(words))
    recognized = [
        RecognizedWord(word, float(index), index + 0.2, 0.98 if index == 10 else 0.10)
        for index, word in enumerate(words)
    ]

    aligned, report = align_document(lyrics, recognized)

    assert report.coverage == 1.0
    assert report.trusted_timing_units == 1
    assert aligned.lines[0].tokens[0].start == 0.0
    assert aligned.lines[0].tokens[-1].start == 19.0


def test_low_confidence_exact_text_is_interpolated_between_representative_anchors() -> None:
    lyrics = parse_plain("alpha beta gamma")
    recognized = [
        RecognizedWord("alpha", 1.0, 1.2, 0.98),
        RecognizedWord("beta", 1.3, 1.5, 0.10),
        RecognizedWord("gamma", 3.0, 3.3, 0.98),
    ]

    aligned, report = align_document(lyrics, recognized)

    assert report.trusted_timing_units == 2
    assert aligned.lines[0].tokens[1].start > 1.8


def test_two_extreme_anchors_do_not_enable_timeline_stretching() -> None:
    lyrics = parse_lrc("[00:00.00]alpha\n[00:10.00]beta\n")
    recognized = [
        RecognizedWord("alpha", 0.0, 0.3, 0.98),
        RecognizedWord("beta", 50.0, 50.3, 0.98),
    ]

    refined, report = refine_timed_document(lyrics, recognized)

    assert report.timing_anchor_lines == 0
    assert [line.start for line in refined.lines] == [0.0, 10.0]
    assert "timeline_correction" not in refined.metadata


def test_one_bad_endpoint_is_excluded_from_a_plausible_anchor_chain() -> None:
    lyrics = parse_lrc(
        "[00:00.00]alpha\n"
        "[00:10.00]beta\n"
        "[00:20.00]gamma\n"
        "[00:30.00]delta\n"
    )
    recognized = [
        RecognizedWord("alpha", 0.0, 0.3, 0.98),
        RecognizedWord("beta", 10.0, 10.3, 0.98),
        RecognizedWord("gamma", 20.0, 20.3, 0.98),
        RecognizedWord("delta", 80.0, 80.3, 0.98),
    ]

    refined, report = refine_timed_document(lyrics, recognized)

    assert report.timing_anchor_lines == 3
    assert [line.start for line in refined.lines] == [0.0, 10.0, 20.0, 30.0]


def test_overlapping_asr_words_are_normalized_to_a_monotonic_timeline() -> None:
    lyrics = parse_plain("one two three")
    recognized = [
        RecognizedWord("one", 1.0, 1.3, 0.95),
        RecognizedWord("two", 1.2, 1.25, 0.95),
        RecognizedWord("three", 1.2, 1.4, 0.95),
    ]

    aligned, _report = align_document(lyrics, recognized)
    tokens = aligned.lines[0].tokens

    assert all(right.start >= left.end for left, right in pairwise(tokens))
    assert tokens[0].start == 1.0
    assert tokens[-1].end >= tokens[-1].start + 0.01


def test_forced_line_alignment_only_changes_the_reliable_line() -> None:
    lyrics = LyricsDocument(
        lines=[
            LyricLine(text="alpha beta", start=0.0, end=2.0),
            LyricLine(text="gamma delta", start=3.0, end=5.0),
        ],
        metadata={"word_timing": "synthetic"},
    )
    recognized_by_line = {
        0: [
            RecognizedWord("alpha", 0.20, 0.55, 0.96),
            RecognizedWord("beta", 0.85, 1.20, 0.94),
        ],
        1: [
            RecognizedWord("unrelated", 3.20, 3.50, 0.99),
            RecognizedWord("words", 3.70, 4.00, 0.99),
        ],
    }

    refined, accepted = apply_forced_line_alignments(lyrics, recognized_by_line)

    assert accepted == 1
    assert [token.start for token in refined.lines[0].tokens] == [0.20, 0.85]
    assert refined.lines[1].tokens == []
    assert lyrics.lines[0].tokens == []
    assert refined.metadata["forced_alignment_accepted_lines"] == "1"
    assert refined.metadata["word_timing"] == "audio-forced"


def test_forced_line_alignment_rejects_low_confidence_exact_words() -> None:
    lyrics = LyricsDocument(
        lines=[LyricLine(text="alpha beta", start=1.0, end=3.0)],
        metadata={"word_timing": "synthetic"},
    )
    recognized_by_line = {
        0: [
            RecognizedWord("alpha", 1.20, 1.50, 0.10),
            RecognizedWord("beta", 1.80, 2.10, 0.10),
        ]
    }

    refined, accepted = apply_forced_line_alignments(lyrics, recognized_by_line)

    assert accepted == 0
    assert refined.lines[0].tokens == []
    assert refined.metadata["forced_alignment_accepted_lines"] == "0"
    assert refined.metadata["word_timing"] == "synthetic"


def test_forced_alignment_treats_overlapping_lines_as_independent_timelines() -> None:
    lyrics = LyricsDocument(
        lines=[
            LyricLine(text="alpha beta", start=0.0, end=2.0),
            LyricLine(text="gamma delta", start=0.0, end=2.0),
        ]
    )
    recognized_by_line = {
        0: [
            RecognizedWord("alpha", 0.20, 0.50, 0.98),
            RecognizedWord("beta", 1.20, 1.50, 0.98),
        ],
        1: [
            RecognizedWord("gamma", 0.30, 0.60, 0.98),
            RecognizedWord("delta", 0.80, 1.10, 0.98),
        ],
    }

    refined, accepted = apply_forced_line_alignments(lyrics, recognized_by_line)

    assert accepted == 2
    assert refined.lines[0].tokens[-1].end == 1.50
    assert refined.lines[1].tokens[0].start == 0.30
    assert refined.lines[1].tokens[0].start < refined.lines[0].tokens[-1].end
