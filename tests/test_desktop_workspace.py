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
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QLineEdit, QMessageBox

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
    destination = tmp_path / "saved-now"
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(destination))
    assert window.workspace.save_project()
    window.runner.finish()
    assert Path(window.outputs.directory) == destination
    names = [window.outputs.files.item(i).text() for i in range(window.outputs.files.count())]
    assert "old.ass" not in names
    assert PROJECT_FILENAME in names
    assert len(names) == 7
    assert window.workspace.results_button.isChecked()


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
