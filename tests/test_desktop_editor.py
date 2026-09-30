"""Native editor data-flow regressions, without a desktop or audio device."""

from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

# Preload the app's Windows ICU guard before loading Qt extension modules.
importlib.import_module("karaoke_forge.desktop")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QMainWindow, QTableWidgetItem

from karaoke_forge.desktop.editor_page import EditorPage
from karaoke_forge.editor_history import history_stacks
from karaoke_forge.models import (
    KaraokeToken,
    LyricLine,
    LyricsDocument,
    PronunciationSpan,
)
from karaoke_forge.preferences import load_preferences, preferences_path, save_preferences
from karaoke_forge.projects import (
    PROJECT_FILENAME,
    list_workspace_projects,
    load_workspace_project,
    save_workspace_project,
)


class ImmediateRunner(QObject):
    message = Signal(str)
    busy_changed = Signal(bool)

    def submit(self, _title, task, on_success):
        on_success(task(self.message.emit))
        return True


class DeferredRunner(ImmediateRunner):
    def submit(self, _title, task, on_success):
        self.task = task
        self.on_success = on_success
        return True

    def complete(self):
        self.on_success(self.task(self.message.emit))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def document():
    return LyricsDocument(
        lines=[
            LyricLine(
                "hello world",
                1.0,
                3.0,
                tokens=[
                    KaraokeToken("hello ", 1.0, 2.0, 0.91),
                    KaraokeToken("world", 2.0, 3.0, 0.94),
                ],
                translation="你好世界",
                pronunciation="he lo",
                pronunciation_units=[PronunciationSpan("hello", "hé lō", 0, 5)],
            ),
            LyricLine("next", 4.0, 6.0, tokens=[KaraokeToken("next", 4, 6, 0.7)]),
            LyricLine("", 7.0, 8.0, hidden=True),
        ],
        metadata={"ti": "保留工程", "auto_pronunciation": "false", "custom": "保留元数据"},
        source_format="ass",
    )


@pytest.fixture
def page(app, document, monkeypatch, tmp_path):
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "recent-output"))
    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path / "settings"))
    runner = ImmediateRunner()
    editor = EditorPage(runner)
    editor.load_document(document, name="保留工程")
    yield editor
    editor.stop_playback()
    editor.close()
    editor.deleteLater()
    app.processEvents()


def set_cell(table, row, column, value):
    item = table.item(row, column)
    if item is None:
        table.setItem(row, column, QTableWidgetItem(value))
    else:
        item.setText(value)


def test_accept_saved_workspace_preserves_undo_and_rejects_other_revision(page):
    page.nudge("start", 0.1)
    saved = page.current_document()
    saved.metadata["workspace_manifest"] = "saved/project.json"
    assert page.adopt_saved_revision(saved)
    assert not page.is_dirty
    assert saved.metadata["workspace_manifest"] == "saved/project.json"
    assert page.current_document().metadata["workspace_manifest"] == "saved/project.json"
    page.undo()
    assert page.current_document().lines[0].start == 1.0
    assert page.is_dirty
    assert not page.adopt_saved_revision(saved)
    assert page.current_document().lines[0].start == 1.0


def test_workspace_mode_hides_duplicate_import_and_handoff_controls(page):
    page.set_workspace_mode()
    assert page.heading_bar.isHidden()
    assert page.sources_bar.isHidden()
    assert page.actions_bar.isHidden()
    assert not page.lines_table.isHidden()
    assert not page.preview.isHidden()


def test_load_and_read_are_lossless_and_do_not_share_mutable_state(page, document):
    assert not page.is_dirty
    assert page.current_document().to_dict() == document.to_dict()
    assert page.current_document().source_format == "ass"
    result = page.current_document()
    result.lines[0].text = "external modification"
    assert page.current_document().lines[0].text == "hello world"
    document.metadata["custom"] = "external metadata"
    assert page.current_document().metadata["custom"] == "保留元数据"


