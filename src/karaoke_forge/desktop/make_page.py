"""Native Qt authoring controls for the existing karaoke production pipeline."""

from __future__ import annotations

import base64
import copy
import html
import inspect
import json
import math
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..formats import write_json
from ..models import KaraokeToken, LyricLine, LyricsDocument
from ..netease_login import (
    acquire_netease_music_u,
    capture_netease_music_u,
    clear_netease_login_profile,
)
from ..preferences import load_preferences, save_preferences
from ..projects import (
    PROJECT_FILENAME,
    WorkspaceProject,
    load_workspace_project,
    read_workspace_lyrics,
    save_workspace_project,
)
from ..web import (
    UiEditorPreparationResult,
    UiJobResult,
    _default_output_root,
    _matching_workspace_manifest,
    prepare_make_editor_job,
    prepare_subtitle_material_preview,
    run_make_job,
)
from .common import PathPicker
from .timeline import LyricPreviewWidget

_PREPARE_FIELDS = set(inspect.signature(prepare_make_editor_job).parameters) - {"progress_callback"}
_RENDER_FIELDS = set(inspect.signature(run_make_job).parameters) - {"progress_callback"}
_PRIVATE_FIELDS = {"music_u", "cookie_browser_profile", "cookie_browser"}
_ASSET_FIELDS = {
    "audio_file",
    "video_file",
    "lyrics_file",
    "cover_file",
    "font_files",
    "pasted_lyrics",
    "output_name",
    "output_root",
}
_STYLE_FIELDS = {
    "font",
    "font_size",
    "text_color",
    "highlight_color",
    "margin_v",
    "show_translation",
    "translation_font_size",
    "translation_color",
    "translation_margin_v",
    "show_pronunciation",
    "pronunciation_font_size",
    "pronunciation_color",
    "auto_english_pronunciation",
    "show_countdown",
    "countdown_gap_threshold",
}
_ALIASES = {
    "alignment_language": "language",
    "alignment_model": "model",
    "alignment_device": "device",
    "alignment_separate_vocals": "separate_vocals",
    "audio_offset": "audio_offset",
}


def _plain(value: object) -> str:
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    return html.unescape(re.sub(r"(?m)^#{1,6}\s*|\*\*", "", text))


def _native_job_arguments(arguments: dict[str, Any], directory: str) -> dict[str, Any]:
    """Use native material selections without the web upload-recovery fallback.

    Run in the worker: a project can point at a lyrics JSON whose old manifest
    would otherwise refill deliberately cleared audio, MV, cover or font fields.
    Only the private input copy loses this reference; source files stay intact.
    """
    source_value = arguments.get("lyrics_file")
    if not source_value:
        return arguments
    source = Path(source_value)
    if source.suffix.lower() != ".json" or not source.is_file():
        return arguments
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict) and payload.get("schema_version") == 1 and payload.get(
        "lyrics_project"
    ):
        workspace = load_workspace_project(source)
        payload = read_workspace_lyrics(workspace).to_dict()
        source = Path(directory) / f"source-{uuid4().hex}.json"
        source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict) and "workspace_manifest" in metadata:
        manifest = Path(metadata["workspace_manifest"])
        if not manifest.is_absolute():
            manifest = Path(source_value).parent / manifest
        try:
            workspace = load_workspace_project(manifest)
            if Path(source_value).resolve() == workspace.lyrics_project.resolve():
                payload = read_workspace_lyrics(workspace).to_dict()
                metadata = payload["metadata"]
        except (OSError, TypeError, ValueError):
            pass
        metadata.pop("workspace_manifest")
        source = Path(directory) / f"source-{uuid4().hex}.json"
        source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return {**arguments, "lyrics_file": str(source)}


