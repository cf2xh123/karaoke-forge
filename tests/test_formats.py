from karaoke_forge.ass import AssStyle, write_ass
from karaoke_forge.formats import parse_lrc, parse_srt, write_json, write_lrc, write_srt


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
    assert "你好 world" not in output
