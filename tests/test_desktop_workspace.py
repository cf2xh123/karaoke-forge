"""The native production flow keeps the current editor and project choices in place."""

from __future__ import annotations

import os
import shutil
import threading
import wave
from dataclasses import replace
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

import karaoke_forge.desktop  # prepare Windows ICU before Qt

# isort: split
from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QLineEdit, QMessageBox

from karaoke_forge.desktop.app import MainWindow
from karaoke_forge.desktop.workspace import save_workspace_revision
from karaoke_forge.formats import read_lyrics, write_json
from karaoke_forge.models import KaraokeToken, LyricLine, LyricsDocument, PronunciationSpan
from karaoke_forge.projects import (
    PROJECT_FILENAME,
    load_workspace_project,
    read_workspace_lyrics,
    save_workspace_project,
)
from karaoke_forge.web import UiEditorPreparationResult, UiJobResult


class DeferredRunner(QObject):
    busy_changed = Signal(bool)
    message = Signal(str)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.is_busy = False
        self.jobs = []

    def submit(self, title, task, on_success=None):
        if self.is_busy:
            return False
        self.is_busy = True
        self.jobs.append((title, task, on_success))
        self.busy_changed.emit(True)
        return True

    def finish(self):
        _title, task, callback = self.jobs.pop(0)
        result = task(self.message.emit)
        self.is_busy = False
        self.busy_changed.emit(False)
        if callback:
            callback(result)
        return result


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path / "preferences"))
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr("karaoke_forge.desktop.app.JobRunner", DeferredRunner)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    widget = MainWindow()
    yield widget
    widget.runner.is_busy = False
    widget.close()
    widget.deleteLater()
    qt_app.processEvents()


def document():
    return LyricsDocument(
        [
            LyricLine(
                "春",
                0,
                1,
                [KaraokeToken("春", 0, 1, confidence=0.92)],
                translation="spring",
                pronunciation_units=[PronunciationSpan("春", "はる", 0, 1)],
            ),
            LyricLine("hidden", 1, 2, [KaraokeToken("hidden", 1, 2)], hidden=True),
        ],
        metadata={"custom": "retained"},
    )


def preparation(source=None):
    source = source or document()
    return UiEditorPreparationResult(
        status="Ready",
        payload=source.to_dict(),
        rows=[],
        line_number=1,
        whole_pronunciation="",
        pronunciation_rows=[],
        preview="",
        project="lyrics.json",
        audio=None,
        project_name="Prepared song",
        files=[],
        log="Prepared",
        output_dir=None,
    )


def audio_file(path):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0\0" * 8000)
    return path


def source_dialog(monkeypatch, *, accepted, settings=None):
    class Dialog:
        def __init__(self, *args, **kwargs):
            self.original_settings = kwargs.get("settings")

        def exec(self):
            return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

        def material_settings(self):
            return settings

        def project_settings(self):
            return settings

        def project_directory(self):
            return None

    monkeypatch.setattr("karaoke_forge.desktop.app.ProjectDialog", Dialog)


def test_new_project_cancel_leaves_document_materials_and_dirty_state_untouched(window, monkeypatch):
    window.workspace.adopt_prepared(preparation())
    window.editor.lines_table.item(0, 5).setText("Unsaved translation")
    before_document = window.editor.current_document().to_dict()
    before_settings = window.make.get_settings()
    source_dialog(monkeypatch, accepted=False)
    monkeypatch.setattr(window, "_allow_replace", lambda: pytest.fail("cancel must not ask to discard"))
    window.new_project_dialog()
    assert window.editor.current_document().to_dict() == before_document
    assert window.make.get_settings() == before_settings
    assert window.workspace.is_dirty
    assert not window.runner.jobs
    monkeypatch.setattr(window, "_allow_replace", lambda: True)


def test_new_project_confirmation_respects_rejected_discard(window, monkeypatch):
    window.workspace.adopt_prepared(preparation())
    source_dialog(monkeypatch, accepted=True, settings={"lyrics_file": "replacement.lrc"})
    monkeypatch.setattr(window, "_allow_replace", lambda: False)
    before = window.make.get_settings()
    window.new_project_dialog()
    assert window.editor.current_document().to_dict() == document().to_dict()
    assert window.make.get_settings() == before
    assert not window.runner.jobs


def test_new_online_project_clears_stale_content_and_applies_only_configuration(window, monkeypatch):
    window.workspace.adopt_prepared(preparation())
    window.make.controls["lyrics_file"].set_value("previous.json")
    window.make.controls["pasted_lyrics"].setPlainText("old pasted text")
    window.make.controls["font_size"].setValue(77)
    source_dialog(monkeypatch, accepted=True, settings={
        "output_name": "New online song", "netease_link": "https://music.163.com/song?id=123",
        "use_netease_lyrics": True, "rights_confirmed": True, "audio_file": "",
    })
    window.new_project_dialog()
    values = window.make.get_settings()
    assert values["netease_link"].endswith("123")
    assert not any(values[key] for key in ("lyrics_file", "pasted_lyrics", "qqmusic_link", "utaten_link"))
    assert values["font_size"] == 77
    assert window.editor.lines_table.rowCount() == 0
    assert not window.editor.is_dirty and window.workspace.is_dirty
    assert not window.workspace._has_document
    assert window.editor.player.source().isEmpty()
    assert window.outputs.player.source().isEmpty()
    assert not window.outputs.directory and not window.outputs.files.count()
    assert not window.runner.jobs
    assert window.navigation.currentRow() == 0
    assert "网易云" in window.workspace.project_summary.text()
    assert window.workspace.source_button.text() == "项目设置…"
    assert window.workspace.lyrics_picker.isHidden()


def test_change_sources_keeps_current_edits_and_requires_regeneration(window, monkeypatch, tmp_path):
    window.workspace.adopt_prepared(preparation())
    window.editor.lines_table.item(0, 5).setText("Keep this translation")
    before = window.editor.current_document().to_dict()
    source_dialog(monkeypatch, accepted=True, settings={
        "lyrics_file": "", "pasted_lyrics": "New lyrics", "netease_link": "",
        "qqmusic_link": "", "utaten_link": "", "audio_file": "",
    })
    destination = tmp_path / "saved-with-settings"
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(destination))
    window.edit_project_sources()
    window.runner.finish()
    assert window.editor.current_document().to_dict()["lines"] == before["lines"]
    assert window.workspace.project_directory == str(destination)
    assert "来源已更改" in window.workspace.document_status.text()
    assert window.make.get_settings()["pasted_lyrics"] == "New lyrics"
    assert not window.runner.jobs


