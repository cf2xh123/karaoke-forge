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

from PySide6.QtCore import QEvent, QObject, QPoint, QSettings, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QFont,
    QFontDatabase,
    QInputMethodEvent,
    QKeyEvent,
    QKeySequence,
    QShortcut,
)
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from karaoke_forge.alignment_quality import annotate_alignment_review
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


@pytest.mark.parametrize("column,text", [(5, "live translation"), (2, "invalid time")])
def test_undo_consumes_the_live_cell_draft_before_prior_history(page, app, column, text):
    page.nudge("start", 0.1)
    committed = page.current_document().to_dict()
    past_before, _ = history_stacks(page._history)
    page.show()
    app.processEvents()
    item = page.lines_table.item(0, column)
    old_text = item.text()
    page.lines_table.editItem(item)
    app.processEvents()
    editor = QApplication.focusWidget()
    assert isinstance(editor, QLineEdit)
    editor.selectAll()
    QTest.keyClicks(editor, text)
    assert item.text() == old_text
    assert editor.hasFocus()
    page.undo()
    assert page.current_document().to_dict() == committed
    past_after, _ = history_stacks(page._history)
    assert len(past_after) == len(past_before)
    page.undo()
    assert page.current_document().lines[0].start == 1.0


def test_first_live_cell_draft_enables_undo_without_stealing_focus(page, app):
    page.show()
    app.processEvents()
    page.lines_table.editItem(page.lines_table.item(0, 5))
    app.processEvents()
    editor = QApplication.focusWidget()
    assert isinstance(editor, QLineEdit)
    editor.selectAll()
    QTest.keyClicks(editor, "first draft")
    assert page.undo_button.isEnabled()
    assert editor.hasFocus()
    page.undo()
    assert not page.is_dirty
    assert page.current_document().lines[0].translation == "你好世界"


@pytest.mark.parametrize("open_manifest", [True, False])
def test_editor_imports_legacy_render_project_on_the_audio_clock(page, document, tmp_path, open_manifest):
    from karaoke_forge.formats import write_json

    project = tmp_path / "mv-old"
    assets = project / "Song.assets"
    assets.mkdir(parents=True)
    lyrics = assets / "Song.json"
    rendered = document.shifted(2)
    rendered.metadata["workspace_manifest"] = str(project / PROJECT_FILENAME)
    lyrics.write_text(write_json(rendered), encoding="utf-8")
    workspace = save_workspace_project(
        project, name="Old rendered song", lyrics_project=lyrics,
        settings={"audio_offset": 2, "auto_sync": False}, recent_root=tmp_path / "recent",
    )
    original = lyrics.read_bytes()
    page.load_source(str(workspace.manifest if open_manifest else lyrics), confirm_replace=False)
    assert page.current_document().lines[0].start == 1
    assert page.current_document().lines[0].tokens[0].start == 1
    assert not page.is_dirty
    assert lyrics.read_bytes() == original


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


def test_quality_findings_remain_visible_and_navigate_to_the_affected_lines(page, document):
    document.metadata.update({
        "alignment_coverage": "0.15",
        "alignment_review_lines": "1,2",
        "alignment_review_reasons": json.dumps({"1": ["低置信词较多"], "2": ["歌词未匹配"]}),
    })
    page.load_document(document)
    assert "2 句" in page.review_summary and "15%" in page.review_summary
    assert not page.review_bar.isHidden()
    assert "低置信词较多" in page.lines_table.item(0, 0).toolTip()
    assert page.lines_table.item(0, 1).text() == "显示"
    page.select_review_line(1)
    assert page._selected == 1
    assert "歌词未匹配" in page.status.text()
    page.select_review_line(1)
    assert page._selected == 0
    page.select_review_line(-1)
    assert page._selected == 1
    set_cell(page.lines_table, 1, 5, "人工修正翻译仍不意味着时间准确")
    assert page.apply_pending()
    assert "2 句" in page.review_summary
    page.mark_saved()
    assert not page.is_dirty and "2 句" in page.review_summary


