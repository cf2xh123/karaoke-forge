from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

import karaoke_forge.desktop  # noqa: F401 -- initialize the Windows Qt DLL environment

# isort: split

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QLineEdit

from karaoke_forge.desktop.make_page import MakePage
from karaoke_forge.formats import read_lyrics, write_json
from karaoke_forge.models import KaraokeToken, LyricLine, LyricsDocument
from karaoke_forge.projects import load_workspace_project, save_workspace_project
from karaoke_forge.web import UiEditorPreparationResult, UiJobResult, _workspace_asset_fallbacks


class DeferredRunner(QObject):
    busy_changed = Signal(bool)
    message = Signal(str)
    is_busy = False

    def __init__(self):
        super().__init__()
        self.jobs = []

    def submit(self, title, task, on_success):
        self.jobs.append((title, task, on_success))
        return True

    def finish(self):
        _title, task, callback = self.jobs.pop(0)
        result = task(self.message.emit)
        callback(result)
        return result


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def page(qt_app, monkeypatch, tmp_path, request):
    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path / "settings"))
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    runner = DeferredRunner()
    widget = MakePage(runner, embedded=getattr(request, "param", False))
    yield widget, runner
    widget.close()
    widget.deleteLater()
    qt_app.processEvents()


def document() -> LyricsDocument:
    return LyricsDocument(lines=[LyricLine("Song", 1, 2, [KaraokeToken("Song", 1, 2)])])


def test_new_project_drops_edited_snapshot_and_old_sources_before_render(page, monkeypatch):
    widget, runner = page
    widget.set_editor_document(document())
    widget._editor_source_settings = {"source_refs": {"netease": "old"}}
    widget.controls["netease_link"].setText("old-link")
    widget.controls["audio_offset"].setValue(25)
    widget.controls["font_size"].setValue(70)
    widget.controls["music_u"].setText("session-account")
    snapshots = []
    widget.settings_changed.connect(lambda _values: snapshots.append(widget._editor_document))
    widget.reset_project({"netease_link": "new-link", "output_name": "New song"})
    settings = widget.get_settings()
    assert settings["netease_link"] == "new-link"
    assert settings["lyrics_file"] == settings["pasted_lyrics"] == ""
    assert settings["audio_offset"] == 0
    assert settings["timing_refinement"] == "auto"
    assert settings["font_size"] == 70
    assert widget._workspace is widget._editor_document is widget._render_lyrics_snapshot is None
    assert widget._editor_source_settings == {}
    assert snapshots == [None]
    assert widget.controls["music_u"].text() == "session-account"
    assert not widget._match_timer.isActive()
    assert runner.jobs == []
    captured = []
    monkeypatch.setattr(
        "karaoke_forge.desktop.make_page.run_make_job",
        lambda **kwargs: captured.append(kwargs) or UiJobResult("Rendered", None, [], "", None),
    )
    widget.render_video()
    runner.finish()
    assert captured[0]["netease_link"] == "new-link"
    assert captured[0]["lyrics_file"] is None


def test_changing_source_keeps_edited_lyrics_until_explicit_regeneration(page):
    widget, runner = page
    widget.set_editor_document(document())
    original = widget._editor_document.to_dict()
    snapshots = []
    widget.settings_changed.connect(snapshots.append)
    widget.apply_material_settings({"lyrics_file": "", "netease_link": "new-song"})
    assert widget._editor_document.to_dict() == original
    assert len(snapshots) == 1
    assert snapshots[0]["netease_link"] == "new-song"
    assert not widget._match_timer.isActive()
    assert runner.jobs == []


def test_explicit_online_audio_preference_reaches_prepare_and_render_jobs(page, monkeypatch):
    widget, runner = page
    widget.apply_material_settings({
        "netease_link": "https://music.163.com/song?id=123", "prefer_netease_audio": True,
        "video_file": "independent-visual.mp4", "rights_confirmed": True,
    })
    captured = []
    monkeypatch.setattr(
        "karaoke_forge.desktop.make_page.prepare_make_editor_job",
        lambda **kwargs: captured.append(kwargs) or preparation(),
    )
    widget.prepare_project()
    runner.finish()
    assert captured[0]["prefer_netease_audio"] is True
    assert captured[0]["audio_file"] is None
    assert captured[0]["video_file"] == "independent-visual.mp4"
    widget.stage_editor_document(document())
    widget.controls["audio_file"].set_value("project-assets/downloaded.m4a")
    monkeypatch.setattr(
        "karaoke_forge.desktop.make_page.run_make_job",
        lambda **kwargs: captured.append(kwargs) or UiJobResult("Rendered", None, [], "", None),
    )
    widget.render_video()
    runner.finish()
    assert captured[1]["audio_file"] == "project-assets/downloaded.m4a"
    assert captured[1]["video_file"] == "independent-visual.mp4"
    assert captured[1]["netease_link"] == ""