def test_ctrl_n_and_top_new_button_use_the_same_local_dialog(window, qt_app, monkeypatch):
    source_dialog(monkeypatch, accepted=False)
    calls = []
    monkeypatch.setattr(
        "karaoke_forge.desktop.app.ProjectDialog.exec",
        lambda self: calls.append("dialog") or QDialog.DialogCode.Rejected,
    )
    window.show()
    window.activateWindow()
    window.workspace.new_button.setFocus()
    qt_app.processEvents()
    QTest.keyClick(window.workspace.new_button, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
    QTest.mouseClick(window.workspace.new_button, Qt.MouseButton.LeftButton)
    assert calls == ["dialog", "dialog"]


def test_space_routes_to_output_player_and_keeps_text_input(window, qt_app, monkeypatch):
    window.workspace.adopt_prepared(preparation())
    window.show()
    window.activateWindow()
    window.workspace.results_button.setChecked(True)
    calls = []
    monkeypatch.setattr(window.outputs, "toggle_playback", lambda: calls.append("output"))
    monkeypatch.setattr(window.editor, "toggle_playback", lambda: calls.append("editor"))
    window.outputs.play_button.setFocus()
    qt_app.processEvents()
    QTest.keyClick(window.outputs.play_button, Qt.Key.Key_Space)
    assert calls == ["output"]
    window.editor.whole_pronunciation.setFocus()
    QTest.keyClick(window.editor.whole_pronunciation, Qt.Key.Key_Space)
    assert calls == ["output"]
    assert window.editor.whole_pronunciation.text().endswith(" ")


def test_output_space_recovers_when_released_outside_the_application(window, qt_app, monkeypatch):
    window.workspace.adopt_prepared(preparation())
    window.show()
    window.activateWindow()
    window.workspace.results_button.setChecked(True)
    calls = []
    monkeypatch.setattr(window.outputs, "toggle_playback", lambda: calls.append("output"))
    window.outputs.play_button.setFocus()
    qt_app.processEvents()
    QTest.keyPress(window.outputs.play_button, Qt.Key.Key_Space)
    QApplication.sendEvent(window, QEvent(QEvent.Type.WindowDeactivate))
    # No release reaches the app while another window has focus.
    QApplication.sendEvent(window, QEvent(QEvent.Type.WindowActivate))
    QTest.keyClick(window.outputs.play_button, Qt.Key.Key_Space)
    assert calls == ["output", "output"]


def test_preparation_fills_the_existing_editor_without_navigation_or_resetting_style(
    window, monkeypatch, tmp_path
):
    audio = audio_file(tmp_path / "song.wav")
    window.workspace.song_picker.set_value(str(audio))
    window.workspace.lyrics_picker.set_value("chosen.lrc")
    window.make.controls["font_size"].setValue(76)
    window.make.controls["cover_file"].set_value("chosen.png")
    original_editor = window.editor
    monkeypatch.setattr(
        "karaoke_forge.desktop.make_page.prepare_make_editor_job", lambda **kwargs: preparation()
    )
    window.workspace.prepare_project()
    assert window.runner.is_busy
    assert not window.workspace.input_bar.isEnabled()
    assert not window.editor.isEnabled()
    assert not window.make.isEnabled()
    assert window.workspace.results_button.isEnabled()
    window.runner.finish()
    assert window.editor is original_editor
    assert window.navigation.currentRow() == 0
    assert window.editor.current_document().to_dict() == document().to_dict()
    assert window.make.get_settings()["cover_file"] == "chosen.png"
    assert window.editor.preview._style["font_size"] == 76
    assert window.workspace.input_bar.isEnabled() and window.editor.isEnabled()
    assert not window.workspace.is_dirty


def test_low_quality_preparation_warning_survives_progress_save_and_reopen(window, tmp_path):
    source = document()
    source.metadata.update({
        "alignment_status": "low_coverage_recovery",
        "alignment_coverage": "0.1",
        "alignment_review_lines": "1",
        "alignment_review_reasons": '{"1": ["日语歌词未可靠匹配"]}',
    })
    result = preparation(source)
    window.make._prepared_result(result)
    assert "1 句" in window.workspace.activity.text()
    assert "10%" in window.editor.review_summary
    assert "已就绪" in window.workspace.document_status.text()
    assert not window.editor.review_bar.isHidden()
    window.runner.message.emit("输出文件：lyrics.json")
    assert "1 句" in window.editor.review_summary
    window.editor.lines_table.item(0, 5).setText("待保存的人工修改")
    assert "未保存修改" in window.workspace.document_status.text()
    saved_document, saved, _ = save_workspace_revision(
        window.editor.current_document(), window.make.get_settings(), str(tmp_path / "quality")
    )
    window.workspace.load_project(saved_document, saved, str(saved.manifest))
    assert not window.workspace.is_dirty
    assert "已就绪" in window.workspace.document_status.text()
    assert "10%" in window.editor.review_summary
    assert "日语歌词未可靠匹配" in window.editor.lines_table.item(0, 0).toolTip()


@pytest.mark.parametrize("name", ["karaoke-forge-project", "KARAOKE-FORGE-PROJECT.json"])
def test_reserved_project_name_exports_recoverable_lyrics_and_preserves_existing_files(
    tmp_path, name
):
    existing = tmp_path / f"{Path(name).stem}-lyrics.json"
    existing.write_text("Unrelated user file", encoding="utf-8")
    original = document()
    _saved_document, saved, files = save_workspace_revision(
        original, {"output_name": name}, str(tmp_path)
    )
    assert saved.lyrics_project != saved.manifest
    assert saved.lyrics_project != existing
    assert str(saved.lyrics_project) in files
    assert existing.read_text(encoding="utf-8") == "Unrelated user file"
    restored = read_workspace_lyrics(saved)
    assert restored.lines == original.lines
    assert saved.name == name


def test_missing_selected_material_fails_before_overwriting_saved_lyrics(tmp_path):
    original = document()
    _document, saved, _files = save_workspace_revision(
        original, {"output_name": "Song"}, str(tmp_path)
    )
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    original.lines[0].translation = "This revision must stay unsaved"
    with pytest.raises(FileNotFoundError, match="所选音频文件不存在"):
        save_workspace_revision(
            original,
            {"output_name": "Song", "audio_file": str(tmp_path / "missing.wav")},
            str(tmp_path),
        )
    assert saved.lyrics_project.name == "Song.json"
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()} == before