class ColorButton(QPushButton):
    changed = Signal(str)

    def __init__(self, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value = color
        self.set_value(color)
        self.clicked.connect(self._choose)

    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        color = QColor(value)
        if color.isValid():
            self._value = color.name().upper()
            self.setText(self._value)
            foreground = "#18202D" if color.lightness() > 150 else "#FFFFFF"
            self.setStyleSheet(f"background:{self._value};color:{foreground};padding:7px;")
            self.changed.emit(self._value)

    def _choose(self) -> None:
        color = QColorDialog.getColor(QColor(self._value), self, "选择字幕颜色")
        if color.isValid():
            self.set_value(color.name())


class MakePage(QWidget):
    """Collect options on the GUI thread; execute pipeline jobs through JobRunner."""

    prepared = Signal(object)
    rendered = Signal(object)
    settings_changed = Signal(object)
    style_changed = Signal(object)
    material_preview_changed = Signal(object)
    workspace_requested = Signal(str)

    def __init__(
        self, runner: object, parent: QWidget | None = None, *, embedded: bool = False,
        settings_only: bool = False,
    ) -> None:
        super().__init__(parent)
        self.runner = runner
        self.embedded = embedded
        self.settings_only = settings_only
        self.before_prepare: Callable[[], bool] | None = None
        self.controls: dict[str, QWidget] = {}
        self._temporary = tempfile.TemporaryDirectory(prefix="karaoke-forge-desktop-")
        self._workspace: WorkspaceProject | None = None
        self._editor_document: LyricsDocument | None = None
        self._editor_source_settings: dict[str, Any] = {}
        self._render_lyrics_snapshot: Path | None = None
        self._restoring = False
        self._matched_manifest: str | None = None
        self._match_timer = QTimer(self)
        self._match_timer.setSingleShot(True)
        self._match_timer.setInterval(600)
        self._match_timer.timeout.connect(self._match_online_project)
        self._task_buttons: list[QPushButton] = []
        self._build_ui()
        self._restore_values(load_preferences())
        self._initial_settings = self.get_settings()
        self._connect_style_controls()
        self._connect_settings_controls()
        if not self.settings_only:
            for key in ("netease_link", "qqmusic_link", "utaten_link"):
                self.controls[key].textChanged.connect(self._schedule_link_match)
        self._set_sample()
        self._update_style()
        self.runner.busy_changed.connect(self._set_busy)
        self.runner.message.connect(self._append_log)
        self._set_busy(bool(self.runner.is_busy))

    def _scroll_tab(self, title: str) -> QVBoxLayout:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(14)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)
        self.tabs.addTab(scroll, title)
        return layout

    @staticmethod
    def _group(layout: QVBoxLayout, title: str) -> QFormLayout:
        group = QGroupBox(title)
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setSpacing(10)
        layout.addWidget(group)
        return form

    def _add(self, form: QFormLayout, key: str, label: str, widget: QWidget) -> QWidget:
        self.controls[key] = widget
        widget.setObjectName(f"make-{key}")
        widget.setAccessibleName(label)
        form.addRow(label, widget)
        return widget

    def _line(self, form: QFormLayout, key: str, label: str, value: str = "") -> QLineEdit:
        widget = QLineEdit(value)
        self._add(form, key, label, widget)
        return widget

    def _check(self, form: QFormLayout, key: str, label: str, value: bool = True) -> QCheckBox:
        widget = QCheckBox(label)
        widget.setChecked(value)
        self._add(form, key, "", widget)
        widget.setAccessibleName(label)
        return widget

    def _combo(
        self, form: QFormLayout, key: str, label: str, choices: list, default: str
    ) -> QComboBox:
        widget = QComboBox()
        for choice in choices:
            display, value = choice if isinstance(choice, tuple) else (choice, choice)
            widget.addItem(display, value)
        widget.setCurrentIndex(max(0, widget.findData(default)))
        self._add(form, key, label, widget)
        return widget

    def _number(
        self,
        form: QFormLayout,
        key: str,
        label: str,
        low: float,
        high: float,
        value: float,
        *,
        decimals: int = 0,
    ) -> QWidget:
        widget = QDoubleSpinBox() if decimals else QSpinBox()
        widget.setRange(low, high) if decimals else widget.setRange(int(low), int(high))
        if decimals:
            widget.setDecimals(decimals)
            widget.setSingleStep(0.1)
        widget.setValue(value if decimals else int(value))
        self._add(form, key, label, widget)
        return widget

    def _path(self, form: QFormLayout, key: str, label: str, **kwargs: Any) -> PathPicker:
        picker = PathPicker(**kwargs)
        self._add(form, key, label, picker)
        picker.changed.connect(lambda _value: self._materials_changed(key))
        return picker

    def _button(self, label: str, callback: object) -> QPushButton:
        button = QPushButton(label)
        button.clicked.connect(callback)
        self._task_buttons.append(button)
        return button

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        if self.embedded or self.settings_only:
            layout.setContentsMargins(0, 0, 0, 0)
        else:
            heading = QLabel("制作卡拉 OK 视频")
            heading.setObjectName("pageTitle")
            layout.addWidget(heading)
            explanation = QLabel("导入素材 → 生成可编辑工程 → 校准歌词 → 导出成片")
            explanation.setObjectName("page-subtitle")
            layout.addWidget(explanation)
        self.tabs = QTabWidget()
        self.tabs.setMinimumWidth(380)
        self._build_materials()
        self._build_online()
        self._build_style()
        self._build_advanced()
        self._build_results()
        if self.settings_only:
            self._arrange_settings_tabs()

        preview_panel = QWidget(self)
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.addWidget(QLabel("字幕与画面预览"))
        self.preview = LyricPreviewWidget()
        self.preview.setMinimumSize(300, 220)
        self.preview.setMaximumHeight(360)
        preview_layout.addWidget(self.preview)
        self.preview_status = QLabel(
            "这里使用示例歌词检查字幕样式；实际歌曲画面将在编辑器预览。"
            if self.settings_only else "选择素材后点击刷新预览，可查看实际 MV 或唱片背景。"
        )
        self.preview_status.setWordWrap(True)
        preview_layout.addWidget(self.preview_status)
        if not self.settings_only:
            preview_layout.addWidget(self._button("刷新素材预览", self.refresh_preview))
        tip = QLabel("当前预览用于确认布局。逐字时序请在歌词编辑器中结合音频校准。")
        tip.setWordWrap(True)
        preview_layout.addWidget(tip)
        self._build_sample_controls(preview_layout)
        preview_layout.addStretch(1)
        if self.embedded or self.settings_only:
            self.tabs.addTab(preview_panel, "示例预览")
            layout.addWidget(self.tabs, 1)
        else:
            splitter = QSplitter(Qt.Orientation.Horizontal)
            splitter.addWidget(self.tabs)
            splitter.addWidget(preview_panel)
            splitter.setStretchFactor(0, 3)
            splitter.setStretchFactor(1, 2)
            layout.addWidget(splitter, 1)
        self.status = QLabel("准备就绪", self)
        self.status.setWordWrap(True)
        action_panel = QWidget(self)
        actions = QHBoxLayout(action_panel)
        actions.setContentsMargins(0, 0, 0, 0)
        self.prepare_button = self._button("生成工程 · 进入歌词校准", self.prepare_project)
        self.render_button = self._button("渲染视频", self.render_video)
        self.prepare_button.setObjectName("primary-button")
        self.render_button.setObjectName("primary-button")
        actions.addWidget(self.prepare_button, 1)
        actions.addWidget(self.render_button, 1)
        if self.embedded or self.settings_only:
            self.status.hide()
            action_panel.hide()
        else:
            layout.addWidget(self.status)
            layout.addWidget(action_panel)

    def _build_materials(self) -> None:
        layout = self._scroll_tab("素材")
        form = self._group(layout, "本地素材")
        self._path(
            form,
            "audio_file",
            "歌曲音频",
            file_filter="音频 (*.wav *.mp3 *.flac *.m4a *.ogg *.aac);;所有文件 (*)",
        )
        self._path(
            form,
            "video_file",
            "歌曲 MV",
            file_filter="视频 (*.mp4 *.mkv *.mov *.webm *.avi);;所有文件 (*)",
        )
        form.addRow(QLabel("有声 MV 可直接使用内嵌音轨，无需重复上传音频。"))
        self._path(
            form,
            "lyrics_file",
            "歌词 / 字幕",
            file_filter="歌词 (*.txt *.lrc *.elrc *.srt *.vtt *.ass *.json);;所有文件 (*)",
        )
        self._path(
            form,
            "cover_file",
            "专辑封面",
            file_filter="图像 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*)",
        )
        form = self._group(layout, "没有文件？粘贴歌词")
        pasted = QPlainTextEdit()
        pasted.setPlaceholderText("一行一句。没有时间轴时会使用歌曲音频自动对齐。")
        pasted.setMaximumHeight(140)
        self._add(form, "pasted_lyrics", "", pasted)
        pasted.textChanged.connect(lambda: self._materials_changed("pasted_lyrics"))
        form = self._group(layout, "成片")
        self._line(form, "output_name", "成品名称").setPlaceholderText("留空采用歌曲或素材名称")
        self._combo(
            form,
            "language",
            "歌曲语言",
            [
                ("自动识别", "自动识别"),
                ("中文", "zh"),
                ("英语", "en"),
                ("日语", "ja"),
                ("韩语", "ko"),
                ("粤语", "yue"),
            ],
            "自动识别",
        )
        self._combo(form, "quality", "视频质量", ["快速预览", "推荐质量", "高质量"], "推荐质量")
        form = self._group(layout, "无 MV：背景与唱片布局")
        self._combo(
            form,
            "cover_background",
            "背景主题",
            [
                ("专辑流光", "adaptive"),
                ("深空星环", "midnight"),
                ("日落玻璃", "sunset"),
                ("海盐极光", "ocean"),
                ("纸艺花园", "paper"),
            ],
            "adaptive",
        )
        self._combo(
            form,
            "cover_style",
            "唱片布局",
            [
                ("黑胶唱片机", "turntable"),
                ("星环唱片", "aurora"),
                ("偏置黑胶", "vinyl"),
                ("环绕唱片", "halo"),
                ("侧置频谱", "spectrum"),
            ],
            "turntable",
        )
        self._check(form, "cover_waveform", "显示音乐波形 / 频谱")
        layout.addStretch()

    def _arrange_settings_tabs(self) -> None:
        """Reuse every configuration field without duplicating the source wizard."""
        self.tabs.setTabText(0, "画面与视频")
        self.tabs.setTabText(1, "官方注音")
        # Source selection is transactional in ProjectDialog. Its explicit fields
        # replace these hidden originals when the final settings are collected.
        for key in ("audio_file", "video_file", "lyrics_file", "cover_file", "pasted_lyrics"):
            group = self.controls[key].parentWidget()
            if isinstance(group, QGroupBox):
                group.hide()
        form = self.controls["output_name"].parentWidget().layout()
        if isinstance(form, QFormLayout):
            form.setRowVisible(self.controls["output_name"], False)
        # Accounts belong to the running application session, not saved projects.
        for key in ("netease_link", "qqmusic_link", "rights_confirmed"):
            group = self.controls[key].parentWidget()
            if isinstance(group, QGroupBox):
                group.hide()
        self.match_status.hide()
        self.open_match_button.hide()
        utaten_group = self.controls["utaten_link"].parentWidget()
        utaten_group.setTitle("已有歌词：补充 UtaTen 官方注音（可选）")
        utaten_group.layout().setRowVisible(self.controls["use_utaten_lyrics"], False)
        note = QLabel("已有本地 / 粘贴歌词时，可填写 UtaTen 页面并开启仅补充官方注音。")
        note.setWordWrap(True)
        utaten_group.layout().insertRow(0, note)
        self.controls["rights_confirmed"].setText("我确认拥有所导入歌词及注音的使用权")
        utaten_group.layout().addRow(self.controls["rights_confirmed"])
        # Keep the manual subtitle library available without displaying account
        # and primary source forms again in the processing step.
        library = QPushButton("打开 Vmoe 字幕库")
        library.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://karaoke.vmoe.info/")))
        utaten_group.layout().addRow(library)

    def _build_online(self) -> None:
        layout = self._scroll_tab("在线来源")
        note = QLabel(
            "主歌词来源已在第一步选择；这里可配置已有歌词的官方注音补充。"
            if self.settings_only else
            "一次选择一个在线来源；QQ 音乐和 UtaTen 只提供歌词，请同时选择本地音频或有声 MV。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.match_status = QLabel("粘贴单曲链接后，会自动查找本机已保存的同来源工程。")
        self.match_status.setWordWrap(True)
        layout.addWidget(self.match_status)
        self.open_match_button = QPushButton("打开匹配工程")
        self.open_match_button.setEnabled(False)
        self.open_match_button.clicked.connect(self._open_matched_project)
        layout.addWidget(self.open_match_button)
        form = self._group(layout, "网易云音乐")
        self._line(form, "netease_link", "单曲链接")
        self._check(form, "use_netease_lyrics", "没有上传歌词时导入网易云公开歌词")
        self._check(form, "prefer_netease_audio", "使用网易云音频，独立 MV 仅提供画面", False)
        form.setRowVisible(self.controls["prefer_netease_audio"], False)
        login_actions = QHBoxLayout()
        login_actions.addWidget(self._button("连接网易云账号", self.login_netease))
        login_actions.addWidget(self._button("重新登录", lambda: self.login_netease(relogin=True)))
        login_actions.addWidget(self._button("退出账号", self.logout_netease))
        form.addRow(login_actions)
        self.login_status = QLabel("尚未连接；点击连接会打开专用 Edge 官方登录窗口。")
        self.login_status.setWordWrap(True)
        form.addRow(self.login_status)
        self._combo(
            form,
            "cookie_browser",
            "兼容：浏览器登录",
            [
                ("不读取浏览器", ""),
                ("Edge", "edge"),
                ("Chrome", "chrome"),
                ("Firefox", "firefox"),
                ("Brave", "brave"),
            ],
            "",
        )
        self._line(form, "cookie_browser_profile", "浏览器配置（可选）")
        token = self._line(form, "music_u", "手动 MUSIC_U（排障用）")
        token.setEchoMode(QLineEdit.EchoMode.Password)
        form = self._group(layout, "QQ 音乐")
        self._line(form, "qqmusic_link", "单曲链接")
        self._check(form, "use_qqmusic_lyrics", "没有上传歌词时导入公开 LRC 与翻译")
        form = self._group(layout, "UtaTen")
        self._line(form, "utaten_link", "歌词页链接")
        self._check(form, "use_utaten_lyrics", "没有上传歌词时导入公开歌词与假名")
        self._check(
            form, "utaten_pronunciation_only", "仅补充官方注音，保留已有正文与时间轴", False
        )
        form = self._group(layout, "使用确认与其他来源")
        self._check(
            form,
            "rights_confirmed",
            "我确认拥有歌曲及歌词使用权，不绕过地区、验证码或 DRM 限制",
            False,
        )
        vmoe = QPushButton("在浏览器打开 Vmoe 字幕库")
        vmoe.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://karaoke.vmoe.info/")))
        form.addRow(vmoe)
        help_text = QLabel(
            "在官方网站完成搜索和验证，下载 ASS 后从素材页导入。登录凭据仅保存在本次会话内存中。"
        )
        help_text.setWordWrap(True)
        form.addRow(help_text)
        layout.addStretch()

    def _schedule_link_match(self, *_args: object) -> None:
        if self._restoring:
            return
        self._matched_manifest = None
        self.open_match_button.setEnabled(False)
        self.match_status.setText("正在查找同来源的已保存工程…")
        self._match_timer.start()

    def _match_online_project(self) -> None:
        if self.runner.is_busy:
            self._match_timer.start()
            return
        keys = ("netease_link", "qqmusic_link", "utaten_link")
        links = tuple(self.controls[key].text().strip() for key in keys)
        if not any(links):
            self.match_status.setText("粘贴单曲链接后，会自动查找本机已保存的同来源工程。")
            return

        def task(_log):
            manifest = _matching_workspace_manifest(*links)
            return (manifest, load_workspace_project(manifest).name) if manifest else (None, None)

        def matched(result):
            if links != tuple(self.controls[key].text().strip() for key in keys):
                return
            manifest, name = result
            self._matched_manifest = manifest
            self.open_match_button.setEnabled(bool(manifest))
            self.match_status.setText(
                f"已找到同来源工程：{name}。点击下方按钮可继续编辑。"
                if manifest
                else "没有找到同来源的已保存工程，可以继续创建新工程。"
            )

        self.runner.submit("查找链接对应工程", task, matched)

    def _open_matched_project(self) -> None:
        if self._matched_manifest and not self.runner.is_busy:
            self.workspace_requested.emit(self._matched_manifest)

    def _build_sample_controls(self, layout: QVBoxLayout) -> None:
        self._sample_updating = False
        form = self._group(layout, "可编辑预览样例（不改变工程歌词）")
        self.sample_text = QPlainTextEdit()
        self.sample_text.setMaximumHeight(100)
        self.sample_text.setPlaceholderText("每行一句，可粘贴长句检查字号与换行")
        form.addRow("示例歌词", self.sample_text)
        self.sample_translation = QPlainTextEdit()
        self.sample_translation.setMaximumHeight(74)
        self.sample_translation.setPlaceholderText("每行对应一句翻译，可留空")
        form.addRow("示例翻译", self.sample_translation)
        self.sample_row = QSpinBox()
        self.sample_row.setMinimum(1)
        form.addRow("当前句", self.sample_row)
        self.sample_progress = QDoubleSpinBox()
        self.sample_progress.setRange(0, 100)
        self.sample_progress.setSuffix(" %")
        form.addRow("扫色进度", self.sample_progress)
        self.sample_text.textChanged.connect(self._refresh_sample)
        self.sample_translation.textChanged.connect(self._refresh_sample)
        self.sample_row.valueChanged.connect(self._refresh_sample)
        self.sample_progress.valueChanged.connect(self._refresh_sample)

    def _refresh_sample(self, *_args: object) -> None:
        if self._sample_updating:
            return
        lines = [line for line in self.sample_text.toPlainText().splitlines() if line.strip()]
        translations = self.sample_translation.toPlainText().splitlines()
        self._sample_updating = True
        try:
            self.sample_row.setMaximum(max(1, len(lines)))
        finally:
            self._sample_updating = False
        document = LyricsDocument(
            lines=[
                LyricLine(
                    text=line,
                    translation=translations[index] if index < len(translations) else None,
                    start=float(index * 5),
                    end=float(index * 5 + 4),
                    tokens=[KaraokeToken(line, index * 5, index * 5 + 4)],
                )
                for index, line in enumerate(lines)
            ]
        )
        selected = self.sample_row.value() - 1
        self.preview.set_document(document)
        self.preview.set_current_line(selected)
        self.preview.set_position(selected * 5 + self.sample_progress.value() / 100 * 4)

    def _build_style(self) -> None:
        layout = self._scroll_tab("字幕样式")
        form = self._group(layout, "主歌词")
        font = self._combo(
            form,
            "font",
            "字幕字体",
            ["Microsoft YaHei", "Noto Sans CJK SC", "PingFang SC", "Arial"],
            "Microsoft YaHei",
        )
        font.setEditable(True)
        self._path(
            form, "font_files", "导入字体", file_filter="字体 (*.ttf *.otf *.ttc)", multiple=True
        )
        self._number(form, "font_size", "字号", 32, 88, 58)
        self._number(form, "margin_v", "底部距离", 30, 180, 72)
        self._add(form, "text_color", "未唱颜色", ColorButton("#FFFFFF"))
        self._add(form, "highlight_color", "已唱颜色", ColorButton("#FFD54A"))
        form = self._group(layout, "翻译")
        self._check(form, "show_translation", "显示翻译")
        self._number(form, "translation_font_size", "翻译字号", 24, 58, 38)
        self._number(form, "translation_margin_v", "距顶部（1080p 坐标）", 16, 760, 54)
        self._add(form, "translation_color", "翻译颜色", ColorButton("#EAF4FF"))
        form = self._group(layout, "日语 / 英语注音")
        self._check(form, "show_pronunciation", "显示日语假名与英语注音")
        self._check(form, "auto_english_pronunciation", "显示英语片假名（包括旧工程中的英文注音）")
        self._number(form, "pronunciation_font_size", "注音字号", 18, 40, 26)
        self._add(form, "pronunciation_color", "注音颜色", ColorButton("#FFFFFF"))
        form = self._group(layout, "间奏与开唱提示")
        self._check(form, "show_countdown", "长间奏结束前显示三音符提示")
        self._number(form, "countdown_gap_threshold", "长间奏阈值（秒）", 5, 20, 8)
        layout.addStretch()

    def _build_advanced(self) -> None:
        layout = self._scroll_tab("识别与导出")
        form = self._group(layout, "识别与时间轴")
        choices = [
            ("快速 · small", "profile:fast"),
            ("均衡 · large-v3-turbo", "profile:balanced"),
            ("KTV 精准 · large-v3", "profile:precise"),
            "tiny",
            "base",
            "small",
            "medium",
            "large-v3",
            "large-v3-turbo",
        ]
        self._combo(form, "model", "识别档位 / 模型", choices, "profile:balanced")
        self._combo(
            form,
            "device",
            "运行设备",
            [("自动", "auto"), ("CPU", "cpu"), ("NVIDIA CUDA", "cuda")],
            "auto",
        )
        self._check(form, "separate_vocals", "先分离人声（复杂伴奏可尝试）", False)
        self._check(form, "auto_sync", "自动定位 MV 中歌曲开始位置")
        self._combo(
            form,
            "timing_refinement",
            "逐字时间精修",
            [
                ("关闭：保留输入时间", "off"),
                ("自动：精修行级时间", "auto"),
                ("强制检查：仅采纳可靠修正", "force"),
            ],
            "auto",
        )
        self._number(form, "audio_offset", "定位后的手动偏移（秒）", -3600, 3600, 0, decimals=3)
        form = self._group(layout, "文件输出")
        self._check(form, "export_original", "导出原声版")
        self._check(form, "export_instrumental", "导出无人声伴奏版", False)
        output = self._path(form, "output_root", "输出目录", directory=True)
        output.set_value(str(_default_output_root()))
        note = QLabel(
            "原声与伴奏可同时导出。首次使用模型或人声分离时需要下载资源，进度会显示在任务日志中。"
        )
        note.setWordWrap(True)
        form.addRow(note)
        layout.addStretch()

    def _build_results(self) -> None:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("双击文件可使用默认应用打开"))
        self.files = QListWidget()
        self.files.itemDoubleClicked.connect(self._open_file)
        layout.addWidget(self.files, 1)
        layout.addWidget(QLabel("任务日志"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        layout.addWidget(self.log, 2)
        if self.embedded or self.settings_only:
            panel.hide()
        else:
            self.tabs.addTab(panel, "结果与日志")

    @staticmethod
    def _value(widget: QWidget) -> object:
        if isinstance(widget, PathPicker):
            return widget.paths() if widget.multiple else widget.value()
        if isinstance(widget, ColorButton):
            return widget.value()
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QComboBox):
            if widget.isEditable() and widget.currentText() != widget.itemText(
                widget.currentIndex()
            ):
                return widget.currentText()
            return (
                widget.currentData() if widget.currentData() is not None else widget.currentText()
            )
        if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            return widget.value()
        if isinstance(widget, QPlainTextEdit):
            return widget.toPlainText()
        if isinstance(widget, QLineEdit):
            return widget.text().strip()
        raise TypeError(f"Unsupported make control: {type(widget).__name__}")

    def get_settings(self) -> dict[str, Any]:
        """Return persistable settings; authentication credentials are never included."""
        result = {
            key: self._value(widget)
            for key, widget in self.controls.items()
            if key not in _PRIVATE_FIELDS
        }
        result["font_files"] = self.controls["font_files"].paths()
        return result

    def _job_values(self) -> dict[str, Any]:
        values = self.get_settings()
        values.update({key: self._value(self.controls[key]) for key in _PRIVATE_FIELDS})
        if values["music_u"]:
            values["cookie_browser"] = ""
            values["cookie_browser_profile"] = ""
        for key in ("audio_file", "video_file", "lyrics_file", "cover_file"):
            values[key] = values[key] or None
        return values

    def _validated_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Validate conversions before changing any of the current project's controls."""
        normalized = dict(settings)
        for old_key, value in settings.items():
            widget = self.controls.get(_ALIASES.get(old_key, old_key))
            if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                try:
                    number = float(value)
                    if not math.isfinite(number):
                        raise ValueError("not finite")
                    number = max(widget.minimum(), min(widget.maximum(), number))
                    normalized[old_key] = number if isinstance(widget, QDoubleSpinBox) else int(number)
                except (ValueError, TypeError, OverflowError) as exc:
                    raise ValueError(f"工程设置 {old_key} 需要有效数字，当前工程未更改。") from exc
            elif isinstance(widget, QCheckBox) and isinstance(value, str):
                normalized[old_key] = value.strip().lower() not in {"", "false", "0", "no", "off"}
        return normalized

    def _restore_values(self, settings: dict[str, Any]) -> None:
        settings = self._validated_settings(settings)
        for old_key, value in settings.items():
            key = _ALIASES.get(old_key, old_key)
            if key in _PRIVATE_FIELDS or key not in self.controls:
                continue
            widget = self.controls[key]
            if isinstance(widget, PathPicker) and widget.multiple:
                widget.set_paths(list(map(str, value)) if isinstance(value, (list, tuple)) else [])
            elif isinstance(widget, (PathPicker, ColorButton)):
                widget.set_value(
                    ";".join(map(str, value))
                    if isinstance(value, (list, tuple))
                    else str(value or "")
                )
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QComboBox):
                index = widget.findData(value)
                if index >= 0:
                    widget.setCurrentIndex(index)
                elif widget.isEditable():
                    widget.setCurrentText(str(value))
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                widget.setValue(float(value) if isinstance(widget, QDoubleSpinBox) else int(value))
            elif isinstance(widget, QPlainTextEdit):
                widget.setPlainText(str(value or ""))
            elif isinstance(widget, QLineEdit):
                widget.setText(str(value or ""))

    def restore_workspace(self, workspace: WorkspaceProject, *, configured: bool = False) -> None:
        settings = self._validated_settings(workspace.settings)
        preserve_source = configured or bool(
            settings.get("preserve_lyrics_source") or settings.get("pending_lyrics_source")
        )
        source_lyrics = settings.get("lyrics_file", "")
        if preserve_source and settings.get("lyrics_source_asset"):
            root = workspace.manifest.parent.resolve()
            source_lyrics = (root / settings["lyrics_source_asset"]).resolve()
            try:
                source_lyrics.relative_to(root)
            except ValueError as exc:
                raise ValueError("工程歌词来源路径超出项目文件夹，当前工程未更改。") from exc
        self._restoring = True
        try:
            self._workspace = workspace
            self._editor_document = None
            self._editor_source_settings = {}
            self._render_lyrics_snapshot = None
            self._restore_values(self._initial_settings)
            self._restore_values(settings)
            self._restore_values(
                {
                    "audio_file": workspace.audio,
                    "video_file": workspace.video,
                    "lyrics_file": source_lyrics if preserve_source else workspace.lyrics_project,
                    "cover_file": workspace.cover,
                    "font_files": workspace.font_files,
                    "output_name": workspace.name,
                    "pasted_lyrics": settings.get("pasted_lyrics", "") if preserve_source else "",
                }
            )
        finally:
            self._restoring = False
        self._update_style()
        self.status.setText(f"已恢复工程：{workspace.name}")

    def apply_material_settings(self, settings: dict[str, Any]) -> None:
        """Apply a source choice together, retaining the lyrics being edited."""
        if self.runner.is_busy:
            raise ValueError("请等待当前任务完成后再更改工程来源。")
        normalized = self._validated_settings(settings)
        self._match_timer.stop()
        previous = self._restoring
        blocked = self.blockSignals(True)
        self._restoring = True
        try:
            self._restore_values(normalized)
            self._matched_manifest = None
            self.open_match_button.setEnabled(False)
            self.match_status.setText("来源已设置；载入歌词或生成时间轴后开始校准。")
        finally:
            self._restoring = previous
            self.blockSignals(blocked)
        self._update_style()

    def new_project_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Validate a new project's complete configuration before it is saved."""
        if self.runner.is_busy:
            raise ValueError("请等待当前任务完成后再新建工程。")
        normalized = self._validated_settings(settings)
        current = self.get_settings()
        preferred = {
            key: current[key]
            for key in _STYLE_FIELDS | {"model", "device", "language", "output_root"}
            if key in current
        }
        empty = {
            key: "" for key in (
                "audio_file", "video_file", "lyrics_file", "cover_file", "pasted_lyrics",
                "output_name", "netease_link", "qqmusic_link", "utaten_link",
            )
        }
        empty.update({"font_files": [], "utaten_pronunciation_only": False})
        return {
            key: value for key, value in {**self._initial_settings, **preferred, **empty, **normalized}.items()
            if key not in _PRIVATE_FIELDS
        }

    def reset_project(self, settings: dict[str, Any]) -> None:
        """Start with new materials and discard all previous lyric/render snapshots."""
        values = self.new_project_settings(settings)
        self._workspace = None
        self._editor_document = None
        self._editor_source_settings = {}
        self._render_lyrics_snapshot = None
        self.apply_material_settings(values)
        self.preview.set_background("")
        self._set_sample()
        self.status.setText("新工程已就绪，载入歌词或生成时间轴后开始校准。")

    def stage_editor_document(self, document: LyricsDocument) -> None:
        """Freeze current lyrics for rendering without reloading project settings.

        The controller calls this with the editor's validated document immediately
        before rendering. Each snapshot has its own file so a later edit cannot
        change a queued job. Media, output name and style remain the user's current
        choices; the lyrics are never fetched or refined again during rendering.
        """
        from ..editor import document_from_payload

        edited = document_from_payload(document.to_dict())
        self._editor_source_settings = {}
        if manifest := edited.metadata.get("workspace_manifest"):
            try:
                original = load_workspace_project(manifest)
                refs = (original.settings or {}).get("source_refs")
                if isinstance(refs, dict):
                    self._editor_source_settings = {"source_refs": copy.deepcopy(refs)}
            except (OSError, TypeError, ValueError):
                pass
        # The web pipeline otherwise fills cleared media fields from this older
        # manifest. The controller already owns the current explicit selections.
        edited.metadata.pop("workspace_manifest", None)
        snapshot = Path(self._temporary.name) / f"edited-{uuid4().hex}.json"
        snapshot.write_text(write_json(edited), encoding="utf-8")
        self._editor_document = edited
        self._render_lyrics_snapshot = snapshot

    def set_editor_document(self, document: LyricsDocument) -> None:
        """Render the exact edited lyrics while retaining their original media assets."""
        from ..editor import document_from_payload

        manifest = document.metadata.get("workspace_manifest")
        workspace = None
        if manifest:
            try:
                workspace = load_workspace_project(manifest)
            except (OSError, TypeError, ValueError) as exc:
                self._append_log(f"歌词关联工程不可用，已清除旧工程素材：{exc}")
        workspace_settings = self._validated_settings(workspace.settings) if workspace else {}
        edited = document_from_payload(document.to_dict())
        snapshot = Path(self._temporary.name) / f"edited-{uuid4().hex}.json"
        snapshot.write_text(write_json(edited), encoding="utf-8")
        self._restoring = True
        try:
            self._workspace = workspace
            refs = (workspace.settings or {}).get("source_refs") if workspace else None
            self._editor_source_settings = (
                {"source_refs": copy.deepcopy(refs)} if isinstance(refs, dict) else {}
            )
            # An editor can load another song independently of this page. Drop
            # previous sources first, then restore only the new song's assets
            # and persisted settings. The caller may supply its chosen audio.
            self._restore_values(self._initial_settings)
            self._restore_values(
                {
                    "audio_file": "",
                    "video_file": "",
                    "cover_file": "",
                    "font_files": [],
                    "output_name": "",
                    "netease_link": "",
                    "qqmusic_link": "",
                    "utaten_link": "",
                    "utaten_pronunciation_only": False,
                }
            )
            if workspace is not None:
                self._restore_values(workspace_settings)
                self._restore_values(
                    {
                        "audio_file": workspace.audio,
                        "video_file": workspace.video,
                        "cover_file": workspace.cover,
                        "font_files": workspace.font_files,
                        "output_name": workspace.name,
                    }
                )
            self._editor_document = edited
            self._render_lyrics_snapshot = None
            self.controls["lyrics_file"].set_value(str(snapshot))
            self.controls["pasted_lyrics"].setPlainText("")
            self._restore_values({"timing_refinement": "off"})
        finally:
            self._restoring = False
        self._update_style()
        self.preview.set_background("")
        self.preview.set_document(self._editor_document)
        self.preview.set_current_line(0)
        self.preview_status.setText("已切换到编辑后的歌词，点击“刷新素材预览”查看当前画面。")
        self.status.setText(
            "已载入编辑后的歌词；渲染将保留手动时间轴及原始素材。"
            if workspace is not None
            else "已载入独立歌词；请确认试听音频后渲染，手动时间轴会保留。"
        )

    def has_materials(self) -> bool:
        values = self.get_settings()
        return any(
            values[key]
            for key in (
                "audio_file",
                "video_file",
                "lyrics_file",
                "cover_file",
                "pasted_lyrics",
                "netease_link",
                "qqmusic_link",
                "utaten_link",
            )
        )

    @staticmethod
    def _save_job_settings(result: object, settings: dict[str, Any], log: object) -> None:
        output = getattr(result, "output_dir", None)
        manifest = Path(output) / PROJECT_FILENAME if output else None
        if manifest is None or not manifest.is_file():
            return
        workspace = load_workspace_project(manifest)
        safe = {
            key: value
            for key, value in settings.items()
            if key not in _PRIVATE_FIELDS | _ASSET_FIELDS
        }
        save_workspace_project(
            manifest.parent,
            name=workspace.name,
            lyrics_project=workspace.lyrics_project,
            audio=workspace.audio,
            video=workspace.video,
            cover=workspace.cover,
            font_files=workspace.font_files,
            settings={**workspace.settings, **safe},
            recent_root=_default_output_root(),
        )
        log("已保存完整字幕、识别和导出设置。")

    def prepare_project(self) -> None:
        if self.settings_only:
            return
        if self.before_prepare is not None and not self.before_prepare():
            return
        values = self._job_values()
        settings = self.get_settings()
        arguments = {key: value for key, value in values.items() if key in _PREPARE_FIELDS}
        directory = self._temporary.name

        def task(log):
            result = prepare_make_editor_job(
                **_native_job_arguments(arguments, directory), progress_callback=log
            )
            try:
                self._save_job_settings(result, settings, log)
            except (OSError, TypeError, ValueError) as exc:
                log(f"工程已处理，但完整界面设置保存失败：{exc}")
            return result

        self.runner.submit("生成可编辑歌词工程", task, self._prepared_result)

    def render_video(self) -> None:
        if self.settings_only:
            return
        values = self._job_values()
        if self._editor_document is not None:
            if self._render_lyrics_snapshot is not None:
                values["lyrics_file"] = str(self._render_lyrics_snapshot)
            values.update(
                {
                    "timing_refinement": "off",
                    "pasted_lyrics": "",
                    "netease_link": "",
                    "qqmusic_link": "",
                    "utaten_link": "",
                    "utaten_pronunciation_only": False,
                }
            )
        settings = {**self._editor_source_settings, **self.get_settings()}
        settings["timing_refinement"] = values["timing_refinement"]
        arguments = {key: value for key, value in values.items() if key in _RENDER_FIELDS}
        directory = self._temporary.name

        def task(log):
            result = run_make_job(
                **_native_job_arguments(arguments, directory), progress_callback=log
            )
            try:
                self._save_job_settings(result, settings, log)
            except (OSError, TypeError, ValueError) as exc:
                log(f"成片已处理，但完整界面设置保存失败：{exc}")
            return result

        self.runner.submit("渲染卡拉 OK 视频", task, self._rendered_result)

    def _show_result(self, result: object) -> None:
        self.status.setText(_plain(getattr(result, "status", "任务完成")))
        self.files.clear()
        for filename in getattr(result, "files", []):
            item = QListWidgetItem(Path(filename).name)
            item.setToolTip(filename)
            item.setData(Qt.ItemDataRole.UserRole, filename)
            self.files.addItem(item)
        log = getattr(result, "log", "")
        if log:
            self._append_log(log)
        self.runner.message.emit(_plain(getattr(result, "status", "任务完成")))
        for filename in getattr(result, "files", []):
            self.runner.message.emit(f"输出文件：{filename}")

    def _prepared_result(self, result: UiEditorPreparationResult) -> None:
        self._show_result(result)
        if result.project and result.payload:
            if not self.embedded:
                from ..editor import document_from_payload

                document = document_from_payload(result.payload)
                self.set_editor_document(document)
            self.status.setText(_plain(result.status))
            self.prepared.emit(result)
        elif not self.embedded:
            self.tabs.setCurrentIndex(self.tabs.count() - 1)

    def _rendered_result(self, result: UiJobResult) -> None:
        self._show_result(result)
        if not self.embedded:
            self.tabs.setCurrentIndex(self.tabs.count() - 1)
        self.rendered.emit(result)

    def refresh_preview(self) -> None:
        if self.settings_only:
            return
        values = self._job_values()
        arguments = {
            key: values[key]
            for key in (
                "audio_file",
                "video_file",
                "cover_file",
                "lyrics_file",
                "pasted_lyrics",
                "auto_sync",
                "netease_link",
                "qqmusic_link",
                "utaten_link",
            )
        }
        arguments.update(
            offset=values["audio_offset"],
            background_theme=values["cover_background"],
            cover_style=values["cover_style"],
            show_waveform=values["cover_waveform"],
        )
        directory = self._temporary.name

        def task(log):
            log("正在读取素材画面与歌词预览…")
            result = prepare_subtitle_material_preview(**_native_job_arguments(arguments, directory))
            background = None
            if result[2].startswith("data:image/") and "," in result[2]:
                background = Path(directory) / f"preview-{uuid4().hex}.png"
                background.write_bytes(base64.b64decode(result[2].split(",", 1)[1]))
            return result, str(background) if background else None

        self.runner.submit("刷新素材预览", task, self._preview_result)

    def _preview_result(self, value: tuple) -> None:
        result, background = value
        text, translation, _data, badge, _material, progress, active_row, status = result
        self._sample_updating = True
        try:
            self.sample_text.setPlainText(text)
            self.sample_translation.setPlainText(translation)
            self.sample_row.setMaximum(max(1, len(text.splitlines())))
            self.sample_row.setValue(int(active_row))
            self.sample_progress.setValue(float(progress) * 100)
        finally:
            self._sample_updating = False
        self._refresh_sample()
        self.preview.set_background(background or "")
        self.material_preview_changed.emit(background)
        self.preview_status.setText(f"{_plain(badge)}\n{_plain(status)}")
        self._update_style()

    def _set_sample(self) -> None:
        self.sample_text.setPlainText("夜空に響くメロディー\nI hear the flowers whisper")
        self.sample_translation.setPlainText("旋律回荡在夜空")
        self.sample_progress.setValue(40)

    def _connect_style_controls(self) -> None:
        for key in _STYLE_FIELDS:
            widget = self.controls[key]
            signal = (
                widget.changed
                if isinstance(widget, ColorButton)
                else widget.toggled
                if isinstance(widget, QCheckBox)
                else widget.currentTextChanged
                if isinstance(widget, QComboBox)
                else widget.valueChanged
            )
            signal.connect(self._update_style)

    def _connect_settings_controls(self) -> None:
        for key, widget in self.controls.items():
            if key in _STYLE_FIELDS | _PRIVATE_FIELDS:
                continue
            if isinstance(widget, PathPicker):
                signal = widget.changed
            elif isinstance(widget, QCheckBox):
                signal = widget.toggled
            elif isinstance(widget, QComboBox):
                signal = widget.currentTextChanged
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                signal = widget.valueChanged
            else:
                signal = widget.textChanged
            signal.connect(self._emit_settings_changed)

    def _emit_settings_changed(self, *_args: object) -> None:
        if not self._restoring:
            self.settings_changed.emit(self.get_settings())

    def _update_style(self, *_args: object) -> None:
        settings = self.get_settings()
        style = {key: settings[key] for key in _STYLE_FIELDS}
        style.update(primary_color=style["highlight_color"], secondary_color=style["text_color"])
        self.preview.set_style(style)
        if not self._restoring:
            self.style_changed.emit(style)
            self.settings_changed.emit(settings)
            if not self.settings_only:
                try:
                    save_preferences({key: settings[key] for key in _STYLE_FIELDS})
                except OSError as exc:
                    self._append_log(f"偏好设置未保存：{exc}")

    def _materials_changed(self, key: str) -> None:
        if self._restoring or not hasattr(self, "preview_status"):
            return
        if key in {"lyrics_file", "pasted_lyrics"}:
            self._editor_document = None
            self._editor_source_settings = {}
            self._render_lyrics_snapshot = None
        if key == "font_files" and not self.settings_only:
            families = []
            for path in self.controls[key].paths():
                font_id = QFontDatabase.addApplicationFont(path)
                if font_id >= 0:
                    families.extend(QFontDatabase.applicationFontFamilies(font_id))
            if families:
                self.controls["font"].setCurrentText(families[0])
        self.preview_status.setText(
            "字幕配置已更新；实际歌曲画面将在编辑器预览。"
            if self.settings_only else "素材已更新，点击“刷新素材预览”查看实际画面。"
        )

    def show_account_settings(self, parent: QWidget | None = None) -> int:
        """Edit account options in this session without saving them in a project."""
        if self.settings_only:
            return QDialog.DialogCode.Rejected
        dialog = QDialog(parent or self)
        dialog.setWindowTitle("网易云账号 · 本次会话")
        dialog.resize(540, 360)
        layout = QVBoxLayout(dialog)
        note = QLabel("账号与登录凭据仅用于本次应用会话，不写入工程或工程设置。")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        browser = QComboBox()
        actual_browser = self.controls["cookie_browser"]
        for index in range(actual_browser.count()):
            browser.addItem(actual_browser.itemText(index), actual_browser.itemData(index))
        browser.setCurrentIndex(actual_browser.currentIndex())
        profile = QLineEdit(self.controls["cookie_browser_profile"].text())
        token = QLineEdit(self.controls["music_u"].text())
        token.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("兼容：浏览器登录", browser)
        form.addRow("浏览器配置（可选）", profile)
        form.addRow("手动 MUSIC_U（排障用）", token)
        layout.addLayout(form)
        account_status = QLabel(self.login_status.text())
        account_status.setWordWrap(True)
        layout.addWidget(account_status)
        actions = QHBoxLayout()
        for label, callback in (
            ("连接网易云账号", self.login_netease),
            ("重新登录", lambda: self.login_netease(relogin=True)),
            ("退出账号", self.logout_netease),
        ):
            button = QPushButton(label)
            button.setEnabled(not self.runner.is_busy)
            button.clicked.connect(callback)
            self.runner.busy_changed.connect(button.setDisabled)
            actions.addWidget(button)
        layout.addLayout(actions)
        # Qt signals are automatically disconnected when this dialog is destroyed.
        self.controls["music_u"].textChanged.connect(token.setText)
        self.runner.message.connect(account_status.setText)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存本次会话设置")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("关闭")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        result = dialog.exec()
        if result == QDialog.DialogCode.Accepted:
            self.controls["cookie_browser"].setCurrentIndex(browser.currentIndex())
            self.controls["cookie_browser_profile"].setText(profile.text())
            self.controls["music_u"].setText(token.text())
        dialog.deleteLater()
        return result

    def login_netease(self, _checked: bool = False, *, relogin: bool = False) -> None:
        if self.settings_only:
            return
        def task(log):
            log("请在专用 Edge 窗口中完成网易云官方登录。")
            if relogin:
                clear_netease_login_profile()
                return capture_netease_music_u()
            return acquire_netease_music_u()

        if self.runner.submit("连接网易云账号", task, self._logged_in) and relogin:
            self.controls["music_u"].clear()
            self.login_status.setText("正在重新登录，请在专用 Edge 窗口中选择账号。")

    def _logged_in(self, token: str) -> None:
        self.controls["music_u"].setText(token)
        self.login_status.setText("账号已连接，登录凭据仅用于本次会话。")
        self.runner.message.emit("网易云账号已连接。")

    def logout_netease(self) -> None:
        if self.settings_only:
            return
        def task(log):
            log("正在清除 Karaoke Forge 专用登录数据…")
            return clear_netease_login_profile()

        def finished(message):
            self.controls["music_u"].clear()
            self.login_status.setText(_plain(message) or "账号已退出。")
            self.runner.message.emit(_plain(message) or "网易云账号已退出。")

        if self.runner.submit("退出网易云账号", task, finished):
            self.controls["music_u"].clear()
            self.login_status.setText("已断开本次会话，正在清除专用登录数据。")

    def _set_busy(self, busy: bool) -> None:
        for button in self._task_buttons:
            button.setEnabled(not busy)
        if busy:
            self.status.setText("任务进行中，可在“结果与日志”查看详细进度。")

    def _append_log(self, message: str) -> None:
        self.log.appendPlainText(_plain(message))

    @staticmethod
    def _open_file(item: QListWidgetItem) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(item.data(Qt.ItemDataRole.UserRole)))