def test_new_project_rejects_invalid_settings_or_busy_job_without_clearing_lyrics(page):
    widget, runner = page
    widget.set_editor_document(document())
    before = widget.get_settings()
    original = widget._editor_document.to_dict()
    with pytest.raises(ValueError, match="有效数字"):
        widget.reset_project({"font_size": "invalid"})
    assert widget.get_settings() == before
    assert widget._editor_document.to_dict() == original
    runner.is_busy = True
    with pytest.raises(ValueError, match="当前任务"):
        widget.reset_project({"netease_link": "new"})
    assert widget.get_settings() == before
    assert widget._editor_document.to_dict() == original


def test_pure_settings_cannot_submit_work_or_save_preferences(qt_app, monkeypatch):
    persisted = []
    monkeypatch.setattr("karaoke_forge.desktop.make_page.save_preferences", lambda value: persisted.append(value))
    runner = DeferredRunner()
    widget = MakePage(runner, embedded=True, settings_only=True)
    widget.controls["font_size"].setValue(72)
    widget.controls["netease_link"].setText("https://music.163.com/song?id=123")
    widget.prepare_project()
    widget.render_video()
    widget.refresh_preview()
    widget.login_netease()
    widget.logout_netease()
    assert runner.jobs == []
    assert not widget._match_timer.isActive()
    assert persisted == []
    widget.deleteLater()


def test_new_project_settings_validate_before_changes_and_exclude_private_fields(page):
    widget, _runner = page
    before = widget.get_settings()
    settings = widget.new_project_settings({
        "output_name": "New", "font_size": 70, "music_u": "private",
        "cookie_browser": "edge", "cookie_browser_profile": "private-profile",
    })
    assert settings["output_name"] == "New" and settings["font_size"] == 70
    assert not {"music_u", "cookie_browser", "cookie_browser_profile"} & settings.keys()
    assert widget.get_settings() == before


@pytest.mark.parametrize("accepted", [False, True])
def test_account_settings_only_changes_session_when_saved(page, monkeypatch, accepted):
    widget, runner = page
    widget.controls["music_u"].setText("original-session")
    widget.controls["cookie_browser_profile"].setText("original-profile")

    def edit(dialog):
        browser = dialog.findChild(QComboBox)
        browser.setCurrentIndex(browser.findData("edge"))
        fields = dialog.findChildren(QLineEdit)
        fields[0].setText("new-profile")
        fields[1].setText("new-session")
        return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", edit)
    widget.show_account_settings()
    assert widget.controls["music_u"].text() == ("new-session" if accepted else "original-session")
    assert widget.controls["cookie_browser_profile"].text() == ("new-profile" if accepted else "original-profile")
    assert not {"music_u", "cookie_browser", "cookie_browser_profile"} & widget.get_settings().keys()
    assert runner.jobs == []


@pytest.mark.parametrize("configured", [False, True])
def test_restored_project_keeps_original_lyrics_source_and_processing_settings(page, tmp_path, configured):
    widget, _runner = page
    lyrics = tmp_path / "lyrics.json"
    lyrics.write_text(write_json(document()), encoding="utf-8")
    workspace = save_workspace_project(
        tmp_path / "project", name="Song", lyrics_project=lyrics,
        settings={
            "lyrics_file": "original.lrc", "pasted_lyrics": "原始歌词\nSing with me",
            "preserve_lyrics_source": not configured, "language": "ja", "model": "profile:precise",
        }, recent_root=tmp_path,
    )
    widget.restore_workspace(workspace, configured=configured)
    settings = widget.get_settings()
    assert settings["lyrics_file"] == "original.lrc"
    assert settings["pasted_lyrics"] == "原始歌词\nSing with me"
    assert settings["language"] == "ja" and settings["model"] == "profile:precise"


def test_restored_relative_source_stays_inside_project_and_rejects_escaped_path(page, tmp_path):
    widget, _runner = page
    lyrics = tmp_path / "lyrics.json"
    lyrics.write_text(write_json(document()), encoding="utf-8")
    workspace = save_workspace_project(
        tmp_path / "project", name="Song", lyrics_project=lyrics,
        settings={"preserve_lyrics_source": True, "lyrics_source_asset": "project-assets/source.lrc"},
        recent_root=tmp_path,
    )
    widget.restore_workspace(workspace)
    assert widget.get_settings()["lyrics_file"] == str(workspace.manifest.parent / "project-assets" / "source.lrc")
    before = widget.get_settings()
    workspace.settings["lyrics_source_asset"] = "../outside.lrc"
    with pytest.raises(ValueError, match="超出"):
        widget.restore_workspace(workspace)
    assert widget.get_settings() == before