def test_failed_missing_material_save_does_not_adopt_the_pending_revision(window, tmp_path, monkeypatch):
    window.workspace.adopt_prepared(preparation())
    window.workspace.song_picker.set_value(str(tmp_path / "missing.wav"))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path / "save"))
    assert window.workspace.is_dirty
    assert window.workspace.save_project()
    with pytest.raises(FileNotFoundError, match="不存在"):
        window.runner.finish()
    assert window.workspace.is_dirty
    assert not (tmp_path / "save" / PROJECT_FILENAME).exists()


def test_project_save_preserves_remote_source_refs_without_treating_them_as_local_assets(tmp_path):
    _document, saved, _files = save_workspace_revision(
        document(),
        {"output_name": "Local project", "cover_url": "https://example.com/cover.jpg",
         "source_refs": {"qqmusic": "https://example.com/song"}},
        str(tmp_path),
    )
    assert saved.cover is None
    assert saved.settings["cover_url"] == "https://example.com/cover.jpg"
    assert saved.settings["source_refs"] == {"qqmusic": "https://example.com/song"}


def test_generated_online_assets_are_adopted_without_overwriting_current_style(window, tmp_path):
    source = document()
    lyrics = tmp_path / "source.json"
    lyrics.write_text(write_json(source), encoding="utf-8")
    audio = audio_file(tmp_path / "downloaded.wav")
    cover = tmp_path / "downloaded.png"
    cover.write_bytes(b"cover")
    saved = save_workspace_project(
        tmp_path / "generated",
        name="Online song",
        lyrics_project=lyrics,
        audio=audio,
        cover=cover,
        settings={"font_size": 38},
        recent_root=tmp_path / "recent",
    )
    source.metadata["workspace_manifest"] = str(saved.manifest)
    window.make.controls["font_size"].setValue(84)
    window._prepared(replace(preparation(source), audio=str(audio)))
    current = window.make.get_settings()
    assert current["audio_file"] == str(saved.audio)
    assert current["cover_file"] == str(saved.cover)
    assert current["font_size"] == 84
    assert window.editor.audio_picker.value() == str(saved.audio)
    assert not window.workspace.is_dirty


def test_linked_lyric_json_restores_its_audio_without_a_false_dirty_state(window, tmp_path):
    source = document()
    lyrics = tmp_path / "song.json"
    lyrics.write_text(write_json(source), encoding="utf-8")
    audio = audio_file(tmp_path / "song.wav")
    saved = save_workspace_project(
        tmp_path / "linked",
        name="Linked project",
        lyrics_project=lyrics,
        audio=audio,
        settings={"font_size": 72},
        recent_root=tmp_path / "recent",
    )
    source.metadata["workspace_manifest"] = str(saved.manifest)
    window.workspace.load_project(source, None, str(saved.lyrics_project))
    assert window.editor.audio_picker.value() == str(saved.audio)
    assert window.editor.name_edit.text() == "Linked project"
    assert window.editor.preview._style["font_size"] == 72
    assert not window.workspace.is_dirty


def test_opening_legacy_linked_json_recovers_source_clock(window, tmp_path):
    root = tmp_path / "old-render"
    assets = root / "Song.assets"
    assets.mkdir(parents=True)
    source = document().shifted(2)
    source.metadata["workspace_manifest"] = str(root / PROJECT_FILENAME)
    lyrics = assets / "Song.json"
    lyrics.write_text(write_json(source), encoding="utf-8")
    saved = save_workspace_project(
        root,
        name="Song",
        lyrics_project=lyrics,
        settings={"audio_offset": 2, "auto_sync": False},
        recent_root=tmp_path / "recent",
    )
    window.open_project(str(saved.lyrics_project))
    window.runner.finish()
    assert window.editor.current_document().lines == document().lines
    assert window.make.get_settings()["audio_offset"] == 2


def test_save_as_retains_source_identity_and_current_controls(window, tmp_path):
    source = document()
    lyrics = tmp_path / "song.json"
    lyrics.write_text(write_json(source), encoding="utf-8")
    previous = save_workspace_project(
        tmp_path / "original",
        name="Song",
        lyrics_project=lyrics,
        settings={"source_refs": {"netease": "123456"}, "font_size": 38},
        recent_root=tmp_path / "recent",
    )
    source.metadata["workspace_manifest"] = str(previous.manifest)
    settings = window.make.get_settings()
    settings["font_size"] = 80
    _document, saved, _files = save_workspace_revision(source, settings, str(tmp_path / "copy"))
    assert saved.settings["source_refs"] == {"netease": "123456"}
    assert saved.settings["font_size"] == 80
    assert saved.settings["lyrics_timebase"] == "audio"


@pytest.mark.parametrize("bad_value", ["not a number", float("nan"), None])
def test_corrupt_project_settings_preserve_current_materials_lyrics_and_history(
    window, tmp_path, bad_value
):
    window._prepared(preparation())
    window.make.controls["audio_file"].set_value("previous.wav")
    window.make.controls["output_name"].setText("Previous song")
    window.editor.nudge("start", 0.1)
    before = window.editor.current_document().to_dict()
    settings = window.make.get_settings()
    history = window.editor._history.copy()
    incoming = document()
    lyrics = tmp_path / "incoming.json"
    lyrics.write_text(write_json(incoming), encoding="utf-8")
    saved = save_workspace_project(
        tmp_path / "broken", name="Broken song", lyrics_project=lyrics,
        settings={"font_size": bad_value}, recent_root=tmp_path / "recent",
    )
    with pytest.raises(ValueError, match="当前工程未更改"):
        window.workspace.load_project(incoming, saved, str(saved.manifest))
    assert window.make.get_settings() == settings
    assert window.editor.current_document().to_dict() == before
    assert window.editor._history == history


def test_export_uses_pending_manual_edits_and_current_assets_without_handoff(
    window, monkeypatch, tmp_path
):
    window._prepared(preparation())
    window.editor.lines_table.item(0, 5).setText("manually corrected")
    window.make.controls["video_file"].set_value("new-video.mp4")
    window.make.controls["font_size"].setValue(82)
    captured = []

    def render(**arguments):
        captured.append((arguments, read_lyrics(arguments["lyrics_file"])))
        return UiJobResult("Rendered", None, [str(tmp_path / "render.mp4")], "complete", None)

    monkeypatch.setattr("karaoke_forge.desktop.make_page.run_make_job", render)
    window.workspace.render_video()
    window.runner.finish()
    arguments, submitted = captured[0]
    assert submitted.lines[0].translation == "manually corrected"
    assert submitted.lines[0].pronunciation_units == document().lines[0].pronunciation_units
    assert submitted.lines[1].hidden
    assert submitted.lines[0].tokens[0].confidence == 0.92
    assert arguments["video_file"] == "new-video.mp4"
    assert arguments["font_size"] == 82
    assert arguments["timing_refinement"] == "off"
    assert window.navigation.currentRow() == 0
    assert window.workspace.results_button.isChecked()
    assert window.outputs.files.count() == 1
    assert window.editor.lines_table.item(0, 5).text() == "manually corrected"
    assert window.workspace.is_dirty


