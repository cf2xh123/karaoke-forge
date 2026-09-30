from __future__ import annotations

import json

import pytest

from karaoke_forge.models import LyricLine, LyricsDocument
from karaoke_forge.projects import save_workspace_project
from karaoke_forge.web import create_web_app


@pytest.fixture(scope="module")
def open_editor_callback(tmp_path_factory):
    root = tmp_path_factory.mktemp("editor-handoff")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(root / "outputs"))
        patch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(root / "settings"))
        patch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
        app = create_web_app()
        yield next(
            callback
            for callback in app.fns.values()
            if getattr(callback.fn, "__name__", "") == "open_current_make_project_editor"
        )
        app.close()


@pytest.fixture
def lyrics_file(tmp_path):
    path = tmp_path / "song.json"
    document = LyricsDocument(lines=[LyricLine(text="Line", start=1.0, end=2.0)])
    path.write_text(json.dumps(document.to_dict()), encoding="utf-8")
    return path


def test_make_editor_handoff_passes_the_uploaded_video(open_editor_callback) -> None:
    assert len(open_editor_callback.inputs) == 3
    assert open_editor_callback.inputs[2].file_types == ["video"]


@pytest.mark.parametrize("audio_input", ["none", "missing", "placeholder"])
def test_make_editor_uses_an_audible_mv_when_audio_is_unavailable(
    open_editor_callback, lyrics_file, tmp_path, monkeypatch, audio_input
) -> None:
    video = tmp_path / "song.mp4"
    video.write_bytes(b"video")
    audio = None
    if audio_input == "missing":
        audio = str(tmp_path / "missing.wav")
    elif audio_input == "placeholder":
        placeholder = tmp_path / "audio-song.wav"
        placeholder.write_bytes(b"audio")
        audio = str(placeholder)
    probed = []

    def has_audio(path):
        probed.append(path)
        return True

    monkeypatch.setattr("karaoke_forge.web.probe_media_has_audio", has_audio)
    result = open_editor_callback.fn(str(lyrics_file), audio, str(video))

    assert len(result) == 15
    assert result[0]["lines"][0]["text"] == "Line"
    assert result[11] == str(video)
    assert result[13]["selected"] == "editor"
    assert "已使用 MV 音轨" in result[2]
    assert "已使用 MV 音轨" in result[14]
    assert probed == [video]


def test_make_editor_preserves_explicit_audio_over_mv(
    open_editor_callback, lyrics_file, tmp_path, monkeypatch
) -> None:
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    video = tmp_path / "silent.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        "karaoke_forge.web.probe_media_has_audio",
        lambda _path: pytest.fail("An explicit audio upload should not probe the MV"),
    )

    result = open_editor_callback.fn(str(lyrics_file), str(audio), str(video))

    assert result[11] == str(audio)
    assert result[13]["selected"] == "editor"


@pytest.mark.parametrize(
    "probe_result, expected_message", [(False, "MV 没有音轨"), (None, "暂时无法确认 MV 音轨")]
)
def test_make_editor_reports_silent_or_unverified_mv_without_loading_it_as_audio(
    open_editor_callback, lyrics_file, tmp_path, monkeypatch, probe_result, expected_message
) -> None:
    video = tmp_path / "song.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr("karaoke_forge.web.probe_media_has_audio", lambda _path: probe_result)

    result = open_editor_callback.fn(str(lyrics_file), None, str(video))

    assert result[11] is None
    assert result[13]["selected"] == "editor"
    assert expected_message in result[2]
    assert expected_message in result[14]
    assert "上传音频" in result[2]


def test_make_editor_recovers_saved_mv_without_requiring_a_third_argument(
    open_editor_callback, lyrics_file, tmp_path, monkeypatch
) -> None:
    video = tmp_path / "song.mp4"
    video.write_bytes(b"video")
    workspace = save_workspace_project(
        tmp_path / "saved-project",
        name="Saved song",
        lyrics_project=lyrics_file,
        video=video,
        recent_root=tmp_path,
    )
    monkeypatch.setattr("karaoke_forge.web.probe_media_has_audio", lambda _path: True)

    result = open_editor_callback.fn(str(workspace.manifest), None)

    assert result[11] == str(workspace.video)
    assert result[12] == "Saved song"
    assert result[13]["selected"] == "editor"


def test_make_editor_still_opens_lyrics_without_any_media(
    open_editor_callback, lyrics_file
) -> None:
    result = open_editor_callback.fn(str(lyrics_file), None)

    assert result[11] is None
    assert result[13]["selected"] == "editor"
    assert "尚未载入试听音频" in result[2]
