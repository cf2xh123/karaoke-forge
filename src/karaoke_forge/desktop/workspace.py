"""One persistent workspace for importing, editing, rendering and reviewing a song."""

from __future__ import annotations

import copy
from dataclasses import fields
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)

from ..ass import AssStyle
from ..editor import document_from_payload
from ..formats import export_formats
from ..models import LyricsDocument
from ..projects import (
    PROJECT_FILENAME,
    WorkspaceProject,
    load_workspace_project,
    persist_project_asset,
    read_workspace_lyrics,
    safe_lyrics_export_stem,
    save_workspace_project,
    validate_project_assets,
)
from ..web import UiJobResult, _default_output_root, _safe_stem
from .common import PathPicker
from .project_dialog import lyrics_source_summary

_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
_LYRIC_SOURCES = ("lyrics_file", "pasted_lyrics", "netease_link", "qqmusic_link", "utaten_link")


class _SummaryLabel(QLabel):
    """Keep long paths and progress messages from shrinking the timeline."""

    def __init__(self, text=""):
        super().__init__(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setToolTip(text)

    def setText(self, text) -> None:
        super().setText(text)
        self.setToolTip(text)

    def paintEvent(self, _event) -> None:
        rectangle = self.contentsRect()
        text = self.fontMetrics().elidedText(
            self.text(), Qt.TextElideMode.ElideRight, rectangle.width()
        )
        painter = QStylePainter(self)
        painter.drawItemText(
            rectangle, int(self.alignment()), self.palette(), self.isEnabled(), text,
            QPalette.ColorRole.WindowText,
        )


class _RevisionWriter(QThread):
    """Archive large project assets without blocking the GUI event loop."""

    def __init__(self, document: LyricsDocument, settings: dict, directory: str):
        super().__init__()
        self.document = copy.deepcopy(document)
        self.settings = copy.deepcopy(settings)
        self.directory = directory
        self.result = None
        self.error = None

    def run(self) -> None:
        try:
            self.result = save_workspace_revision(self.document, self.settings, self.directory)
        except Exception as exc:  # noqa: BLE001 - propagate worker failures on the GUI thread
            self.error = exc


class _ArchiveProgress(QProgressDialog):
    """Do not let Escape or close abandon an in-flight asset copy."""

    def reject(self) -> None:
        pass

    def closeEvent(self, event) -> None:
        event.ignore()


def save_workspace_revision(document: LyricsDocument, settings: dict, directory: str):
    """Write the submitted lyrics, material choices and appearance as one project."""
    document = copy.deepcopy(document)
    previous_settings = {}
    if manifest := document.metadata.get("workspace_manifest"):
        try:
            previous_settings = load_workspace_project(manifest).settings
        except (OSError, TypeError, ValueError):
            pass
    settings = {
        **previous_settings, **settings, "lyrics_timebase": "audio", "preserve_lyrics_source": True,
    }
    validate_project_assets(
        audio=settings.get("audio_file"), video=settings.get("video_file"),
        cover=settings.get("cover_file"), font_files=tuple(settings.get("font_files") or ()),
    )
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    settings["lyrics_source_asset"] = ""
    if source := settings.get("lyrics_file"):
        if Path(source).is_file():
            local_source = persist_project_asset(source, root, "source-lyrics")
            settings["lyrics_file"] = str(local_source)
            settings["lyrics_source_asset"] = local_source.relative_to(root).as_posix()
        elif document.metadata.get("project_state") == "configured" or settings.get("pending_lyrics_source"):
            raise FileNotFoundError(f"所选歌词文件不存在，请重新选择后保存工程：{source}")
    for key, role in (("audio_file", "audio"), ("video_file", "video"), ("cover_file", "cover")):
        if settings.get(key):
            settings[key] = str(persist_project_asset(settings[key], root, role))
    if settings.get("font_files"):
        settings["font_files"] = [
            str(persist_project_asset(font, root, f"font-{index}"))
            for index, font in enumerate(settings["font_files"], 1)
        ]
    name = str(settings.get("output_name") or document.metadata.get("ti") or "歌词工程")
    document.metadata["workspace_manifest"] = str(root / PROJECT_FILENAME)
    formats = ["lrc", "elrc", "srt", "vtt", "ass", "json"] if document.is_timed else ["json"]
    style_fields = {field.name for field in fields(AssStyle)}
    style = AssStyle(**{key: value for key, value in settings.items() if key in style_fields})
    stem = safe_lyrics_export_stem(root, _safe_stem(name))

    def export_paths(basename, selected_formats=None):
        return [
            root / (f"{basename}.enhanced.lrc" if fmt == "elrc" else f"{basename}.{fmt}")
            for fmt in (formats if selected_formats is None else selected_formats)
        ]

    owned_paths = set()
    if (root / PROJECT_FILENAME).is_file():
        existing = load_workspace_project(root / PROJECT_FILENAME)
        if existing.lyrics_project.parent == root:
            owned_formats = (existing.settings or {}).get("lyrics_export_formats")
            if not isinstance(owned_formats, list):
                placeholder = read_workspace_lyrics(existing).metadata.get("project_state") == "configured"
                owned_formats = ["json"] if placeholder else formats
            owned_paths.update(export_paths(
                existing.lyrics_project.stem, [fmt for fmt in formats if fmt in owned_formats]
            ))
            if existing.name == name:
                stem = existing.lyrics_project.stem
    source_path = Path(settings["lyrics_file"]).resolve() if settings.get("lyrics_file") else None
    if source_path in export_paths(stem):
        # A local source may live in the chosen project folder. Its contents
        # must survive both the initial placeholder and later generated lyrics.
        stem = safe_lyrics_export_stem(root, f"{stem}-edited")
    base_stem, suffix = stem, 2
    while any(
        path == source_path or (path.exists() and path not in owned_paths)
        for path in export_paths(stem)
    ):
        stem = f"{base_stem}-{suffix}"
        suffix += 1
    # A later manifest failure must not leave an older project pointing at a
    # partially written lyric revision. Media is already validated/archived.
    previous_files = {
        path: path.read_bytes() if path.is_file() else None
        for path in [*export_paths(stem), root / PROJECT_FILENAME]
    }
    settings["lyrics_export_formats"] = list(formats)
    try:
        exports = export_formats(document, root, stem, formats, ass_style=style)
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
    except (OSError, TypeError, ValueError) as exc:
        failed_restores = []
        for path, content in previous_files.items():
            try:
                if content is None:
                    path.unlink(missing_ok=True)
                elif not path.is_file() or path.read_bytes() != content:
                    path.write_bytes(content)
            except OSError:
                failed_restores.append(path.name)
        if failed_restores:
            raise OSError(
                "工程保存失败，部分原文件无法恢复：" + "、".join(failed_restores)
            ) from exc
        raise
    return document, workspace, [str(path) for path in exports.values()]


class WorkspacePage(QWidget):
    """Keep the editor and project inputs in place throughout the production flow."""

    changed = Signal(bool)
    open_requested = Signal()
    new_requested = Signal()
    source_requested = Signal()

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
        self._project_directory: Path | None = None
        self._configured_document: LyricsDocument | None = None
        self._pending_lyrics_source = False
        self.is_saving_revision = False
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
        layout.setSpacing(6)
        self.input_bar = QWidget()
        input_layout = QVBoxLayout(self.input_bar)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(4)
        toolbar = QHBoxLayout()
        title = QLabel("歌曲工作台")
        title.setObjectName("pageTitle")
        title.setStyleSheet("font-size: 18px; font-weight: 700; padding: 0;")
        toolbar.addWidget(title)
        self.project_name = _SummaryLabel("尚未打开工程")
        self.project_name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        toolbar.addWidget(self.project_name, 1)
        self.save_button = self._button("保存工程", self.save_project, toolbar)
        self.source_button = self._button("项目设置…", self.source_requested.emit, toolbar)
        self.render_button = self._button("导出视频", self.render_video, toolbar)
        self.render_button.setProperty("primary", True)
        input_layout.addLayout(toolbar)
        summary = QHBoxLayout()
        self.project_summary = _SummaryLabel("创建工程时选择歌曲、歌词来源和制作设置，然后开始编辑。")
        summary.addWidget(self.project_summary, 1)
        self.prepare_button = self._button("生成时间轴", self.prepare_project, summary)
        input_layout.addLayout(summary)
        # Keep the legacy adapters available for integrations. Project materials
        # are configured in the project dialog and never occupy the editor area.
        self.name_edit = QLineEdit(self)
        self.name_edit.textEdited.connect(self._name_changed)
        self.song_picker = PathPicker(parent=self,
            filter="歌曲与视频 (*.wav *.mp3 *.flac *.m4a *.ogg *.aac *.mp4 *.mkv *.mov *.webm *.avi);;所有文件 (*)"
        )
        self.song_picker.changed.connect(self._song_changed)
        self.lyrics_picker = PathPicker(parent=self,
            filter="歌词与字幕 (*.txt *.lrc *.elrc *.yrc *.srt *.vtt *.ass *.json);;所有文件 (*)"
        )
        self.lyrics_picker.changed.connect(self._lyrics_changed)
        self.visual_summary = QLabel("画面：默认动态背景", self)
        for adapter in (self.name_edit, self.song_picker, self.lyrics_picker, self.visual_summary):
            adapter.hide()
        layout.addWidget(self.input_bar)
        self.document_status = _SummaryLabel("先创建或打开工程。")
        layout.addWidget(self.document_status)
        self.content = QSplitter(Qt.Orientation.Vertical)
        self.content.addWidget(self.editor)
        self.content.addWidget(self.outputs)
        self.content.setStretchFactor(0, 4)
        self.content.setStretchFactor(1, 1)
        self.outputs.hide()
        self.pages = QStackedWidget()
        self.empty_page = QWidget()
        empty_layout = QVBoxLayout(self.empty_page)
        empty_layout.addStretch()
        self.empty_title = QLabel("开始制作一首歌")
        self.empty_title.setObjectName("pageTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_title)
        self.empty_description = QLabel("创建工程，依次选择歌词来源、处理方式与外观；保存后进入编辑。")
        self.empty_description.setWordWrap(True)
        self.empty_description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_description)
        actions = QHBoxLayout()
        actions.addStretch()
        self.new_button = self._button("新建工程", self.new_requested.emit, actions)
        self.open_button = self._button("打开工程", self.open_requested.emit, actions)
        actions.addStretch()
        empty_layout.addLayout(actions)
        empty_layout.addStretch()
        self.pages.addWidget(self.empty_page)
        self.pages.addWidget(self.content)
        layout.addWidget(self.pages, 1)
        progress = QHBoxLayout()
        self.results_button = QPushButton("展开结果与日志")
        self.results_button.setCheckable(True)
        self.results_button.toggled.connect(self._show_results)
        progress.addWidget(self.results_button)
        self.activity = _SummaryLabel("就绪")
        progress.addWidget(self.activity, 1)
        layout.addLayout(progress)
        self.make.setParent(self)
        self.make.hide()

    @staticmethod
    def _lyrics_sources(settings):
        return (
            *(settings.get(key) or "" for key in _LYRIC_SOURCES),
            bool(settings.get("prefer_netease_audio")),
        )

    @property
    def is_dirty(self) -> bool:
        return self.editor.is_dirty or self.make.get_settings() != self._saved_settings

    @property
    def project_directory(self) -> str:
        return str(self._project_directory or "")

    @property
    def has_project(self) -> bool:
        return self._has_document or self._configured_document is not None

    def _update_dirty(self) -> None:
        dirty = self.is_dirty
        if dirty != self._dirty:
            self._dirty = dirty
            self.changed.emit(dirty)
        if self._has_document:
            pending_source = self._source_pending()
            if pending_source:
                self.document_status.setText(
                    "歌词来源已更改；重新生成时间轴后采用新歌词。当前编辑内容和设置均可保存。"
                )
            else:
                self.document_status.setText(
                    "● 有未保存修改 · 导出视频会直接使用下方当前歌词与时间轴。"
                    if dirty
                    else "歌词已就绪 · 可直接校准；导出视频会使用当前歌词与素材。"
                )
        elif self.has_project:
            self.document_status.setText(
                "● 有未保存的项目设置。" if dirty else "项目设置已保存；生成时间轴后开始编辑。"
            )

    def _settings_changed(self, settings) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self.song_picker.set_value(
                str(settings.get("audio_file") or settings.get("video_file") or "")
            )
            self.song_picker.edit.setPlaceholderText(
                "网易云在线音频（载入时获取）"
                if settings.get("netease_link") and not (
                    settings.get("audio_file") or settings.get("video_file")
                )
                else "选择或拖入本地文件…"
            )
            self.lyrics_picker.set_value(str(settings.get("lyrics_file") or ""))
            source = lyrics_source_summary(settings)
            self.source_button.setText("项目设置…")
            self.source_button.setToolTip("修改这个工程的歌词来源、处理方式、素材与外观")
            self.name_edit.setText(str(settings.get("output_name") or ""))
            self.project_name.setText(str(settings.get("output_name") or "尚未打开工程"))
            song = settings.get("audio_file") or settings.get("video_file")
            self.project_summary.setText(
                f"{source} · {Path(song).name if song else '尚未配置音频'}"
                if self.has_project else "创建工程时选择歌曲、歌词来源和制作设置，然后开始编辑。"
            )
            self.project_summary.setToolTip(
                f"{self.project_summary.text()}\n{self.project_directory}".rstrip()
            )
            self.pages.setCurrentWidget(self.content if self._has_document else self.empty_page)
            self.save_button.setEnabled(self.has_project and not self.runner.is_busy)
            self.source_button.setEnabled(self.has_project and not self.runner.is_busy)
            self.render_button.setEnabled(self._has_document and not self.runner.is_busy)
            self.prepare_button.setVisible(self.has_project)
            self.prepare_button.setText("重新生成时间轴" if self._has_document else "生成时间轴")
            self.empty_title.setText("工程已创建" if self.has_project else "开始制作一首歌")
            self.empty_description.setText(
                "项目配置已保存。点击上方“生成时间轴”，完成后直接进入歌词编辑。"
                if self.has_project else
                "创建工程，依次选择歌词来源、处理方式与外观；保存后进入编辑。"
            )
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
            # Picking a local file explicitly switches away from online / pasted text.
            self.make.apply_material_settings({
                **dict.fromkeys(_LYRIC_SOURCES, ""),
                "lyrics_file": path,
                "use_netease_lyrics": False,
                "use_qqmusic_lyrics": False,
                "use_utaten_lyrics": False,
                "utaten_pronunciation_only": False,
            })

    def _name_changed(self, name: str) -> None:
        if not self._syncing:
            # Keep the user's cursor and spaces while typing; get_settings trims
            # the saved value, so reflecting it into this edit would eat spaces.
            self._syncing = True
            try:
                self.make.controls["output_name"].setText(name)
                if self._has_document:
                    self.editor.name_edit.setText(name)
            finally:
                self._syncing = False
            self._update_dirty()

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
        self.source_requested.emit()

    def _source_pending(self) -> bool:
        return self._has_document and (
            self._pending_lyrics_source
            or self._lyrics_sources(self.make.get_settings()) != self._active_lyrics_sources
        )

    def _document_for_save(self) -> LyricsDocument:
        if self._has_document:
            return self.editor.current_document()
        if self._configured_document is not None:
            return copy.deepcopy(self._configured_document)
        raise ValueError("请先新建或打开工程。")

    def _adopt_saved_document(self, document: LyricsDocument) -> bool:
        if self._has_document:
            return self.editor.adopt_saved_revision(document)
        if self._configured_document is None:
            return False
        self._configured_document = copy.deepcopy(document)
        return True

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

    def _save_revision_responsive(self, document, settings, directory):
        """Keep setup's transactional return value while large assets are copied."""
        progress = _ArchiveProgress("正在归档工程素材并保存设置……", "", 0, 0, self)
        progress.setWindowTitle("保存工程")
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.CustomizeWindowHint | Qt.WindowType.WindowTitleHint
        )
        progress.setCancelButton(None)
        progress.setAutoClose(False)
        worker = _RevisionWriter(document, settings, directory)
        worker.finished.connect(progress.accept)
        self.is_saving_revision = True
        try:
            worker.start()
            progress.exec()
            worker.wait()
            if worker.error is not None:
                raise worker.error
            return worker.result
        finally:
            worker.wait()
            self.is_saving_revision = False
            worker.deleteLater()
            progress.deleteLater()

    def start_project(self, settings: dict, directory: str | None = None) -> bool:
        """Replace a project only after the main window has approved the replacement."""
        if self.runner.is_busy:
            return False
        try:
            normalized = self.make.new_project_settings(settings)
            configured = LyricsDocument([], metadata={"project_state": "configured"})
            saved = None
            if directory:
                manifest = Path(directory).resolve() / PROJECT_FILENAME
                if manifest.exists():
                    self.activity.setText("此目录已有工程，请选择一个新的工程目录。")
                    return False
                configured, saved, _files = self._save_revision_responsive(
                    configured, normalized, directory
                )
        except (OSError, TypeError, ValueError) as exc:
            self.activity.setText(f"工程未创建，当前工程仍保留：{exc}")
            return False
        self._syncing = True
        try:
            if saved:
                self.make.restore_workspace(saved, configured=True)
            else:
                self.make.reset_project(normalized)
            self.editor.clear_project()
            self.outputs.clear_project()
            self._has_document = False
            self._pending_lyrics_source = False
            self._configured_document = configured
            self._project_directory = saved.manifest.parent if saved else None
            self._render_settings = None
            self._preview_materials = None
            self._saved_settings = copy.deepcopy(self.make.get_settings()) if saved else {}
            self._active_lyrics_sources = self._lyrics_sources(self.make.get_settings())
        finally:
            self._syncing = False
        self._settings_changed(self.make.get_settings())
        self.results_button.setChecked(False)
        self.activity.setText(
            f"工程配置已保存至 {self.project_directory}；生成时间轴后开始编辑。"
            if saved else "工程配置已应用；保存工程或生成时间轴后继续。"
        )
        return True

    def apply_project_settings(self, settings: dict) -> bool:
        """Save source and style choices without replacing the lyrics being edited."""
        if self.runner.is_busy or not self.has_project:
            return False
        try:
            normalized = self.make._validated_settings(settings)
            document = self._document_for_save()
        except (OSError, TypeError, ValueError) as exc:
            self.activity.setText(f"项目设置未更改：{exc}")
            return False
        merged = {**self.make.get_settings(), **normalized}
        submitted_controls = copy.deepcopy(self.make.get_settings())
        pending_source = self._has_document and (
            self._pending_lyrics_source or self._lyrics_sources(merged) != self._active_lyrics_sources
        )
        merged["pending_lyrics_source"] = pending_source
        directory = self.project_directory
        if not directory:
            directory = QFileDialog.getExistingDirectory(
                self, "保存完整工程与项目设置", str(_default_output_root())
            )
            if not directory:
                return False
            if (Path(directory).resolve() / PROJECT_FILENAME).exists():
                overwrite = QMessageBox.question(
                    self, "此目录已有其他工程",
                    "所选目录已包含另一份工程。继续将替换其中的工程索引。是否覆盖？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if overwrite != QMessageBox.StandardButton.Yes:
                    return False

        def task(log):
            log("正在保存项目设置与当前歌词……")
            return save_workspace_revision(document, merged, directory)

        def saved(result):
            saved_document, workspace, _files = result
            unchanged = self.make.get_settings() == submitted_controls
            self._syncing = True
            try:
                if unchanged:
                    self.make.apply_material_settings(workspace.settings or merged)
                self._pending_lyrics_source = pending_source
                if not pending_source and unchanged:
                    self._active_lyrics_sources = self._lyrics_sources(self.make.get_settings())
            finally:
                self._syncing = False
            self._project_directory = workspace.manifest.parent
            self._settings_changed(self.make.get_settings())
            self._adopt_saved_document(saved_document)
            self._saved_settings = copy.deepcopy(self.make.get_settings() if unchanged else merged)
            self._update_dirty()
            self.activity.setText("项目设置已保存；当前歌词仍保留。更改歌词来源后可重新生成时间轴。")

        return self.runner.submit("保存项目设置", task, saved)

    def apply_material_settings(self, settings: dict) -> None:
        self.make.apply_material_settings(settings)
        self.activity.setText("素材来源已更改；点击载入 / 生成时间轴后采用新歌词。")

    def adopt_prepared(self, result) -> None:
        if not result.payload or not result.payload.get("lines"):
            self.activity.setText(result.status)
            return
        document = document_from_payload(result.payload)
        project_directory = self._project_directory
        project_save_failed = False
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
            self._pending_lyrics_source = False
            if project_directory:
                try:
                    document, saved, _files = self._save_revision_responsive(
                        document,
                        {**settings, "pending_lyrics_source": False},
                        str(project_directory),
                    )
                    # Generation output is an intermediate result. The created
                    # project remains the document's home throughout editing.
                    self.make.apply_material_settings(saved.settings or settings)
                except (OSError, TypeError, ValueError) as exc:
                    document.metadata["workspace_manifest"] = str(project_directory / PROJECT_FILENAME)
                    self.outputs.log.appendPlainText(f"时间轴已生成，原工程目录保存失败：{exc}")
                    project_save_failed = True
            settings = self.make.get_settings()
            audio = settings.get("audio_file") or settings.get("video_file") or result.audio
            self.editor.load_document(document, audio, name)
            self._has_document = True
            self._configured_document = None
            if not project_directory and document.metadata.get("workspace_manifest"):
                self._project_directory = Path(document.metadata["workspace_manifest"]).resolve().parent
            self._saved_settings = {} if project_save_failed else copy.deepcopy(settings)
            self._active_lyrics_sources = self._lyrics_sources(settings)
        finally:
            self._syncing = False
        self._settings_changed(self.make.get_settings())
        self.activity.setText(
            f"时间轴已生成 · 有 {self.editor.review_count} 句待核对，可用待核对句按钮定位。"
            if self.editor.review_count
            else "时间轴已生成 · 在当前工作台直接试听和调整，满意后导出视频。"
        )

    def load_project(self, document, workspace: WorkspaceProject | None, source: str) -> None:
        # Validate before replacing any part of the currently open project.
        # A failed import must leave both the lyrics and its materials intact.
        configured = not document.lines and document.metadata.get("project_state") == "configured"
        if not document.lines and (not configured or workspace is None):
            raise ValueError("歌词工程为空，当前工程未更改。")
        self._syncing = True
        try:
            if workspace:
                self.make.restore_workspace(workspace, configured=configured)
                audio = str(workspace.audio or workspace.video or "") or None
                name = workspace.name
            else:
                self.make.set_editor_document(document)
                self.make.controls["lyrics_file"].set_value(source)
                settings = self.make.get_settings()
                audio = settings.get("audio_file") or settings.get("video_file") or None
                name = settings.get("output_name") or Path(source).stem
                self.make.controls["output_name"].setText(name)
            if configured:
                self.editor.clear_project()
                self._configured_document = copy.deepcopy(document)
            else:
                self.editor.load_document(document, audio, name)
                self._configured_document = None
            self._has_document = bool(document.lines)
            self.outputs.clear_project()
            self.results_button.setChecked(False)
            self._pending_lyrics_source = bool(
                workspace and (workspace.settings or {}).get("pending_lyrics_source")
            )
            manifest = workspace.manifest if workspace else document.metadata.get("workspace_manifest")
            self._project_directory = Path(manifest).resolve().parent if manifest else None
            self._saved_settings = copy.deepcopy(self.make.get_settings())
            self._active_lyrics_sources = self._lyrics_sources(self._saved_settings)
        finally:
            self._syncing = False
        self._settings_changed(self.make.get_settings())
        self.activity.setText(
            f"已打开 {name} · 项目配置已恢复，生成时间轴后开始编辑。"
            if configured else f"已打开 {name} · 继续试听和校准当前歌词。"
        )
        if warning := document.metadata.get("legacy_timing_warning"):
            self.activity.setText(f"已打开 {name} · {warning}")
            self.outputs.log.appendPlainText(warning)

    def render_video(self) -> None:
        if self.runner.is_busy:
            return
        if self._has_document:
            if self._source_pending():
                self.activity.setText(
                    "请先重新生成时间轴，采用新选择的歌词来源，再导出视频；当前编辑内容仍保留。"
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
            saved = read_workspace_lyrics(workspace)
            if self._project_directory:
                saved, workspace, _files = self._save_revision_responsive(
                    saved,
                    {**submitted, "pending_lyrics_source": False},
                    str(self._project_directory),
                )
            else:
                saved.metadata["workspace_manifest"] = str(workspace.manifest)
            self._syncing = True
            try:
                self.make.apply_material_settings(workspace.settings or submitted)
                self._active_lyrics_sources = self._lyrics_sources(self.make.get_settings())
            finally:
                self._syncing = False
            self._settings_changed(self.make.get_settings())
            if not self.editor.adopt_saved_revision(saved):
                return False
        except (OSError, ValueError, TypeError) as exc:
            self.outputs.log.appendPlainText(f"成片已输出，工程保存状态需手动确认：{exc}")
            return False
        self._project_directory = workspace.manifest.parent
        self._saved_settings = copy.deepcopy(self.make.get_settings())
        self._update_dirty()
        return True

    def save_project(self, *, as_new: bool = False) -> bool:
        if self.runner.is_busy:
            return False
        try:
            document = self._document_for_save()
        except (ValueError, TypeError) as exc:
            self.activity.setText(f"请先载入或修正歌词：{exc}")
            return False
        settings = copy.deepcopy(self.make.get_settings())
        pending_source = self._source_pending()
        save_settings = {**settings, "pending_lyrics_source": pending_source}
        directory = self.project_directory if not as_new else ""
        if not directory:
            directory = QFileDialog.getExistingDirectory(
                self, "另存完整工程" if as_new else "保存完整工程", str(_default_output_root())
            )
        if not directory:
            return False
        target_manifest = Path(directory).resolve() / PROJECT_FILENAME
        current_manifest = (
            self._project_directory / PROJECT_FILENAME if self._project_directory
            else document.metadata.get("workspace_manifest")
        )
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
            return save_workspace_revision(document, save_settings, directory)

        def saved(result):
            saved_document, workspace, files = result
            unchanged = settings == self.make.get_settings()
            if unchanged:
                self._syncing = True
                try:
                    self.make.apply_material_settings(workspace.settings or settings)
                    if not pending_source:
                        self._active_lyrics_sources = self._lyrics_sources(self.make.get_settings())
                finally:
                    self._syncing = False
                self._settings_changed(self.make.get_settings())
            try:
                adopted = self._adopt_saved_document(saved_document)
            except (ValueError, TypeError):
                adopted = False
            self._project_directory = workspace.manifest.parent
            self._pending_lyrics_source = pending_source
            self._saved_settings = copy.deepcopy(self.make.get_settings() if unchanged else settings)
            self._settings_changed(self.make.get_settings())
            self._update_dirty()
            suffix = "当前还有更新的未保存修改。" if not adopted or self.is_dirty else ""
            message = (
                f"完整工程已保存至 {workspace.manifest.parent}，导出 {len(files)} 个歌词文件。{suffix}"
            )
            self.outputs.show_result(UiJobResult(
                message, None, [*files, str(workspace.manifest)], "", str(workspace.manifest.parent)
            ))
            self.activity.setText(message)

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
