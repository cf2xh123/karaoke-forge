"""Native startup, file controls, and real worker thread lifecycle."""

from __future__ import annotations

import threading
import wave

import pytest

pytest.importorskip("PySide6")

import karaoke_forge.desktop  # noqa: F401 - prepare Windows ICU before Qt

# isort: split
from PySide6.QtCore import QEventLoop, Qt, QThread, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from karaoke_forge.cli import build_parser
from karaoke_forge.desktop.app import MainWindow
from karaoke_forge.desktop.common import JobRunner, PathPicker
from karaoke_forge.formats import write_json
from karaoke_forge.models import KaraokeToken, LyricLine, LyricsDocument, PronunciationSpan
from karaoke_forge.projects import save_workspace_project


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def test_desktop_command_accepts_a_project_path() -> None:
    args = build_parser().parse_args(["desktop", "song.json"])
    assert args.project == "song.json"
    assert args.handler.__name__ == "_handle_desktop"


def test_native_window_starts_without_importing_gradio_or_a_web_engine(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path))
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    import karaoke_forge.web

    monkeypatch.setattr(
        karaoke_forge.web,
        "create_web_app",
        lambda **kwargs: pytest.fail("native must not start Gradio"),
    )
    window = MainWindow()
    assert window.pages.count() == 3
    assert window.make.before_prepare is not None
    assert not window.editor.is_dirty
    window.close()


def test_multiple_file_picker_preserves_semicolons_inside_selected_filenames(qt_app):
    picker = PathPicker(multiple=True)
    picker.set_paths(["C:/fonts/Font;One.ttf", "C:/fonts/Second.otf"])
    assert picker.paths() == ["C:/fonts/Font;One.ttf", "C:/fonts/Second.otf"]
    picker.set_value("")
    assert picker.paths() == []


def test_worker_runs_off_gui_thread_and_returns_on_gui_thread(qt_app):
    runner = JobRunner()
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    done = []
    main_ident = threading.get_ident()

    def task(log):
        log("progress")
        return threading.get_ident()

    def finish(worker_ident):
        done.append(
            (worker_ident, threading.get_ident(), QThread.currentThread() == qt_app.thread())
        )
        loop.quit()

    assert runner.submit("test", task, finish)
    assert not runner.submit("overlap", task)
    timer.start(5000)
    loop.exec()
    assert done and done[0][0] != main_ident
    assert done[0][1:] == (main_ident, True)
    assert not runner.is_busy


def test_worker_failure_releases_busy_state_and_reports_error(qt_app):
    runner = JobRunner()
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    errors = []

    def fail(log):
        raise ValueError("invalid lyrics")

    def failed(message):
        errors.append(message)
        loop.quit()

    runner.failed.connect(failed)
    runner.submit("failure", fail)
    timer.start(5000)
    loop.exec()
    assert errors == ["invalid lyrics"]
    assert not runner.is_busy


def test_renamed_workspace_opens_with_lyrics_assets_and_settings(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path / "settings"))
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    source = LyricsDocument(
        [
            LyricLine(
                "春",
                0,
                1,
                [KaraokeToken("春", 0, 1, confidence=0.9)],
                translation="spring",
                pronunciation_units=[PronunciationSpan("春", "はる", 0, 1)],
            ),
            LyricLine("hidden", 1, 2, [KaraokeToken("hidden", 1, 2)], hidden=True),
        ],
        metadata={"custom": "retained"},
    )
    lyrics = tmp_path / "lyrics.json"
    lyrics.write_text(write_json(source), encoding="utf-8")
    audio = tmp_path / "song.wav"
    with wave.open(str(audio), "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(8000)
        recording.writeframes(b"\0\0" * 8000)
    workspace = save_workspace_project(
        tmp_path / "project",
        name="saved song",
        lyrics_project=lyrics,
        audio=audio,
        settings={"font_size": 64, "show_translation": False},
        recent_root=tmp_path / "recent",
    )
    renamed = workspace.manifest.with_name("my-song-project.json")
    workspace.manifest.rename(renamed)
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: errors.append(args[-1]))
    window = MainWindow()
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    window.runner.busy_changed.connect(lambda busy: None if busy else loop.quit())
    window.open_project(str(renamed))
    timer.start(5000)
    loop.exec()
    assert not errors
    assert not window.runner.is_busy
    loaded = window.editor.current_document()
    assert loaded.to_dict()["lines"] == source.to_dict()["lines"]
    assert loaded.metadata["custom"] == "retained"
    assert loaded.metadata["workspace_manifest"] == str(renamed.resolve())
    assert window.editor.audio_picker.value() == str(workspace.audio)
    assert window.editor.name_edit.text() == "saved song"
    assert window.make.get_settings()["font_size"] == 64
    assert window.make.get_settings()["show_translation"] is False
    assert window.editor.preview._style["font_size"] == 64
    assert window.editor.preview._style["show_translation"] is False
    assert window.navigation.currentRow() == 0
    assert not window.editor.is_dirty
    window.close()


