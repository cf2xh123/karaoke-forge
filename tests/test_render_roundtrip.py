"""Persist source time independently of video sync and honor saved ruby policy."""

from dataclasses import replace
from pathlib import Path

import pytest

from karaoke_forge.ass import AssStyle, write_ass
from karaoke_forge.formats import read_lyrics, write_json
from karaoke_forge.media import AudioSyncResult
from karaoke_forge.models import KaraokeToken, LyricLine, LyricsDocument, PronunciationSpan
from karaoke_forge.projects import (
    PROJECT_FILENAME,
    load_workspace_project,
    read_workspace_lyrics,
    save_workspace_project,
)
from karaoke_forge.web import prepare_make_editor_job, run_make_job


def source_document():
    return LyricsDocument(
        [LyricLine("Hello", 1, 2, [KaraokeToken("Hello", 1, 2, confidence=0.9)])],
        metadata={"auto_pronunciation": "false", "custom": "preserved"},
    )


def render_options(audio, video, lyrics, output_root, *, offset=2, auto_sync=False):
    return {
        "audio_file": str(audio), "video_file": str(video), "lyrics_file": str(lyrics),
        "pasted_lyrics": "", "output_name": "Round trip", "language": "en", "model": "small",
        "device": "cpu", "separate_vocals": False, "quality": "快速预览", "audio_offset": offset,
        "font": "Arial", "font_size": 58, "text_color": "#FFFFFF", "highlight_color": "#FFD54A",
        "margin_v": 72, "timing_refinement": "off", "auto_sync": auto_sync,
        "output_root": str(output_root),
    }


@pytest.mark.parametrize("offset,sync_offset", [(2.0, None), (-2.0, None), (0.5, 3.0)])
def test_real_exports_round_trip_without_reapplying_offset(tmp_path, monkeypatch, offset, sync_offset):
    audio, video, lyrics = (tmp_path / name for name in ("song.mp3", "mv.mp4", "lyrics.json"))
    audio.write_bytes(b"audio")
    video.write_bytes(b"video")
    source = source_document()
    lyrics.write_text(write_json(source), encoding="utf-8")
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr("karaoke_forge.workflows.probe_media_has_audio", lambda _path: True)
    sync = AudioSyncResult(sync_offset or 0, 0.9, 5, 5, 20, 25, 0.9)
    monkeypatch.setattr("karaoke_forge.workflows.detect_audio_sync", lambda *_args, **_kw: sync)
    observed_offsets = []

    def render(_video, _ass, target, **kwargs):
        observed_offsets.append(kwargs["audio_offset"])
        Path(target).write_bytes(b"rendered")
        return Path(target)

    monkeypatch.setattr("karaoke_forge.workflows.render_karaoke_video", render)
    first = run_make_job(**render_options(
        audio, video, lyrics, tmp_path / "outputs", offset=offset, auto_sync=sync_offset is not None,
    ))
    assert first.video, first.log
    workspace = load_workspace_project(Path(first.output_dir) / PROJECT_FILENAME)
    editable = read_workspace_lyrics(workspace)
    assert editable.lines == source.lines
    assert editable.metadata["custom"] == "preserved"
    assert workspace.settings["lyrics_timebase"] == "audio"
    rendered_json = next(Path(path) for path in first.files
                         if Path(path).parent.name.endswith(".assets") and path.endswith(".json"))
    expected_start = max(0, 1 + offset + (sync_offset or 0))
    assert read_lyrics(rendered_json).lines[0].start == expected_start
    assert rendered_json != workspace.lyrics_project
    assert "workspace_manifest" not in read_lyrics(rendered_json).metadata

    second = run_make_job(**render_options(
        workspace.audio, workspace.video, workspace.lyrics_project, tmp_path / "outputs",
        offset=workspace.settings["audio_offset"], auto_sync=workspace.settings["auto_sync"],
    ))
    assert second.video, second.log
    second_workspace = load_workspace_project(Path(second.output_dir) / PROJECT_FILENAME)
    assert read_workspace_lyrics(second_workspace).lines == source.lines
    first_ass = next(Path(path).read_text(encoding="utf-8-sig") for path in first.files
                     if path.endswith(".ass"))
    second_ass = next(Path(path).read_text(encoding="utf-8-sig") for path in second.files
                      if path.endswith(".ass"))
    assert first_ass == second_ass
    assert not any(
        line.startswith("Dialogue:") and ",Pronunciation," in line
        for line in second_ass.splitlines()
    )
    assert observed_offsets == [offset + (sync_offset or 0)] * 2


