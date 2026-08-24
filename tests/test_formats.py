import re
from itertools import pairwise

from karaoke_forge.ass import AssStyle, write_ass
from karaoke_forge.formats import (
    attach_lrc_translation,
    attach_reference_translation,
    parse_ass,
    parse_lrc,
    parse_srt,
    parse_yrc,
    write_json,
    write_lrc,
    write_srt,
)
from karaoke_forge.models import KaraokeToken, LyricLine, LyricsDocument, PronunciationSpan
from karaoke_forge.timecode import parse_clock


def _dialogue_rows(output: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in output.splitlines():
        if not row.startswith("Dialogue:"):
            continue
        fields = row.split(":", 1)[1].lstrip().split(",", 9)
        rows.append(
            {
                "layer": int(fields[0]),
                "start": parse_clock(fields[1]),
                "end": parse_clock(fields[2]),
                "style": fields[3],
                "text": fields[9],
            }
        )
    return rows


def test_yrc_preserves_non_uniform_word_timing() -> None:
    document = parse_yrc(
        "[70,6390](70,720,0)吐(790,130,0)い(920,1650,0)た"
        "(2570,160,0) (2730,510,0)息(3240,110,0)は\n"
    )

    line = document.lines[0]
    assert line.text == "吐いた 息は"
    assert line.start == 0.07
    assert line.end == 6.46
    assert document.metadata["word_timing"] == "source"
    assert [token.start for token in line.tokens] == [0.07, 0.79, 0.92, 2.57, 2.73, 3.24]
    assert round(line.tokens[2].end - line.tokens[2].start, 2) == 1.65


def test_yrc_translation_is_fuzzy_aligned_through_original_lrc() -> None:
    document = parse_yrc(
        "[1000,900](1000,200,0)I(1200,300,0) wish(1500,400,0) you\n"
        "[3000,1000](3000,500,0)魂(3500,100,0)が(3600,400,0)揺れる\n"
    )
    attached = attach_reference_translation(
        document,
        "[00:02.20]I wish you\n[00:04.10]魂が揺れる\n",
        "[00:02.20]我希望你\n[00:04.10]灵魂摇摆\n",
    )

    assert attached == 2
    assert [line.translation for line in document.lines] == ["我希望你", "灵魂摇摆"]


def test_enhanced_lrc_parsing_and_export() -> None:
    document = parse_lrc(
        "[ar:Example]\n"
        "[00:01.00]<00:01.000>你<00:01.400>好\n"
        "[00:02.00]<00:02.000>Hello <00:02.500>world\n"
    )

    assert document.is_timed
    assert document.metadata["ar"] == "Example"
    assert document.lines[0].text == "你好"
    assert len(document.lines[0].tokens) == 2
    assert "<00:01.000>你" in write_lrc(document, enhanced=True)
    assert '"version": 1' in write_json(document)


def test_plain_lrc_gets_evenly_distributed_karaoke_tokens() -> None:
    document = parse_lrc("[00:01.00]Hello world\n[00:03.00]Next line\n")

    assert [token.text for token in document.lines[0].tokens] == ["Hello ", "world"]
    assert document.lines[0].tokens[0].start == 1.0
    assert document.lines[0].tokens[-1].end == document.lines[0].end
    assert r"{\kf" in write_ass(document)


def test_srt_round_trip() -> None:
    source = "1\n00:00:01,000 --> 00:00:02,500\nHello world\n"
    document = parse_srt(source)
    output = write_srt(document)

    assert "00:00:01,000 --> 00:00:02,500" in output
    assert document.lines[0].tokens


def test_ass_has_karaoke_tags_and_style() -> None:
    document = parse_srt("1\n00:00:01,000 --> 00:00:03,000\n你好 world\n")
    output = write_ass(document, AssStyle(font="Noto Sans CJK SC"))

    assert "Style: Karaoke,Noto Sans CJK SC" in output
    assert r"{\kf" in output
    assert "KaraokeInactive" in output
    assert r"{\kf67}你" in output


def test_ass_round_trip_preserves_detected_pauses_between_words() -> None:
    document = LyricsDocument(
        lines=[
            LyricLine(
                text="one two three",
                start=1.0,
                end=4.0,
                tokens=[
                    KaraokeToken("one ", 1.0, 1.1),
                    KaraokeToken("two ", 1.2, 1.4),
                    KaraokeToken("three", 2.5, 2.9),
                ],
            )
        ]
    )

    output = write_ass(document, AssStyle(show_pronunciation=False))
    restored = parse_ass(output)

    assert (
        r"{\kf10}one{\k0} {\k10}{\kf20}two{\k0} {\k110}{\kf40}three"
        in output
    )
    assert [token.text for token in restored.lines[0].tokens] == ["one ", "two ", "three"]
    assert [round(token.start, 2) for token in restored.lines[0].tokens] == [1.0, 1.2, 2.5]
    assert [round(token.end, 2) for token in restored.lines[0].tokens] == [1.1, 1.4, 2.9]


def test_ass_preserves_a_delayed_first_word_and_uses_token_timing_for_reading() -> None:
    document = LyricsDocument(
        lines=[
            LyricLine(
                text="one two",
                start=1.0,
                end=3.0,
                tokens=[
                    KaraokeToken("one ", 1.4, 1.7),
                    KaraokeToken("two", 2.2, 2.6),
                ],
                pronunciation_units=[PronunciationSpan("two", "トゥー", 4, 7)],
            )
        ]
    )

    output = write_ass(document)
    restored = parse_ass(output)

    assert r"{\k40}{\kf30}one{\k0} {\k50}{\kf40}two" in output
    assert r"{\k120}{\kf40}トゥー" in output
    assert [token.start for token in restored.lines[0].tokens] == [1.4, 2.2]


def test_generated_ass_round_trip_ignores_preview_and_pronunciation_events() -> None:
    document = parse_lrc("[00:01.00]Hello world\n[00:03.00]Next line\n")
    document.lines[0].translation = "你好，世界"
    document.lines[0].pronunciation = "ハロー ワールド"

    restored = parse_ass(write_ass(document))

    assert [line.text for line in restored.lines] == ["Hello world", "Next line"]
    assert restored.lines[0].translation == "你好，世界"
    assert restored.lines[0].pronunciation == "ハロー ワールド"
    assert restored.metadata["word_timing"] == "source"
    assert [token.text for token in restored.lines[0].tokens] == ["Hello ", "world"]
    assert restored.lines[0].tokens[0].start == 1.0
    assert restored.lines[0].tokens[1].start == 1.99


def test_translation_uses_top_center_split_ktv_layout() -> None:
    document = parse_lrc("[00:01.00]Hello world\n[00:03.00]Next line\n")
    document.lines[0].pronunciation = "ハロー　ワールド"
    document.lines[1].pronunciation = "ネクスト　ライン"
    count = attach_lrc_translation(
        document,
        "[00:01.00]你好，世界\n[00:03.00]下一句\n",
    )

    output = write_ass(
        document,
        AssStyle(font="Microsoft YaHei", translation_color="#AADDFF"),
    )

    assert count == 2
    assert "Style: Translation,Microsoft YaHei,38" in output
    assert ",8,60,60,54,1" in output
    assert "Style: KaraokeLower,Microsoft YaHei" in output
    assert "Style: KaraokeInactive,Microsoft YaHei" in output
    assert "Dialogue: 0,0:00:01.00,0:00:03.00,KaraokeLowerInactive" in output
    assert "Dialogue: 0,0:00:01.00,0:00:03.00,KaraokeInactive" not in output
    assert "Dialogue: 3,0:00:01.00" in output
    assert "你好，世界" in output
    assert "Dialogue: 1,0:00:01.00" in output
    assert "Dialogue: 1,0:00:03.00,0:00:07.00,KaraokeLower" in output
    assert "Style: Pronunciation,Microsoft YaHei,26" in output
    assert "Dialogue: 2,0:00:01.00,0:00:02.98,Pronunciation" in output
    assert "ハロー　ワールド" in output
    assert '"translation": "你好，世界"' in write_json(document)
    assert '"pronunciation": "ハロー　ワールド"' in write_json(document)
    assert "你好，世界\nHello world" in write_srt(document)


def test_long_instrumental_break_clears_lyrics_and_cues_the_next_line() -> None:
    document = LyricsDocument(
        lines=[
            LyricLine(text="First verse", start=2.0, end=5.0),
            LyricLine(text="Second verse", start=20.0, end=23.0),
        ]
    )

    output = write_ass(
        document,
        AssStyle(
            show_pronunciation=False,
            countdown_gap_threshold=8.0,
            countdown_lead_in=3.0,
        ),
    )
    dialogue_events = [
        (row["start"], row["end"], row["style"]) for row in _dialogue_rows(output)
    ]

    assert not any(start <= 10.0 < end for start, end, _style in dialogue_events)
    assert "Dialogue: 0,0:00:02.00,0:00:05.00,KaraokeLowerInactive" in output
    assert "Dialogue: 0,0:00:17.00,0:00:20.00,KaraokeLowerInactive" in output
    assert "Dialogue: 4,0:00:17.00,0:00:20.00,CountdownBackdrop" in output
    assert "Dialogue: 5,0:00:17.00,0:00:18.00,Countdown" in output
    assert "Dialogue: 5,0:00:18.00,0:00:19.00,Countdown" in output
    assert "Dialogue: 5,0:00:19.00,0:00:20.00,Countdown" in output
    assert r"\p1}m " in output
    assert "●" in output
    assert "100)}}" not in output


def test_translation_top_margin_and_countdown_can_be_configured() -> None:
    document = LyricsDocument(
        lines=[
            LyricLine(
                text="Opening",
                start=12.0,
                end=15.0,
                translation="开场",
            )
        ]
    )

    output = write_ass(
        document,
        AssStyle(
            translation_margin_v=240,
            show_countdown=False,
            countdown_gap_threshold=8.0,
            show_pronunciation=False,
        ),
    )

    assert ",8,60,60,240,1" in output
    assert "Dialogue: 4," not in output
    assert "Dialogue: 5," not in output
    assert "Dialogue: 0,0:00:09.00,0:00:12.00,KaraokeInactive" in output


def test_ass_filters_saved_english_readings_when_english_pronunciation_is_off() -> None:
    document = parse_lrc("[00:01.00]Hello 魂\n[00:03.00]English only\n")
    document.lines[0].pronunciation_units = [
        PronunciationSpan("Hello", "ハロー", 0, 5),
        PronunciationSpan("魂", "たましい", 6, 7),
    ]
    document.lines[1].pronunciation = "イングリッシュ オンリー"

    output = write_ass(document, AssStyle(auto_english_pronunciation=False))

    assert "ハロー" not in output
    assert "イングリッシュ オンリー" not in output
    assert "たましい" in output


def test_ass_rolls_one_ktv_row_at_each_new_line() -> None:
    document = parse_srt(
        "1\n00:00:01,000 --> 00:00:02,000\nA\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nB\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\nC\n\n"
        "4\n00:00:07,000 --> 00:00:08,000\nD\n"
    )

    output = write_ass(document, AssStyle(show_pronunciation=False))

    assert "Dialogue: 0,0:00:01.00,0:00:03.00,KaraokeInactive" not in output
    assert "Dialogue: 0,0:00:01.00,0:00:03.00,KaraokeLowerInactive" in output
    assert r"{\fad(120,180)}B" in output
    assert "Dialogue: 0,0:00:03.00,0:00:05.00,KaraokeInactive" in output
    assert r"{\fad(120,180)}C" in output
    assert "Dialogue: 0,0:00:05.00,0:00:07.00,KaraokeLowerInactive" in output
    assert r"{\fad(120,180)}D" in output


def test_ass_ignores_timed_blank_rows_for_events_and_ktv_parity() -> None:
    document = LyricsDocument(
        lines=[
            LyricLine(text="First", start=1.0, end=2.0),
            LyricLine(
                text=" \t ",
                start=2.0,
                end=5.0,
                translation="MUST NOT RENDER",
                pronunciation="MUST NOT RENDER",
            ),
            LyricLine(text="Second", start=5.0, end=7.0),
        ]
    )

    output = write_ass(
        document,
        AssStyle(show_countdown=False, show_pronunciation=False),
    )
    rows = _dialogue_rows(output)
    active = [row for row in rows if row["style"] in {"Karaoke", "KaraokeLower"}]

    assert [(row["style"], row["text"].split("}")[-1]) for row in active] == [
        ("Karaoke", "First"),
        ("KaraokeLower", "Second"),
    ]
    assert "MUST NOT RENDER" not in output


def test_ass_never_overlaps_active_events_on_the_same_ktv_row_or_translation() -> None:
    document = LyricsDocument(
        lines=[
            LyricLine(text="First", start=1.0, end=5.5, translation="第一"),
            LyricLine(text="Second", start=3.0, end=6.5, translation="第二"),
            LyricLine(text="Third", start=5.0, end=8.0, translation="第三"),
        ]
    )

    rows = _dialogue_rows(
        write_ass(
            document,
            AssStyle(show_countdown=False, show_pronunciation=False),
        )
    )
    for style_name in ("Karaoke", "KaraokeLower", "Translation"):
        events = sorted(
            (row for row in rows if row["style"] == style_name),
            key=lambda row: float(row["start"]),
        )
        for left, right in pairwise(events):
            assert float(left["end"]) <= float(right["start"])

    first = next(row for row in rows if row["style"] == "Karaoke" and "First" in row["text"])
    third = next(row for row in rows if row["style"] == "Karaoke" and "Third" in row["text"])
    assert first["end"] == third["start"] == 5.0
    assert not any(row["style"] == "KaraokeInactive" and "Third" in row["text"] for row in rows)


def test_inactive_lyrics_are_only_next_line_previews() -> None:
    document = LyricsDocument(
        lines=[
            LyricLine(text="A", start=1.0, end=2.0),
            LyricLine(text="B", start=3.0, end=4.0),
            LyricLine(text="C", start=5.0, end=6.0),
        ]
    )

    rows = _dialogue_rows(
        write_ass(
            document,
            AssStyle(show_countdown=False, show_pronunciation=False),
        )
    )
    inactive = [row for row in rows if str(row["style"]).endswith("Inactive")]

    assert [(row["start"], row["end"], row["text"].split("}")[-1]) for row in inactive] == [
        (1.0, 3.0, "B"),
        (3.0, 5.0, "C"),
    ]
    line_starts = {"A": 1.0, "B": 3.0, "C": 5.0}
    for row in inactive:
        assert float(row["end"]) <= line_starts[row["text"].split("}")[-1]]


def test_countdown_arrow_tracks_the_upcoming_row_and_actual_pronunciation() -> None:
    lower_document = LyricsDocument(
        lines=[
            LyricLine(text="First", start=1.0, end=3.0),
            LyricLine(text="Lower cue", start=20.0, end=23.0),
        ]
    )
    upper_document = LyricsDocument(
        lines=[
            LyricLine(text="First", start=1.0, end=3.0),
            LyricLine(text="Second", start=4.0, end=6.0),
            LyricLine(text="Upper cue", start=20.0, end=23.0),
        ]
    )

    def countdown_position(document: LyricsDocument, style: AssStyle) -> tuple[float, float, str]:
        output = write_ass(document, style)
        marker = next(row for row in _dialogue_rows(output) if row["style"] == "Countdown")
        match = re.search(r"\\pos\(([\d.]+),([\d.]+)\)", str(marker["text"]))
        assert match is not None
        return float(match.group(1)), float(match.group(2)), output

    no_reading = AssStyle(
        show_pronunciation=False,
        countdown_gap_threshold=8.0,
    )
    upper_x, upper_y, upper_output = countdown_position(upper_document, no_reading)
    lower_x, lower_y, lower_output = countdown_position(lower_document, no_reading)

    assert upper_x < 960 < lower_x
    assert upper_y < lower_y
    assert "CountdownBackdrop" in upper_output
    assert r"\p1}m " in upper_output
    assert "Dialogue: 4,0:00:17.00,0:00:20.00,CountdownBackdrop" in lower_output

    lower_document.lines[1].pronunciation = "ローワー キュー"
    _reading_x, reading_y, _reading_output = countdown_position(
        lower_document,
        AssStyle(
            show_pronunciation=True,
            auto_pronunciation=False,
            countdown_gap_threshold=8.0,
        ),
    )
    assert reading_y < lower_y


def test_countdown_requires_a_real_lyric_free_gap_across_overlapping_rows() -> None:
    document = LyricsDocument(
        lines=[
            LyricLine(text="Long upper", start=1.0, end=15.0),
            LyricLine(text="Short lower", start=3.0, end=4.0),
            LyricLine(text="Next upper", start=20.0, end=23.0),
        ]
    )

    output = write_ass(
        document,
        AssStyle(show_pronunciation=False, countdown_gap_threshold=8.0),
    )

    assert "CountdownBackdrop" in output
    assert "Dialogue: 4," not in output
    assert "Dialogue: 5," not in output
