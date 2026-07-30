from karaoke_forge.ass import AssStyle, write_ass
from karaoke_forge.formats import (
    attach_lrc_translation,
    attach_reference_translation,
    parse_lrc,
    parse_srt,
    parse_yrc,
    write_json,
    write_lrc,
    write_srt,
)


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
    assert "Dialogue: 0,0:00:01.00,0:00:07.00,KaraokeInactive" in output
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
