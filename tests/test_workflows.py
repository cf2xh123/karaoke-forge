from pathlib import Path

from karaoke_forge.workflows import MakeOptions, make_karaoke_video


def test_make_skips_auto_sync_when_video_has_no_audio(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.m4a"
    video = tmp_path / "silent.webm"
    lyrics = tmp_path / "lyrics.lrc"
    output = tmp_path / "karaoke.mp4"
    assets = tmp_path / "assets"
    audio.write_bytes(b"audio")
    video.write_bytes(b"video only")
    lyrics.write_text("[00:01.00]Hello\n[00:03.00]World\n", encoding="utf-8")
    messages: list[str] = []

    monkeypatch.setattr("karaoke_forge.workflows.probe_media_has_audio", lambda _path: False)

    def unexpected_sync(*_args, **_kwargs):
        raise AssertionError("audio fingerprint sync should not run for a silent video")

    monkeypatch.setattr("karaoke_forge.workflows.detect_audio_sync", unexpected_sync)

    def fake_render(_video, _ass, target, **kwargs):
        assert Path(kwargs["audio_path"]) == audio
        assert kwargs["audio_offset"] == 0.35
        target = Path(target)
        target.write_bytes(b"rendered")
        return target

    monkeypatch.setattr("karaoke_forge.workflows.render_karaoke_video", fake_render)

    result = make_karaoke_video(
        audio,
        video,
        lyrics,
        output,
        assets,
        options=MakeOptions(
            auto_sync=True,
            audio_offset=0.35,
            timing_refinement="off",
        ),
        progress=messages.append,
    )

    assert result.video == output
    assert result.audio_offset == 0.35
    assert result.sync_result is None
    assert any("MV 没有内嵌音轨" in message for message in messages)
    assert any("保留 +0.35 秒手动偏移" in message for message in messages)


def test_make_reports_auto_refinement_fallback(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.m4a"
    video = tmp_path / "mv.mp4"
    lyrics = tmp_path / "lyrics.lrc"
    output = tmp_path / "karaoke.mp4"
    audio.write_bytes(b"audio")
    video.write_bytes(b"video")
    lyrics.write_text("[00:01.00]Hello\n", encoding="utf-8")
    monkeypatch.setattr(
        "karaoke_forge.workflows.refine_audio_word_timing_with_fallback",
        lambda *_args, **_kwargs: None,
    )

    def fake_render(_video, _ass, target, **_kwargs):
        target = Path(target)
        target.write_bytes(b"rendered")
        return target

    monkeypatch.setattr("karaoke_forge.workflows.render_karaoke_video", fake_render)

    result = make_karaoke_video(
        audio,
        video,
        lyrics,
        output,
        tmp_path / "assets",
    )

    assert result.video == output
    assert result.alignment_skipped
    assert result.timing_refinement_warning is not None
    assert "已保留原时间轴" in result.timing_refinement_warning


def test_make_uses_spinning_cover_when_video_is_missing(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.m4a"
    cover = tmp_path / "cover.jpg"
    lyrics = tmp_path / "lyrics.lrc"
    output = tmp_path / "karaoke.mp4"
    assets = tmp_path / "assets"
    font = tmp_path / "pretty.otf"
    for path in (audio, cover, font):
        path.write_bytes(b"asset")
    lyrics.write_text("[00:01.00]Hello\n", encoding="utf-8")

    def fake_cover(_image, _audio, target, **kwargs):
        assert kwargs["background_theme"] == "paper"
        assert kwargs["style"] == "spectrum"
        assert kwargs["show_waveform"] is False
        target = Path(target)
        target.write_bytes(b"background")
        return target

    def fake_render(video, _ass, target, **kwargs):
        assert Path(video).name == "spinning-cover-background.mp4"
        assert kwargs["font_files"] == (font,)
        target = Path(target)
        target.write_bytes(b"rendered")
        return target

    monkeypatch.setattr("karaoke_forge.workflows.create_spinning_cover_video", fake_cover)
    monkeypatch.setattr("karaoke_forge.workflows.render_karaoke_video", fake_render)

    result = make_karaoke_video(
        audio,
        None,
        lyrics,
        output,
        assets,
        options=MakeOptions(
            cover_image=cover,
            font_files=(font,),
            cover_background="paper",
            cover_style="spectrum",
            cover_waveform=False,
            timing_refinement="off",
            auto_sync=True,
        ),
    )

    assert result.video == output
    assert result.sync_result is None
