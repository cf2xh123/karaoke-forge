"""Transactional project setup: materials, processing/style, then save."""

from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .common import PathPicker
from .make_page import MakePage

SOURCE_CHOICES = (
    ("本地歌词文件", "local"),
    ("粘贴歌词", "paste"),
    ("网易云音乐", "netease"),
    ("QQ 音乐", "qqmusic"),
    ("UtaTen 日语歌词", "utaten"),
    ("自动识别音频（无歌词）", "recognize"),
)
_LINKS = {"netease": "netease_link", "qqmusic": "qqmusic_link", "utaten": "utaten_link"}
_SUPPLEMENT_SOURCES = {"local", "paste"}
_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
_PRIVATE_FIELDS = {"music_u", "cookie_browser_profile", "cookie_browser"}
_MATERIAL_FIELDS = {
    "audio_file", "video_file", "lyrics_file", "cover_file", "pasted_lyrics", "font_files",
    "output_name", "netease_link", "qqmusic_link", "utaten_link", "rights_confirmed",
    "source_refs", "utaten_pronunciation_only", "lyrics_source_asset", "pending_lyrics_source",
    "preserve_lyrics_source", "lyrics_timebase",
    "prefer_netease_audio",
}


class _SettingsRunner(QObject):
    """A project draft cannot enqueue production or account tasks."""

    busy_changed = Signal(bool)
    message = Signal(str)
    is_busy = False

    def submit(self, *_args, **_kwargs) -> bool:
        return False


def lyrics_source_summary(settings: dict) -> str:
    """Describe the source that the existing production pipeline would use."""
    if settings.get("lyrics_file"):
        return "本地歌词文件"
    if str(settings.get("pasted_lyrics") or "").strip():
        return "粘贴歌词"
    names = [label for label, source in SOURCE_CHOICES if settings.get(_LINKS.get(source, ""))]
    return " / ".join(names) if names else "自动识别音频"