def legacy_project(tmp_path, *, settings):
    project = tmp_path / "mv-old"
    assets = project / "Song.assets"
    assets.mkdir(parents=True)
    lyrics = assets / "Song.json"
    lyrics.write_text(write_json(source_document().shifted(2)), encoding="utf-8")
    audio, video = project / "audio.wav", project / "mv.mp4"
    audio.write_bytes(b"audio")
    video.write_bytes(b"video")
    return save_workspace_project(
        project, name="Song", lyrics_project=lyrics, audio=audio, video=video,
        settings=settings, recent_root=tmp_path / "recent",
    )


def test_legacy_manual_offset_restores_source_without_rewriting_old_files(tmp_path):
    workspace = legacy_project(tmp_path, settings={"audio_offset": 2, "auto_sync": False})
    original_bytes = workspace.lyrics_project.read_bytes()
    assert read_workspace_lyrics(workspace).lines == source_document().lines
    assert workspace.lyrics_project.read_bytes() == original_bytes
    # Ordinary saved source projects are not mistaken for old video exports.
    source_path = workspace.manifest.parent / "saved.json"
    source_path.write_bytes(original_bytes)
    assert read_workspace_lyrics(replace(workspace, lyrics_project=source_path)).lines[0].start == 3


def test_legacy_auto_sync_only_recovers_a_reliable_match(tmp_path, monkeypatch):
    workspace = legacy_project(tmp_path, settings={"audio_offset": 0.5, "auto_sync": True})
    monkeypatch.setattr("karaoke_forge.media.probe_media_has_audio", lambda _path: True)
    sync = AudioSyncResult(1.5, 0.9, 5, 5, 20, 25, 0.9)
    monkeypatch.setattr("karaoke_forge.media.detect_audio_sync", lambda *_args: sync)
    assert read_workspace_lyrics(workspace).lines == source_document().lines
    monkeypatch.setattr("karaoke_forge.media.detect_audio_sync", lambda *_args: replace(sync, confidence=0.1))
    unchanged = read_workspace_lyrics(workspace)
    assert unchanged.lines[0].start == 3
    assert "无法可靠匹配" in unchanged.metadata["legacy_timing_warning"]


def test_legacy_unknown_offset_is_not_guessed(tmp_path):
    workspace = legacy_project(tmp_path, settings={})
    unchanged = read_workspace_lyrics(workspace)
    assert unchanged.lines[0].start == 3
    assert "未保存完整偏移设置" in unchanged.metadata["legacy_timing_warning"]


def test_legacy_negative_offset_with_lost_start_is_reported_without_guessing(tmp_path):
    workspace = legacy_project(tmp_path, settings={"audio_offset": -2, "auto_sync": False})
    workspace.lyrics_project.write_text(write_json(source_document().shifted(-2)), encoding="utf-8")
    unchanged = read_workspace_lyrics(workspace)
    assert unchanged.lines[0].start == 0
    assert "负偏移已裁掉" in unchanged.metadata["legacy_timing_warning"]


def test_official_only_policy_survives_offline_preparation(tmp_path, monkeypatch):
    import inspect

    audio, video, lyrics = (tmp_path / name for name in ("song.mp3", "mv.mp4", "lyrics.json"))
    audio.write_bytes(b"audio")
    video.write_bytes(b"video")
    lyrics.write_text(write_json(source_document()), encoding="utf-8")
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))

    def unexpected_generated(*_args, **_kwargs):
        raise AssertionError("An official-only document must not gain automatic readings")

    monkeypatch.setattr("karaoke_forge.web.generate_pronunciation", unexpected_generated)
    accepted = inspect.signature(prepare_make_editor_job).parameters
    arguments = render_options(audio, video, lyrics, tmp_path / "outputs")
    result = prepare_make_editor_job(**{key: value for key, value in arguments.items() if key in accepted})
    assert result.project, result.log
    assert result.payload["metadata"]["auto_pronunciation"] == "false"
    assert not result.payload["lines"][0]["pronunciation_units"]


def test_official_only_ruby_keeps_explicit_readings_and_does_not_generate_missing_ones():
    document = source_document()
    document.lines.append(LyricLine("World", 3, 4, pronunciation_units=[
        PronunciationSpan("World", "公式", 0, 5),
    ]))
    ass = write_ass(document, AssStyle(auto_pronunciation=True))
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:") and ",Pronunciation," in line]
    assert len(events) == 1
    assert "公式" in events[0]
    assert "ハロー" not in ass
