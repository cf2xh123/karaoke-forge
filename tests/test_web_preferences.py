import json
from types import SimpleNamespace

from karaoke_forge.models import LyricLine, LyricsDocument
from karaoke_forge.preferences import load_preferences, preferences_path, save_preferences
from karaoke_forge.web import create_web_app


def test_preferences_merge_only_valid_settings_and_recover_from_corrupt_file(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path))
    save_preferences({"font_size": 66, "ripple_following": False})
    saved = save_preferences(
        {
            "global_zoom": 2.0,
            "font_size": 200,
            "timing_mode": "global",
            "show_countdown": "false",
            "text_color": "invalid",
            "audio": "private-song.wav",
            "lyrics": "private lyrics",
            "music_u": "secret",
        }
    )

    assert saved == {
        "font_size": 88,
        "ripple_following": False,
        "global_zoom": 2.0,
        "timing_mode": "global",
    }
    assert load_preferences() == saved
    assert not list(tmp_path.glob("*.tmp"))
    preferences_path().write_text("not JSON", encoding="utf-8")
    assert load_preferences() == {}
    assert save_preferences({"playback_rate": 1.5}) == {"playback_rate": 1.5}


def test_web_preferences_follow_user_changes_and_win_over_restored_projects(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path / "settings"))
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    save_preferences(
        {
            "font_size": 66,
            "timing_mode": "global",
            "ripple_following": False,
            "playback_rate": 1.5,
            "show_countdown": False,
            "global_zoom": 2,
        }
    )
    app = create_web_app()
    by_label = {getattr(component, "label", None): component for component in app.blocks.values()}
    by_id = {getattr(component, "elem_id", None): component for component in app.blocks.values()}
    assert by_label["字号"].value == 66
    assert by_label["播放倍速"].value == 1.5
    assert by_id["editor-timing-mode"].value == "global"
    assert by_id["editor-global-mode-panel"].visible is True
    assert by_id["editor-audio-panel"].visible is False
    assert json.loads(by_id["kf-editor-preferences"].value)["global_zoom"] == 2

    font_save = next(
        callback
        for callback in app.fns.values()
        if getattr(callback.fn, "__name__", "") == "save_control_preference"
        and callback.inputs == [by_label["字号"]]
    )
    assert font_save.outputs == []
    font_save.fn(74)
    assert load_preferences()["font_size"] == 74

    bridge_save = next(
        callback
        for callback in app.fns.values()
        if getattr(callback.fn, "__name__", "") == "save_editor_preferences_update"
    )
    assert bridge_save.outputs == []
    assert any(
        tuple(target) == (by_id["kf-editor-preferences-update"]._id, "change")
        for dependency in app.config["dependencies"]
        for target in dependency["targets"]
    )
    bridge_save.fn('{"snap_enabled":false,"global_zoom":3,"audio":"private.wav"}')
    bridge_save.fn("invalid JSON")
    assert load_preferences()["snap_enabled"] is False
    assert "audio" not in load_preferences()

    # Reopening a page must read changes made after the server created Blocks.
    save_preferences({"timing_mode": "line", "pronunciation_font_size": 32})
    page_load = next(
        callback
        for callback in app.fns.values()
        if getattr(callback.fn, "__name__", "") == "restore_editor_preferences"
    )
    restored = dict(zip((component._id for component in page_load.outputs), page_load.fn()))
    assert restored[by_label["字号"]._id] == 74
    assert restored[by_label["注音字号"]._id] == 32
    assert restored[by_id["editor-timing-mode"]._id] == "line"
    assert restored[by_id["editor-global-mode-panel"]._id]["visible"] is False
    assert restored[by_id["editor-audio-panel"]._id]["visible"] is True
    assert json.loads(restored[by_id["kf-editor-preferences"]._id])["global_zoom"] == 3

    lyrics = tmp_path / "lyrics.json"
    lyrics.write_text(
        json.dumps(
            LyricsDocument(
                lines=[LyricLine(text="one two", start=1.0, end=3.0)],
            ).to_dict()
        ),
        encoding="utf-8",
    )
    workspace = SimpleNamespace(
        manifest=tmp_path / "project.json",
        lyrics_project=lyrics,
        audio=None,
        video=None,
        cover=None,
        font_files=(),
        settings={"font_size": 40, "font": "Arial", "show_countdown": True},
        name="Different project",
    )
    monkeypatch.setattr("karaoke_forge.web.load_workspace_project", lambda _path: workspace)
    restore_project = next(
        callback
        for callback in app.fns.values()
        if getattr(callback.fn, "__name__", "") == "restore_recent_workspace"
    )
    result = dict(
        zip(
            (component._id for component in restore_project.outputs),
            restore_project.fn(str(workspace.manifest)),
        )
    )
    assert result[by_label["字号"]._id] == {"__type__": "update"}
    # An appearance field the user has never saved can still use project data.
    assert result[by_label["字幕字体"]._id] == "Arial"
    assert load_preferences()["font_size"] == 74

    font_upload = next(
        callback
        for callback in app.fns.values()
        if getattr(callback.fn, "__name__", "") == "use_uploaded_font"
    )
    file_control = font_upload.inputs[0]
    dependencies = [
        dependency
        for dependency in app.config["dependencies"]
        if dependency["id"] == font_upload._id
    ]
    assert dependencies[0]["targets"] == [(file_control._id, "upload")]
    assert font_upload.fn([str(tmp_path / "My Font.ttf")]) == "My Font"
    assert load_preferences()["font"] == "My Font"
    assert font_upload.fn([]) == {"__type__": "update"}
    assert load_preferences()["font"] == "My Font"