class ProjectDialog(QDialog):
    """Collect a complete project draft without changing the running workspace."""

    def __init__(
        self, parent=None, *, settings: dict | None = None,
        default_settings: dict | None = None, directory: str | Path | None = None,
    ) -> None:
        super().__init__(parent)
        editing = settings is not None
        self.editing = editing
        inherited = settings if editing else default_settings or {}
        self._settings = copy.deepcopy({
            key: value for key, value in (inherited or {}).items()
            if key not in _PRIVATE_FIELDS and (editing or key not in _MATERIAL_FIELDS)
        })
        if not editing:
            self._settings["audio_offset"] = 0.0
        settings = self._settings
        self._video_was_audio = bool(
            settings.get("video_file") and not settings.get("audio_file")
            and not settings.get("prefer_netease_audio")
        )
        self.setWindowTitle("项目设置" if editing else "新建项目")
        self.resize(760, 720)
        self.setMinimumSize(420, 480)
        root = QVBoxLayout(self)
        self.step_label = QLabel()
        self.step_label.setWordWrap(True)
        self.step_label.setObjectName("pageTitle")
        root.addWidget(self.step_label)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 0)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.name_edit = QLineEdit(str(settings.get("output_name") or ""))
        self.name_edit.setPlaceholderText("给项目起一个名字")
        form.addRow("项目名称", self.name_edit)
        self.directory_picker = PathPicker(directory=True)
        self.directory_picker.set_value(str(directory or ""))
        self.directory_picker.edit.setReadOnly(editing)
        if editing and not directory:
            self.directory_picker.edit.setPlaceholderText("保存时选择工程文件夹")
        self.directory_picker.button.setVisible(not editing)
        self.directory_picker.setAcceptDrops(not editing)
        form.addRow("项目文件夹", self.directory_picker)
        self.source = QComboBox()
        for label, value in SOURCE_CHOICES:
            self.source.addItem(label, value)
        form.addRow("歌词来源", self.source)
        layout.addLayout(form)

        self.file_label = QLabel("歌词文件")
        self.lyrics_picker = PathPicker(
            filter="歌词与字幕 (*.txt *.lrc *.elrc *.yrc *.srt *.vtt *.ass *.json);;所有文件 (*)"
        )
        self.lyrics_picker.set_value(str(settings.get("lyrics_file") or ""))
        self.paste_label = QLabel("歌词正文 · 支持纯文本与 LRC")
        self.pasted_lyrics = QPlainTextEdit(str(settings.get("pasted_lyrics") or ""))
        self.pasted_lyrics.setPlaceholderText("在这里粘贴整首歌词…")
        self.pasted_lyrics.setMinimumHeight(120)
        self.pasted_lyrics.setMaximumHeight(180)
        self.link_label = QLabel("歌曲链接")
        self.link_edit = QLineEdit()
        self.source_note = QLabel()
        self.source_note.setWordWrap(True)
        for widget in (
            self.file_label, self.lyrics_picker, self.paste_label, self.pasted_lyrics,
            self.link_label, self.link_edit, self.source_note,
        ):
            layout.addWidget(widget)

        self.audio_mode_label = QLabel("歌曲音频")
        self.audio_mode = QComboBox()
        self.audio_mode.addItem("使用本地音频 / 有声 MV", "local")
        self.audio_mode.addItem("从网易云获取在线音频", "online")
        layout.addWidget(self.audio_mode_label)
        layout.addWidget(self.audio_mode)
        self.audio_label = QLabel("本地音频 / 有声 MV（可稍后补充）")
        self.audio_picker = PathPicker(
            filter="歌曲与视频 (*.wav *.mp3 *.flac *.m4a *.ogg *.aac *.mp4 *.mkv *.mov *.webm *.avi);;所有文件 (*)"
        )
        self.audio_picker.set_value(str(
            settings.get("audio_file") or (settings.get("video_file") if self._video_was_audio else "") or ""
        ))
        layout.addWidget(self.audio_label)
        layout.addWidget(self.audio_picker)
        self.audio_note = QLabel()
        self.audio_note.setWordWrap(True)
        layout.addWidget(self.audio_note)
        visual_form = QFormLayout()
        visual_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.video_picker = PathPicker(filter="视频 (*.mp4 *.mkv *.mov *.webm *.avi);;所有文件 (*)")
        self.video_picker.set_value(
            str(settings.get("video_file") or "") if not self._video_was_audio else ""
        )
        self.cover_picker = PathPicker(filter="图像 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*)")
        self.cover_picker.set_value(str(settings.get("cover_file") or ""))
        visual_form.addRow("独立画面 MV（可选）", self.video_picker)
        visual_form.addRow("专辑封面（可选）", self.cover_picker)
        layout.addLayout(visual_form)
        self.rights = QCheckBox("我确认拥有歌曲及歌词使用权")
        self.rights.setChecked(bool(settings.get("rights_confirmed")))
        layout.addWidget(self.rights)
        layout.addStretch()
        scroll.setWidget(body)
        self.stack.addWidget(scroll)
        self.settings_page = MakePage(_SettingsRunner(self), embedded=True, settings_only=True)
        self.settings_page.apply_material_settings(settings)
        self.stack.addWidget(self.settings_page)
        confirmation = QWidget()
        summary_layout = QVBoxLayout(confirmation)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        summary_layout.addWidget(self.summary)
        note = QLabel(
            "保存后进入歌词编辑。载入歌词 / AI 生成时间轴由你在编辑器中启动；"
            "此处不会下载歌曲、连接账号或运行识别。"
        )
        note.setWordWrap(True)
        summary_layout.addWidget(note)
        summary_layout.addStretch()
        self.stack.addWidget(confirmation)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #a33320;")
        root.addWidget(self.error)
        footer = QHBoxLayout()
        self.back_button = QPushButton("上一步")
        self.next_button = QPushButton("下一步")
        self.back_button.clicked.connect(lambda: self.set_step(self.stack.currentIndex() - 1))
        self.next_button.clicked.connect(self.next_step)
        footer.addWidget(self.back_button)
        footer.addStretch()
        footer.addWidget(self.next_button)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "保存项目设置" if editing else "保存并进入编辑"
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        footer.addWidget(self.buttons)
        root.addLayout(footer)
        self._links = {source: str(settings.get(key) or "") for source, key in _LINKS.items()}
        selected = "local"
        if not settings.get("lyrics_file"):
            if settings.get("pasted_lyrics"):
                selected = "paste"
            else:
                selected = next(
                    (source for source, link in self._links.items() if link),
                    "recognize" if editing else "local",
                )
        self.source.setCurrentIndex(self.source.findData(selected))
        self._previous_source = selected
        self.link_edit.setText(self._links.get(selected, ""))
        if selected == "netease" and (settings.get("prefer_netease_audio") or not self.audio_picker.value()):
            self.audio_mode.setCurrentIndex(1)
        self.source.currentIndexChanged.connect(self._source_changed)
        self.audio_mode.currentIndexChanged.connect(self._refresh)
        self._refresh()
        self.set_step(0)

    def set_step(self, index: int) -> None:
        index = max(0, min(2, index))
        self.stack.setCurrentIndex(index)
        self.step_label.setText((
            "1 / 3 · 选择素材来源", "2 / 3 · 处理、外观与输出", "3 / 3 · 确认并保存",
        )[index])
        self.back_button.setEnabled(index > 0)
        self.next_button.setVisible(index < 2)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setVisible(index == 2)
        self.error.clear()
        if index == 2:
            values = self.project_settings()
            model = self.settings_page.controls["model"].currentText()
            language = self.settings_page.controls["language"].currentText()
            audio = values['audio_file'] or values.get('video_file') or '稍后补充'
            if self.source.currentData() == "netease" and self.audio_mode.currentData() == "online":
                audio = "网易云在线音频（载入时获取）"
            self.summary.setText(
                f"项目：{values['output_name']}\n\n文件夹：{self.project_directory() or '保存时选择工程文件夹'}\n\n"
                f"歌词：{lyrics_source_summary(values)}\n"
                f"音频：{audio}\n\n"
                f"对齐：{language} · {model}\n字幕：{values['font']} · {values['font_size']}\n"
                f"输出：{values['quality']} · {'原声' if values['export_original'] else ''}"
                f"{' / 伴奏' if values['export_instrumental'] else ''}\n"
                f"视频输出目录：{values['output_root']}"
            )

    def next_step(self) -> None:
        if message := self._validation_error(check_processing=self.stack.currentIndex() > 0):
            self.error.setText(message)
            return
        self.set_step(self.stack.currentIndex() + 1)

    def _source_changed(self) -> None:
        if self._previous_source in _LINKS:
            self._links[self._previous_source] = self.link_edit.text()
        self._previous_source = self.source.currentData()
        self.link_edit.setText(self._links.get(self._previous_source, ""))
        self.error.clear()
        self._refresh()

    def _refresh(self) -> None:
        source = self.source.currentData()
        online = source in _LINKS
        for widget in (self.file_label, self.lyrics_picker):
            widget.setVisible(source == "local")
        for widget in (self.paste_label, self.pasted_lyrics):
            widget.setVisible(source == "paste")
        for widget in (self.link_label, self.link_edit, self.rights):
            widget.setVisible(online)
        for widget in (self.audio_mode_label, self.audio_mode):
            widget.setVisible(source == "netease")
        local_audio = source != "netease" or self.audio_mode.currentData() == "local"
        self.audio_label.setVisible(local_audio)
        self.audio_label.setText(
            "本地音频 / 有声 MV" if source == "netease"
            else "本地音频 / 有声 MV（可稍后补充）"
        )
        self.audio_picker.setVisible(local_audio)
        self.link_label.setText("歌词页链接" if source == "utaten" else "歌曲链接")
        self.link_edit.setPlaceholderText({
            "netease": "https://music.163.com/song?id=… 或分享链接",
            "qqmusic": "QQ 音乐单曲链接",
            "utaten": "https://utaten.com/lyric/…",
        }.get(source, ""))
        self.source_note.setText({
            "local": "使用文件中的歌词与时间轴；没有逐词时间时可在载入时进行 AI 对齐。",
            "paste": "使用粘贴正文；没有时间戳时，载入后会按所选音频进行 AI 对齐。",
            "netease": "导入该歌曲的公开歌词、翻译及可用的逐字时间轴。",
            "qqmusic": "导入公开歌词与翻译；QQ 音乐音频请使用本地文件。",
            "utaten": "导入日语歌词和官方假名；歌曲音频请使用本地文件。",
            "recognize": "按本地音频自动识别歌词。已有准确歌词时，选择文件或粘贴通常更可靠。",
        }[source])
        self.audio_note.setText(
            "在线音频受歌曲权限限制。需要账号时，保存项目后可在“网易云账号”中主动连接。"
            if not local_audio else "有声 MV 可直接提供音轨；独立画面与封面可在下方设置。"
        )
        if hasattr(self, "settings_page"):
            self.settings_page.tabs.setTabVisible(1, source in _SUPPLEMENT_SOURCES)

    def project_directory(self) -> str:
        """Keep the project folder independent of the production output directory."""
        return self.directory_picker.value()

    def project_settings(self) -> dict:
        """Return the entire draft, excluding credentials held by the real session."""
        processing = self.settings_page.get_settings()
        materials = self.material_settings()
        result = {**self._settings, **processing, **materials, "cover_file": self.cover_picker.value()}
        if self.video_picker.value() and materials.get("video_file"):
            # A voiced MV can supply the song while another video supplies only
            # the picture. Keep the selected song when replacing its visuals.
            result["audio_file"] = materials["video_file"]
        if self.video_picker.value() or not materials.get("video_file"):
            result["video_file"] = self.video_picker.value()
        if self.source.currentData() in _SUPPLEMENT_SOURCES and processing["utaten_pronunciation_only"]:
            result.update(
                utaten_link=processing["utaten_link"], use_utaten_lyrics=False,
                utaten_pronunciation_only=True, rights_confirmed=processing["rights_confirmed"],
            )
        return {key: copy.deepcopy(value) for key, value in result.items() if key not in _PRIVATE_FIELDS}

    def material_settings(self) -> dict:
        """Return explicit, exclusive sources, including clears for inactive fields."""
        source = self.source.currentData()
        audio = self.audio_picker.value()
        online_audio = source == "netease" and self.audio_mode.currentData() == "online"
        if online_audio:
            # After preparation this is the archived download. Reuse it for
            # unrelated project setting changes, but never for a different song.
            audio = str(self._settings.get("audio_file") or "") if (
                self._settings.get("prefer_netease_audio")
                and self.link_edit.text().strip() == str(self._settings.get("netease_link") or "").strip()
            ) else ""
        result = {
            "output_name": self.name_edit.text().strip(),
            "audio_file": audio,
            "lyrics_file": "",
            "pasted_lyrics": "",
            "netease_link": "",
            "qqmusic_link": "",
            "utaten_link": "",
            "use_netease_lyrics": source == "netease",
            "use_qqmusic_lyrics": source == "qqmusic",
            "use_utaten_lyrics": source == "utaten",
            "utaten_pronunciation_only": False,
            "rights_confirmed": source in _LINKS and self.rights.isChecked(),
            "prefer_netease_audio": online_audio,
        }
        if Path(audio).suffix.lower() in _VIDEO_EXTENSIONS:
            result.update(audio_file="", video_file=audio)
        elif self._video_was_audio:
            result["video_file"] = ""
        # An old video's audio must not silently override a deliberate online choice.
        if online_audio:
            result["video_file"] = ""
        if source == "local":
            result["lyrics_file"] = self.lyrics_picker.value()
        elif source == "paste":
            result["pasted_lyrics"] = self.pasted_lyrics.toPlainText().strip()
        elif source in _LINKS:
            result[_LINKS[source]] = self.link_edit.text().strip()
        return result

    def _validation_error(self, *, check_processing: bool = True) -> str:
        values = self.material_settings()
        source = self.source.currentData()
        message = ""
        if not values["output_name"]:
            message = "请填写项目名称。"
        elif not self.editing and not self.project_directory():
            message = "请选择项目文件夹；工程与视频输出目录分别保存。"
        elif self.project_directory() and Path(self.project_directory()).is_file():
            message = "项目文件夹不能是已有文件，请选择文件夹。"
        elif source == "local" and not values["lyrics_file"]:
            message = "请选择歌词文件，或改用其他歌词来源。"
        elif source == "paste" and not values["pasted_lyrics"]:
            message = "请粘贴歌词正文。"
        elif source in _LINKS and not values[_LINKS[source]]:
            message = "请填写所选来源的链接。"
        elif source in _LINKS and not values["rights_confirmed"]:
            message = "获取在线内容前，请确认歌曲及歌词使用权。"
        elif (
            source == "netease" and self.audio_mode.currentData() == "local"
            and not (values["audio_file"] or values.get("video_file"))
        ):
            message = "已选择使用本地音频，请选择文件；需要在线音频时请切换上方音频选项。"
        elif source == "recognize" and not (values["audio_file"] or values.get("video_file")):
            message = "自动识别需要先选择本地音频或有声 MV。"
        if not message and check_processing and source in _SUPPLEMENT_SOURCES:
            processing = self.settings_page.get_settings()
            if processing["utaten_pronunciation_only"]:
                if not processing["utaten_link"]:
                    message = "请填写用于补充官方注音的 UtaTen 页面链接。"
                elif not processing["rights_confirmed"]:
                    message = "补充在线官方注音前，请在官方注音中确认使用权。"
        return message

    def accept(self) -> None:
        if message := self._validation_error(check_processing=False):
            self.set_step(0)
            self.error.setText(message)
            return
        if message := self._validation_error():
            self.set_step(1)
            self.error.setText(message)
            return
        super().accept()