@pytest.mark.parametrize("source", ["local", "paste"])
def test_saved_pending_source_is_used_for_explicit_regeneration(page, tmp_path, monkeypatch, source):
    widget, runner = page
    edited = tmp_path / "edited.json"
    edited.write_text(write_json(document()), encoding="utf-8")
    replacement = tmp_path / "replacement.lrc"
    replacement.write_text("[00:01.00]新しい歌\n[00:03.00]Sing with me", encoding="utf-8")
    values = {
        "lyrics_file": str(replacement) if source == "local" else "",
        "pasted_lyrics": "新しい歌\nSing with me" if source == "paste" else "",
        "pending_lyrics_source": True,
    }
    saved = save_workspace_project(
        tmp_path / "project", name="Edited song", lyrics_project=edited,
        settings=values, recent_root=tmp_path,
    )
    # Load from disk as reopening does; the edited lyrics and new source coexist.
    widget.restore_workspace(load_workspace_project(saved.manifest))
    captured = []
    monkeypatch.setattr(
        "karaoke_forge.desktop.make_page.prepare_make_editor_job",
        lambda **kwargs: captured.append(kwargs) or preparation(),
    )
    widget.prepare_project()
    runner.finish()
    assert captured[0]["lyrics_file"] == (values["lyrics_file"] or None)
    assert captured[0]["pasted_lyrics"] == values["pasted_lyrics"]
    assert read_lyrics(saved.lyrics_project).lines[0].text == "Song"


def preparation(project: str | None = None, output_dir: str | None = None):
    return UiEditorPreparationResult(
        status="Ready",
        payload=document().to_dict(),
        rows=[],
        line_number=1,
        whole_pronunciation="",
        pronunciation_rows=[],
        preview="unused html",
        project=project,
        audio=None,
        project_name="Song",
        files=[],
        log="Prepared",
        output_dir=output_dir,
    )


def test_all_production_options_are_captured_before_worker_runs(page, monkeypatch):
    widget, runner = page
    widget.controls["audio_file"].set_value("input.wav")
    widget.controls["video_file"].set_value("input.mp4")
    widget.controls["lyrics_file"].set_value("input.lrc")
    widget.controls["cover_file"].set_value("cover.png")
    widget.controls["font_files"].set_paths(["one.ttf", "two.otf"])
    widget.controls["output_name"].setText("Captured name")
    widget.controls["font_size"].setValue(70)
    widget.controls["translation_margin_v"].setValue(128)
    widget.controls["audio_offset"].setValue(1.25)
    widget.controls["export_instrumental"].setChecked(True)
    widget.controls["music_u"].setText("private-token")
    captured = []

    def render(**arguments):
        captured.append(arguments)
        return UiJobResult("Rendered", None, [], "", None)

    monkeypatch.setattr("karaoke_forge.desktop.make_page.run_make_job", render)
    widget.render_video()
    widget.controls["font_size"].setValue(44)
    widget.controls["output_name"].setText("Changed after submission")
    runner.finish()
    values = captured[0]
    assert values["output_name"] == "Captured name"
    assert values["font_size"] == 70
    assert values["font_files"] == ["one.ttf", "two.otf"]
    assert values["audio_offset"] == 1.25
    assert values["translation_margin_v"] == 128
    assert values["export_original"] is values["export_instrumental"] is True
    assert values["music_u"] == "private-token"
    assert values["audio_file"] == "input.wav"
    assert values["video_file"] == "input.mp4"
    assert callable(values["progress_callback"])
    assert "music_u" not in widget.get_settings()
    assert "cookie_browser_profile" not in widget.get_settings()


def test_prepare_maps_online_source_flags_and_emits_prepared_document(page, monkeypatch, tmp_path):
    widget, runner = page
    project = tmp_path / "source.json"
    project.write_text(write_json(document()), encoding="utf-8")
    widget.controls["utaten_link"].setText("https://utaten.com/lyric/example/")
    widget.controls["utaten_pronunciation_only"].setChecked(True)
    widget.controls["rights_confirmed"].setChecked(True)
    widget.controls["auto_english_pronunciation"].setChecked(False)
    captured, emitted = [], []

    def prepare(**arguments):
        captured.append(arguments)
        return preparation(str(project))

    monkeypatch.setattr("karaoke_forge.desktop.make_page.prepare_make_editor_job", prepare)
    widget.prepared.connect(emitted.append)
    widget.prepare_project()
    result = runner.finish()
    assert captured[0]["utaten_link"] == "https://utaten.com/lyric/example/"
    assert captured[0]["utaten_pronunciation_only"] is True
    assert captured[0]["rights_confirmed"] is True
    assert captured[0]["auto_english_pronunciation"] is False
    assert "quality" not in captured[0]
    assert emitted == [result]
    assert read_lyrics(widget.controls["lyrics_file"].value()).lines[0].text == "Song"