def test_quality_findings_follow_insert_delete_hide_and_undo(page, document):
    document.metadata.update({
        "alignment_status": "low_coverage_recovery",
        "unmatched_lyric_lines": "2",
    })
    page.load_document(document)
    page.insert_line(False)
    assert page.current_document().metadata["alignment_review_lines"] == "3"
    assert page.current_document().metadata["unmatched_lyric_lines"] == "3"
    page.select_review_line()
    assert page._selected == 2 and page.current_document().lines[2].text == "next"
    page.toggle_hidden()
    assert page.review_summary == ""
    page.undo()
    assert "1 句" in page.review_summary
    page.delete_line()
    assert page.review_summary == ""
    assert page.current_document().metadata["alignment_review_lines"] == ""
    page.undo()
    assert page.current_document().metadata["alignment_review_lines"] == "3"
    assert page.lines_table.item(2, 0).toolTip().startswith("第 3 句需要核对")


def test_legacy_quality_metadata_and_malformed_details_do_not_break_loading(page, document):
    document.metadata.update({
        "alignment_status": "low_coverage_recovery",
        "alignment_coverage": "not-a-number",
        "alignment_review_reasons": "[invalid JSON",
    })
    page.load_document(document)
    assert page._review_indexes == [0, 1]
    document.metadata["alignment_review_lines"] = "0,2,999,invalid"
    page.load_document(document)
    assert page._review_indexes == [1]
    assert "1 句" in page.review_summary


def test_high_coverage_quality_findings_from_pipeline_survive_hide_and_export_reload(page, tmp_path):
    source = LyricsDocument(
        [LyricLine("空", 1, 2, [KaraokeToken("空", 1, 1.01, .99)])],
        metadata={"alignment_coverage": "1.0", "auto_pronunciation": "false"},
    )
    annotate_alignment_review(source, estimated_source=True, new_alignment=True)
    page.load_document(source)
    assert "100%" in page.review_summary and "1 句" in page.review_summary
    assert "20 毫秒" in page.lines_table.item(0, 0).toolTip()
    page.toggle_hidden()
    assert page.review_summary == ""
    page.toggle_hidden()
    assert "1 句" in page.review_summary
    output = tmp_path / "high-coverage-review"
    page.export_to(str(output))
    page.load_source(str(output / PROJECT_FILENAME))
    assert not page.is_dirty
    assert "100%" in page.review_summary and "1 句" in page.review_summary
    assert "20 毫秒" in page.lines_table.item(0, 0).toolTip()


def test_explicit_review_confirmation_is_undoable_and_preserves_lyrics_and_timing(page, document):
    document.metadata.update({
        "alignment_review_lines": "1,2", "unmatched_lyric_lines": "1,2",
        "alignment_review_reasons": '{"1":["未匹配"],"2":["低置信"]}',
    })
    page.load_document(document)
    assert page.review_confirm_button.isEnabled()
    page.confirm_review_line()
    confirmed = page.current_document()
    assert confirmed.lines == document.lines
    assert confirmed.metadata["alignment_review_lines"] == "2"
    assert confirmed.metadata["unmatched_lyric_lines"] == "2"
    assert page.is_dirty and not page.review_confirm_button.isEnabled()
    page.undo()
    assert page.current_document().to_dict() == document.to_dict()
    assert not page.is_dirty and page.review_confirm_button.isEnabled()
    page.redo()
    assert page.current_document().metadata["alignment_review_lines"] == "2"


def test_review_confirmation_survives_save_but_new_alignment_can_flag_the_line_again(page, tmp_path):
    source = LyricsDocument(
        [LyricLine("空", 1, 2, [KaraokeToken("空", 1, 1.01, .99)])],
        metadata={"auto_pronunciation": "false"},
    )
    annotate_alignment_review(source, estimated_source=True, new_alignment=True)
    page.load_document(source)
    page.confirm_review_line()
    assert page.review_summary == ""
    output = tmp_path / "confirmed-review"
    page.export_to(str(output))
    page.load_source(str(output / PROJECT_FILENAME))
    assert page.review_summary == "" and not page.is_dirty
    revised = page.current_document()
    annotate_alignment_review(revised, estimated_source=True, new_alignment=True)
    page.load_document(revised)
    assert "1 句" in page.review_summary