def test_pending_translation_commits_before_navigation_and_undo_returns_clean(page):
    changes = []
    page.changed.connect(changes.append)
    set_cell(page.lines_table, 0, 5, "更正翻译")
    assert page.is_dirty
    assert page.current_document().lines[0].translation == "更正翻译"
    page.select_line(1)
    assert page._selected == 1
    assert page._document.lines[0].translation == "更正翻译"
    page.undo()
    assert not page.is_dirty
    assert page.current_document().lines[0].translation == "你好世界"
    page.redo()
    assert page.is_dirty
    assert page.current_document().lines[0].translation == "更正翻译"
    assert changes == [True, False, True]


def test_live_cell_editor_is_included_in_close_checks_and_document_reads(page, app):
    page.show()
    app.processEvents()
    item = page.lines_table.item(0, 5)
    page.lines_table.editItem(item)
    app.processEvents()
    editor = QApplication.focusWidget()
    assert isinstance(editor, QLineEdit)
    editor.selectAll()
    QTest.keyClicks(editor, "live cell draft")
    assert page.dirty_label.text() == "● 未保存修改"
    assert item.text() == "你好世界"
    assert page.is_dirty
    assert page.current_document().lines[0].translation == "live cell draft"


def test_window_save_shortcut_remains_unambiguous_and_shift_z_redoes(page, app):
    window = QMainWindow()
    window.setCentralWidget(page)
    save = QAction("Save", window)
    save.setShortcut(QKeySequence.StandardKey.Save)
    window.addAction(save)
    triggered = []
    save.triggered.connect(lambda: triggered.append(True))
    try:
        window.show()
        app.processEvents()
        page.lines_table.setFocus()
        QTest.keyClick(page.lines_table, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        assert triggered == [True]
        page.toggle_hidden()
        page.undo()
        assert not page.current_document().lines[0].hidden
        QTest.keyClick(
            page.lines_table,
            Qt.Key.Key_Z,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )
        assert page.current_document().lines[0].hidden
    finally:
        window.takeCentralWidget().setParent(None)
        window.close()


def test_preferences_restore_without_rewriting_then_persist_control_changes(page, document):
    save_preferences(
        {
            "font_size": 74,
            "playback_rate": 1.5,
            "ripple_following": False,
            "snap_enabled": False,
            "global_zoom": 3.0,
            "follow_playback": False,
        }
    )
    stored = preferences_path().read_bytes()
    restored = EditorPage(page.runner)
    try:
        restored.load_document(document)
        assert preferences_path().read_bytes() == stored
        assert restored.speed.currentData() == 1.5
        assert restored.player.playbackRate() == 1.5
        assert not restored.ripple.isChecked()
        assert not restored.snap.isChecked()
        assert not restored.timeline.snap_enabled
        assert not restored.token_timeline.snap_enabled
        assert restored.zoom.currentData() == 3.0
        assert not restored.follow_playback.isChecked()
        restored.speed.setCurrentIndex(restored.speed.findData(0.75))
        restored.ripple.setChecked(True)
        restored.snap.setChecked(True)
        restored.zoom.setCurrentIndex(restored.zoom.findData(4))
        restored.follow_playback.setChecked(True)
        assert load_preferences() == {
            "font_size": 74,
            "playback_rate": 0.75,
            "ripple_following": True,
            "snap_enabled": True,
            "global_zoom": 4.0,
            "follow_playback": True,
        }
        assert not restored.is_dirty
    finally:
        restored.stop_playback()
        restored.close()
        restored.deleteLater()


def test_preference_save_error_leaves_the_control_effective(page, monkeypatch):
    def unavailable(_patch):
        raise OSError("test read-only settings")

    monkeypatch.setattr("karaoke_forge.desktop.editor_page.save_preferences", unavailable)
    page.snap.setChecked(False)
    assert not page.timeline.snap_enabled
    assert not page.token_timeline.snap_enabled
    assert "暂时无法保存" in page.status.text()


def test_follow_toggle_and_return_button_only_move_the_view(page, monkeypatch):
    calls = []
    monkeypatch.setattr(
        page.timeline, "reveal_position", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setattr(page.token_timeline, "reveal_position", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        page.player, "playbackState", lambda: QMediaPlayer.PlaybackState.PlayingState
    )
    page._position_changed(4200)
    assert calls == [((4.2,), {})]
    page.follow_playback.setChecked(False)
    page._position_changed(4400)
    assert len(calls) == 1
    monkeypatch.setattr(page.player, "position", lambda: 4400)
    page.return_playhead_button.click()
    assert calls[-1] == ((4.4,), {"center": True})
    assert not page.follow_playback.isChecked()
    assert not page.is_dirty


@pytest.mark.parametrize("invalid", ["bad", "nan", "inf", "-1", "4"])
def test_invalid_draft_cannot_leave_the_selected_line_or_handoff(page, invalid):
    handoffs = []
    page.handoff.connect(handoffs.append)
    set_cell(page.lines_table, 0, 2, invalid)
    with pytest.raises(ValueError):
        page.current_document()
    page.select_line(1)
    page._handoff()
    assert page._selected == 0
    assert handoffs == []
    assert page.is_dirty
    page.undo()
    assert not page.is_dirty
    assert page.current_document().lines[0].start == 1.0


def test_token_edit_keeps_untouched_lines_confidence_and_pronunciation(page, document):
    set_cell(page.tokens_table, 1, 2, "3.2")
    candidate = page.current_document()
    assert candidate.lines[0].tokens[0].confidence == 0.91
    assert candidate.lines[0].pronunciation_units == document.lines[0].pronunciation_units
    assert candidate.lines[1:] == document.lines[1:]
    page.apply_pending()
    page.undo()
    assert page.current_document().to_dict() == document.to_dict()


def test_pronunciation_pending_is_in_handoff_without_marking_saved(page):
    handoffs = []
    page.handoff.connect(handoffs.append)
    set_cell(page.pronunciation_table, 0, 1, "new reading")
    page._handoff()
    assert len(handoffs) == 1
    assert handoffs[0].lines[0].pronunciation_units[0].reading == "new reading"
    assert page.is_dirty
    page.mark_saved()
    assert not page.is_dirty
    page.undo()
    assert page.is_dirty


def test_structure_changes_and_history_preserve_hidden_interludes(page, document):
    page.insert_line(True)
    assert len(page.current_document().lines) == 4
    assert page._selected == 1
    page.toggle_hidden()
    assert page.current_document().lines[1].hidden
    page.delete_line()
    assert len(page.current_document().lines) == 3
    page.undo()
    assert page.current_document().lines[1].hidden
    page.undo()
    assert not page.current_document().lines[1].hidden
    page.undo()
    assert page.current_document().to_dict() == document.to_dict()
    assert page.current_document().lines[-1].tokens == []


def test_history_is_bounded_and_editing_after_undo_discards_redo(page):
    for _ in range(105):
        page.toggle_hidden()
    past, future = history_stacks(page._history)
    assert len(past) == 100
    assert future == []
    page.undo()
    assert len(history_stacks(page._history)[1]) == 1
    page.insert_line()
    assert history_stacks(page._history)[1] == []


def test_nudge_and_shift_do_not_hydrate_or_strip_untouched_lines(page, document):
    document.lines[1].text = "  next  "
    document.lines[1].tokens = []
    page.load_document(document)
    page.nudge("end", 0.1)
    assert page.current_document().lines[1:] == document.lines[1:]
    page.undo()
    page.shift_seconds.setValue(0.25)
    page.shift_timing()
    shifted = page.current_document()
    assert shifted.lines[1].text == "  next  "
    assert shifted.lines[1].tokens == []
    assert shifted.lines[1].start == 4.25
    page.undo()
    assert page.current_document().to_dict() == document.to_dict()


def test_audio_and_name_changes_are_dirty_and_mark_saved_resets(page):
    page.name_edit.setText("新工程名")
    assert page.is_dirty
    page.mark_saved()
    assert not page.is_dirty
    page.audio_picker.set_value("new-song.wav")
    assert page.is_dirty


def test_export_roundtrip_keeps_project_assets_settings_and_all_formats(page, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    audio = source / "song.wav"
    audio.write_bytes(b"test fixture")
    cover = source / "cover.png"
    cover.write_bytes(b"test cover")
    lyrics = source / "lyrics.json"
    lyrics.write_text(json.dumps(page.current_document().to_dict()), encoding="utf-8")
    workspace = save_workspace_project(
        source,
        name="原工程",
        lyrics_project=lyrics,
        audio=audio,
        cover=cover,
        settings={"font_size": 74, "custom_setting": "keep"},
    )
    page.load_source(str(workspace.manifest))
    set_cell(page.lines_table, 0, 5, "已编辑")
    output = tmp_path / "output"
    files = page.export_to(str(output))
    assert {Path(path).suffix for path in files} == {".lrc", ".srt", ".vtt", ".ass", ".json"}
    assert len(files) == 7
    assert not page.is_dirty
    saved = load_workspace_project(output / PROJECT_FILENAME)
    assert saved.settings == workspace.settings
    assert saved.audio.read_bytes() == audio.read_bytes()
    assert saved.cover.read_bytes() == cover.read_bytes()
    restored = json.loads(saved.lyrics_project.read_text(encoding="utf-8"))
    assert restored["lines"][0]["translation"] == "已编辑"
    assert restored["lines"][-1]["hidden"] is True
    assert restored["metadata"]["custom"] == "保留元数据"


@pytest.mark.parametrize("change", ["name", "invalid", "unchanged"])
def test_asynchronous_export_only_marks_its_exact_snapshot_saved(
    page, tmp_path, monkeypatch, change
):
    destination = tmp_path / "async-output"
    runner = DeferredRunner()
    page.runner = runner
    monkeypatch.setattr(
        "karaoke_forge.desktop.editor_page.QFileDialog.getExistingDirectory",
        lambda *_args: str(destination),
    )
    continued = []
    saved_revision = copy.deepcopy(page._saved_document)
    assert page._export(after_saved=lambda: continued.append(True))
    if change == "name":
        page.name_edit.setText("保存开始后输入的新名字")
    elif change == "invalid":
        set_cell(page.lines_table, 0, 2, "invalid pending time")
    runner.complete()
    exported = load_workspace_project(destination / PROJECT_FILENAME)
    assert exported.name == "保留工程"
    if change == "unchanged":
        assert not page.is_dirty
        assert continued == [True]
    else:
        assert page.is_dirty
        assert continued == []
        assert page._saved_document == saved_revision
        assert "之后的修改仍保留" in page.status.text()
        if change == "name":
            assert page.name_edit.text() == "保存开始后输入的新名字"
        else:
            assert page.lines_table.item(0, 2).text() == "invalid pending time"
            with pytest.raises(ValueError):
                page.current_document()


def test_hiding_all_lines_still_exports_recoverable_json(page, tmp_path):
    document = page.current_document()
    for line in document.lines:
        line.hidden = True
    page.load_document(document)
    files = page.export_to(str(tmp_path))
    assert len(files) == 2
    assert all(Path(path).suffix == ".json" for path in files)
    saved = load_workspace_project(tmp_path / PROJECT_FILENAME)
    payload = json.loads(saved.lyrics_project.read_text(encoding="utf-8"))
    assert len(payload["lines"]) == 3
    assert all(line["hidden"] for line in payload["lines"])


def test_export_to_custom_folder_updates_recent_catalog_and_safe_filename(page, tmp_path):
    page.name_edit.setText("CON")
    destination = tmp_path / "custom" / "nested"
    files = page.export_to(str(destination))
    assert all(Path(path).name.casefold() != "con.json" for path in files)
    recent = list_workspace_projects(tmp_path / "recent-output")
    assert [project.manifest for project in recent] == [destination / PROJECT_FILENAME]


def test_video_audio_fallback_is_preserved_without_duplicate_asset(page, tmp_path):
    root = tmp_path / "original"
    root.mkdir()
    video = root / "song.mp4"
    video.write_bytes(b"video with sound")
    lyrics = root / "lyrics.json"
    lyrics.write_text(json.dumps(page.current_document().to_dict()), encoding="utf-8")
    workspace = save_workspace_project(root, name="有声MV", lyrics_project=lyrics, video=video)
    page.load_source(str(workspace.manifest))
    exported = tmp_path / "new-folder"
    page.export_to(str(exported))
    saved = load_workspace_project(exported / PROJECT_FILENAME)
    assert saved.audio is None
    assert saved.video.read_bytes() == b"video with sound"
    page.load_source(str(saved.manifest))
    assert page.audio_picker.value() == str(saved.video)


def test_loading_a_project_does_not_mutate_original_document(page, document):
    original = copy.deepcopy(document.to_dict())
    set_cell(page.tokens_table, 0, 0, "hey ")
    page.apply_pending()
    assert document.to_dict() == original