def test_workspace_restore_preserves_all_style_settings_and_multiple_fonts(page, tmp_path):
    widget, _runner = page
    for key in ["netease_link", "qqmusic_link", "utaten_link"]:
        widget.controls[key].setText("https://example.com/old-song")
    widget.controls["utaten_pronunciation_only"].setChecked(True)
    widget.controls["audio_offset"].setValue(25)
    lyrics = tmp_path / "lyrics.json"
    lyrics.write_text(write_json(document()), encoding="utf-8")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    fonts = [tmp_path / "one.ttf", tmp_path / "two.otf"]
    for font in fonts:
        font.write_bytes(b"font")
    workspace = save_workspace_project(
        tmp_path / "project",
        name="Restored",
        lyrics_project=lyrics,
        audio=audio,
        font_files=tuple(fonts),
        settings={
            "alignment_model": "profile:precise",
            "font_size": 72,
            "show_translation": False,
            "translation_margin_v": 128,
            "pronunciation_color": "#AA88CC",
            "show_countdown": False,
            "cover_background": "ocean",
            "cover_style": "halo",
            "quality": "高质量",
        },
        recent_root=tmp_path,
    )
    widget.restore_workspace(workspace)
    settings = widget.get_settings()
    assert settings["model"] == "profile:precise"
    assert settings["font_size"] == 72
    assert settings["show_translation"] is False
    assert settings["show_countdown"] is False
    assert settings["pronunciation_color"] == "#AA88CC"
    assert settings["translation_margin_v"] == 128
    assert settings["cover_background"] == "ocean"
    assert settings["cover_style"] == "halo"
    assert settings["quality"] == "高质量"
    assert settings["font_files"] == [str(path) for path in workspace.font_files]
    assert settings["audio_file"] == str(workspace.audio)
    assert settings["netease_link"] == settings["qqmusic_link"] == settings["utaten_link"] == ""
    assert settings["utaten_pronunciation_only"] is False
    assert settings["audio_offset"] == 0
    assert widget.has_materials()


def test_rendering_editor_document_keeps_original_media_and_exact_timeline(
    page, monkeypatch, tmp_path
):
    widget, runner = page
    lyrics = tmp_path / "lyrics.json"
    lyrics.write_text(write_json(document()), encoding="utf-8")
    media = {}
    for key, name in [("audio", "song.wav"), ("video", "mv.mp4"), ("cover", "cover.png")]:
        media[key] = tmp_path / name
        media[key].write_bytes(b"media")
    workspace = save_workspace_project(
        tmp_path / "project",
        name="Song",
        lyrics_project=lyrics,
        **media,
        settings={
            "font_size": 76,
            "show_translation": False,
            "translation_margin_v": 160,
            "cover_style": "halo",
            "quality": "高质量",
        },
        recent_root=tmp_path,
    )
    changed = document()
    changed.metadata["workspace_manifest"] = str(workspace.manifest)
    changed.lines[0].tokens[0].start = 1.3
    widget.controls["audio_file"].set_value("wrong-song.wav")
    widget.controls["font_size"].setValue(34)
    widget.controls["show_translation"].setChecked(True)
    widget.controls["netease_link"].setText("https://music.163.com/song?id=123")
    widget.set_editor_document(changed)
    captured = []

    def render(**arguments):
        captured.append(arguments)
        return UiJobResult("Rendered", None, [], "", None)

    monkeypatch.setattr("karaoke_forge.desktop.make_page.run_make_job", render)
    widget.render_video()
    runner.finish()
    values = captured[0]
    assert values["audio_file"] == str(workspace.audio)
    assert values["video_file"] == str(workspace.video)
    assert values["cover_file"] == str(workspace.cover)
    assert values["font_size"] == 76
    assert values["show_translation"] is False
    assert values["translation_margin_v"] == 160
    assert values["cover_style"] == "halo"
    assert values["quality"] == "高质量"
    assert values["timing_refinement"] == "off"
    assert values["netease_link"] == values["qqmusic_link"] == values["utaten_link"] == ""
    assert read_lyrics(values["lyrics_file"]).lines[0].tokens[0].start == pytest.approx(1.3)
    assert "workspace_manifest" not in read_lyrics(values["lyrics_file"]).metadata
    assert changed.metadata["workspace_manifest"] == str(workspace.manifest)
    assert read_lyrics(widget.controls["lyrics_file"].value()).metadata[
        "workspace_manifest"
    ] == str(workspace.manifest)