@pytest.mark.parametrize("surface", ["lines_table", "timeline", "token_timeline", "play_button"])
def test_space_toggles_from_navigation_without_competing_shortcuts_or_repeat(page, app, monkeypatch, surface):
    calls, conflicting = [], []
    monkeypatch.setattr(page, "toggle_playback", lambda: calls.append(True))
    shortcut = QShortcut(QKeySequence("Space"), page)
    shortcut.activated.connect(lambda: conflicting.append(True))
    page.resize(1280, 900)
    page.show()
    target = getattr(page, surface)
    if surface == "timeline":
        page.time_tabs.setCurrentWidget(page.song_panel)
    target.setFocus()
    app.processEvents()
    QTest.keyPress(target, Qt.Key.Key_Space)
    for _ in range(3):
        QApplication.sendEvent(target, QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier, " ", True,
        ))
    QTest.keyRelease(target, Qt.Key.Key_Space)
    assert calls == [True] and conflicting == []
    QTest.keyClick(target, Qt.Key.Key_Space)
    assert calls == [True, True]
    assert QApplication.focusWidget() is target


def test_space_remains_text_during_input_table_edit_and_ime_composition(page, app, monkeypatch):
    calls = []
    monkeypatch.setattr(page, "toggle_playback", lambda: calls.append(True))
    page.show()
    page.name_edit.setFocus()
    page.name_edit.setText("A")
    QTest.keyClick(page.name_edit, Qt.Key.Key_Space)
    assert page.name_edit.text() == "A "
    table = page.lines_table
    table.setCurrentCell(0, 4)
    table.editItem(table.item(0, 4))
    app.processEvents()
    active = QApplication.focusWidget()
    assert isinstance(active, QLineEdit)
    active.selectAll()
    QTest.keyClicks(active, "new words")
    assert active.text() == "new words"
    QApplication.sendEvent(active, QInputMethodEvent("かな", []))
    QTest.keyClick(active, Qt.Key.Key_Space)
    assert calls == []
    assert QApplication.focusWidget() is active


def test_space_scope_covers_workspace_buttons_and_excludes_other_player(page, app, monkeypatch):
    container = QWidget()
    layout = QVBoxLayout(container)
    outside = QPushButton("Save project")
    output = QPushButton("Output player")
    text = QLineEdit()
    for widget in (outside, output, text, page):
        layout.addWidget(widget)
    allowed, calls = [True], []
    monkeypatch.setattr(page, "toggle_playback", lambda: calls.append(True))
    page.set_playback_shortcut_scope(container, excluded=(output,), guard=lambda: allowed[0])
    container.show()
    app.processEvents()
    try:
        outside.setFocus()
        QTest.keyClick(outside, Qt.Key.Key_Space)
        assert calls == [True]
        output.setFocus()
        QTest.keyClick(output, Qt.Key.Key_Space)
        text.setFocus()
        QTest.keyClick(text, Qt.Key.Key_Space)
        assert text.text() == " " and calls == [True]
        allowed[0] = False
        outside.setFocus()
        QTest.keyClick(outside, Qt.Key.Key_Space)
        assert calls == [True]
    finally:
        page.set_playback_shortcut_scope(page)
        page.setParent(None)
        container.close()


def test_seek_feedback_is_immediate_and_ignores_queued_old_media_positions(page, monkeypatch):
    seeks = []
    monkeypatch.setattr(page.player, "duration", lambda: 10000)
    monkeypatch.setattr(page.player, "setPosition", seeks.append)
    page.seek(7.25)
    assert seeks == [7250]
    assert page.position_label.text() == "00:07.25"
    assert page.timeline._position == 7.25 and page.preview._position == 7.25
    page._position_changed(1200)
    assert page.timeline._position == 7.25
    page._position_changed(7250)
    page._position_changed(7300)
    assert page.timeline._position == 7.3
    page.seek_slider.sliderMoved.emit(4500)
    assert seeks[-1] == 4500 and page.timeline._position == 4.5


