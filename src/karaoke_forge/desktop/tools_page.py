"""Native standalone lyric utilities and runtime settings."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import web
from ..network import load_model_download_settings, test_model_download_network
from ..projects import PROJECT_FILENAME
from .common import JobRunner, PathPicker


def combo(choices: list[tuple[str, str]]) -> QComboBox:
    control = QComboBox()
    for label, value in choices:
        control.addItem(label, value)
    return control


class _Form(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.form = QFormLayout(self)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.form.setSpacing(12)

    def row(self, label: str, widget):
        self.form.addRow(label, widget)
        return widget


class ToolsPage(QWidget):
    completed = Signal(object)
    open_requested = Signal(str)

    def __init__(self, runner: JobRunner, parent=None) -> None:
        super().__init__(parent)
        self.runner = runner
        outer = QVBoxLayout(self)
        title = QLabel("歌词工具")
        title.setObjectName("pageTitle")
        outer.addWidget(title)
        outer.addWidget(QLabel("单独生成时间轴、获取公开歌词，或在常见字幕格式之间转换。"))
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        self.align_fields = self._alignment_form(False)
        self.netease_fields = self._alignment_form(True)
        qq = _Form()
        qq_link = qq.row("QQ 音乐单曲链接", QLineEdit())
        qq_name = qq.row("导出名称（可选）", QLineEdit())
        qq_rights = qq.row("", QCheckBox("我确认有权使用这些歌曲与歌词"))
        qq_button = qq.row("", QPushButton("获取公开歌词与翻译"))
        qq_button.clicked.connect(
            lambda: self._submit_qq(qq_link.text(), qq_name.text(), qq_rights.isChecked())
        )
        self._add_tab(qq, "QQ 音乐歌词")
        convert = _Form()
        self.convert_source = convert.row(
            "歌词文件", PathPicker("歌词 (*.lrc *.yrc *.srt *.vtt *.ass *.json)")
        )
        self.convert_format = convert.row(
            "输出格式",
            combo([(v.upper(), v) for v in ["lrc", "elrc", "srt", "vtt", "ass", "json"]]),
        )
        convert_button = convert.row("", QPushButton("转换并导出"))
        convert_button.setProperty("primary", True)
        convert_button.clicked.connect(self.convert)
        self._add_tab(convert, "格式转换")
        self.result_panel = QWidget()
        result_layout = QVBoxLayout(self.result_panel)
        result_layout.setContentsMargins(0, 0, 0, 0)
        self.result_status = QTextBrowser()
        self.result_status.setMaximumHeight(100)
        result_layout.addWidget(self.result_status)
        self.result_files = QListWidget()
        self.result_files.setMaximumHeight(130)
        self.result_files.itemDoubleClicked.connect(
            lambda item: QDesktopServices.openUrl(
                QUrl.fromLocalFile(item.data(Qt.ItemDataRole.UserRole))
            )
        )
        result_layout.addWidget(self.result_files)
        result_actions = QHBoxLayout()
        self.open_directory_button = QPushButton("打开输出文件夹")
        self.open_directory_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self._result_directory))
        )
        result_actions.addWidget(self.open_directory_button)
        self.continue_button = QPushButton("在工作台继续编辑")
        self.continue_button.clicked.connect(lambda: self.open_requested.emit(self._result_project))
        result_actions.addWidget(self.continue_button)
        result_actions.addStretch()
        result_layout.addLayout(result_actions)
        outer.addWidget(self.result_panel)
        self.result_panel.hide()
        self._result_directory = ""
        self._result_project = ""
        runner.busy_changed.connect(lambda busy: self.tabs.setEnabled(not busy))

    def _show_result(self, result) -> None:
        self.result_status.setMarkdown(result.status)
        self._result_directory = result.output_dir or ""
        self._result_project = ""
        self.result_files.clear()
        best_rank = 100
        for filename in result.files:
            item = QListWidgetItem(Path(filename).name)
            item.setData(Qt.ItemDataRole.UserRole, filename)
            item.setToolTip(filename)
            self.result_files.addItem(item)
            path = Path(filename)
            # Jobs list plain LRC first, but continuing an edit must prefer the
            # lossless project over interchange formats with fewer data fields.
            rank = (
                0
                if path.name == PROJECT_FILENAME
                else 1
                if path.suffix.lower() == ".json"
                else 2
                if path.suffix.lower() == ".yrc"
                else 3
                if path.suffix.lower() == ".elrc" or path.name.lower().endswith(".enhanced.lrc")
                else {".ass": 4, ".lrc": 5, ".srt": 6, ".vtt": 7}.get(path.suffix.lower(), 100)
            )
            if rank < best_rank:
                best_rank = rank
                self._result_project = filename
        self.open_directory_button.setEnabled(bool(self._result_directory))
        self.continue_button.setEnabled(bool(self._result_project))
        self.result_panel.show()
        self.completed.emit(result)

    def _add_tab(self, form: QWidget, label: str) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(form)
        self.tabs.addTab(scroll, label)

    def _alignment_form(self, online: bool) -> dict:
        form = _Form()
        fields = {}
        if online:
            fields["link"] = form.row("网易云歌曲链接", QLineEdit())
        fields["audio"] = form.row("歌曲音频", PathPicker("音频 (*.wav *.mp3 *.flac *.m4a *.ogg)"))
        fields["lyrics"] = form.row("歌词文件（或下方粘贴）", PathPicker())
        fields["pasted"] = form.row("一行一句歌词", QPlainTextEdit())
        fields["pasted"].setMaximumHeight(130)
        fields["name"] = form.row("导出名称（可选）", QLineEdit())
        fields["language"] = form.row(
            "歌曲语言",
            combo(
                [
                    ("自动识别", "自动识别"),
                    ("中文", "zh"),
                    ("日语", "ja"),
                    ("英语", "en"),
                    ("韩语", "ko"),
                    ("粤语", "yue"),
                ]
            ),
        )
        fields["model"] = form.row("识别档位", combo(web._ALIGNMENT_MODEL_CHOICES))
        fields["model"].setCurrentIndex(1)
        fields["device"] = form.row(
            "处理设备", combo([("自动选择", "auto"), ("CPU", "cpu"), ("NVIDIA GPU", "cuda")])
        )
        fields["separate"] = form.row("", QCheckBox("先分离人声（需要 Demucs）"))
        fields["refinement"] = form.row(
            "已有时间轴",
            combo([("自动精修", "auto"), ("保留原时间", "off"), ("强制检查", "force")]),
        )
        if online:
            fields["page"] = form.row("", QCheckBox("使用页面公开歌词"))
            fields["page"].setChecked(True)
            fields["keep"] = form.row("", QCheckBox("保留下载的音频"))
            fields["keep"].setChecked(True)
            fields["rights"] = form.row("", QCheckBox("我确认有权使用歌曲、歌词与账号音频"))
            fields["music_u"] = form.row("MUSIC_U（可选，仅当前会话）", QLineEdit())
            fields["music_u"].setEchoMode(QLineEdit.EchoMode.Password)
            login = form.row("", QPushButton("在官方页面登录网易云"))
            login.clicked.connect(lambda: self._login(fields["music_u"]))
        start = form.row("", QPushButton("生成时间轴歌词"))
        start.setProperty("primary", True)
        start.clicked.connect(lambda: self.align(online))
        self._add_tab(form, "网易云歌词" if online else "音频对齐")
        return fields

    def _login(self, target: QLineEdit) -> None:
        from ..netease_login import acquire_netease_music_u

        self.runner.submit("连接网易云账号", lambda log: acquire_netease_music_u(), target.setText)

    def align(self, online: bool = False) -> None:
        f = self.netease_fields if online else self.align_fields
        args = {
            "lyrics_file": f["lyrics"].value() or None,
            "pasted_lyrics": f["pasted"].toPlainText(),
            "output_name": f["name"].text(),
            "language": f["language"].currentData(),
            "model": f["model"].currentData(),
            "device": f["device"].currentData(),
            "separate_vocals": f["separate"].isChecked(),
            "timing_refinement": f["refinement"].currentData(),
        }
        if online:
            args.update(
                link=f["link"].text(),
                local_audio_file=f["audio"].value() or None,
                use_page_lyrics=f["page"].isChecked(),
                keep_audio=f["keep"].isChecked(),
                rights_confirmed=f["rights"].isChecked(),
                music_u=f["music_u"].text(),
            )
            function = web.run_netease_align_job
        else:
            args["audio_file"] = f["audio"].value() or None
            function = web.run_align_job
        self.runner.submit(
            "生成歌词时间轴",
            lambda log: function(**args, progress_callback=log),
            self._show_result,
        )

    def _submit_qq(self, link: str, name: str, rights: bool) -> None:
        self.runner.submit(
            "获取 QQ 音乐歌词",
            lambda log: web.run_qqmusic_job(link, name, rights),
            self._show_result,
        )

    def convert(self) -> None:
        source = self.convert_source.value()
        output_format = self.convert_format.currentData()
        self.runner.submit(
            "转换歌词格式",
            lambda log: web.run_convert_job(source, output_format),
            self._show_result,
        )


class SettingsPage(QWidget):
    def __init__(self, runner: JobRunner, parent=None) -> None:
        super().__init__(parent)
        self.runner = runner
        layout = QVBoxLayout(self)
        title = QLabel("环境与模型设置")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        form = _Form()
        self.mode = form.row(
            "模型下载来源",
            combo(
                [
                    ("国内直连 · ModelScope", "modelscope"),
                    ("Hugging Face 官方源", "official"),
                    ("本机代理", "proxy"),
                    ("hf-mirror（需确认）", "mirror"),
                    ("离线缓存", "offline"),
                ]
            ),
        )
        self.proxy = form.row("HTTP / HTTPS 代理", QLineEdit())
        self.proxy.setPlaceholderText("http://127.0.0.1:7890")
        self.mirror = form.row("", QCheckBox("我确认使用 hf-mirror 第三方镜像下载公开模型"))
        self.profile = form.row(
            "预下载模型", combo([("快速", "fast"), ("均衡", "balanced"), ("精准", "precise")])
        )
        layout.addWidget(form)
        buttons = QHBoxLayout()
        for label, callback in [
            ("保存设置", self.save),
            ("测试网络", self.test),
            ("自动检测网络", self.auto),
            ("预下载模型", self.download),
            ("检查本机环境", self.inspect),
        ]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.report = QTextBrowser()
        self.report.setOpenExternalLinks(True)
        self.report.setPlainText("点击“检查本机环境”查看 FFmpeg、识别模型与人声分离依赖。")
        layout.addWidget(self.report, 1)
        self.reload_settings()
        runner.busy_changed.connect(lambda busy: form.setEnabled(not busy))

    def reload_settings(self) -> None:
        mode, proxy, mirror, message = web._model_network_form_defaults()
        self.mode.setCurrentIndex(max(0, self.mode.findData(mode)))
        self.proxy.setText(proxy)
        self.mirror.setChecked(mirror)
        if message:
            self.report.setMarkdown(message)

    def save(self) -> None:
        values = (self.mode.currentData(), self.proxy.text(), self.mirror.isChecked())
        self.runner.submit(
            "保存模型设置",
            lambda log: web.configure_model_network_for_web(*values),
            self.report.setMarkdown,
        )

    def test(self) -> None:
        def task(log):
            result = test_model_download_network(load_model_download_settings(), timeout=8)
            return f"{result.summary_zh}\n\n{result.detail_zh}"

        self.runner.submit("测试已保存的网络配置", task, self.report.setPlainText)

    def auto(self) -> None:
        def finish(result):
            self.report.setMarkdown(result[0])
            self.reload_settings()

        self.runner.submit(
            "自动检测模型下载网络", lambda log: web.auto_configure_model_network_for_web(), finish
        )

    def download(self) -> None:
        profile = self.profile.currentData()
        self.runner.submit(
            "预下载模型",
            lambda log: web.predownload_model_for_web(profile),
            self.report.setMarkdown,
        )

    def inspect(self) -> None:
        self.runner.submit(
            "检查本机环境", lambda log: web.environment_markdown(), self.report.setMarkdown
        )
