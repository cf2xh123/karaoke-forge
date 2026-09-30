"""One persistent workspace for importing, editing, rendering and reviewing a song."""

from __future__ import annotations

import copy
from dataclasses import fields
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..ass import AssStyle
from ..editor import document_from_payload
from ..formats import export_formats, read_lyrics
from ..models import LyricsDocument
from ..projects import (
    PROJECT_FILENAME,
    WorkspaceProject,
    load_workspace_project,
    save_workspace_project,
)
from ..web import _default_output_root, _safe_stem
from .common import PathPicker

_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
_LYRIC_SOURCES = ("lyrics_file", "pasted_lyrics", "netease_link", "qqmusic_link", "utaten_link")


def save_workspace_revision(document: LyricsDocument, settings: dict, directory: str):
    """Write the submitted lyrics, material choices and appearance as one project."""
    document = copy.deepcopy(document)
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    name = str(settings.get("output_name") or document.metadata.get("ti") or "歌词工程")
    document.metadata["workspace_manifest"] = str(root / PROJECT_FILENAME)
    formats = ["lrc", "elrc", "srt", "vtt", "ass", "json"] if document.is_timed else ["json"]
    style_fields = {field.name for field in fields(AssStyle)}
    style = AssStyle(**{key: value for key, value in settings.items() if key in style_fields})
    exports = export_formats(document, root, _safe_stem(name), formats, ass_style=style)
    workspace = save_workspace_project(
        root,
        name=name,
        lyrics_project=exports["json"],
        audio=settings.get("audio_file") or None,
        video=settings.get("video_file") or None,
        cover=settings.get("cover_file") or None,
        font_files=tuple(settings.get("font_files") or ()),
        settings=settings,
        recent_root=_default_output_root(),
    )
    return document, workspace, [str(path) for path in exports.values()]