def test_playhead_interpolates_rate_without_backward_jitter_or_unbounded_prediction(page, monkeypatch):
    now = [10.0]
    monkeypatch.setattr("karaoke_forge.desktop.editor_page.time.monotonic", lambda: now[0])
    monkeypatch.setattr(page.player, "playbackState", lambda: QMediaPlayer.PlaybackState.PlayingState)
    monkeypatch.setattr(page.player, "playbackRate", lambda: 1.5)
    monkeypatch.setattr(page.player, "duration", lambda: 20000)
    page._position_changed(1000)
    now[0] += .1
    page._advance_playhead()
    assert page.timeline._position == pytest.approx(1.15)
    now[0] += .02
    page._position_changed(1100)
    assert page.timeline._position == pytest.approx(1.15)
    now[0] += .04
    page._advance_playhead()
    assert page.timeline._position == pytest.approx(1.16)
    now[0] += 5
    page._advance_playhead()
    assert page.timeline._position == pytest.approx(1.475)


def test_word_axis_is_visible_and_selection_stays_in_sync_with_word_table(page, app):
    page.set_workspace_mode(True)
    page.resize(1280, 900)
    page.show()
    app.processEvents()
    assert not page.details_panel.isVisible()
    assert page.token_timeline.isVisible()
    assert page.token_boundary_mode.currentData() is False
    target = page.token_timeline.token_rect(1).center().toPoint()
    QTest.mouseClick(page.token_timeline, Qt.MouseButton.LeftButton, pos=target)
    assert page.tokens_table.currentRow() == page._selected_token == 1
    page.tokens_table.setCurrentCell(0, 1)
    assert page._selected_token == 0
    assert "第 1 词" in page.token_selection_label.text()


def test_dragging_touching_words_independently_and_moving_one_preserves_neighbor(page, app, document):
    page.resize(1280, 900)
    page.show()
    app.processEvents()
    axis = page.token_timeline
    axis.set_snap_enabled(False)
    page.select_token(0)
    end = axis.token_rect(0).topRight().toPoint() + QPoint(0, 20)
    target = end - QPoint(round(.2 * axis.pixels_per_second), 0)
    QTest.mousePress(axis, Qt.MouseButton.LeftButton, pos=end)
    QTest.mouseRelease(axis, Qt.MouseButton.LeftButton, pos=target)
    changed = page.current_document()
    assert changed.lines[0].tokens[0].end == pytest.approx(1.8, abs=.005)
    assert changed.lines[0].tokens[1] == document.lines[0].tokens[1]
    before_move = copy.deepcopy(changed.lines[0].tokens[0])
    page.token_adjust_edge.setCurrentIndex(page.token_adjust_edge.findData("move"))
    page.token_adjust_step.setValue(.1)
    page.adjust_selected_token(1)
    moved = page.current_document()
    assert moved.lines[0].tokens[0].start == pytest.approx(before_move.start + .1)
    assert moved.lines[0].tokens[0].end == pytest.approx(before_move.end + .1)
    assert moved.lines[0].tokens[1] == document.lines[0].tokens[1]
    assert moved.lines[1:] == document.lines[1:]


def test_linked_boundary_is_explicit_and_word_adjustments_are_undoable(page, document):
    page.select_token(0)
    page.token_adjust_edge.setCurrentIndex(page.token_adjust_edge.findData("end"))
    page.token_adjust_step.setValue(.1)
    page.adjust_selected_token(1)
    assert page.current_document().to_dict() == document.to_dict()
    page.token_boundary_mode.setCurrentIndex(page.token_boundary_mode.findData(True))
    page.adjust_selected_token(1)
    linked = page.current_document()
    assert linked.lines[0].tokens[0].end == pytest.approx(2.1)
    assert linked.lines[0].tokens[1].start == pytest.approx(2.1)
    assert linked.lines[0].tokens[1].end == 3
    page.undo()
    assert page.current_document().to_dict() == document.to_dict()


