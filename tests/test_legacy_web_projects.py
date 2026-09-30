"""Legacy browser entry points preserve source timing and newer editor snapshots."""

import pytest

from karaoke_forge.formats import read_lyrics, write_json
from karaoke_forge.media import AudioSyncResult
from karaoke_forge.models import KaraokeToken, LyricLine, LyricsDocument
from karaoke_forge.projects import PROJECT_FILENAME, save_workspace_project
from karaoke_forge.web import _prepare_lyrics, load_editor_project


def legacy_workspace(tmp_path, *, offset=2, settings=None):
    root = tmp_path / "old-project"
    assets = root / "Song.assets"
    assets.mkdir(parents=True)
    source = LyricsDocument([
        LyricLine("Song", 6, 7, [KaraokeToken("Song", 6, 7)], translation="歌曲"),
    ], metadata={"custom": "keep", "workspace_manifest": str(root / PROJECT_FILENAME)})
    lyrics = assets / "Song.json"
    lyrics.write_text(write_json(source.shifted(offset)), encoding="utf-8")
    audio, video = root / "audio.wav", root / "video.mp4"
    audio.write_bytes(b"audio")
    video.write_bytes(b"video")
    workspace = save_workspace_project(
        root, name="Song", lyrics_project=lyrics, audio=audio, video=video,
        settings={"audio_offset": offset, "auto_sync": False} if settings is None else settings,
        recent_root=tmp_path / "recent",
    )
    return source, workspace


@pytest.mark.parametrize("offset", [2, -2])
def test_linked_legacy_json_uses_source_clock_in_both_web_entries(tmp_path, offset):
    source, workspace = legacy_workspace(tmp_path, offset=offset)
    original = workspace.lyrics_project.read_bytes()
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    prepared = _prepare_lyrics(str(workspace.lyrics_project), "", job_dir)
    assert prepared.parent == job_dir
    assert read_lyrics(prepared).lines == source.lines
    payload = load_editor_project(str(workspace.lyrics_project))[0]
    assert payload["lines"] == source.to_dict()["lines"]
    assert payload["metadata"]["custom"] == "keep"
    assert workspace.lyrics_project.read_bytes() == original


def test_linked_legacy_automatic_sync_uses_the_recovered_offset(tmp_path, monkeypatch):
    source, workspace = legacy_workspace(
        tmp_path, offset=2, settings={"audio_offset": 0.5, "auto_sync": True},
    )
    monkeypatch.setattr("karaoke_forge.media.probe_media_has_audio", lambda _path: True)
    monkeypatch.setattr("karaoke_forge.media.detect_audio_sync", lambda *_args: AudioSyncResult(
        1.5, 0.9, 5, 5, 20, 25, 0.9,
    ))
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    assert read_lyrics(_prepare_lyrics(workspace.lyrics_project, "", job_dir)).lines == source.lines
    assert load_editor_project(workspace.lyrics_project)[0]["lines"] == source.to_dict()["lines"]


def test_editor_snapshot_with_same_manifest_keeps_unsaved_edits(tmp_path):
    source, workspace = legacy_workspace(tmp_path)
    source.lines[0].text = "New words"
    source.lines[0].start = 5.5
    source.lines[0].tokens = [KaraokeToken("New words", 5.5, 7)]
    snapshot = tmp_path / "edited-snapshot.json"
    snapshot.write_text(write_json(source), encoding="utf-8")
    assert source.metadata["workspace_manifest"] == str(workspace.manifest)
    assert _prepare_lyrics(snapshot, "", tmp_path) == snapshot
    assert load_editor_project(snapshot)[0]["lines"] == source.to_dict()["lines"]


@pytest.mark.parametrize("missing_manifest", [False, True])
def test_standalone_json_without_a_valid_link_remains_usable(tmp_path, missing_manifest):
    document = LyricsDocument([
        LyricLine("Standalone", 1, 2, [KaraokeToken("Standalone", 1, 2)]),
    ])
    if missing_manifest:
        document.metadata["workspace_manifest"] = str(tmp_path / "missing.json")
    path = tmp_path / "standalone.json"
    path.write_text(write_json(document), encoding="utf-8")
    assert _prepare_lyrics(path, "", tmp_path) == path
    assert load_editor_project(path)[0]["lines"] == document.to_dict()["lines"]


def test_unknown_legacy_offset_warns_editor_and_requires_review_before_render(tmp_path):
    _source, workspace = legacy_workspace(tmp_path, settings={})
    result = load_editor_project(workspace.lyrics_project)
    assert result[0]["lines"][0]["start"] == 8
    warning = result[0]["metadata"]["legacy_timing_warning"]
    assert warning in result[2]
    with pytest.raises(ValueError, match="请先打开编辑器核对并保存时间轴"):
        _prepare_lyrics(workspace.lyrics_project, "", tmp_path)
    # A separate manually corrected editor snapshot is never replaced or blocked.
    snapshot = tmp_path / "checked.json"
    snapshot.write_text(write_json(read_lyrics(workspace.lyrics_project)), encoding="utf-8")
    assert _prepare_lyrics(snapshot, "", tmp_path) == snapshot


def test_marked_audio_clock_projects_are_not_shifted_again(tmp_path):
    _source, workspace = legacy_workspace(
        tmp_path, settings={"audio_offset": 2, "auto_sync": False, "lyrics_timebase": "audio"},
    )
    assert _prepare_lyrics(workspace.lyrics_project, "", tmp_path) == workspace.lyrics_project
    assert load_editor_project(workspace.lyrics_project)[0]["lines"][0]["start"] == 8
