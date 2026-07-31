from pathlib import Path

from karaoke_forge.cli import build_parser, main


def test_convert_command(tmp_path: Path) -> None:
    source = tmp_path / "input.srt"
    output = tmp_path / "output.vtt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHello\n",
        encoding="utf-8",
    )

    result = main(["convert", str(source), "-o", str(output)])

    assert result == 0
    assert output.read_text(encoding="utf-8").startswith("WEBVTT")


def test_align_make_and_netease_share_timing_refinement_choices() -> None:
    parser = build_parser()

    align = parser.parse_args(
        [
            "align",
            "song.mp3",
            "lyrics.lrc",
            "--timing-refinement",
            "auto",
        ]
    )
    make = parser.parse_args(
        [
            "make",
            "song.mp3",
            "mv.mp4",
            "lyrics.lrc",
            "-o",
            "out.mp4",
            "--timing-refinement",
            "force",
        ]
    )
    netease = parser.parse_args(
        [
            "netease",
            "https://music.163.com/song?id=1",
            "--i-have-rights",
            "--timing-refinement",
            "off",
        ]
    )

    assert align.timing_refinement == "auto"
    assert make.timing_refinement == "force"
    assert netease.timing_refinement == "off"


def test_align_off_preserves_timed_lyrics_without_whisper(tmp_path: Path) -> None:
    audio = tmp_path / "song.wav"
    lyrics = tmp_path / "lyrics.lrc"
    output = tmp_path / "build"
    audio.write_bytes(b"placeholder")
    lyrics.write_text("[00:01.00]Hello\n[00:03.00]World\n", encoding="utf-8")

    result = main(
        [
            "align",
            str(audio),
            str(lyrics),
            "--timing-refinement",
            "off",
            "--formats",
            "lrc,json",
            "-o",
            str(output),
        ]
    )

    assert result == 0
    assert (output / "lyrics.lrc").read_text(encoding="utf-8").startswith("[00:01.00]")
