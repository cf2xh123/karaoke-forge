"""Karaoke Forge's native Qt workspace; no browser or HTTP server is started."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QFont, QKeySequence
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..formats import read_lyrics
from ..projects import (
    PROJECT_FILENAME,
    list_workspace_projects,
    load_workspace_project,
    read_workspace_lyrics,
)
from ..web import UiJobResult, _default_output_root
from .common import JobRunner
from .editor_page import EditorPage
from .make_page import MakePage
from .project_dialog import ProjectDialog
from .tools_page import SettingsPage, ToolsPage
from .workspace import WorkspacePage


class OutputPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.directory = ""
        self._shortcut_scope = self
        self._space_held = False
        layout = QVBoxLayout(self)
        self.status = QTextBrowser()
        self.status.setMaximumHeight(64)
        self.status.setPlainText("制作、转换或导出后，这里会显示成品和文件。")
        layout.addWidget(self.status)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        media = QWidget()
        media_layout = QVBoxLayout(media)
        media_layout.setContentsMargins(0, 0, 0, 0)
        self.video = QVideoWidget()
        self.video.setMinimumHeight(100)
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video)
        media_layout.addWidget(self.video)
        controls = QHBoxLayout()
        self.play_button = QPushButton("播放 / 暂停")
        self.play_button.clicked.connect(self.toggle_playback)
        controls.addWidget(self.play_button)
        self.position = QSlider(Qt.Orientation.Horizontal)
        self.position.setRange(0, 0)
        self.position.sliderMoved.connect(self.player.setPosition)
        self.player.durationChanged.connect(
            lambda duration: self.position.setMaximum(int(duration))
        )
        self.player.positionChanged.connect(self._position_changed)
        controls.addWidget(self.position, 1)
        media_layout.addLayout(controls)
        splitter.addWidget(media)
        files_panel = QWidget()
        files_layout = QVBoxLayout(files_panel)
        files_layout.addWidget(QLabel("输出文件 · 双击用默认程序打开"))
        self.files = QListWidget()
        self.files.itemDoubleClicked.connect(self.open_file)
        files_layout.addWidget(self.files)
        open_directory = QPushButton("打开输出文件夹")
        open_directory.clicked.connect(self.open_directory)
        files_layout.addWidget(open_directory)
        splitter.addWidget(files_panel)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setPlaceholderText("后台任务日志")
        splitter.addWidget(self.log)
        splitter.setSizes([450, 260, 350])
        layout.addWidget(splitter, 1)
        self.player.errorOccurred.connect(
            lambda error, message: self.log.appendPlainText(f"播放失败：{message}")
        )
        QApplication.instance().installEventFilter(self)

    def set_playback_shortcut_scope(self, scope: QWidget) -> None:
        self._shortcut_scope = scope

    def _can_handle_space(self) -> bool:
        focused = QApplication.focusWidget()
        scope = self._shortcut_scope
        if (
            focused is None or not scope.isVisible() or not scope.isEnabled()
            or QApplication.activeModalWidget() is not None
            or QApplication.activePopupWidget() is not None
            or focused.window() is not self.window()
            or not (focused is scope or scope.isAncestorOf(focused))
        ):
            return False
        if not (
            focused is self or self.isAncestorOf(focused)
            or self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        ):
            return False
        widget = focused
        while widget is not None and widget is not scope.parentWidget():
            if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
                return False
            if isinstance(widget, QComboBox) and widget.isEditable():
                return False
            widget = widget.parentWidget()
        return True

    def eventFilter(self, watched, event):
        if event.type() in (
            QEvent.Type.FocusOut, QEvent.Type.WindowDeactivate, QEvent.Type.ApplicationDeactivate,
        ):
            self._space_held = False
        if (
            event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease, QEvent.Type.ShortcutOverride)
            and event.key() == Qt.Key.Key_Space
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            if event.type() == QEvent.Type.KeyRelease and self._space_held:
                if not event.isAutoRepeat():
                    self._space_held = False
                event.accept()
                return True
            if self._can_handle_space():
                if event.type() == QEvent.Type.ShortcutOverride:
                    event.accept()
                    return True
                if event.type() == QEvent.Type.KeyPress:
                    if not event.isAutoRepeat() and not self._space_held:
                        self._space_held = True
                        self.toggle_playback()
                    event.accept()
                    return True
        return super().eventFilter(watched, event)

    def clear_project(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        self.directory = ""
        self.files.clear()
        self.log.clear()
        self.status.setPlainText("制作、转换或导出后，这里会显示成品和文件。")

    def _position_changed(self, value: int) -> None:
        if not self.position.isSliderDown():
            self.position.setValue(value)

    def toggle_playback(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def show_result(self, result: UiJobResult) -> None:
        self.status.setMarkdown(result.status)
        self.directory = result.output_dir or ""
        self.files.clear()
        for path in result.files:
            item = QListWidgetItem(Path(path).name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            self.files.addItem(item)
        self.log.appendPlainText(result.log)
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(result.video) if result.video else QUrl())

    def open_file(self, item: QListWidgetItem) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(item.data(Qt.ItemDataRole.UserRole)))

    def open_directory(self) -> None:
        if self.directory:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.directory))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Karaoke Forge {__version__} · 本地卡拉 OK 工作台")
        self.resize(1380, 900)
        self.setMinimumSize(900, 640)
        self.settings = QSettings("KaraokeForge", "Desktop")
        self.runner = JobRunner(self)
        self.make = MakePage(self.runner, embedded=True)
        self.editor = EditorPage(self.runner)
        self.tools = ToolsPage(self.runner)
        self.outputs = OutputPage()
        self.environment = SettingsPage(self.runner)
        self.workspace = WorkspacePage(self.make, self.editor, self.outputs, self.runner)
        self.editor.set_playback_shortcut_scope(
            self, excluded=(self.outputs,),
            guard=lambda: self.pages.currentWidget() is self.workspace
            and self.outputs.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState,
        )
        self.outputs.set_playback_shortcut_scope(self)
        self.outputs.player.playbackStateChanged.connect(
            lambda state: self.editor.stop_playback()
            if state == QMediaPlayer.PlaybackState.PlayingState else None
        )
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        rail = QWidget()
        rail.setObjectName("navigationRail")
        rail.setFixedWidth(150)
        rail_layout = QVBoxLayout(rail)
        brand = QLabel("KARAOKE\nFORGE")
        brand.setObjectName("brand")
        rail_layout.addWidget(brand)
        version = QLabel(f"原生桌面工作台\nv{__version__}")
        version.setObjectName("subtle")
        rail_layout.addWidget(version)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.addItems(["项目编辑", "歌词工具", "环境设置"])
        rail_layout.addWidget(self.navigation, 1)
        new_project = QPushButton("新建工程…")
        new_project.clicked.connect(self.new_project_dialog)
        rail_layout.addWidget(new_project)
        open_project = QPushButton("打开工程…")
        open_project.clicked.connect(self.open_project_dialog)
        rail_layout.addWidget(open_project)
        rail_layout.addWidget(QLabel("本地处理 · 文件留在本机"))
        root.addWidget(rail)
        self.pages = QStackedWidget()
        for page in [self.workspace, self.tools, self.environment]:
            self.pages.addWidget(page)
        root.addWidget(self.pages, 1)
        self.setCentralWidget(central)
        self.navigation.currentRowChanged.connect(self._navigate)
        self.navigation.setCurrentRow(0)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(180)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().showMessage("就绪 · 新建工程并保存配置，或打开已保存的工程")
        self.runner.busy_changed.connect(self._busy_changed)
        self.runner.message.connect(self.statusBar().showMessage)
        self.runner.message.connect(self.outputs.log.appendPlainText)
        self.runner.failed.connect(lambda text: QMessageBox.warning(self, "任务未完成", text))
        self.make.prepared.connect(self._prepared)
        self.make.rendered.connect(self._result)
        self.make.workspace_requested.connect(self.open_project)
        self.tools.completed.connect(self._tool_result)
        self.tools.open_requested.connect(self.open_project)
        self.workspace.open_requested.connect(self.open_project_dialog)
        self.workspace.new_requested.connect(self.new_project_dialog)
        self.workspace.source_requested.connect(self.edit_project_sources)
        self.workspace.changed.connect(self.setWindowModified)
        self.setWindowTitle(self.windowTitle() + "[*]")
        self._build_menu()
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("文件")
        self.new_project_action = QAction("新建工程…", self)
        self.new_project_action.setShortcut(QKeySequence.StandardKey.New)
        self.new_project_action.triggered.connect(self.new_project_dialog)
        menu.addAction(self.new_project_action)
        open_action = QAction("打开工程或歌词…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_project_dialog)
        menu.addAction(open_action)
        save_action = QAction("保存完整工程…", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self.workspace.save_project)
        menu.addAction(save_action)
        save_as_action = QAction("工程另存为…", self)
        save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        save_as_action.triggered.connect(lambda: self.workspace.save_project(as_new=True))
        menu.addAction(save_as_action)
        self.project_settings_action = QAction("项目设置…", self)
        self.project_settings_action.triggered.connect(self.edit_project_sources)
        menu.addAction(self.project_settings_action)
        self.recent = menu.addMenu("最近的工程")
        self.recent.aboutToShow.connect(self._refresh_recent)
        menu.addSeparator()
        close_action = QAction("退出", self)
        close_action.setShortcut(QKeySequence.StandardKey.Quit)
        close_action.triggered.connect(self.close)
        menu.addAction(close_action)
        edit_menu = self.menuBar().addMenu("编辑")
        undo_action = edit_menu.addAction("撤销\tCtrl+Z")
        undo_action.triggered.connect(self.editor.undo)
        redo_action = edit_menu.addAction("重做\tCtrl+Y")
        redo_action.triggered.connect(self.editor.redo)
        account_menu = self.menuBar().addMenu("网易云账号")
        login_action = account_menu.addAction("连接官方账号…")
        login_action.triggered.connect(self.make.login_netease)
        relogin_action = account_menu.addAction("重新连接…")
        relogin_action.triggered.connect(lambda: self.make.login_netease(relogin=True))
        logout_action = account_menu.addAction("断开账号")
        logout_action.triggered.connect(self.make.logout_netease)
        account_menu.addSeparator()
        account_options = account_menu.addAction("高级连接选项…")
        account_options.triggered.connect(lambda: self.make.show_account_settings(self))
        help_menu = self.menuBar().addMenu("帮助")
        help_action = QAction("使用说明", self)
        help_action.triggered.connect(
            lambda: QMessageBox.information(
                self,
                "使用流程",
                "1. 新建工程：选择保存位置、音频和歌词来源。\n"
                "2. 配置 AI 对齐、字幕外观和输出选项，确认后保存工程并进入编辑。\n"
                "3. 生成时间轴后，在编辑区试听、调整歌词，完成后导出视频。\n\n"
                "项目设置可随时修改，保存工程会保留配置、素材和当前歌词。\n"
                "当前句逐词与整曲时间轴在同一区域切换；空格播放 / 暂停，输入文字时仍输入空格。\n"
                "桌面版与网页版共用工程 JSON，旧工程可从“文件”菜单打开。",
            )
        )
        help_menu.addAction(help_action)

    def _refresh_recent(self) -> None:
        self.recent.clear()
        projects = list_workspace_projects(_default_output_root())
        for workspace in projects[:20]:
            action = self.recent.addAction(workspace.name)
            action.triggered.connect(
                lambda checked=False, path=workspace.manifest: self.open_project(str(path))
            )
        if not projects:
            self.recent.addAction("暂无已保存工程").setEnabled(False)

    def _navigate(self, index: int) -> None:
        if index != 0:
            self.editor.stop_playback()
            self.outputs.player.pause()
        self.pages.setCurrentIndex(index)

    def _busy_changed(self, busy: bool) -> None:
        self.progress.setRange(0, 0 if busy else 1)
        if not busy:
            self.progress.setValue(0)

    def _allow_replace(self) -> bool:
        if self.runner.is_busy or self.workspace.is_saving_revision:
            QMessageBox.information(self, "任务进行中", "请等待当前任务完成后再打开其他工程。")
            return False
        if not self.workspace.is_dirty:
            return True
        return (
            QMessageBox.question(
                self,
                "保留未保存的工程修改",
                "当前歌词、素材或样式有未保存修改。是否放弃这些修改并继续？\n选择“否”可返回工作台保存完整工程。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def open_project_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开工程或时间轴歌词",
            "",
            "工程与歌词 (*.json *.txt *.lrc *.elrc *.yrc *.srt *.vtt *.ass);;所有文件 (*)",
        )
        if path:
            self.open_project(path)

    def new_project_dialog(self) -> None:
        if self.runner.is_busy or self.workspace.is_saving_revision:
            QMessageBox.information(self, "任务进行中", "请等待当前任务完成后再新建工程。")
            return
        dialog = ProjectDialog(self, default_settings=self.make.get_settings())
        replacement_allowed = False
        try:
            while dialog.exec() == QDialog.DialogCode.Accepted:
                if not replacement_allowed:
                    if not self._allow_replace():
                        return
                    replacement_allowed = True
                if self.workspace.start_project(dialog.project_settings(), dialog.project_directory()):
                    self.navigation.setCurrentRow(0)
                    self.statusBar().showMessage("工程与配置已保存 · 生成时间轴后开始编辑")
                    return
                QMessageBox.warning(self, "工程未创建", self.workspace.activity.text())
                # Keep the same setup draft so a missing file or unwritable folder
                # can be corrected without entering all the project options again.
        finally:
            if isinstance(dialog, QDialog):
                dialog.deleteLater()

    def edit_project_sources(self) -> None:
        if self.runner.is_busy or self.workspace.is_saving_revision:
            return
        if not self.workspace.project_directory and not self.workspace.has_project:
            self.new_project_dialog()
            return
        dialog = ProjectDialog(
            self, settings=self.make.get_settings(), directory=self.workspace.project_directory
        )
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.workspace.apply_project_settings(dialog.project_settings())
        finally:
            if isinstance(dialog, QDialog):
                dialog.deleteLater()

    def open_project(self, path: str) -> None:
        if not self._allow_replace():
            return

        def load(log):
            source = Path(path)
            is_workspace = source.name == PROJECT_FILENAME
            if source.suffix.lower() == ".json" and not is_workspace:
                payload = json.loads(source.read_text(encoding="utf-8-sig"))
                is_workspace = (
                    isinstance(payload, dict)
                    and payload.get("schema_version") == 1
                    and bool(payload.get("lyrics_project"))
                )
            if is_workspace:
                workspace = load_workspace_project(source)
                document = read_workspace_lyrics(workspace)
                document.metadata["workspace_manifest"] = str(workspace.manifest)
                return workspace, document
            document = read_lyrics(source)
            manifest = document.metadata.get("workspace_manifest")
            if manifest:
                try:
                    manifest = Path(manifest)
                    if not manifest.is_absolute():
                        manifest = source.parent / manifest
                    workspace = load_workspace_project(manifest)
                except (OSError, TypeError, ValueError):
                    pass
                else:
                    if source.resolve() == workspace.lyrics_project.resolve():
                        return workspace, read_workspace_lyrics(workspace)
            return None, document

        def finish(result):
            workspace, document = result
            self.workspace.load_project(document, workspace, path)
            self.navigation.setCurrentRow(0)

        self.runner.submit("载入工程", load, finish)

    def _prepared(self, result) -> None:
        if not result.payload or not result.payload.get("lines"):
            self._result(
                UiJobResult(result.status, None, result.files, result.log, result.output_dir)
            )
            return
        self.workspace.adopt_prepared(result)
        self.statusBar().showMessage("时间轴已载入当前工作台 · 直接试听校准或导出视频")

    def _result(self, result) -> None:
        self.workspace.show_result(result)

    def _tool_result(self, result) -> None:
        # Utilities own their result view, so completing a conversion does not
        # hide its outcome or replace the current song's output files.
        self.statusBar().showMessage(self.tools.result_status.toPlainText())

    def closeEvent(self, event) -> None:
        if self.runner.is_busy or self.workspace.is_saving_revision:
            QMessageBox.information(
                self, "后台任务尚未完成", "请等待处理结束后退出。可以最小化窗口，让任务继续运行。"
            )
            event.ignore()
            return
        if not self._allow_replace():
            event.ignore()
            return
        self.editor.stop_playback()
        self.outputs.player.stop()
        self.settings.setValue("geometry", self.saveGeometry())
        super().closeEvent(event)


def run(project: str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Karaoke Forge")
    app.setOrganizationName("KaraokeForge")
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet((Path(__file__).with_name("theme.qss")).read_text(encoding="utf-8"))
    window = MainWindow()
    window.show()
    if project:
        QTimer.singleShot(0, lambda: window.open_project(project))
    return app.exec()