@pytest.mark.parametrize("manifest_kind", ["absent", "missing", "invalid"])
def test_standalone_editor_document_clears_previous_project_materials(
    page, tmp_path, manifest_kind
):
    widget, _runner = page
    widget.controls["audio_file"].set_value("old-song.wav")
    widget.controls["video_file"].set_value("old-song.mp4")
    widget.controls["cover_file"].set_value("old-cover.png")
    widget.controls["font_files"].set_paths(["old-font.ttf"])
    widget.controls["output_name"].setText("Old song")
    widget.controls["pasted_lyrics"].setPlainText("Old lyrics")
    widget.controls["utaten_pronunciation_only"].setChecked(True)
    for key in ["netease_link", "qqmusic_link", "utaten_link"]:
        widget.controls[key].setText("https://example.com/old-song")
    new_document = document()
    if manifest_kind != "absent":
        manifest = tmp_path / "workspace.json"
        new_document.metadata["workspace_manifest"] = str(manifest)
        if manifest_kind == "invalid":
            manifest.write_text("invalid json", encoding="utf-8")

    widget.set_editor_document(new_document)

    values = widget.get_settings()
    for key in [
        "audio_file",
        "video_file",
        "cover_file",
        "pasted_lyrics",
        "output_name",
        "netease_link",
        "qqmusic_link",
        "utaten_link",
    ]:
        assert values[key] == ""
    assert values["font_files"] == []
    assert values["utaten_pronunciation_only"] is False
    assert widget._workspace is None
    assert read_lyrics(values["lyrics_file"]).lines[0].text == "Song"
    assert values["timing_refinement"] == "off"


def test_job_settings_merge_keeps_existing_project_metadata_without_credentials(page, tmp_path):
    widget, _runner = page
    lyrics = tmp_path / "lyrics.json"
    lyrics.write_text(write_json(document()), encoding="utf-8")
    workspace = save_workspace_project(
        tmp_path / "project",
        name="Song",
        lyrics_project=lyrics,
        settings={"source_specific": "retain"},
        recent_root=tmp_path,
    )
    settings = {**widget.get_settings(), "music_u": "never-write", "font_size": 76}
    widget._save_job_settings(
        preparation(output_dir=str(workspace.manifest.parent)), settings, lambda _: None
    )
    saved = load_workspace_project(workspace.manifest)
    assert saved.settings["source_specific"] == "retain"
    assert saved.settings["font_size"] == 76
    assert "music_u" not in saved.settings
    assert "lyrics_file" not in saved.settings


def test_netease_login_is_only_started_by_explicit_action(page, monkeypatch):
    widget, runner = page
    captured = []
    monkeypatch.setattr(
        "karaoke_forge.desktop.make_page.acquire_netease_music_u",
        lambda: captured.append("login") or "token",
    )
    monkeypatch.setattr(
        "karaoke_forge.desktop.make_page.capture_netease_music_u",
        lambda: captured.append("relogin") or "new-token",
    )
    monkeypatch.setattr(
        "karaoke_forge.desktop.make_page.clear_netease_login_profile",
        lambda: captured.append("clear") or "Cleared",
    )
    assert not runner.jobs
    widget.login_netease()
    assert not captured
    runner.finish()
    assert captured == ["login"]
    assert widget.controls["music_u"].text() == "token"
    assert "token" not in widget.log.toPlainText()
    widget.login_netease(relogin=True)
    assert widget.controls["music_u"].text() == ""
    runner.finish()
    assert captured == ["login", "clear", "relogin"]
    assert widget.controls["music_u"].text() == "new-token"


def test_failed_preparation_does_not_replace_current_editor_data(page, monkeypatch):
    widget, runner = page
    widget.set_editor_document(document())
    existing = widget.controls["lyrics_file"].value()
    monkeypatch.setattr(
        "karaoke_forge.desktop.make_page.prepare_make_editor_job",
        lambda **_: UiEditorPreparationResult(
            "Failure", {}, [], 1, "", [], "", None, None, None, [], "Failed", None
        ),
    )
    emitted = []
    widget.prepared.connect(emitted.append)
    widget.prepare_project()
    runner.finish()
    assert emitted == []
    assert widget.controls["lyrics_file"].value() == existing
    assert "Failure" in widget.status.text()


def test_prepare_can_be_vetoed_before_starting_worker_or_replacing_editor(page):
    widget, runner = page
    widget.set_editor_document(document())
    existing = widget.controls["lyrics_file"].value()
    widget.before_prepare = lambda: False
    widget.prepare_project()
    assert runner.jobs == []
    assert widget.controls["lyrics_file"].value() == existing