def test_pending_new_lyrics_can_be_saved_but_block_export_without_discarding_manual_edits(
    window, monkeypatch, tmp_path
):
    window.workspace.lyrics_picker.set_value("first.lrc")
    window._prepared(preparation())
    window.editor.lines_table.item(0, 5).setText("keep this draft")
    replacement = tmp_path / "second.lrc"
    replacement.write_text("[00:00.00]新しい夢", encoding="utf-8")
    window.workspace.lyrics_picker.set_value(str(replacement))
    destination = tmp_path / "pending"
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(destination))
    assert window.workspace.save_project()
    window.runner.finish()
    window.workspace.render_video()
    assert window.runner.jobs == []
    assert "重新生成时间轴" in window.workspace.activity.text()
    assert window.editor.current_document().lines[0].translation == "keep this draft"
    assert not window.workspace.is_dirty
    stored = load_workspace_project(destination / PROJECT_FILENAME)
    assert stored.settings["pending_lyrics_source"]
    assert read_workspace_lyrics(stored).lines[0].translation == "keep this draft"


def successful_renderer(monkeypatch, tmp_path, settings):
    def render(**arguments):
        document = read_lyrics(arguments["lyrics_file"])
        directory = tmp_path / "rendered"
        _document, saved, files = save_workspace_revision(document, settings, str(directory))
        video = directory / "result.mp4"
        video.write_bytes(b"video")
        return UiJobResult(
            "Rendered", str(video), [*files, str(saved.manifest)], "", str(directory)
        )

    monkeypatch.setattr("karaoke_forge.desktop.make_page.run_make_job", render)


def test_successful_render_marks_matching_project_saved_and_retains_editor_history(
    window, monkeypatch, tmp_path
):
    window._prepared(preparation())
    window.editor.lines_table.item(0, 5).setText("rendered translation")
    window.editor.select_line(1, seek=False)
    successful_renderer(monkeypatch, tmp_path, window.make.get_settings())
    window.workspace.render_video()
    window.runner.finish()
    assert not window.workspace.is_dirty
    assert window.editor._selected == 1
    assert window.navigation.currentRow() == 0
    assert "完整工程已保存" in window.workspace.activity.text()
    assert Path(window.editor.current_document().metadata["workspace_manifest"]).is_file()
    window.editor.undo()
    assert window.editor.current_document().lines[0].translation == "spring"
    assert window.workspace.is_dirty


@pytest.mark.parametrize("changed", ["lyrics", "style"])
def test_successful_render_does_not_mark_newer_lyrics_or_style_saved(
    window, monkeypatch, tmp_path, changed
):
    window._prepared(preparation())
    window.editor.lines_table.item(0, 5).setText("submitted translation")
    successful_renderer(monkeypatch, tmp_path, window.make.get_settings())
    window.workspace.render_video()
    if changed == "lyrics":
        window.editor.lines_table.item(0, 5).setText("newer translation")
    else:
        window.make.controls["font_size"].setValue(86)
    window.runner.finish()
    assert window.workspace.is_dirty
    assert window.workspace.results_button.isChecked()
    if changed == "lyrics":
        assert window.editor.current_document().lines[0].translation == "newer translation"
    else:
        assert window.make.get_settings()["font_size"] == 86


def test_repreparing_dirty_lyrics_requires_explicit_replacement(window, monkeypatch):
    window._prepared(preparation())
    window.editor.lines_table.item(0, 5).setText("keep draft")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    window.workspace.prepare_project()
    assert not window.runner.jobs
    assert window.editor.current_document().lines[0].translation == "keep draft"


def test_clearing_primary_mv_removes_the_current_song_instead_of_restoring_it(window):
    window.workspace.song_picker.set_value("song.mp4")
    assert window.make.get_settings()["video_file"] == "song.mp4"
    window.workspace.song_picker.set_value("")
    assert window.workspace.song_picker.value() == ""
    settings = window.make.get_settings()
    assert not settings["audio_file"] and not settings["video_file"]
    assert "默认动态背景" in window.workspace.visual_summary.text()


def test_choosing_cover_replaces_the_picture_but_keeps_mv_sound(window, monkeypatch):
    window.workspace.song_picker.set_value("song.mp4")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: ("cover.png", ""))
    window.workspace.choose_visual()
    settings = window.make.get_settings()
    assert settings["audio_file"] == "song.mp4"
    assert not settings["video_file"]
    assert settings["cover_file"] == "cover.png"


def test_save_captures_current_project_and_preserves_undo(window, monkeypatch, tmp_path):
    window._prepared(preparation())
    audio = audio_file(tmp_path / "selected.wav")
    window.workspace.song_picker.set_value(str(audio))
    window.make.controls["font_size"].setValue(80)
    window.editor.lines_table.item(0, 5).setText("corrected translation")
    destination = tmp_path / "saved"
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(destination))
    assert window.workspace.save_project()
    window.runner.finish()
    saved = load_workspace_project(destination / PROJECT_FILENAME)
    restored = read_lyrics(saved.lyrics_project)
    assert restored.lines[0].translation == "corrected translation"
    assert saved.audio.is_file()
    assert saved.settings["font_size"] == 80
    ass = saved.lyrics_project.with_suffix(".ass").read_text(encoding="utf-8")
    assert "Microsoft YaHei,80," in ass
    assert not window.workspace.is_dirty
    window.editor.undo()
    assert window.editor.current_document().lines[0].translation == "spring"
    assert window.workspace.is_dirty


@pytest.mark.parametrize("new_draft", ["new translation", "invalid"])
def test_save_completion_does_not_replace_a_newer_or_invalid_draft(
    window, monkeypatch, tmp_path, new_draft
):
    window._prepared(preparation())
    window.editor.lines_table.item(0, 5).setText("submitted translation")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path / "save"))
    assert window.workspace.save_project()
    column = 2 if new_draft == "invalid" else 5
    window.editor.lines_table.item(0, column).setText(new_draft)
    window.make.controls["output_name"].setText("new title")
    window.runner.finish()
    assert window.editor.lines_table.item(0, column).text() == new_draft
    assert window.workspace.is_dirty
    assert "未保存修改" in window.workspace.activity.text()


