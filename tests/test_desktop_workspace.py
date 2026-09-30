"""The native production flow keeps the current editor and project choices in place."""

from __future__ import annotations

import os
import wave
from dataclasses import replace
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

import karaoke_forge.desktop  # prepare Windows ICU before Qt

# isort: split
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from karaoke_forge.desktop.app import MainWindow
from karaoke_forge.desktop.workspace import save_workspace_revision
from karaoke_forge.formats import read_lyrics, write_json
from karaoke_forge.models import KaraokeToken, LyricLine, LyricsDocument, PronunciationSpan
from karaoke_forge.projects import PROJECT_FILENAME, load_workspace_project, save_workspace_project
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


def test_pending_new_lyrics_block_save_and_export_without_discarding_manual_edits(
    window, monkeypatch
):
    window.workspace.lyrics_picker.set_value("first.lrc")
    window._prepared(preparation())
    window.editor.lines_table.item(0, 5).setText("keep this draft")
    window.workspace.lyrics_picker.set_value("second.lrc")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: pytest.fail("no save"))
    assert window.workspace.save_project() is False
    window.workspace.render_video()
    assert window.runner.jobs == []
    assert "先载入" in window.workspace.activity.text()
    assert window.editor.current_document().lines[0].translation == "keep this draft"
    assert window.workspace.is_dirty


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