def test_native_preview_receives_material_result_without_html_widget(page, monkeypatch):
    widget, runner = page
    captured, backgrounds = [], []
    widget.material_preview_changed.connect(backgrounds.append)

    def preview(**arguments):
        captured.append(arguments)
        return ("First\nNext", "Translation", "", "MV preview", False, 0.5, 1, "Ready")

    monkeypatch.setattr(
        "karaoke_forge.desktop.make_page.prepare_subtitle_material_preview", preview
    )
    widget.controls["audio_offset"].setValue(-0.3)
    widget.refresh_preview()
    runner.finish()
    assert captured[0]["offset"] == -0.3
    assert captured[0]["background_theme"] == "adaptive"
    assert "MV preview" in widget.preview_status.text()
    assert widget.preview.metaObject().className() == "LyricPreviewWidget"
    assert backgrounds == [None]


def test_stage_latest_editor_lyrics_preserves_current_materials_and_style(page, monkeypatch):
    widget, runner = page
    widget.controls["audio_file"].set_value("chosen-audio.wav")
    widget.controls["video_file"].set_value("chosen-mv.mp4")
    widget.controls["cover_file"].set_value("chosen-cover.png")
    widget.controls["font_files"].set_paths(["chosen.ttf"])
    widget.controls["lyrics_file"].set_value("original.lrc")
    widget.controls["pasted_lyrics"].setPlainText("Original unedited lyrics")
    widget.controls["output_name"].setText("Current title")
    widget.controls["font_size"].setValue(82)
    widget.controls["translation_margin_v"].setValue(144)
    widget.controls["timing_refinement"].setCurrentIndex(
        widget.controls["timing_refinement"].findData("force")
    )
    widget.controls["utaten_pronunciation_only"].setChecked(True)
    for key in ["netease_link", "qqmusic_link", "utaten_link"]:
        widget.controls[key].setText("https://example.com/source")
    original_settings = widget.get_settings()
    emitted, captured = [], []
    widget.settings_changed.connect(emitted.append)
    latest = document()
    latest.metadata["workspace_manifest"] = "stale-project.json"
    latest.lines[0].text = "Latest edit"
    latest.lines[0].tokens[0].text = "Latest edit"
    latest.lines[0].tokens[0].start = 1.35
    widget.stage_editor_document(latest)
    assert widget.get_settings() == original_settings
    assert emitted == []

    def render(**arguments):
        captured.append(arguments)
        return UiJobResult("Rendered", None, [], "", None)

    monkeypatch.setattr("karaoke_forge.desktop.make_page.run_make_job", render)
    widget.render_video()
    latest.lines[0].text = "A later unsent edit"
    runner.finish()
    values = captured[0]
    for key in [
        "audio_file", "video_file", "cover_file", "font_files", "output_name", "font_size",
        "translation_margin_v", "auto_sync", "audio_offset",
    ]:
        assert values[key] == original_settings[key]
    assert values["timing_refinement"] == "off"
    assert values["pasted_lyrics"] == ""
    assert values["netease_link"] == values["qqmusic_link"] == values["utaten_link"] == ""
    assert values["utaten_pronunciation_only"] is False
    frozen = read_lyrics(values["lyrics_file"])
    assert frozen.lines[0].text == "Latest edit"
    assert frozen.lines[0].tokens[0].start == pytest.approx(1.35)
    assert "workspace_manifest" not in frozen.metadata
    assert latest.metadata["workspace_manifest"] == "stale-project.json"


def test_staged_snapshot_does_not_restore_media_explicitly_removed_from_project(page, tmp_path):
    widget, _runner = page
    lyrics = tmp_path / "lyrics.json"
    lyrics.write_text(write_json(document()), encoding="utf-8")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    workspace = save_workspace_project(
        tmp_path / "project", name="Song", lyrics_project=lyrics, audio=audio,
        recent_root=tmp_path,
    )
    current = document()
    current.metadata["workspace_manifest"] = str(workspace.manifest)
    widget.restore_workspace(workspace)
    widget.controls["audio_file"].set_value("")
    widget.stage_editor_document(current)
    audio, video, cover, fonts = _workspace_asset_fallbacks(
        None, None, str(widget._render_lyrics_snapshot), None, []
    )
    assert audio is video is cover is None
    assert fonts == ()


def test_each_render_uses_its_own_editor_snapshot(page, monkeypatch):
    widget, runner = page
    captured = []

    def render(**arguments):
        captured.append(arguments["lyrics_file"])
        return UiJobResult("Rendered", None, [], "", None)

    monkeypatch.setattr("karaoke_forge.desktop.make_page.run_make_job", render)
    latest = document()
    widget.stage_editor_document(latest)
    widget.render_video()
    latest.lines[0].tokens[0].start = 1.7
    widget.stage_editor_document(latest)
    widget.render_video()
    runner.finish()
    runner.finish()
    assert captured[0] != captured[1]
    assert read_lyrics(captured[0]).lines[0].tokens[0].start == 1
    assert read_lyrics(captured[1]).lines[0].tokens[0].start == 1.7