class WorkspacePage(QWidget):
    """Keep the editor and project inputs in place throughout the production flow."""

    changed = Signal(bool)
    open_requested = Signal()

    def __init__(self, make, editor, outputs, runner, parent=None):
        super().__init__(parent)
        self.make = make
        self.editor = editor
        self.outputs = outputs
        self.runner = runner
        self._syncing = False
        self._has_document = False
        self._dirty = False
        self._preview_materials = None
        self._render_settings = None
        self._saved_settings = copy.deepcopy(make.get_settings())
        self._active_lyrics_sources = self._lyrics_sources(self._saved_settings)
        self._build_ui()
        self.editor.set_workspace_mode(True)
        self.editor.changed.connect(lambda _dirty: self._update_dirty())
        self.make.settings_changed.connect(self._settings_changed)
        self.make.style_changed.connect(self.editor.preview.set_style)
        self.make.material_preview_changed.connect(self.editor.preview.set_background)
        self.make.before_prepare = self._allow_regenerate
        self.runner.busy_changed.connect(self._busy_changed)
        self.runner.message.connect(self.activity.setText)
        self.runner.failed.connect(self._render_failed)
        self._settings_changed(self.make.get_settings())

    def _button(self, text, callback, layout):
        button = QPushButton(text)
        button.clicked.connect(lambda _checked=False: callback())
        layout.addWidget(button)
        return button

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)
        self.input_bar = QWidget()
        input_layout = QVBoxLayout(self.input_bar)
        input_layout.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        title = QLabel("歌曲工作台")
        title.setObjectName("pageTitle")
        toolbar.addWidget(title)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("工程 / 成片名称")
        self.name_edit.textEdited.connect(self._name_changed)
        toolbar.addWidget(self.name_edit, 1)
        self._button("打开工程", self.open_requested.emit, toolbar)
        self.save_button = self._button("保存工程", self.save_project, toolbar)
        self.render_button = self._button("导出视频", self.render_video, toolbar)
        self.render_button.setProperty("primary", True)
        input_layout.addLayout(toolbar)
        imports = QHBoxLayout()
        imports.addWidget(QLabel("歌曲 / 有声 MV"))
        self.song_picker = PathPicker(
            filter="歌曲与视频 (*.wav *.mp3 *.flac *.m4a *.ogg *.aac *.mp4 *.mkv *.mov *.webm *.avi);;所有文件 (*)"
        )
        self.song_picker.changed.connect(self._song_changed)
        imports.addWidget(self.song_picker, 2)
        imports.addWidget(QLabel("歌词"))
        self.lyrics_picker = PathPicker(
            filter="歌词与字幕 (*.txt *.lrc *.elrc *.yrc *.srt *.vtt *.ass *.json);;所有文件 (*)"
        )
        self.lyrics_picker.changed.connect(self._lyrics_changed)
        imports.addWidget(self.lyrics_picker, 2)
        self.prepare_button = self._button("载入 / 生成时间轴", self.prepare_project, imports)
        input_layout.addLayout(imports)
        context = QHBoxLayout()
        self._button("添加 MV / 封面", self.choose_visual, context)
        self.visual_summary = QLabel("画面：默认动态背景")
        self.visual_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        context.addWidget(self.visual_summary, 1)
        self._button("撤销", self.editor.undo, context)
        self._button("重做", self.editor.redo, context)
        self.settings_button = self._button("素材与样式…", self.show_settings, context)
        input_layout.addLayout(context)
        layout.addWidget(self.input_bar)
        self.document_status = QLabel("选择歌曲与歌词，载入时间轴后在下方直接试听、校准和导出。")
        self.document_status.setWordWrap(True)
        layout.addWidget(self.document_status)
        self.content = QSplitter(Qt.Orientation.Vertical)
        self.content.addWidget(self.editor)
        self.content.addWidget(self.outputs)
        self.content.setStretchFactor(0, 4)
        self.content.setStretchFactor(1, 1)
        self.outputs.hide()
        layout.addWidget(self.content, 1)
        progress = QHBoxLayout()
        self.results_button = QPushButton("展开结果与日志")
        self.results_button.setCheckable(True)
        self.results_button.toggled.connect(self._show_results)
        progress.addWidget(self.results_button)
        self.activity = QLabel("就绪")
        self.activity.setWordWrap(True)
        progress.addWidget(self.activity, 1)
        layout.addLayout(progress)
        self.settings_dialog = QDialog(self)
        self.settings_dialog.setWindowTitle("当前工程 · 素材与样式")
        self.settings_dialog.resize(720, 720)
        settings_layout = QVBoxLayout(self.settings_dialog)
        settings_layout.addWidget(self.make)
        buttons = QHBoxLayout()
        self._button("刷新画面预览", self.make.refresh_preview, buttons)
        buttons.addStretch()
        self._button("完成", self.settings_dialog.hide, buttons)
        settings_layout.addLayout(buttons)

    @staticmethod
    def _lyrics_sources(settings):
        return tuple(settings.get(key) or "" for key in _LYRIC_SOURCES)

    @property
    def is_dirty(self) -> bool:
        return self.editor.is_dirty or self.make.get_settings() != self._saved_settings

    def _update_dirty(self) -> None:
        dirty = self.is_dirty
        if dirty != self._dirty:
            self._dirty = dirty
            self.changed.emit(dirty)
        if self._has_document:
            pending_source = (
                self._lyrics_sources(self.make.get_settings()) != self._active_lyrics_sources
            )
            if pending_source:
                self.document_status.setText(
                    "歌词来源已更改；点击“载入 / 生成时间轴”采用新歌词。当前编辑内容仍保留。"
                )
            else:
                self.document_status.setText(
                    "● 有未保存修改 · 导出视频会直接使用下方当前歌词与时间轴。"
                    if dirty
                    else "歌词已就绪 · 可直接校准；导出视频会使用当前歌词与素材。"
                )

    def _settings_changed(self, settings) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self.song_picker.set_value(
                str(settings.get("audio_file") or settings.get("video_file") or "")
            )
            self.lyrics_picker.set_value(str(settings.get("lyrics_file") or ""))
            self.name_edit.setText(str(settings.get("output_name") or ""))
            visual = settings.get("video_file") or settings.get("cover_file")
            self.visual_summary.setText(
                f"画面：{Path(visual).name}" if visual else "画面：默认动态背景"
            )
            materials = tuple(
                settings.get(key)
                for key in (
                    "video_file",
                    "cover_file",
                    "cover_background",
                    "cover_style",
                    "cover_waveform",
                )
            )
            if materials != self._preview_materials:
                self._preview_materials = materials
                self.editor.preview.set_background(
                    settings.get("cover_file") if not settings.get("video_file") else None
                )
            if self._has_document:
                self.editor.name_edit.setText(str(settings.get("output_name") or ""))
                self.editor.audio_picker.set_value(
                    str(settings.get("audio_file") or settings.get("video_file") or "")
                )
        finally:
            self._syncing = False
        self.editor.preview.set_style(settings)
        self._update_dirty()

    def _song_changed(self, path: str) -> None:
        if self._syncing:
            return
        is_video = Path(path).suffix.lower() in _VIDEO_EXTENSIONS
        self._syncing = True
        try:
            self.make.controls["audio_file"].set_value("" if is_video else path)
            if is_video or not path:
                self.make.controls["video_file"].set_value(path)
        finally:
            self._syncing = False
        self._settings_changed(self.make.get_settings())

    def _lyrics_changed(self, path: str) -> None:
        if not self._syncing:
            self.make.controls["lyrics_file"].set_value(path)

    def _name_changed(self, name: str) -> None:
        if not self._syncing:
            self.make.controls["output_name"].setText(name)

    def choose_visual(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "添加歌曲 MV 或专辑封面",
            "",
            "视频与图片 (*.mp4 *.mkv *.mov *.webm *.avi *.png *.jpg *.jpeg *.webp *.bmp)",
        )
        if path:
            key = "video_file" if Path(path).suffix.lower() in _VIDEO_EXTENSIONS else "cover_file"
            self._syncing = True
            try:
                if key == "cover_file":
                    settings = self.make.get_settings()
                    if settings.get("video_file") and not settings.get("audio_file"):
                        self.make.controls["audio_file"].set_value(settings["video_file"])
                    self.make.controls["video_file"].set_value("")
                self.make.controls[key].set_value(path)
            finally:
                self._syncing = False
            self._settings_changed(self.make.get_settings())

    def show_settings(self) -> None:
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def _busy_changed(self, busy: bool) -> None:
        self.input_bar.setEnabled(not busy)
        self.editor.setEnabled(not busy)
        self.make.setEnabled(not busy)
        if busy:
            self.editor.stop_playback()
            self.outputs.player.pause()

    def _allow_regenerate(self) -> bool:
        if self.runner.is_busy:
            return False
        if not self.editor.is_dirty:
            return True
        return (
            QMessageBox.question(
                self,
                "重新生成歌词时间轴",
                "当前歌词有未保存的手动修改。重新生成将替换这些修改，是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def prepare_project(self) -> None:
        self.make.prepare_project()

    def adopt_prepared(self, result) -> None:
        if not result.payload or not result.payload.get("lines"):
            self.activity.setText(result.status)
            return
        document = document_from_payload(result.payload)
        self._syncing = True
        try:
            self.make.stage_editor_document(document)
            settings = self.make.get_settings()
            # Online preparation can download new media. Adopt only its missing
            # assets; the active controls remain authoritative for user choices.
            manifest = document.metadata.get("workspace_manifest")
            if not manifest and result.output_dir:
                manifest = str(Path(result.output_dir) / PROJECT_FILENAME)
            if manifest:
                try:
                    prepared = load_workspace_project(manifest)
                    for key, value in (
                        ("audio_file", prepared.audio),
                        ("video_file", prepared.video),
                        ("cover_file", prepared.cover),
                    ):
                        if not settings.get(key) and value:
                            self.make.controls[key].set_value(str(value))
                    if not settings.get("font_files") and prepared.font_files:
                        self.make.controls["font_files"].set_paths(
                            [str(path) for path in prepared.font_files]
                        )
                except (OSError, TypeError, ValueError) as exc:
                    self.outputs.log.appendPlainText(f"生成工程的素材索引不可用：{exc}")
            name = str(settings.get("output_name") or result.project_name or "歌词工程")
            self.make.controls["output_name"].setText(name)
            settings = self.make.get_settings()
            if not settings.get("audio_file") and not settings.get("video_file") and result.audio:
                self.make.controls["audio_file"].set_value(result.audio)
            settings = self.make.get_settings()
            audio = settings.get("audio_file") or settings.get("video_file") or result.audio
            self.editor.load_document(document, audio, name)
            self._has_document = True
            self._saved_settings = copy.deepcopy(self.make.get_settings())
            self._active_lyrics_sources = self._lyrics_sources(self._saved_settings)
        finally:
            self._syncing = False
        self._settings_changed(self.make.get_settings())
        self.prepare_button.setText("重新载入 / 生成时间轴")
        self.activity.setText("时间轴已生成 · 在当前工作台直接试听和调整，满意后导出视频。")

    def load_project(self, document, workspace: WorkspaceProject | None, source: str) -> None:
        self._syncing = True
        try:
            if workspace:
                self.make.restore_workspace(workspace)
                audio = str(workspace.audio or workspace.video or "") or None
                name = workspace.name
            else:
                self.make.set_editor_document(document)
                self.make.controls["lyrics_file"].set_value(source)
                settings = self.make.get_settings()
                audio = settings.get("audio_file") or settings.get("video_file") or None
                name = settings.get("output_name") or Path(source).stem
                self.make.controls["output_name"].setText(name)
            self.editor.load_document(document, audio, name)
            self._has_document = True
            self._saved_settings = copy.deepcopy(self.make.get_settings())
            self._active_lyrics_sources = self._lyrics_sources(self._saved_settings)
        finally:
            self._syncing = False
        self._settings_changed(self.make.get_settings())
        self.prepare_button.setText("重新载入 / 生成时间轴")
        self.activity.setText(f"已打开 {name} · 素材、样式和歌词在同一个工作台继续编辑。")

    def render_video(self) -> None:
        if self.runner.is_busy:
            return
        if self._has_document:
            if self._lyrics_sources(self.make.get_settings()) != self._active_lyrics_sources:
                self.activity.setText(
                    "请先载入新选择的歌词来源，再导出视频；原有编辑内容尚未被替换。"
                )
                return
            try:
                document = self.editor.current_document()
                self.make.stage_editor_document(document)
                self._active_lyrics_sources = self._lyrics_sources(self.make.get_settings())
            except (ValueError, TypeError, OSError) as exc:
                self.activity.setText(f"请先修正歌词修改：{exc}")
                return
        self.editor.stop_playback()
        self._render_settings = (
            copy.deepcopy(self.make.get_settings()) if self._has_document else None
        )
        self.make.render_video()

    def _render_failed(self, _message: str) -> None:
        self._render_settings = None

    def _adopt_rendered_project(self, result) -> bool:
        submitted = self._render_settings
        self._render_settings = None
        if (
            submitted is None
            or submitted != self.make.get_settings()
            or not result.video
            or not Path(result.video).is_file()
            or not result.output_dir
        ):
            return False
        manifest = Path(result.output_dir) / PROJECT_FILENAME
        if not manifest.is_file():
            return False
        try:
            workspace = load_workspace_project(manifest)
            saved = read_lyrics(workspace.lyrics_project)
            saved.metadata["workspace_manifest"] = str(workspace.manifest)
            if not self.editor.adopt_saved_revision(saved):
                return False
        except (OSError, ValueError, TypeError) as exc:
            self.outputs.log.appendPlainText(f"成片已输出，工程保存状态需手动确认：{exc}")
            return False
        self._saved_settings = copy.deepcopy(submitted)
        self._update_dirty()
        return True

    def save_project(self) -> bool:
        if self.runner.is_busy:
            return False
        if self._has_document and (
            self._lyrics_sources(self.make.get_settings()) != self._active_lyrics_sources
        ):
            self.activity.setText("请先载入新选择的歌词来源，再保存完整工程；当前编辑内容仍保留。")
            return False
        try:
            document = self.editor.current_document()
        except (ValueError, TypeError) as exc:
            self.activity.setText(f"请先载入或修正歌词：{exc}")
            return False
        settings = copy.deepcopy(self.make.get_settings())
        directory = QFileDialog.getExistingDirectory(
            self, "保存完整工程", str(_default_output_root())
        )
        if not directory:
            return False
        target_manifest = Path(directory).resolve() / PROJECT_FILENAME
        current_manifest = document.metadata.get("workspace_manifest")
        same_project = bool(
            current_manifest and Path(current_manifest).resolve() == target_manifest
        )
        if target_manifest.exists() and not same_project:
            overwrite = QMessageBox.question(
                self,
                "此目录已有其他工程",
                "所选目录已包含另一份工程。继续将替换其中的工程索引和同名歌词文件。\n"
                "是否覆盖？选择“否”可返回并选择其他目录。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if overwrite != QMessageBox.StandardButton.Yes:
                return False

        def task(log):
            log("正在保存歌词、素材与当前样式……")
            return save_workspace_revision(document, settings, directory)

        def saved(result):
            saved_document, workspace, files = result
            try:
                adopted = self.editor.adopt_saved_revision(saved_document)
            except (ValueError, TypeError):
                adopted = False
            self._saved_settings = copy.deepcopy(settings)
            self._update_dirty()
            suffix = "当前还有更新的未保存修改。" if not adopted or self.is_dirty else ""
            self.activity.setText(
                f"完整工程已保存至 {workspace.manifest.parent}，导出 {len(files)} 个歌词文件。{suffix}"
            )

        return self.runner.submit("保存完整工程", task, saved)

    def show_result(self, result) -> None:
        self.outputs.show_result(result)
        self.results_button.setChecked(True)
        saved = self._adopt_rendered_project(result)
        self.activity.setText(
            "视频与完整工程已保存，结果显示在下方；可以继续调整。"
            if saved
            else "本次结果已显示在下方；可以继续调整当前歌词和样式。"
        )

    def _show_results(self, visible: bool) -> None:
        self.outputs.setVisible(visible)
        self.results_button.setText("收起结果与日志" if visible else "展开结果与日志")
        if visible:
            self.content.setSizes([max(320, self.height() - 400), 230])
        else:
            self.outputs.player.pause()