def test_playback_does_not_replace_the_word_axis_during_an_active_drag(page, app, monkeypatch):
    page.resize(1280, 900)
    page.show()
    app.processEvents()
    axis = page.token_timeline
    edge = axis.token_rect(0).topRight().toPoint() + QPoint(0, 20)
    QTest.mousePress(axis, Qt.MouseButton.LeftButton, pos=edge)
    assert axis.is_interacting
    monkeypatch.setattr(page.player, "playbackState", lambda: QMediaPlayer.PlaybackState.PlayingState)
    page._position_changed(4500)
    assert page._selected == 0 and axis.is_interacting
    QTest.keyClick(axis, Qt.Key.Key_Escape)
    assert not axis.is_interacting


def test_invalid_word_draft_cancels_a_timeline_adjustment_without_losing_the_draft(page, document):
    set_cell(page.tokens_table, 0, 2, "invalid")
    page.adjust_selected_token(1)
    assert not page.token_timeline.is_interacting
    assert page.tokens_table.item(0, 2).text() == "invalid"
    assert page._document.lines == document.lines
    assert page.is_dirty


def test_line_edits_and_undo_pause_without_rewinding_the_playhead(page, monkeypatch):
    pauses, stops = [], []
    monkeypatch.setattr(page.player, "pause", lambda: pauses.append(True))
    monkeypatch.setattr(page.player, "stop", lambda: stops.append(True))
    page._position_changed(2300)
    page.nudge("end", .1)
    page.undo()
    page.redo()
    assert len(pauses) >= 3 and stops == []
    assert page.timeline._position == 2.3 and page._display_position_ms == 2300


def test_clear_project_discards_live_draft_media_history_and_review(page, app, document):
    document.metadata["alignment_review_lines"] = "1"
    page.load_document(document, "old-song.wav", "Old project")
    page.nudge("end", .1)
    page.show()
    page.lines_table.editItem(page.lines_table.item(0, 4))
    app.processEvents()
    active = QApplication.focusWidget()
    assert isinstance(active, QLineEdit)
    QTest.keyClicks(active, "unfinished")
    page.clear_project()
    assert not page.is_dirty and page._history == {}
    assert page.lines_table.rowCount() == page.tokens_table.rowCount() == 0
    assert page.player.source().isEmpty()
    assert page.audio_picker.value() == page.source_picker.value() == page.name_edit.text() == ""
    assert page.review_summary == ""
    assert page.timeline._document.lines == [] and page.token_timeline._tokens == []
    with pytest.raises(ValueError, match="先载入"):
        page.current_document()


def test_space_recovers_after_focus_or_window_loss_during_a_held_key(page, app, monkeypatch):
    calls = []
    monkeypatch.setattr(page, "toggle_playback", lambda: calls.append(True))
    page.show()
    page.token_timeline.setFocus()
    app.processEvents()
    QTest.keyPress(page.token_timeline, Qt.Key.Key_Space)
    assert page._space_held
    QApplication.sendEvent(page, QEvent(QEvent.Type.WindowDeactivate))
    assert not page._space_held
    QTest.keyClick(page.token_timeline, Qt.Key.Key_Space)
    assert calls == [True, True]


def test_invalid_word_draft_restores_axis_and_table_selection(page, app):
    page.show()
    app.processEvents()
    page.select_token(0)
    set_cell(page.tokens_table, 0, 2, "invalid")
    page.tokens_table.setCurrentCell(1, 0)
    assert page._selected_token == page.tokens_table.currentRow() == page.token_timeline._selected == 0
    target = page.token_timeline.token_rect(1).center().toPoint()
    QTest.mouseClick(page.token_timeline, Qt.MouseButton.LeftButton, pos=target)
    assert page._selected_token == page.tokens_table.currentRow() == page.token_timeline._selected == 0
    assert page.tokens_table.item(0, 2).text() == "invalid"
    assert page.is_dirty


def test_calibration_tabs_keep_the_chosen_tool_when_switching_lyrics(page):
    assert page.time_tabs.currentWidget() is page.token_panel
    page.time_tabs.setCurrentWidget(page.song_panel)
    page.select_line(1, seek=False)
    assert page.time_tabs.currentWidget() is page.song_panel
    page.time_tabs.setCurrentIndex(2)
    page.select_line(0, seek=False)
    assert page.time_tabs.currentIndex() == 2