@pytest.mark.parametrize("page", [True], indirect=True)
def test_embedded_preparation_and_render_leave_controller_settings_in_place(
    page, monkeypatch, tmp_path
):
    widget, runner = page
    source = tmp_path / "original.json"
    source.write_text(write_json(document()), encoding="utf-8")
    widget.controls["lyrics_file"].set_value(str(source))
    widget.controls["audio_file"].set_value("current.wav")
    widget.controls["font_size"].setValue(80)
    widget.tabs.setCurrentIndex(1)
    settings = widget.get_settings()
    prepared, rendered = [], []
    widget.prepared.connect(prepared.append)
    widget.rendered.connect(rendered.append)
    monkeypatch.setattr(
        "karaoke_forge.desktop.make_page.prepare_make_editor_job",
        lambda **_: preparation(str(source)),
    )
    monkeypatch.setattr(
        "karaoke_forge.desktop.make_page.run_make_job",
        lambda **_: UiJobResult("Rendered", None, [], "", None),
    )
    widget.show()
    assert widget.tabs.count() == 5
    assert widget.tabs.isVisible()
    assert not widget.preview.isVisible()
    assert not widget.prepare_button.isVisible()
    assert not widget.render_button.isVisible()
    assert not widget.log.isVisible()
    widget.prepare_project()
    result = runner.finish()
    assert prepared == [result]
    assert widget.get_settings() == settings
    assert widget.tabs.currentIndex() == 1
    widget.render_video()
    result = runner.finish()
    assert rendered == [result]
    assert widget.tabs.currentIndex() == 1


def test_public_settings_and_preview_style_signals_never_include_credentials(page):
    widget, _runner = page
    settings, styles = [], []
    widget.settings_changed.connect(settings.append)
    widget.style_changed.connect(styles.append)
    widget.controls["music_u"].setText("private-token")
    widget.controls["cookie_browser_profile"].setText("private-profile")
    assert not settings
    widget.controls["audio_file"].set_value("song.wav")
    assert settings[-1]["audio_file"] == "song.wav"
    widget.controls["font_size"].setValue(78)
    assert styles[-1]["font_size"] == settings[-1]["font_size"] == 78
    assert styles[-1]["primary_color"] == settings[-1]["highlight_color"]
    assert all("music_u" not in values for values in settings)
    assert all("cookie_browser_profile" not in values for values in settings)


@pytest.mark.parametrize("source_kind", ["lyrics", "manifest"])
@pytest.mark.parametrize("job_kind", ["prepare", "render", "preview"])
def test_native_jobs_keep_current_media_choices_without_old_workspace_fallback(
    page, monkeypatch, tmp_path, source_kind, job_kind
):
    widget, runner = page
    lyrics = tmp_path / "lyrics.json"
    lyrics.write_text(write_json(document()), encoding="utf-8")
    media = {}
    for key, filename in [("audio", "old.wav"), ("video", "old.mp4"), ("cover", "old.png")]:
        media[key] = tmp_path / filename
        media[key].write_bytes(b"old media")
    font = tmp_path / "old.ttf"
    font.write_bytes(b"font")
    workspace = save_workspace_project(
        tmp_path / "project", name="Old project", lyrics_project=lyrics, **media,
        font_files=(font,), recent_root=tmp_path,
    )
    original = document()
    original.metadata["workspace_manifest"] = str(workspace.manifest)
    workspace.lyrics_project.write_text(write_json(original), encoding="utf-8")
    source = workspace.manifest if source_kind == "manifest" else workspace.lyrics_project
    video = tmp_path / "new-mv.mp4"
    video.write_bytes(b"chosen video")
    widget.controls["lyrics_file"].set_value(str(source))
    widget.controls["video_file"].set_value(str(video))
    captured = []

    def run(**arguments):
        captured.append(arguments)
        if job_kind == "prepare":
            return preparation()
        if job_kind == "preview":
            return ("Song", "", "", "Preview", False, 0, 1, "Ready")
        return UiJobResult("Rendered", None, [], "", None)

    backend = {
        "prepare": "prepare_make_editor_job", "render": "run_make_job",
        "preview": "prepare_subtitle_material_preview",
    }[job_kind]
    callback = {
        "prepare": widget.prepare_project, "render": widget.render_video,
        "preview": widget.refresh_preview,
    }[job_kind]
    monkeypatch.setattr(f"karaoke_forge.desktop.make_page.{backend}", run)
    callback()
    assert captured == []
    runner.finish()
    arguments = captured[0]
    actual = _workspace_asset_fallbacks(
        arguments["audio_file"], arguments["video_file"], arguments["lyrics_file"],
        arguments["cover_file"], arguments.get("font_files"),
    )
    assert actual == (None, video, None, ())
    assert read_lyrics(arguments["lyrics_file"]).lines[0].text == "Song"
    assert "workspace_manifest" not in read_lyrics(arguments["lyrics_file"]).metadata
    assert read_lyrics(workspace.lyrics_project).metadata["workspace_manifest"] == str(
        workspace.manifest
    )
    assert widget.controls["lyrics_file"].value() == str(source)


