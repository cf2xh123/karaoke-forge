from karaoke_forge.align import RecognizedWord, align_document, refine_timed_document
from karaoke_forge.formats import parse_lrc, parse_plain, parse_yrc
from karaoke_forge.models import LyricLine, LyricsDocument


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
    assert all(right.start >= left.end for left, right in zip(tokens, tokens[1:]))
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