@pytest.mark.parametrize("size", [(730, 510), (1180, 760)])
def test_common_calibration_controls_fit_without_page_scrolling(page, app, size):
    # Match the application's font and control padding for physical dimensions.
    page.setFont(QFont("Microsoft YaHei", 10))
    page.setStyleSheet((Path(__file__).parents[1] / "src/karaoke_forge/desktop/theme.qss").read_text())
    page.set_workspace_mode()
    page.show()
    app.processEvents()
    page.resize(*size)
    app.processEvents()
    assert page.size().width() == size[0] and page.size().height() == size[1]
    for widget in (page.preview, page.play_button, page.token_timeline, page.token_adjust_step):
        assert widget.isVisible()
        position = widget.mapTo(page, QPoint(0, 0))
        assert position.x() >= 0 and position.y() >= 0
        assert position.x() + widget.width() <= page.width()
        assert position.y() + widget.height() <= page.height()
    page.editing_splitter.setSizes([180, 330])
    page.time_tabs.setCurrentWidget(page.song_panel)
    app.processEvents()
    assert page.timeline.isVisible() and not page.token_timeline.isVisible()


@pytest.mark.parametrize("size", [(900, 640), (1380, 940)])
def test_complete_window_keeps_calibration_controls_clear_of_each_other(
    app, document, monkeypatch, tmp_path, size
):
    from karaoke_forge.desktop.app import MainWindow

    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path / "settings"))
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(
        "karaoke_forge.desktop.app.QSettings",
        lambda *_: QSettings(str(tmp_path / "window.ini"), QSettings.Format.IniFormat),
    )
    # The offscreen platform does not enumerate the Windows font directory.
    # Load the real application font so placeholder glyphs cannot mask clipping.
    font_ids = [
        QFontDatabase.addApplicationFont(str(path))
        for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/msyhbd.ttc"))
        if path.is_file()
    ]
    window = MainWindow()
    window.setFont(QFont("Microsoft YaHei", 10))
    window.setStyleSheet(
        (Path(__file__).parents[1] / "src/karaoke_forge/desktop/theme.qss").read_text()
    )
    document.lines[0].tokens[0].text = "長い歌詞のまとまり " * 15
    document.lines[0].text = "".join(token.text for token in document.lines[0].tokens)
    window.workspace.load_project(document, None, "")
    window.show()
    app.processEvents()
    window.resize(*size)
    app.processEvents()
    editor = window.editor
    try:
        assert (window.width(), window.height()) == size
        assert not editor.status.isVisible()
        header = editor.lines_table.horizontalHeader()
        assert [header.logicalIndex(index) for index in range(header.count())] == [0, 4, 2, 3, 5, 1]
        lyric_rect = editor.lines_table.visualItemRect(editor.lines_table.item(0, 4))
        assert editor.lines_table.columnWidth(4) >= 140
        assert 0 <= lyric_rect.left() <= lyric_rect.right() < editor.lines_table.viewport().width()
        for widget in (
            editor.preview, editor.play_button, editor.seek_slider, editor.follow_playback,
            editor.token_timeline, editor.token_selection_label, editor.token_adjust_step,
        ):
            assert widget.isVisible()
            point = widget.mapTo(editor, QPoint(0, 0))
            assert 0 <= point.x() <= editor.width() - widget.width()
            assert 0 <= point.y() <= editor.height() - widget.height()
        axis = editor.token_timeline
        label = editor.token_selection_label
        step = editor.token_adjust_step
        assert axis.mapTo(editor, QPoint(0, axis.height())).y() <= label.mapTo(editor, QPoint()).y()
        assert label.mapTo(editor, QPoint(0, label.height())).y() <= step.mapTo(editor, QPoint()).y()
        editor.token_zoom.setCurrentIndex(1)
        app.processEvents()
        assert axis.token_rect(0).bottom() < axis._scroll.y()
        for tab in (1, 2, 0):
            editor.time_tabs.setCurrentIndex(tab)
            editor.select_line(1, seek=False)
            app.processEvents()
            assert editor.time_tabs.currentIndex() == tab
            assert (window.width(), window.height()) == size
    finally:
        window.hide()
        window.deleteLater()
        app.processEvents()
        for font_id in font_ids:
            QFontDatabase.removeApplicationFont(font_id)
