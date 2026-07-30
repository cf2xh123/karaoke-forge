from karaoke_forge.align import RecognizedWord, align_document
from karaoke_forge.formats import parse_plain


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
