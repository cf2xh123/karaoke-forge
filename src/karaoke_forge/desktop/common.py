"""Shared native controls and a single background-job owner."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLineEdit, QPushButton, QWidget


class PathPicker(QWidget):
    changed = Signal(str)

    def __init__(
        self,
        file_filter: str = "所有文件 (*)",
        *,
        directory: bool = False,
        multiple: bool = False,
        parent: QWidget | None = None,
        filter: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.file_filter = filter or file_filter
        self.directory = directory
        self.multiple = multiple
        self._selected: list[str] = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("选择或拖入文件夹…" if directory else "选择或拖入本地文件…")
        self.button = QPushButton("浏览…")
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)
        self.button.clicked.connect(self.browse)
        self.edit.textChanged.connect(self.changed)
        self.setAcceptDrops(True)

    def value(self) -> str:
        return self.edit.text().strip()

    def set_value(self, value: str) -> None:
        self._selected = [str(value)] if value else []
        self.edit.setText(str(value or ""))

    def set_paths(self, values: list[str]) -> None:
        self._selected = [str(value) for value in values]
        self.edit.setText("; ".join(self._selected))

    def paths(self) -> list[str]:
        value = self.value()
        if not value:
            return []
        if value == "; ".join(self._selected):
            return self._selected.copy()
        return (
            [part.strip() for part in value.split(";") if part.strip()]
            if self.multiple
            else [value]
        )

    @Slot()
    def browse(self) -> None:
        if self.directory:
            selected = QFileDialog.getExistingDirectory(self, "选择输出目录", self.value())
            if selected:
                self.set_value(selected)
        elif self.multiple:
            selected, _ = QFileDialog.getOpenFileNames(self, "选择文件", "", self.file_filter)
            if selected:
                self.set_paths(selected)
        else:
            selected, _ = QFileDialog.getOpenFileName(self, "选择文件", "", self.file_filter)
            if selected:
                self.set_value(selected)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        paths = [path for path in paths if Path(path).is_dir() == self.directory]
        if paths:
            if self.multiple:
                self.set_paths(paths)
            else:
                self.set_value(paths[0])
            event.acceptProposedAction()


class _Job(QThread):
    progress = Signal(str)

    def __init__(self, task: Callable, parent: QObject) -> None:
        super().__init__(parent)
        self.task = task
        self.result = None
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            self.result = self.task(self.progress.emit)
        except Exception as exc:  # noqa: BLE001 - contain third-party worker failures
            self.error = exc


class JobRunner(QObject):
    """Keep the UI responsive; deliver all callbacks on the GUI thread."""

    busy_changed = Signal(bool)
    message = Signal(str)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._job: _Job | None = None
        self._callback: Callable | None = None
        self.title = ""

    @property
    def is_busy(self) -> bool:
        return self._job is not None

    def submit(self, title: str, task: Callable, on_success: Callable | None = None) -> bool:
        if self.is_busy:
            self.message.emit(f"正在{self.title}，请等待当前任务完成。")
            return False
        self.title = title
        self._callback = on_success
        self._job = _Job(task, self)
        self._job.progress.connect(self.message)
        self._job.finished.connect(self._finished)
        self.busy_changed.emit(True)
        self.message.emit(f"开始：{title}")
        self._job.start()
        return True

    @Slot()
    def _finished(self) -> None:
        job, callback = self._job, self._callback
        self._job = None
        self._callback = None
        self.busy_changed.emit(False)
        if job is None:
            return
        try:
            if job.error is not None:
                self.failed.emit(str(job.error))
                self.message.emit(f"{self.title}失败：{job.error}")
            elif callback is not None:
                callback(job.result)
        except Exception as exc:  # noqa: BLE001 - Qt must not swallow callback failures
            self.failed.emit(str(exc))
        finally:
            job.deleteLater()