def test_all_hidden_lyrics_save_a_restorable_json_project(monkeypatch, tmp_path):
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "recent"))
    source = document()
    for line in source.lines:
        line.hidden = True
    _document, saved, files = save_workspace_revision(
        source, {"output_name": "CON"}, str(tmp_path / "project")
    )
    assert len(files) == 1 and Path(files[0]).suffix == ".json"
    assert read_lyrics(saved.lyrics_project).to_dict()["lines"] == source.to_dict()["lines"]


def test_saving_into_another_project_directory_requires_explicit_overwrite(
    window, monkeypatch, tmp_path
):
    destination = tmp_path / "existing"
    _source, existing, _files = save_workspace_revision(
        document(), {"output_name": "Earlier song"}, str(destination)
    )
    original = existing.manifest.read_bytes()
    window._prepared(preparation())
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(destination))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    assert not window.workspace.save_project()
    assert not window.runner.jobs
    assert existing.manifest.read_bytes() == original


def test_workspace_inputs_fit_the_supported_minimum_window(window, qt_app):
    window.workspace.adopt_prepared(preparation())
    window.setFont(QFont("Microsoft YaHei UI", 10))
    theme = Path(karaoke_forge.desktop.__file__).with_name("theme.qss")
    window.setStyleSheet(theme.read_text(encoding="utf-8"))
    window.resize(900, 640)
    window.show()
    qt_app.processEvents()
    assert window.width() == 900
    assert window.workspace.input_bar.minimumSizeHint().width() <= window.workspace.width()
    assert window.workspace.render_button.isVisible()
    assert window.editor.preview.isVisible()


def test_typing_in_integrated_workspace_retains_focus_and_saves_complete_draft(
    window, qt_app, monkeypatch, tmp_path
):
    window._prepared(preparation())
    window.show()
    qt_app.processEvents()
    item = window.editor.lines_table.item(0, 5)
    window.editor.lines_table.editItem(item)
    qt_app.processEvents()
    cell = QApplication.focusWidget()
    assert isinstance(cell, QLineEdit)
    cell.selectAll()
    QTest.keyClicks(cell, "first complete sentence")
    assert QApplication.focusWidget() is cell
    assert cell.text() == "first complete sentence"
    assert window.workspace.is_dirty
    assert QApplication.focusWidget() is cell
    # The save boundary, rather than a status query, commits the active editor.
    destination = tmp_path / "live-save"
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(destination))
    assert window.workspace.save_project()
    window.runner.finish()
    saved = load_workspace_project(destination / PROJECT_FILENAME)
    assert read_lyrics(saved.lyrics_project).lines[0].translation == "first complete sentence"
    assert not window.workspace.is_dirty


def test_failed_empty_project_import_preserves_current_lyrics_materials_and_history(window):
    window._prepared(preparation())
    window.make.controls["audio_file"].set_value("current.wav")
    window.make.controls["output_name"].setText("Current song")
    window.editor.lines_table.item(0, 5).setText("keep this correction")
    window.editor.apply_pending()
    before_settings = window.make.get_settings()
    before_document = window.editor.current_document().to_dict()
    with pytest.raises(ValueError, match="当前工程未更改"):
        window.workspace.load_project(LyricsDocument([]), None, "empty.json")
    assert window.make.get_settings() == before_settings
    assert window.editor.current_document().to_dict() == before_document
    window.editor.undo()
    assert window.editor.current_document().lines[0].translation == "spring"


def test_saving_project_replaces_stale_result_files_and_directory(window, monkeypatch, tmp_path):
    window._prepared(preparation())
    earlier = tmp_path / "earlier"
    window.workspace.show_result(
        UiJobResult("Earlier", None, [str(earlier / "old.ass")], "", str(earlier))
    )
    window.workspace.results_button.setChecked(False)
    destination = tmp_path / "saved-now"
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(destination))
    assert window.workspace.save_project()
    window.runner.finish()
    assert Path(window.outputs.directory) == destination
    names = [window.outputs.files.item(i).text() for i in range(window.outputs.files.count())]
    assert "old.ass" not in names
    assert PROJECT_FILENAME in names
    assert len(names) == 7
    assert not window.workspace.results_button.isChecked()


@pytest.mark.parametrize("valid_source", [True, False])
def test_tools_show_success_or_failure_on_the_current_page_without_overwriting_song_results(
    window, qt_app, tmp_path, valid_source
):
    source = tmp_path / "convert.json"
    if valid_source:
        source.write_text(write_json(document()), encoding="utf-8")
    window.outputs.directory = "unchanged-song-output"
    window.show()
    window.navigation.setCurrentRow(1)
    window.tools.convert_source.set_value(str(source))
    window.tools.convert()
    window.runner.finish()
    qt_app.processEvents()
    assert window.navigation.currentRow() == 1
    assert window.tools.result_status.isVisible()
    assert "开始：" not in window.statusBar().currentMessage()
    assert window.tools.result_files.count() == (1 if valid_source else 0)
    assert window.tools.continue_button.isEnabled() == valid_source
    assert window.outputs.directory == "unchanged-song-output"
    if valid_source:
        requests = []
        window.tools.open_requested.disconnect()
        window.tools.open_requested.connect(requests.append)
        window.tools.continue_button.click()
        assert len(requests) == 1 and Path(requests[0]).is_file()


def test_corrupt_model_settings_do_not_block_startup_and_can_be_repaired(
    qt_app, monkeypatch, tmp_path
):
    from karaoke_forge.network import load_model_download_settings, settings_file_path

    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path / "broken-settings"))
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr("karaoke_forge.desktop.app.JobRunner", DeferredRunner)
    config = settings_file_path()
    config.parent.mkdir(parents=True)
    config.write_text("{broken", encoding="utf-8")
    widget = MainWindow()
    try:
        assert "设置文件无效" in widget.environment.report.toPlainText()
        assert widget.environment.mode.currentData() == "modelscope"
        widget.environment.mode.setCurrentIndex(widget.environment.mode.findData("offline"))
        widget.environment.save()
        widget.runner.finish()
        assert load_model_download_settings().mode == "offline"
        assert "设置文件无效" not in widget.environment.report.toPlainText()
    finally:
        widget.close()
        widget.deleteLater()
        qt_app.processEvents()


