from pathlib import Path
from types import SimpleNamespace

from karaoke_forge.netease import NeteaseSongInfo
from karaoke_forge.web import (
    _safe_stem,
    environment_markdown,
    run_align_job,
    run_convert_job,
    run_make_job,
)


def test_safe_stem_removes_windows_path_characters() -> None:
    assert _safe_stem("  my:karaoke*video?.mp4  ") == "my-karaoke-video"
    assert _safe_stem("", fallback="song") == "song"


def test_web_convert_job_exports_downloadable_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    source = tmp_path / "lyrics.lrc"
    source.write_text("[00:01.00]Hello world\n", encoding="utf-8")

    result = run_convert_job(str(source), "srt")

    assert result.video is None
    assert len(result.files) == 1
    assert Path(result.files[0]).is_file()
    assert "✅" in result.status


def test_web_align_job_skips_recognition_for_timed_lyrics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"placeholder")
    lyrics = tmp_path / "lyrics.lrc"
    lyrics.write_text("[00:01.00]Hello\n[00:02.00]World\n", encoding="utf-8")

    result = run_align_job(
        str(audio),
        str(lyrics),
        "",
        "demo",
        "en",
        "small",
        "cpu",
        False,
    )

    assert len(result.files) == 6
    assert all(Path(path).is_file() for path in result.files)
    assert "已有时间轴" in result.status


def test_environment_report_mentions_local_processing() -> None:
    report = environment_markdown()
    assert "FFmpeg" in report
    assert "素材不会自动上传到公网" in report


def test_make_job_can_use_netease_page_lyrics_with_local_audio(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    audio = tmp_path / "authorized.flac"
    video = tmp_path / "mv.mp4"
    audio.write_bytes(b"audio")
    video.write_bytes(b"video")
    info = NeteaseSongInfo(
        song_id="42",
        title="Linked Song",
        artists=("Artist",),
        canonical_url="https://music.163.com/song?id=42",
        page_lyrics="[00:01.00]Hello\n[00:02.00]World\n",
    )
    monkeypatch.setattr(
        "karaoke_forge.web.fetch_public_netease_info",
        lambda _link: info,
    )

    def fake_make(
        _audio,
        _video,
        lyrics,
        output,
        assets,
        **_kwargs,
    ):
        assert Path(lyrics).read_text(encoding="utf-8") == info.page_lyrics
        output = Path(output)
        output.write_bytes(b"rendered")
        assets = Path(assets)
        assets.mkdir(parents=True)
        exported = assets / "lyrics.lrc"
        exported.write_text(info.page_lyrics or "", encoding="utf-8")
        return SimpleNamespace(
            video=output,
            exports={"lrc": exported},
            alignment_report=None,
        )

    monkeypatch.setattr("karaoke_forge.web.make_karaoke_video", fake_make)
    result = run_make_job(
        str(audio),
        str(video),
        None,
        "",
        "linked-karaoke",
        "自动识别",
        "small",
        "auto",
        False,
        "快速预览",
        0.0,
        "Microsoft YaHei",
        58,
        "#FFFFFF",
        "#FFD54A",
        72,
        info.canonical_url,
        True,
        True,
    )

    assert result.video is not None
    assert "已生成" in result.status
    assert "仅从网易云读取" in result.log
