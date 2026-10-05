"""The source chooser is local, exclusive, and safe to cancel."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

import karaoke_forge.desktop  # noqa: F401 - prepare Windows ICU before Qt

# isort: split
from PySide6.QtWidgets import QApplication, QDialog

from karaoke_forge.desktop.make_page import MakePage
from karaoke_forge.desktop.project_dialog import ProjectDialog


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def chooser(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(tmp_path / "settings"))
    dialog = ProjectDialog(directory=tmp_path / "project")
    dialog.name_edit.setText("Test song")
    yield dialog
    dialog.close()
    dialog.deleteLater()
    qt_app.processEvents()


@pytest.mark.parametrize(
    ("source", "field", "value"),
    [
        ("local", "lyrics_file", "new.lrc"),
        ("paste", "pasted_lyrics", "春の歌\nSing with me"),
        ("netease", "netease_link", "https://music.163.com/song?id=123"),
        ("qqmusic", "qqmusic_link", "https://y.qq.com/n/ryqq/songDetail/001"),
        ("utaten", "utaten_link", "https://utaten.com/lyric/test/"),
        ("recognize", None, None),
    ],
)
def test_source_choice_returns_one_explicit_source_and_clears_all_others(chooser, source, field, value):
    chooser.lyrics_picker.set_value("old.lrc")
    chooser.pasted_lyrics.setPlainText("old pasted lyrics")
    chooser.audio_picker.set_value("song.wav")
    for online in ("netease", "qqmusic", "utaten"):
        chooser.source.setCurrentIndex(chooser.source.findData(online))
        chooser.link_edit.setText(f"https://old-{online}.example")
    chooser.source.setCurrentIndex(chooser.source.findData(source))
    chooser.rights.setChecked(True)
    if source == "local":
        chooser.lyrics_picker.set_value(value)
    elif source == "paste":
        chooser.pasted_lyrics.setPlainText(value)
    elif field:
        chooser.link_edit.setText(value)
    values = chooser.material_settings()
    sources = ("lyrics_file", "pasted_lyrics", "netease_link", "qqmusic_link", "utaten_link")
    assert {key: values[key] for key in sources} == {
        key: value if key == field else "" for key in sources
    }
    assert values["utaten_pronunciation_only"] is False
    for online in ("netease", "qqmusic", "utaten"):
        assert values[f"use_{online}_lyrics"] == (source == online)
    chooser.accept()
    assert chooser.result() == QDialog.DialogCode.Accepted


def test_netease_shows_link_and_audio_choice_immediately_without_running_jobs(chooser):
    chooser.show()
    chooser.source.setCurrentIndex(chooser.source.findData("netease"))
    assert chooser.link_edit.isVisible() and chooser.audio_mode.isVisible()
    assert chooser.audio_picker.isVisible()
    chooser.audio_picker.set_value("old-song.wav")
    chooser.audio_mode.setCurrentIndex(chooser.audio_mode.findData("online"))
    assert not chooser.audio_picker.isVisible()
    values = chooser.material_settings()
    assert values["audio_file"] == values["video_file"] == ""
    chooser.audio_mode.setCurrentIndex(chooser.audio_mode.findData("local"))
    assert chooser.material_settings()["audio_file"] == "old-song.wav"


def test_cancel_and_source_switches_do_not_mutate_the_initial_project(qt_app):
    original = {"lyrics_file": "saved.json", "netease_link": "old-link", "audio_file": "old.wav"}
    before = original.copy()
    dialog = ProjectDialog(settings=original)
    dialog.source.setCurrentIndex(dialog.source.findData("paste"))
    dialog.pasted_lyrics.setPlainText("New lyrics")
    dialog.reject()
    assert original == before
    assert dialog.result() == QDialog.DialogCode.Rejected
    dialog.deleteLater()


def test_invalid_configuration_stays_in_dialog_and_keeps_user_input(chooser):
    chooser.source.setCurrentIndex(chooser.source.findData("netease"))
    chooser.accept()
    assert chooser.result() == QDialog.DialogCode.Rejected
    assert chooser.error.text()
    chooser.link_edit.setText("https://music.163.com/song?id=123")
    chooser.accept()
    assert chooser.result() == QDialog.DialogCode.Rejected
    assert chooser.link_edit.text().endswith("123")
    chooser.rights.setChecked(True)
    chooser.audio_mode.setCurrentIndex(chooser.audio_mode.findData("online"))
    chooser.accept()
    assert chooser.result() == QDialog.DialogCode.Accepted


def test_netease_local_mode_cannot_silently_fall_back_to_online_audio(chooser):
    chooser.source.setCurrentIndex(chooser.source.findData("netease"))
    chooser.link_edit.setText("https://music.163.com/song?id=123")
    chooser.rights.setChecked(True)
    chooser.accept()
    assert chooser.result() == QDialog.DialogCode.Rejected
    chooser.audio_picker.set_value("song.wav")
    chooser.accept()
    assert chooser.result() == QDialog.DialogCode.Accepted


def test_local_video_is_both_sound_source_and_visual_and_replacement_clears_old_mv(qt_app):
    dialog = ProjectDialog(settings={"video_file": "old.mp4", "lyrics_file": "song.lrc"})
    dialog.audio_picker.set_value("new.mp4")
    values = dialog.material_settings()
    assert values["video_file"] == "new.mp4" and values["audio_file"] == ""
    dialog.audio_picker.set_value("new.wav")
    values = dialog.material_settings()
    assert values["video_file"] == "" and values["audio_file"] == "new.wav"
    dialog.deleteLater()


def test_local_audio_change_keeps_an_independent_visual(qt_app):
    dialog = ProjectDialog(settings={
        "audio_file": "song.wav", "video_file": "visual.mp4", "lyrics_file": "song.lrc"
    })
    dialog.audio_picker.set_value("new.wav")
    values = dialog.material_settings()
    assert values["audio_file"] == "new.wav"
    assert "video_file" not in values
    dialog.deleteLater()


def test_new_project_requires_its_own_directory_and_uses_three_steps(qt_app, tmp_path):
    dialog = ProjectDialog(default_settings={"output_root": str(tmp_path / "renders")})
    dialog.name_edit.setText("春の歌")
    dialog.lyrics_picker.set_value("lyrics.lrc")
    dialog.next_step()
    assert dialog.stack.currentIndex() == 0
    assert "项目文件夹" in dialog.error.text()
    dialog.directory_picker.set_value(str(tmp_path / "project"))
    dialog.next_step()
    assert dialog.stack.currentIndex() == 1
    dialog.next_step()
    assert dialog.stack.currentIndex() == 2
    assert "春の歌" in dialog.summary.text()
    assert dialog.project_directory() == str(tmp_path / "project")
    assert dialog.project_settings()["output_root"] == str(tmp_path / "renders")
    assert "project_directory" not in dialog.project_settings()
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.deleteLater()


def test_new_project_inherits_processing_but_clears_previous_materials_and_private_data(qt_app):
    defaults = {
        "output_name": "Old", "lyrics_file": "old.lrc", "pasted_lyrics": "old text",
        "audio_file": "old.wav", "video_file": "old.mp4", "cover_file": "old.jpg",
        "font_files": ["old.ttf"], "netease_link": "old-link", "music_u": "secret",
        "cookie_browser": "edge", "cookie_browser_profile": "private-profile",
        "model": "profile:precise", "language": "ja", "timing_refinement": "force",
        "font_size": 66, "quality": "高质量", "source_refs": {"netease": "old"},
        "audio_offset": 3.25, "pending_lyrics_source": True,
        "lyrics_source_asset": "project-assets/old.lrc", "preserve_lyrics_source": True,
    }
    before = defaults.copy()
    dialog = ProjectDialog(default_settings=defaults)
    values = dialog.project_settings()
    for key in ("output_name", "lyrics_file", "pasted_lyrics", "audio_file", "video_file", "cover_file", "netease_link"):
        assert values[key] == ""
    assert values["font_files"] == []
    assert values["model"] == "profile:precise" and values["language"] == "ja"
    assert values["timing_refinement"] == "force"
    assert values["font_size"] == 66 and values["quality"] == "高质量"
    assert values["audio_offset"] == 0
    assert not {"music_u", "cookie_browser", "cookie_browser_profile", "source_refs"} & values.keys()
    assert not {"pending_lyrics_source", "lyrics_source_asset", "preserve_lyrics_source"} & values.keys()
    assert defaults == before
    dialog.deleteLater()


def test_edit_draft_reuses_every_public_setting_and_cancel_has_no_side_effects(qt_app, monkeypatch, tmp_path):
    persisted = []
    monkeypatch.setattr("karaoke_forge.desktop.make_page.save_preferences", lambda value: persisted.append(value))
    dialog = ProjectDialog(settings={
        "output_name": "Existing", "lyrics_file": "source.lrc", "audio_file": "song.wav",
        "audio_offset": 0.425, "font_files": ["font.ttf"], "export_instrumental": True,
    }, directory=tmp_path / "existing")
    assert dialog.windowTitle() == "项目设置"
    assert dialog.directory_picker.edit.isReadOnly()
    public_fields = set(dialog.settings_page.controls) - {"music_u", "cookie_browser", "cookie_browser_profile"}
    assert public_fields <= dialog.project_settings().keys()
    dialog.settings_page.controls["font_size"].setValue(72)
    dialog.settings_page.controls["model"].setCurrentIndex(
        dialog.settings_page.controls["model"].findData("large-v3")
    )
    dialog.set_step(1)
    dialog.set_step(0)
    assert dialog.project_settings()["audio_offset"] == pytest.approx(0.425)
    assert dialog.project_settings()["font_size"] == 72
    assert dialog.project_settings()["font_files"] == ["font.ttf"]
    assert dialog.project_settings()["model"] == "large-v3"
    dialog.reject()
    assert persisted == []
    assert dialog.settings_page._match_timer.isActive() is False
    dialog.deleteLater()


def test_pure_settings_page_hides_production_and_duplicate_sources(chooser):
    chooser.show()
    chooser.set_step(1)
    page = chooser.settings_page
    assert isinstance(page, MakePage) and page.settings_only
    assert not page.prepare_button.isVisible() and not page.render_button.isVisible()
    assert not page.log.isVisible() and not page.files.isVisible()
    assert not page.controls["audio_file"].isVisible()
    assert not page.controls["lyrics_file"].isVisible()
    assert not page.controls["netease_link"].isVisible()
    assert not page.controls["music_u"].isVisible()
    assert page.tabs.tabText(0) == "画面与视频"


def test_audio_mv_and_independent_visual_cover_are_complete_settings(chooser):
    chooser.audio_picker.set_value("song.wav")
    chooser.video_picker.set_value("visual.mp4")
    chooser.cover_picker.set_value("cover.png")
    values = chooser.project_settings()
    assert values["audio_file"] == "song.wav"
    assert values["video_file"] == "visual.mp4" and values["cover_file"] == "cover.png"
    chooser.audio_picker.set_value("voiced.mp4")
    chooser.video_picker.set_value("")
    values = chooser.project_settings()
    assert values["audio_file"] == "" and values["video_file"] == "voiced.mp4"


def test_voiced_mv_remains_audio_source_when_an_independent_visual_is_selected(chooser):
    chooser.lyrics_picker.set_value("song.lrc")
    chooser.audio_picker.set_value("song-with-audio.mp4")
    chooser.video_picker.set_value("independent-visual.mp4")
    chooser.accept()
    assert chooser.result() == QDialog.DialogCode.Accepted
    values = chooser.project_settings()
    assert values["audio_file"] == "song-with-audio.mp4"
    assert values["video_file"] == "independent-visual.mp4"
    reopened = ProjectDialog(settings=values, directory=chooser.project_directory())
    assert reopened.audio_picker.value() == "song-with-audio.mp4"
    assert reopened.video_picker.value() == "independent-visual.mp4"
    assert reopened.project_settings() == values
    reopened.video_picker.set_value("")
    assert reopened.project_settings()["audio_file"] == ""
    assert reopened.project_settings()["video_file"] == "song-with-audio.mp4"
    reopened.deleteLater()


@pytest.mark.parametrize("source", ["local", "paste"])
def test_official_pronunciation_can_supplement_existing_lyrics_without_replacing_text(chooser, source):
    chooser.source.setCurrentIndex(chooser.source.findData(source))
    chooser.lyrics_picker.set_value("local.lrc")
    chooser.pasted_lyrics.setPlainText("春の歌\nSing with me")
    page = chooser.settings_page
    page.controls["utaten_pronunciation_only"].setChecked(True)
    chooser.next_step()
    assert chooser.stack.currentIndex() == 1
    chooser.next_step()
    assert chooser.stack.currentIndex() == 1
    assert "UtaTen" in chooser.error.text()
    page.controls["utaten_link"].setText("https://utaten.com/lyric/test/")
    page.controls["rights_confirmed"].setChecked(True)
    chooser.next_step()
    assert chooser.stack.currentIndex() == 2
    values = chooser.project_settings()
    assert values["lyrics_file"] == ("local.lrc" if source == "local" else "")
    assert values["pasted_lyrics"] == ("春の歌\nSing with me" if source == "paste" else "")
    assert values["utaten_link"].endswith("test/")
    assert values["utaten_pronunciation_only"] is True and values["rights_confirmed"] is True
    assert values["use_utaten_lyrics"] is False
    reopened = ProjectDialog(settings=values, directory=chooser.project_directory())
    reopened.accept()
    assert reopened.result() == QDialog.DialogCode.Accepted
    assert reopened.project_settings() == values
    reopened.deleteLater()


def test_primary_utaten_uses_only_first_step_source_and_confirmation(chooser):
    chooser.source.setCurrentIndex(chooser.source.findData("utaten"))
    chooser.link_edit.setText("https://utaten.com/lyric/primary/")
    chooser.rights.setChecked(True)
    # Stale supplemental values from a previously selected source cannot replace
    # the visible primary source or require a second consent checkbox.
    page = chooser.settings_page
    page.controls["utaten_link"].setText("https://utaten.com/lyric/old-supplement/")
    page.controls["utaten_pronunciation_only"].setChecked(True)
    page.controls["rights_confirmed"].setChecked(False)
    chooser.next_step()
    assert not page.tabs.isTabVisible(1)
    chooser.next_step()
    chooser.accept()
    assert chooser.result() == QDialog.DialogCode.Accepted
    values = chooser.project_settings()
    assert values["utaten_link"] == "https://utaten.com/lyric/primary/"
    assert values["use_utaten_lyrics"] is True
    assert values["utaten_pronunciation_only"] is False and values["rights_confirmed"] is True


@pytest.mark.parametrize("source", ["netease", "qqmusic", "recognize"])
def test_switching_from_local_supplement_to_other_source_drops_incompatible_utaten(chooser, source):
    page = chooser.settings_page
    page.controls["utaten_pronunciation_only"].setChecked(True)
    page.controls["utaten_link"].setText("https://utaten.com/lyric/supplement/")
    chooser.source.setCurrentIndex(chooser.source.findData(source))
    chooser.link_edit.setText("https://primary.example/song")
    chooser.rights.setChecked(True)
    chooser.audio_picker.set_value("song.wav")
    chooser.accept()
    assert chooser.result() == QDialog.DialogCode.Accepted
    assert not page.tabs.isTabVisible(1)
    values = chooser.project_settings()
    assert values["utaten_link"] == ""
    assert values["utaten_pronunciation_only"] is False
    if source != "recognize":
        assert values[f"{source}_link"] == "https://primary.example/song"


def test_confirmation_describes_selected_online_audio(chooser):
    chooser.source.setCurrentIndex(chooser.source.findData("netease"))
    chooser.link_edit.setText("https://music.163.com/song?id=123")
    chooser.rights.setChecked(True)
    chooser.audio_mode.setCurrentIndex(chooser.audio_mode.findData("online"))
    chooser.next_step()
    chooser.next_step()
    assert "网易云在线音频" in chooser.summary.text()
    values = chooser.project_settings()
    assert values["prefer_netease_audio"] is True
    values["video_file"] = "visual.mp4"
    configured = ProjectDialog(settings=values, directory=chooser.project_directory())
    assert configured.project_settings()["video_file"] == "visual.mp4"
    assert configured.project_settings()["audio_file"] == ""
    configured.deleteLater()
    values.update(audio_file="project-assets/downloaded.m4a", video_file="visual.mp4")
    reopened = ProjectDialog(settings=values, directory=chooser.project_directory())
    assert reopened.audio_mode.currentData() == "online"
    assert reopened.project_settings()["audio_file"] == "project-assets/downloaded.m4a"
    assert reopened.project_settings()["video_file"] == "visual.mp4"
    reopened.link_edit.setText("https://music.163.com/song?id=456")
    assert reopened.project_settings()["audio_file"] == ""
    reopened.audio_mode.setCurrentIndex(reopened.audio_mode.findData("local"))
    assert reopened.project_settings()["prefer_netease_audio"] is False
    reopened.deleteLater()


def test_unsaved_document_settings_explain_when_project_folder_will_be_selected(qt_app):
    dialog = ProjectDialog(settings={"output_name": "Lyrics", "lyrics_file": "lyrics.json"})
    assert dialog.directory_picker.edit.placeholderText() == "保存时选择工程文件夹"
    dialog.set_step(2)
    assert "文件夹：保存时选择工程文件夹" in dialog.summary.text()
    dialog.deleteLater()


def test_folder_field_rejects_an_existing_file(chooser, tmp_path):
    file = tmp_path / "not-a-folder"
    file.write_text("keep", encoding="utf-8")
    chooser.directory_picker.set_value(str(file))
    chooser.lyrics_picker.set_value("source.lrc")
    chooser.accept()
    assert chooser.result() == QDialog.DialogCode.Rejected
    assert "已有文件" in chooser.error.text()
    assert file.read_text(encoding="utf-8") == "keep"