def test_tools_continue_prefers_lossless_json_and_retains_word_timing_and_annotations(
    window, tmp_path
):
    from karaoke_forge.formats import export_formats

    source = document()
    source.lines[0].tokens = [KaraokeToken("春", 0.15, 0.85, confidence=0.92)]
    source.metadata["auto_pronunciation"] = "false"
    outputs = export_formats(
        source, tmp_path / "tool-output", "song", ["lrc", "elrc", "srt", "vtt", "ass", "json"]
    )
    window.tools._show_result(
        UiJobResult(
            "Completed",
            None,
            [str(path) for path in outputs.values()],
            "",
            str(tmp_path / "tool-output"),
        )
    )
    assert window.tools._result_project == str(outputs["json"])
    window.tools.continue_button.click()
    window.runner.finish()
    restored = window.editor.current_document()
    assert restored.to_dict() == source.to_dict()
    assert restored.lines[0].translation == "spring"
    assert restored.lines[0].tokens[0].confidence == 0.92
    assert restored.lines[0].pronunciation_units == source.lines[0].pronunciation_units
    assert restored.lines[1].hidden


def test_tools_continue_prioritizes_complete_manifest_regardless_of_result_order(window):
    for paths in (
        ["song.lrc", "song.enhanced.lrc", "song.json", PROJECT_FILENAME],
        [PROJECT_FILENAME, "song.json", "song.lrc"],
    ):
        window.tools._show_result(UiJobResult("Completed", None, paths, "", ""))
        assert window.tools._result_project == PROJECT_FILENAME
    window.tools._show_result(
        UiJobResult("Completed", None, ["song.lrc", "song.enhanced.lrc"], "", "")
    )
    assert window.tools._result_project == "song.enhanced.lrc"


@pytest.mark.parametrize("source_kind", ["local", "paste", "netease"])
def test_created_configuration_is_saved_and_reopens_before_any_timeline_exists(
    window, tmp_path, source_kind
):
    audio = audio_file(tmp_path / "selected.wav")
    settings = {"output_name": "Configured song", "audio_file": str(audio), "font_size": 67}
    if source_kind == "local":
        source = tmp_path / "lyrics.txt"
        source.write_text("春の夢", encoding="utf-8")
        settings["lyrics_file"] = str(source)
    elif source_kind == "paste":
        settings["pasted_lyrics"] = "春の夢\nsing with me"
    else:
        settings.update({"netease_link": "12345", "use_netease_lyrics": True})
    destination = tmp_path / "configured"
    assert window.workspace.start_project(settings, str(destination))
    assert window.workspace.has_project and not window.workspace._has_document
    assert not window.workspace.is_dirty
    assert window.workspace.project_directory == str(destination)
    assert not window.runner.jobs
    saved = load_workspace_project(destination / PROJECT_FILENAME)
    pending = read_workspace_lyrics(saved)
    assert pending.lines == [] and pending.metadata["project_state"] == "configured"
    assert saved.audio.is_file() and saved.audio != audio
    assert saved.settings["font_size"] == 67
    audio.unlink()
    if source_kind == "local":
        source.unlink()
    window.workspace.load_project(pending, saved, str(saved.manifest))
    values = window.make.get_settings()
    assert values["font_size"] == 67 and values["audio_file"] == str(saved.audio)
    assert not window.workspace.is_dirty
    assert not window.workspace.render_button.isEnabled()
    assert window.workspace.pages.currentWidget() is window.workspace.empty_page
    if source_kind == "local":
        assert Path(values["lyrics_file"]).read_text(encoding="utf-8") == "春の夢"
    elif source_kind == "paste":
        assert values["pasted_lyrics"] == "春の夢\nsing with me"
        assert values["lyrics_file"] == ""
    else:
        assert values["netease_link"] == "12345" and values["lyrics_file"] == ""


def test_creation_save_failure_preserves_open_document_settings_and_history(
    window, monkeypatch, tmp_path
):
    window.workspace.adopt_prepared(preparation())
    window.editor.nudge("start", 0.1)
    before = window.editor.current_document().to_dict()
    controls = window.make.get_settings()
    history = window.editor._history.copy()

    def fail(*args, **kwargs):
        raise OSError("read-only destination")

    monkeypatch.setattr("karaoke_forge.desktop.workspace.save_workspace_revision", fail)
    assert not window.workspace.start_project({"output_name": "Replacement"}, str(tmp_path / "new"))
    assert window.editor.current_document().to_dict() == before
    assert window.make.get_settings() == controls
    assert window.editor._history == history
    assert "当前工程仍保留" in window.workspace.activity.text()


def test_configured_source_in_project_directory_is_not_overwritten_by_placeholder(
    window, tmp_path
):
    destination = tmp_path / "created"
    destination.mkdir()
    source = destination / "Song.json"
    original = write_json(document())
    source.write_text(original, encoding="utf-8")
    assert window.workspace.start_project(
        {"output_name": "Song", "lyrics_file": str(source)}, str(destination)
    )
    saved = load_workspace_project(destination / PROJECT_FILENAME)
    assert source.read_text(encoding="utf-8") == original
    assert saved.lyrics_project != source
    assert saved.settings["lyrics_file"] == str(source)
    assert not read_workspace_lyrics(saved).lines


def test_configured_project_ctrl_s_saves_in_place_and_save_as_chooses_a_new_directory(
    window, tmp_path, monkeypatch
):
    destination = tmp_path / "created"
    assert window.workspace.start_project(
        {"output_name": "Song", "pasted_lyrics": "春の夢"}, str(destination)
    )
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: pytest.fail("no reprompt"))
    window.make.controls["pasted_lyrics"].setPlainText("新しい夢")
    assert window.workspace.save_project()
    window.runner.finish()
    saved = load_workspace_project(destination / PROJECT_FILENAME)
    assert saved.settings["pasted_lyrics"] == "新しい夢"
    assert not window.workspace.is_dirty
    copied = tmp_path / "copy"
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(copied))
    assert window.workspace.save_project(as_new=True)
    window.runner.finish()
    assert window.workspace.project_directory == str(copied)
    assert load_workspace_project(copied / PROJECT_FILENAME).settings["pasted_lyrics"] == "新しい夢"


