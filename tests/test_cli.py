from pathlib import Path
from types import SimpleNamespace

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


def test_qqmusic_command_accepts_single_song_links() -> None:
    args = build_parser().parse_args(
        [
            "qqmusic",
            "https://y.qq.com/n/ryqq_v2/songDetail/001gQnW91BEDaN",
            "--i-have-rights",
        ]
    )

    assert args.command == "qqmusic"
    assert args.i_have_rights


def test_utaten_command_accepts_lyric_page_links() -> None:
    args = build_parser().parse_args(
        [
            "utaten",
            "https://utaten.com/lyric/yh15042710/",
            "--i-have-rights",
        ]
    )

    assert args.command == "utaten"
    assert args.i_have_rights


def test_model_download_command_exposes_safe_beginner_modes() -> None:
    args = build_parser().parse_args(
        [
            "model-download",
            "--mode",
            "proxy",
            "--proxy-url",
            "http://127.0.0.1:7890",
            "--download-model",
            "fast",
        ]
    )

    assert args.mode == "proxy"
    assert args.proxy_url == "http://127.0.0.1:7890"
    assert args.download_model == "fast"


def test_model_download_cli_requires_explicit_mirror_confirmation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path))

    result = main(["model-download", "--mode", "mirror"])

    assert result == 2
    assert not (tmp_path / "model-download.json").exists()


def test_model_download_cli_prefetches_selected_profile(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path))
    monkeypatch.setattr("karaoke_forge.cli._test_and_print_model_network", lambda *_a, **_k: True)
    received: list[str] = []
    monkeypatch.setattr(
        "karaoke_forge.cli.predownload_faster_whisper_model",
        lambda model, **_kwargs: received.append(model) or (tmp_path / model),
    )

    result = main(
        [
            "model-download",
            "--mode",
            "official",
            "--download-model",
            "balanced",
        ]
    )

    assert result == 0
    assert received == ["large-v3-turbo"]


def test_make_command_can_disable_only_automatic_english_pronunciation() -> None:
    args = build_parser().parse_args(
        [
            "make",
            "song.mp3",
            "mv.mp4",
            "lyrics.lrc",
            "-o",
            "out.mp4",
            "--no-auto-english-pronunciation",
        ]
    )

    assert not args.auto_english_pronunciation
    assert args.show_pronunciation


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


def test_make_summary_reports_timing_refinement_fallback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "karaoke_forge.cli.make_karaoke_video",
        lambda *_args, **_kwargs: SimpleNamespace(
            timing_refinement_warning="自动精修未完成；已保留原时间轴。",
            alignment_skipped=True,
            alignment_report=None,
            exports={},
            video=Path("out.mp4"),
        ),
    )

    result = main(
        [
            "make",
            "song.mp3",
            "mv.mp4",
            "lyrics.lrc",
            "-o",
            "out.mp4",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "Warning: 自动精修未完成" in output
    assert "alignment was skipped" not in output


def test_netease_summary_reports_timing_refinement_fallback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "karaoke_forge.netease.align_netease_song",
        lambda *_args, **_kwargs: SimpleNamespace(
            track=SimpleNamespace(
                title="Song",
                artist_text="Artist",
                access_text=None,
            ),
            timing_refinement_warning="自动精修未完成；已保留原时间轴。",
            alignment_report=None,
            exports={},
            kept_audio=None,
        ),
    )

    result = main(
        [
            "netease",
            "https://music.163.com/song?id=1",
            "--i-have-rights",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "Warning: 自动精修未完成" in output
    assert "alignment was skipped" not in output