@pytest.mark.parametrize("provider,key,link,source_id", [
    ("netease", "netease_link", "https://music.163.com/song?id=12345", "12345"),
    ("qqmusic", "qqmusic_link", "https://y.qq.com/n/ryqq/songDetail/ABC123", "ABC123"),
    ("utaten", "utaten_link", "https://utaten.com/lyric/abc123/", "abc123"),
])
def test_online_link_finds_saved_project_and_requests_open_without_replacing_inputs(
    page, tmp_path, provider, key, link, source_id,
):
    widget, runner = page
    source = tmp_path / "song.json"
    source.write_text(write_json(document()), encoding="utf-8")
    workspace = save_workspace_project(
        tmp_path / "outputs" / "saved-song", name="Saved song", lyrics_project=source,
        settings={"source_refs": {provider: {"id": source_id}}},
        recent_root=tmp_path / "outputs",
    )
    widget.controls["audio_file"].set_value("current-audio.wav")
    widget.controls[key].setText(link)
    requested = []
    widget.workspace_requested.connect(requested.append)
    widget._match_online_project()
    runner.finish()
    assert widget.open_match_button.isEnabled()
    assert "Saved song" in widget.match_status.text()
    assert widget.controls["audio_file"].value() == "current-audio.wav"
    assert requested == []
    widget.open_match_button.click()
    assert requested == [str(workspace.manifest)]


def test_stale_online_match_does_not_offer_the_previous_link_project(page, monkeypatch):
    widget, runner = page
    monkeypatch.setattr("karaoke_forge.desktop.make_page._matching_workspace_manifest", lambda *_: None)
    widget.controls["netease_link"].setText("https://music.163.com/song?id=1")
    widget._match_online_project()
    widget.controls["netease_link"].setText("https://music.163.com/song?id=2")
    runner.finish()
    assert not widget.open_match_button.isEnabled()
    assert widget._matched_manifest is None


def test_rendered_edited_project_keeps_its_online_reference_for_latest_matching(page, tmp_path, monkeypatch):
    from karaoke_forge.web import _matching_workspace_manifest

    widget, runner = page
    source = tmp_path / "song.json"
    current = document()
    source.write_text(write_json(current), encoding="utf-8")
    original = save_workspace_project(
        tmp_path / "outputs" / "prepared", name="Prepared song", lyrics_project=source,
        settings={"source_refs": {"netease": {"id": "12345"}}},
        recent_root=tmp_path / "outputs",
    )
    current.metadata["workspace_manifest"] = str(original.manifest)
    widget.stage_editor_document(current)
    destination = tmp_path / "outputs" / "rendered"

    def render(**arguments):
        saved = save_workspace_project(
            destination, name="Rendered song", lyrics_project=arguments["lyrics_file"],
            recent_root=tmp_path / "outputs",
        )
        return UiJobResult("Rendered", None, [str(saved.manifest)], "", str(destination))

    monkeypatch.setattr("karaoke_forge.desktop.make_page.run_make_job", render)
    widget.render_video()
    runner.finish()
    saved = load_workspace_project(destination / "karaoke-forge-project.json")
    assert saved.settings["source_refs"] == {"netease": {"id": "12345"}}
    assert _matching_workspace_manifest("https://music.163.com/song?id=12345", "", "") == str(saved.manifest)
    widget.stage_editor_document(document())
    assert widget._editor_source_settings == {}


@pytest.mark.parametrize("page", [True], indirect=True)
def test_editable_sample_preview_is_visible_and_does_not_change_project(page):
    widget, _runner = page
    actual = document()
    widget.stage_editor_document(actual)
    before = widget.get_settings()
    tab = next(index for index in range(widget.tabs.count())
               if widget.tabs.tabText(index) == "示例预览")
    widget.tabs.setCurrentIndex(tab)
    widget.show()
    assert widget.preview.isVisible()
    widget.sample_text.setPlainText("Long example lyric\nSecond sample")
    widget.sample_translation.setPlainText("Example translation\nSecond translation")
    widget.sample_row.setValue(2)
    widget.sample_progress.setValue(50)
    assert widget.preview._document.lines[0].text == "Long example lyric"
    assert widget.preview._document.lines[1].translation == "Second translation"
    assert widget.preview._position == 7
    assert widget.get_settings() == before
    assert widget._editor_document.to_dict() == actual.to_dict()