@pytest.mark.parametrize("source_kind", ["local", "paste"])
def test_changed_lyrics_source_is_saved_reopened_and_used_for_generation(
    window, tmp_path, monkeypatch, source_kind
):
    destination = tmp_path / "song"
    saved_document, saved, _ = save_workspace_revision(
        document(), {"output_name": "Song"}, str(destination)
    )
    window.workspace.load_project(saved_document, saved, str(saved.manifest))
    window.editor.lines_table.item(0, 5).setText("Keep the edited translation")
    settings = window.make.get_settings()
    if source_kind == "local":
        source = tmp_path / "replacement.txt"
        source.write_text("新しい夢", encoding="utf-8")
        settings.update({"lyrics_file": str(source), "pasted_lyrics": ""})
    else:
        settings.update({"lyrics_file": "", "pasted_lyrics": "新しい夢"})
    assert window.workspace.apply_project_settings(settings)
    window.runner.finish()
    stored = load_workspace_project(destination / PROJECT_FILENAME)
    reopened = read_workspace_lyrics(stored)
    assert reopened.lines[0].translation == "Keep the edited translation"
    assert stored.settings["pending_lyrics_source"]
    if source_kind == "local":
        source.unlink()
    window.workspace.load_project(reopened, stored, str(stored.manifest))
    window.workspace.render_video()
    assert not window.runner.jobs and "重新生成时间轴" in window.workspace.activity.text()
    captured = []

    def generate(**kwargs):
        captured.append(kwargs)
        return preparation()

    monkeypatch.setattr("karaoke_forge.desktop.make_page.prepare_make_editor_job", generate)
    window.workspace.prepare_project()
    window.runner.finish()
    if source_kind == "local":
        assert Path(captured[0]["lyrics_file"]).read_text(encoding="utf-8") == "新しい夢"
    else:
        assert captured[0]["pasted_lyrics"] == "新しい夢" and captured[0]["lyrics_file"] is None
    assert window.workspace.project_directory == str(destination)
    assert window.editor.current_document().metadata["workspace_manifest"] == str(stored.manifest)
    assert not load_workspace_project(stored.manifest).settings["pending_lyrics_source"]
    assert not window.workspace.is_dirty


def test_style_settings_save_keeps_lyrics_history_and_does_not_request_new_alignment(
    window, tmp_path
):
    destination = tmp_path / "song"
    saved_document, saved, _ = save_workspace_revision(
        document(), {"output_name": "Song"}, str(destination)
    )
    window.workspace.load_project(saved_document, saved, str(saved.manifest))
    window.editor.nudge("start", 0.1)
    before = window.editor.current_document().to_dict()["lines"]
    history = window.editor._history.copy()
    assert window.workspace.apply_project_settings({"font_size": 72})
    window.runner.finish()
    assert window.editor.current_document().to_dict()["lines"] == before
    assert window.editor._history == history
    assert not window.workspace._source_pending()
    assert not window.workspace.is_dirty
    assert load_workspace_project(saved.manifest).settings["font_size"] == 72


def test_generated_and_rendered_results_keep_the_created_project_directory(
    window, monkeypatch, tmp_path
):
    destination = tmp_path / "project-home"
    assert window.workspace.start_project(
        {"output_name": "Song", "pasted_lyrics": "春"}, str(destination)
    )
    generated = tmp_path / "temporary-generation"
    generated.mkdir()
    intermediate, _saved, _files = save_workspace_revision(
        document(), {"output_name": "Song"}, str(generated)
    )
    window.workspace.adopt_prepared(replace(preparation(intermediate), output_dir=str(generated)))
    assert window.workspace.project_directory == str(destination)
    assert window.editor.current_document().metadata["workspace_manifest"] == str(destination / PROJECT_FILENAME)
    window.editor.lines_table.item(0, 5).setText("New saved translation")
    successful_renderer(monkeypatch, tmp_path, window.make.get_settings())
    window.workspace.render_video()
    window.runner.finish()
    assert window.workspace.project_directory == str(destination)
    assert window.editor.current_document().metadata["workspace_manifest"] == str(destination / PROJECT_FILENAME)
    assert read_workspace_lyrics(load_workspace_project(destination / PROJECT_FILENAME)).lines[0].translation == "New saved translation"
    assert not window.workspace.is_dirty


def test_editor_has_no_visible_project_setup_inputs(window, qt_app):
    window.workspace.adopt_prepared(preparation())
    window.show()
    qt_app.processEvents()
    assert window.workspace.project_name.isVisible()
    assert window.workspace.source_button.isVisible()
    assert window.workspace.source_button.text() == "项目设置…"
    assert window.workspace.song_picker.isHidden()
    assert window.workspace.lyrics_picker.isHidden()
    assert window.workspace.name_edit.isHidden()
    assert window.make.isHidden()


def test_copied_project_restores_its_local_lyrics_source_inside_the_new_directory(
    window, tmp_path
):
    source = tmp_path / "source.txt"
    source.write_text("春の夢", encoding="utf-8")
    original = tmp_path / "original"
    assert window.workspace.start_project(
        {"output_name": "Song", "lyrics_file": str(source)}, str(original)
    )
    copied = tmp_path / "copied"
    shutil.copytree(original, copied)
    moved = load_workspace_project(copied / PROJECT_FILENAME)
    window.workspace.load_project(read_workspace_lyrics(moved), moved, str(moved.manifest))
    restored = Path(window.make.get_settings()["lyrics_file"])
    assert restored.is_relative_to(copied)
    assert restored.read_text(encoding="utf-8") == "春の夢"
    assert window.workspace.project_directory == str(copied)


def test_opening_another_project_clears_previous_output_playback_and_files(window, tmp_path):
    window.workspace.adopt_prepared(preparation())
    earlier = tmp_path / "earlier"
    window.workspace.show_result(
        UiJobResult("Earlier", None, [str(earlier / "old.ass")], "", str(earlier))
    )
    saved_document, saved, _ = save_workspace_revision(
        document(), {"output_name": "Another song"}, str(tmp_path / "another")
    )
    window.workspace.load_project(saved_document, saved, str(saved.manifest))
    assert not window.outputs.files.count()
    assert not window.outputs.directory
    assert window.outputs.player.source().isEmpty()
    assert not window.workspace.results_button.isChecked()