def test_close_keeps_running_worker_alive_until_it_finishes(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path / "settings"))
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    notices = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: notices.append(args[-1]))
    window = MainWindow()
    release = threading.Event()
    loop = QEventLoop()
    completed = []

    def finish(result):
        completed.append(result)
        loop.quit()

    window.runner.submit("pending test", lambda log: release.wait(5), finish)
    assert window.close() is False
    assert notices and window.runner.is_busy
    release.set()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(5000)
    loop.exec()
    assert completed == [True]
    assert not window.runner.is_busy
    assert window.close() is True


def test_playback_space_works_from_project_navigation_but_stays_on_active_page(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path / "settings"))
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    window = MainWindow()
    document = LyricsDocument([LyricLine("song", 0, 1)])
    lyrics = tmp_path / "song.json"
    lyrics.write_text(write_json(document), encoding="utf-8")
    window.workspace.load_project(document, None, str(lyrics))
    calls = []
    monkeypatch.setattr(window.editor, "toggle_playback", lambda: calls.append("editor"))
    window.show()
    window.activateWindow()
    window.navigation.setFocus()
    qt_app.processEvents()
    QTest.keyClick(window.navigation, Qt.Key.Key_Space)
    assert calls == ["editor"]
    window.navigation.setCurrentRow(1)
    window.navigation.setFocus()
    qt_app.processEvents()
    QTest.keyClick(window.navigation, Qt.Key.Key_Space)
    assert calls == ["editor"]
    monkeypatch.setattr(window, "_allow_replace", lambda: True)
    window.close()


def test_new_project_save_failure_reopens_same_draft_and_keeps_previous_project(
    qt_app, monkeypatch, tmp_path
):
    import karaoke_forge.desktop.workspace as workspace_module

    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path / "settings"))
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    window = MainWindow()
    old_document = LyricsDocument([LyricLine("previous song", 0, 1)])
    old_path = tmp_path / "previous.json"
    old_path.write_text(write_json(old_document), encoding="utf-8")
    window.workspace.load_project(old_document, None, str(old_path))
    original_save = workspace_module.save_workspace_revision
    saves, dialogs, prompts, warnings = [], [], [], []

    def save(document, settings, directory):
        saves.append(directory)
        if len(saves) == 1:
            raise PermissionError("temporary write failure")
        return original_save(document, settings, directory)

    def configure(dialog):
        dialogs.append(dialog)
        if len(dialogs) == 1:
            dialog.name_edit.setText("New Japanese song")
            dialog.directory_picker.set_value(str(tmp_path / "new-project"))
            dialog.source.setCurrentIndex(dialog.source.findData("paste"))
            dialog.pasted_lyrics.setPlainText("春の夢\nsing with me")
        else:
            assert dialogs[0] is dialog
            assert dialog.name_edit.text() == "New Japanese song"
            assert dialog.pasted_lyrics.toPlainText() == "春の夢\nsing with me"
            assert window.editor.current_document().to_dict() == old_document.to_dict()
        assert len(dialogs) <= 2
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(workspace_module, "save_workspace_revision", save)
    monkeypatch.setattr("karaoke_forge.desktop.app.ProjectDialog.exec", configure)
    monkeypatch.setattr(window, "_allow_replace", lambda: prompts.append(True) or True)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[-1]))
    window.new_project_dialog()
    assert len(dialogs) == 2 and len(prompts) == 1 and len(warnings) == 1
    assert window.workspace.project_directory == str(tmp_path / "new-project")
    assert window.make.get_settings()["pasted_lyrics"] == "春の夢\nsing with me"
    assert not window.workspace.is_dirty
    assert (tmp_path / "new-project" / "karaoke-forge-project.json").is_file()
    window.close()