def test_project_archive_keeps_event_loop_alive_and_cannot_be_abandoned(
    window, qt_app, monkeypatch, tmp_path
):
    from karaoke_forge.desktop.workspace import _ArchiveProgress

    ui_thread = threading.get_ident()
    observed = []
    heartbeat = threading.Event()

    def archive(*args):
        assert threading.get_ident() != ui_thread
        assert heartbeat.wait(3), "GUI event loop did not process the heartbeat"
        return "saved document", "saved workspace", []

    def interact():
        progress = QApplication.activeModalWidget()
        if not isinstance(progress, _ArchiveProgress):
            return
        observed.append(window.workspace.is_saving_revision)
        QTest.keyClick(progress, Qt.Key.Key_Escape)
        assert progress.isVisible()
        progress.close()
        assert progress.isVisible()
        timer.stop()
        heartbeat.set()

    monkeypatch.setattr("karaoke_forge.desktop.workspace.save_workspace_revision", archive)
    timer = QTimer()
    timer.timeout.connect(interact)
    timer.start(10)
    try:
        result = window.workspace._save_revision_responsive(document(), {}, str(tmp_path))
    finally:
        timer.stop()
    assert result == ("saved document", "saved workspace", [])
    assert observed == [True]
    assert not window.workspace.is_saving_revision


def test_cancelled_project_settings_leave_lyrics_and_configuration_untouched(window, monkeypatch):
    window.workspace.adopt_prepared(preparation())
    window.editor.lines_table.item(0, 5).setText("Keep my draft")
    before = window.editor.current_document().to_dict()
    settings = window.make.get_settings()
    source_dialog(monkeypatch, accepted=False)
    window.edit_project_sources()
    assert window.editor.current_document().to_dict() == before
    assert window.make.get_settings() == settings
    assert window.workspace.is_dirty
    assert not window.runner.jobs


def test_failed_project_settings_save_retains_open_and_saved_revision(window, tmp_path):
    destination = tmp_path / "song"
    saved_document, saved, _ = save_workspace_revision(
        document(), {"output_name": "Song"}, str(destination)
    )
    window.workspace.load_project(saved_document, saved, str(saved.manifest))
    window.editor.nudge("start", 0.1)
    before = window.editor.current_document().to_dict()
    history = window.editor._history.copy()
    settings = window.make.get_settings()
    manifest = saved.manifest.read_bytes()
    lyrics = saved.lyrics_project.read_bytes()
    assert window.workspace.apply_project_settings({
        "lyrics_file": str(tmp_path / "missing-new-source.txt"), "font_size": 86,
    })
    with pytest.raises(FileNotFoundError, match="歌词文件不存在"):
        window.runner.finish()
    assert window.editor.current_document().to_dict() == before
    assert window.editor._history == history
    assert window.make.get_settings() == settings
    assert window.workspace.is_dirty
    assert saved.manifest.read_bytes() == manifest
    assert saved.lyrics_project.read_bytes() == lyrics


@pytest.mark.parametrize("new_name", ["Song", "Renamed"])
def test_manifest_failure_restores_the_previous_lyrics_revision(monkeypatch, tmp_path, new_name):
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "recent"))
    _document, saved, _files = save_workspace_revision(
        document(), {"output_name": "Song"}, str(tmp_path)
    )
    before = {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    original_save = save_workspace_project

    def fail_after_manifest(*args, **kwargs):
        original_save(*args, **kwargs)
        raise OSError("catalog destination is unavailable")

    monkeypatch.setattr("karaoke_forge.desktop.workspace.save_workspace_project", fail_after_manifest)
    revised = document()
    revised.lines[0].translation = "Must not replace the saved revision"
    with pytest.raises(OSError, match="catalog destination"):
        save_workspace_revision(revised, {"output_name": new_name}, str(tmp_path))
    assert {path: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()} == before
    assert read_workspace_lyrics(load_workspace_project(saved.manifest)).lines[0].translation == "spring"


@pytest.mark.parametrize("choose_existing", [False, True])
def test_settings_for_loose_lyrics_cancel_without_changing_the_open_document(
    window, monkeypatch, tmp_path, choose_existing
):
    window.workspace.adopt_prepared(preparation())
    window.editor.nudge("start", 0.1)
    before = window.editor.current_document().to_dict()
    controls = window.make.get_settings()
    destination = tmp_path / "other-project"
    if choose_existing:
        save_workspace_revision(document(), {"output_name": "Other"}, str(destination))
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *args: str(destination) if choose_existing else ""
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    assert not window.workspace.apply_project_settings({"font_size": 78})
    assert window.editor.current_document().to_dict() == before
    assert window.make.get_settings() == controls
    assert window.workspace.is_dirty
    assert not window.workspace.project_directory
    assert not window.runner.jobs


def test_new_project_preserves_unrelated_same_name_lyrics_through_generation(window, tmp_path):
    destination = tmp_path / "existing-material-folder"
    destination.mkdir()
    unrelated = destination / "Song.json"
    unrelated.write_text("Unrelated original lyrics", encoding="utf-8")
    assert window.workspace.start_project(
        {"output_name": "Song", "pasted_lyrics": "春"}, str(destination)
    )
    saved = load_workspace_project(destination / PROJECT_FILENAME)
    assert saved.lyrics_project != unrelated
    assert window.workspace.save_project()
    window.runner.finish()
    window.workspace.adopt_prepared(preparation())
    assert unrelated.read_text(encoding="utf-8") == "Unrelated original lyrics"
    assert read_workspace_lyrics(load_workspace_project(saved.manifest)).lines


def test_changing_to_online_audio_requires_preparation_before_render(window, tmp_path):
    saved_document, saved, _ = save_workspace_revision(
        document(), {"output_name": "Song", "netease_link": "123"}, str(tmp_path / "song")
    )
    window.workspace.load_project(saved_document, saved, str(saved.manifest))
    assert not window.workspace._source_pending()
    assert window.workspace.apply_project_settings({"prefer_netease_audio": True})
    window.runner.finish()
    assert window.workspace._source_pending()
    window.workspace.render_video()
    assert not window.runner.jobs
    assert "重新生成时间轴" in window.workspace.activity.text()
    stored = load_workspace_project(saved.manifest)
    assert stored.settings["pending_lyrics_source"]


def test_first_generation_does_not_overwrite_an_unrelated_lrc_beside_the_placeholder(window, tmp_path):
    destination = tmp_path / "materials"
    destination.mkdir()
    unrelated = destination / "Song.lrc"
    unrelated.write_text("User's original subtitles", encoding="utf-8")
    assert window.workspace.start_project(
        {"output_name": "Song", "pasted_lyrics": "春"}, str(destination)
    )
    configured = load_workspace_project(destination / PROJECT_FILENAME)
    assert configured.lyrics_project.name == "Song.json"
    assert configured.settings["lyrics_export_formats"] == ["json"]
    window.workspace.adopt_prepared(preparation())
    assert unrelated.read_text(encoding="utf-8") == "User's original subtitles"
    generated = load_workspace_project(configured.manifest)
    assert generated.lyrics_project != configured.lyrics_project
    assert read_workspace_lyrics(generated).lines
