"""Native Qt lyric editing, playback, history and project export."""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..editor import (
    apply_editor_rows,
    apply_pronunciation_rows,
    apply_token_timing,
    document_pronunciation_to_editor_rows,
    document_to_editor_rows,
    nudge_editor_line_timing,
    ripple_following_line_timing,
    shift_editor_timeline,
    token_timing_to_json,
)
from ..editor_history import history_stacks, record_history, travel_history
from ..formats import export_formats, read_lyrics
from ..models import KaraokeToken, LyricLine, LyricsDocument, PronunciationSpan
from ..preferences import load_preferences, save_preferences
from ..projects import (
    PROJECT_FILENAME,
    load_workspace_project,
    read_workspace_lyrics,
    save_workspace_project,
)
from ..web import _default_output_root, _safe_stem
from .common import PathPicker
from .timeline import LyricPreviewWidget, TimelineWidget, TokenTimelineWidget


def _copy_document(document: LyricsDocument) -> LyricsDocument:
    return copy.deepcopy(document)


def _history_document(snapshot: dict) -> LyricsDocument:
    """Restore without JSON reader hydration, retaining intentional empty tokens."""
    payload = snapshot["document"]
    return LyricsDocument(
        lines=[
            LyricLine(
                **{
                    **line,
                    "tokens": [KaraokeToken(**token) for token in line.get("tokens", [])],
                    "pronunciation_units": [
                        PronunciationSpan(**unit) for unit in line.get("pronunciation_units", [])
                    ],
                }
            )
            for line in payload["lines"]
        ],
        metadata=copy.deepcopy(payload.get("metadata", {})),
        source_format=snapshot.get("source_format", "json"),
    )


def _write_exports(
    document: LyricsDocument, directory: str, name: str, audio: str
) -> tuple[LyricsDocument, list[str]]:
    """Export a frozen revision, preserving its associated project assets/settings."""
    document = _copy_document(document)
    previous = None
    manifest = document.metadata.get("workspace_manifest")
    if manifest:
        try:
            previous = load_workspace_project(manifest)
        except (OSError, ValueError, TypeError):
            pass
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    document.metadata["workspace_manifest"] = str(root / PROJECT_FILENAME)
    stem = _safe_stem(name, fallback="edited-lyrics")
    formats = ["lrc", "elrc", "srt", "vtt", "ass", "json"] if document.is_timed else ["json"]
    exports = export_formats(document, root, stem, formats)
    saved_audio = audio or (previous.audio if previous else None)
    if (
        previous
        and previous.video
        and saved_audio
        and Path(saved_audio).resolve() == previous.video.resolve()
    ):
        # A video can serve as the calibration audio without being copied
        # into the project twice. Its video field retains the sound track.
        saved_audio = None
    workspace = save_workspace_project(
        root,
        name=name.strip() or (previous.name if previous else "编辑后的歌词"),
        lyrics_project=exports["json"],
        audio=saved_audio,
        video=previous.video if previous else None,
        cover=previous.cover if previous else None,
        font_files=previous.font_files if previous else (),
        settings=previous.settings if previous else {},
        recent_root=_default_output_root(),
    )
    return document, [str(path) for path in exports.values()] + [str(workspace.manifest)]


class _DraftDelegate(QStyledItemDelegate):
    """Expose live cell edits before Qt commits its editor back into the table."""

    def __init__(self, page, table):
        super().__init__(table)
        self.page = page
        self.closeEditor.connect(lambda *_: page._finish_cell_edit())

    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        if isinstance(editor, QLineEdit):
            editor.textEdited.connect(lambda _text: self.page._active_cell_changed())
        return editor


class EditorPage(QWidget):
    """A native editor whose form drafts are validated before any data transfer."""

    handoff = Signal(object)
    changed = Signal(bool)

    def __init__(self, runner, parent=None):
        super().__init__(parent)
        self.runner = runner
        self._document = LyricsDocument(lines=[])
        self._saved_document = self._document.to_dict()
        self._saved_audio = ""
        self._saved_name = ""
        self._history: dict = {}
        self._selected = 0
        self._rendering = False
        self._dirty = False
        self._cell_draft = False
        self._flushing_cell = False
        self._line_baseline: list = []
        self._token_baseline: list = []
        self._pronunciation_baseline: list = []
        self._whole_baseline = ""
        self._loop_index = 0
        self._last_export_dir = ""
        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.8)
        self._build_ui()
        self._configure_preferences()
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(lambda value: self.seek_slider.setRange(0, value))
        self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.player.errorOccurred.connect(lambda _error, message: self._report(message))
        if hasattr(runner, "busy_changed"):
            runner.busy_changed.connect(lambda busy: self.setEnabled(not busy))
        self._shortcuts = []
        for sequence, action in (
            ("Ctrl+Z", self.undo),
            ("Ctrl+Y", self.redo),
            ("Ctrl+Shift+Z", self.redo),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(action)
            self._shortcuts.append(shortcut)
        self._render()

    def _button(self, text: str, callback: Callable, layout) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(lambda _checked=False: callback())
        layout.addWidget(button)
        return button

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.heading_bar = QWidget()
        heading = QHBoxLayout(self.heading_bar)
        heading.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel("歌词编辑器")
        self.title.setObjectName("pageTitle")
        heading.addWidget(self.title)
        heading.addStretch()
        self.dirty_label = QLabel("尚未载入工程")
        heading.addWidget(self.dirty_label)
        layout.addWidget(self.heading_bar)

        self.sources_bar = QWidget()
        sources = QHBoxLayout(self.sources_bar)
        sources.setContentsMargins(0, 0, 0, 0)
        self.source_picker = PathPicker(
            filter="歌词与工程 (*.json *.lrc *.yrc *.ass *.srt *.vtt *.txt)"
        )
        self.audio_picker = PathPicker(
            filter="音频或有声视频 (*.wav *.mp3 *.flac *.m4a *.ogg *.mp4 *.mkv);;所有文件 (*)"
        )
        sources.addWidget(QLabel("歌词 / 工程"))
        sources.addWidget(self.source_picker, 2)
        self._button("载入", lambda: self.load_source(self.source_picker.value()), sources)
        sources.addWidget(QLabel("校准音频"))
        sources.addWidget(self.audio_picker, 2)
        self.audio_picker.changed.connect(self._audio_changed)
        layout.addWidget(self.sources_bar)

        self.actions_bar = QWidget()
        actions = QHBoxLayout(self.actions_bar)
        actions.setContentsMargins(0, 0, 0, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("工程名称")
        self.name_edit.textChanged.connect(self._draft_changed)
        actions.addWidget(self.name_edit, 1)
        self.undo_button = self._button("撤销", self.undo, actions)
        self.redo_button = self._button("重做", self.redo, actions)
        self._button("保存并导出全部格式", self._export, actions)
        self._button("交给制作页", self._handoff, actions).setProperty("primary", True)
        self._button("打开导出目录", self._open_export_directory, actions)
        layout.addWidget(self.actions_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        row_actions = QHBoxLayout()
        self._button("上一句", lambda: self.select_line(self._selected - 1), row_actions)
        self._button("下一句", lambda: self.select_line(self._selected + 1), row_actions)
        self._button("隐藏 / 显示", self.toggle_hidden, row_actions)
        self._button("删除", self.delete_line, row_actions)
        left_layout.addLayout(row_actions)
        insertion = QHBoxLayout()
        self._button("上方插入", lambda: self.insert_line(False), insertion)
        self._button("下方插入", lambda: self.insert_line(True), insertion)
        self._button("应用表格修改", self.apply_pending, insertion)
        left_layout.addLayout(insertion)
        self.lines_table = self._table(["序号", "状态", "开始秒", "结束秒", "原文", "翻译"])
        for column, width in enumerate((44, 54, 72, 72)):
            self.lines_table.setColumnWidth(column, width)
        for column in (4, 5):
            self.lines_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch
            )
        self.lines_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.lines_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.lines_table.currentCellChanged.connect(self._table_selection_changed)
        left_layout.addWidget(self.lines_table)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.preview = LyricPreviewWidget()
        self.preview.setMinimumHeight(170)
        right_layout.addWidget(self.preview)
        player_row = QHBoxLayout()
        self.play_button = self._button("播放", self.toggle_playback, player_row)
        self._button("停止", self.stop_playback, player_row)
        self.position_label = QLabel("00:00.00")
        player_row.addWidget(self.position_label)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderMoved.connect(self.player.setPosition)
        player_row.addWidget(self.seek_slider, 1)
        self.speed = QComboBox()
        for value in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
            self.speed.addItem(f"{value:g}×", value)
        self.speed.setCurrentIndex(2)
        self.speed.currentIndexChanged.connect(
            lambda: self.player.setPlaybackRate(float(self.speed.currentData()))
        )
        player_row.addWidget(self.speed)
        self.loop = QCheckBox("循环当前句")
        self.loop.toggled.connect(lambda _: setattr(self, "_loop_index", self._selected))
        player_row.addWidget(self.loop)
        right_layout.addLayout(player_row)

        self.timeline = TimelineWidget()
        self.timeline.lineSelected.connect(self.select_line)
        self.timeline.seekRequested.connect(self.seek)
        self.timeline.boundaryChanged.connect(self._boundary_changed)
        right_layout.addWidget(self.timeline)
        timing_row = QHBoxLayout()
        self.ripple = QCheckBox("句尾延长时联动后续歌词")
        self.ripple.setChecked(True)
        timing_row.addWidget(self.ripple)
        self.snap = QCheckBox("吸附句界")
        self.snap.setChecked(True)
        self.snap.toggled.connect(self._set_snap)
        timing_row.addWidget(self.snap)
        self.zoom = QComboBox()
        for value in (1, 2, 4, 8):
            self.zoom.addItem(f"时间轴 {value:g}×", value)
        self.zoom.setCurrentIndex(0)
        self.zoom.currentIndexChanged.connect(
            lambda: self.timeline.set_zoom(float(self.zoom.currentData()))
        )
        timing_row.addWidget(self.zoom)
        right_layout.addLayout(timing_row)
        follow_row = QHBoxLayout()
        self.follow_playback = QCheckBox("跟随播放头")
        self.follow_playback.setChecked(True)
        follow_row.addWidget(self.follow_playback)
        self.return_playhead_button = self._button(
            "回到播放头", self.return_to_playhead, follow_row
        )
        follow_row.addStretch()
        right_layout.addLayout(follow_row)

        self.details_toggle = QPushButton("精细调整 · 逐词与注音")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setChecked(True)
        self.details_toggle.hide()
        right_layout.addWidget(self.details_toggle)
        self.details_panel = QWidget()
        details_layout = QVBoxLayout(self.details_panel)
        details_layout.setContentsMargins(0, 0, 0, 0)
        self.details_toggle.toggled.connect(self.details_panel.setVisible)
        shift_row = QHBoxLayout()
        self.shift_scope = QComboBox()
        self.shift_scope.addItems(["整首平移", "当前句及之后"])
        shift_row.addWidget(self.shift_scope)
        self.shift_seconds = QDoubleSpinBox()
        self.shift_seconds.setRange(-3600, 3600)
        self.shift_seconds.setDecimals(3)
        self.shift_seconds.setSingleStep(0.1)
        self.shift_seconds.setValue(0.1)
        self.shift_seconds.setSuffix(" 秒")
        shift_row.addWidget(self.shift_seconds)
        self._button("平移", self.shift_timing, shift_row)
        details_layout.addLayout(shift_row)
        nudge_row = QHBoxLayout()
        for label, edge, delta in (
            ("句首 −0.1", "start", -0.1),
            ("句首 +0.1", "start", 0.1),
            ("句尾 −0.1", "end", -0.1),
            ("句尾 +0.1", "end", 0.1),
        ):
            self._button(label, lambda e=edge, d=delta: self.nudge(e, d), nudge_row)
        details_layout.addLayout(nudge_row)
        self.token_timeline = TokenTimelineWidget()
        self.token_timeline.timingChanged.connect(self._tokens_changed)
        self.token_timeline.seekRequested.connect(self.seek)
        details_layout.addWidget(self.token_timeline)

        tabs = QTabWidget()
        token_page = QWidget()
        token_layout = QVBoxLayout(token_page)
        self.tokens_table = self._table(["文本", "开始秒", "结束秒"])
        token_layout.addWidget(self.tokens_table)
        token_actions = QHBoxLayout()
        self._button("增加词块", self.add_token, token_actions)
        self._button("删除选中词块", self.delete_token, token_actions)
        self._button("应用逐词修改", self.apply_pending, token_actions)
        token_layout.addLayout(token_actions)
        tabs.addTab(token_page, "逐词时间与文本")
        pronunciation_page = QWidget()
        pronunciation_layout = QVBoxLayout(pronunciation_page)
        whole = QFormLayout()
        self.whole_pronunciation = QLineEdit()
        self.whole_pronunciation.textChanged.connect(self._draft_changed)
        whole.addRow("整行注音", self.whole_pronunciation)
        pronunciation_layout.addLayout(whole)
        self.pronunciation_table = self._table(["原文片段", "读音", "起始字符", "结束字符"])
        pronunciation_layout.addWidget(self.pronunciation_table)
        pronunciation_actions = QHBoxLayout()
        self._button("增加注音", self.add_pronunciation, pronunciation_actions)
        self._button("删除选中注音", self.delete_pronunciation, pronunciation_actions)
        self._button("应用注音修改", self.apply_pending, pronunciation_actions)
        pronunciation_layout.addLayout(pronunciation_actions)
        tabs.addTab(pronunciation_page, "逐词与整行注音")
        details_layout.addWidget(tabs, 1)
        right_layout.addWidget(self.details_panel, 1)
        right_layout.addStretch()
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setWidget(right)
        splitter.addWidget(right_scroll)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 7)
        splitter.setSizes([450, 720])
        layout.addWidget(splitter, 1)
        self.status = QLabel("载入歌词或已保存工程，使用原生时间轴校准。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def set_workspace_mode(self, enabled: bool = True) -> None:
        """Let a containing workspace own import, save and export controls."""
        for bar in (self.heading_bar, self.sources_bar, self.actions_bar):
            bar.setVisible(not enabled)
        self.details_toggle.setVisible(enabled)
        self.details_toggle.setChecked(not enabled)
        self.details_panel.setVisible(not enabled)
        margin = 0 if enabled else 9
        self.layout().setContentsMargins(margin, margin, margin, margin)

    def _table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setItemDelegate(_DraftDelegate(self, table))
        table.setHorizontalHeaderLabels(headers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        table.cellChanged.connect(self._draft_changed)
        return table

    @staticmethod
    def _values(table: QTableWidget) -> list[list[str]]:
        return [
            [
                table.item(row, column).text() if table.item(row, column) else ""
                for column in range(table.columnCount())
            ]
            for row in range(table.rowCount())
        ]

    def _fill(self, table: QTableWidget, rows: list, *, readonly_id=False) -> None:
        blocked = table.blockSignals(True)
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column, value in enumerate(row):
                item = QTableWidgetItem("" if value is None else str(value))
                item.setToolTip(item.text())
                if readonly_id and column == 0:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(row_index, column, item)
        table.blockSignals(blocked)

    def _render(self) -> None:
        self._rendering = True
        self._fill(self.lines_table, document_to_editor_rows(self._document), readonly_id=True)
        self._line_baseline = self._values(self.lines_table)
        if self._document.lines:
            self._selected = min(max(0, self._selected), len(self._document.lines) - 1)
            self.lines_table.setCurrentCell(self._selected, 0)
        self.preview.set_document(self._document)
        self.timeline.set_document(self._document, self._selected)
        self._render_line()
        self._rendering = False
        self._sync_dirty()

    def _render_line(self) -> None:
        previous_rendering = self._rendering
        self._rendering = True
        line = self._document.lines[self._selected] if self._document.lines else LyricLine("")
        tokens = json.loads(token_timing_to_json(line))
        self._fill(
            self.tokens_table, [[entry["text"], entry["start"], entry["end"]] for entry in tokens]
        )
        self._token_baseline = self._values(self.tokens_table)
        readings = document_pronunciation_to_editor_rows(self._document, line)
        self._fill(self.pronunciation_table, readings)
        self._pronunciation_baseline = self._values(self.pronunciation_table)
        self.whole_pronunciation.setText(line.pronunciation or "")
        self._whole_baseline = self.whole_pronunciation.text()
        self.token_timeline.set_line(line)
        self.preview.set_current_line(self._selected)
        self._rendering = previous_rendering

    def _has_pending(self) -> bool:
        return (
            self._values(self.lines_table) != self._line_baseline
            or self._values(self.tokens_table) != self._token_baseline
            or self._values(self.pronunciation_table) != self._pronunciation_baseline
            or self.whole_pronunciation.text() != self._whole_baseline
        )

    @property
    def is_dirty(self) -> bool:
        # Status notifications must never commit or steal focus from a live
        # delegate editor. Transfer operations explicitly flush their drafts.
        return self._dirty

    def _flush_active_cell(self) -> None:
        if self._flushing_cell:
            return
        focused = QApplication.focusWidget()
        for table in (self.lines_table, self.tokens_table, self.pronunciation_table):
            if focused is not None and focused is not table and table.isAncestorOf(focused):
                self._flushing_cell = True
                try:
                    table.setFocus(Qt.FocusReason.OtherFocusReason)
                finally:
                    self._flushing_cell = False
                break

    def _active_cell_changed(self) -> None:
        self._cell_draft = True
        self._draft_changed()

    def _finish_cell_edit(self) -> None:
        self._cell_draft = False
        self._sync_dirty()

    def _sync_dirty(self) -> None:
        dirty = bool(self._document.lines) and (
            self._cell_draft
            or self._has_pending()
            or self._document.to_dict() != self._saved_document
            or self.audio_picker.value() != self._saved_audio
            or self.name_edit.text() != self._saved_name
        )
        self.dirty_label.setText(
            "● 未保存修改" if dirty else "已保存" if self._document.lines else "尚未载入工程"
        )
        past, future = history_stacks(self._history)
        self.undo_button.setEnabled(bool(past) or self._cell_draft or self._has_pending())
        self.redo_button.setEnabled(bool(future))
        if dirty != self._dirty:
            self._dirty = dirty
            self.changed.emit(dirty)

    def _draft_changed(self, *_args) -> None:
        if not self._rendering:
            self.player.pause()
            self._sync_dirty()

    @staticmethod
    def _number(value: str, label: str) -> float:
        try:
            number = float(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{label}需要填写有效秒数。") from exc
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"{label}必须是大于或等于 0 的有限秒数。")
        return number

    def current_document(self) -> LyricsDocument:
        self._flush_active_cell()
        if not self._document.lines:
            raise ValueError("请先载入歌词工程。")
        document = _copy_document(self._document)
        rows = self._values(self.lines_table)
        tokens = self._values(self.tokens_table)
        readings = self._values(self.pronunciation_table)
        pronunciation_changed = (
            readings != self._pronunciation_baseline
            or self.whole_pronunciation.text() != self._whole_baseline
        )
        if pronunciation_changed:
            document = apply_pronunciation_rows(
                document, self._selected + 1, readings, self.whole_pronunciation.text()
            )
        if rows != self._line_baseline:
            for index, row in enumerate(rows):
                if row[1] not in {"显示", "隐藏"}:
                    raise ValueError(
                        f"第 {index + 1} 行状态应为“显示”或“隐藏”；删除请使用删除按钮。"
                    )
                if row[2] or row[3]:
                    self._number(row[2], f"第 {index + 1} 行开始")
                    self._number(row[3], f"第 {index + 1} 行结束")
            edited = apply_editor_rows(document, rows)
            for index, row in enumerate(rows):
                if row == self._line_baseline[index]:
                    edited.lines[index] = copy.deepcopy(document.lines[index])
            document = edited
        if tokens != self._token_baseline:
            if rows[self._selected][4] != self._line_baseline[self._selected][4]:
                raise ValueError("原文和逐词文本都有修改，请先应用其中一处，再编辑另一处。")
            if not tokens:
                raise ValueError("请保留至少一个词块；删除整句请使用歌词行删除按钮。")
            entries = [
                {
                    "text": row[0],
                    "start": self._number(row[1], f"词块 {index + 1} 开始"),
                    "end": self._number(row[2], f"词块 {index + 1} 结束"),
                }
                for index, row in enumerate(tokens)
            ]
            edited = apply_token_timing(
                document,
                document_to_editor_rows(document),
                self._selected + 1,
                json.dumps(entries, ensure_ascii=False),
            )
            for index, line in enumerate(document.lines):
                if index != self._selected:
                    edited.lines[index] = copy.deepcopy(line)
            for token in edited.lines[self._selected].tokens:
                source = next(
                    (
                        item
                        for item in document.lines[self._selected].tokens
                        if (item.text, item.start, item.end) == (token.text, token.start, token.end)
                    ),
                    None,
                )
                if source is not None:
                    token.confidence = source.confidence
            document = edited
        if self.ripple.isChecked():
            # Account for each earlier extension before applying another edited row.
            shifts = [0.0] * len(document.lines)
            for index, (before, after) in enumerate(zip(self._document.lines, document.lines)):
                if before.end is None or after.end is None:
                    continue
                previous_end = before.end + shifts[index]
                if after.end > previous_end + 1e-9:
                    old_starts = [line.start for line in document.lines]
                    ripple_following_line_timing(document, index + 1, previous_end=previous_end)
                    for later in range(index + 1, len(document.lines)):
                        old, new = old_starts[later], document.lines[later].start
                        if old is not None and new is not None:
                            shifts[later] += new - old
        return document

    def _snapshot(self) -> dict:
        return {
            "document": copy.deepcopy(self._document.to_dict()),
            "source_format": self._document.source_format,
            "selected": self._selected,
        }

    def _replace(self, document: LyricsDocument, selected: int | None = None) -> None:
        if document.to_dict() != self._document.to_dict():
            self._history = record_history(self._history, self._snapshot())
        self._document = _copy_document(document)
        if selected is not None:
            self._selected = selected
        self._render()

    def _commit_pending(self) -> None:
        self._flush_active_cell()
        if self._has_pending():
            self._replace(self.current_document())

    def _guard(self, callback: Callable) -> bool:
        try:
            callback()
            return True
        except (ValueError, TypeError, OSError, IndexError) as exc:
            self._report(str(exc))
            return False

    def _report(self, message: str) -> None:
        self.status.setText(message)
        if hasattr(self.runner, "message"):
            self.runner.message.emit(message)

    def apply_pending(self) -> bool:
        if self._guard(self._commit_pending):
            self._report("已应用修改；保存导出后写入工程。")
            return True
        return False

    def load_document(
        self, document: LyricsDocument, audio: str | None = None, name: str = ""
    ) -> None:
        if not document.lines:
            raise ValueError("歌词工程为空。")
        self.stop_playback()
        self._document = _copy_document(document)
        self._selected = 0
        self._history = {}
        self._rendering = True
        self.name_edit.setText(
            name or document.metadata.get("ti") or document.metadata.get("title") or "歌词工程"
        )
        self.audio_picker.set_value(audio or "")
        self._rendering = False
        self.player.setSource(QUrl.fromLocalFile(str(Path(audio).resolve())) if audio else QUrl())
        self._saved_document = copy.deepcopy(self._document.to_dict())
        self._saved_audio = self.audio_picker.value()
        self._saved_name = self.name_edit.text()
        self._render()
        self._report(f"已载入 {self.name_edit.text()}，共 {len(document.lines)} 行。")

    def load_source(self, path: str, *, confirm_replace: bool = True) -> None:
        if not path:
            self._report("请选择歌词或工程文件。")
            return
        if confirm_replace and self.is_dirty:
            choice = QMessageBox.question(
                self,
                "保留当前修改",
                "当前工程有未保存修改。是否先保存再载入？",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if choice == QMessageBox.StandardButton.Cancel:
                return
            if choice == QMessageBox.StandardButton.Save:
                self._export(after_saved=lambda: self.load_source(path, confirm_replace=False))
                return

        def load():
            source = Path(path)
            workspace = None
            if source.suffix.lower() == ".json":
                candidate = json.loads(source.read_text(encoding="utf-8-sig"))
                if (
                    isinstance(candidate, dict)
                    and candidate.get("schema_version") == 1
                    and candidate.get("lyrics_project")
                ):
                    workspace = load_workspace_project(source)
                    source = workspace.lyrics_project
            document = read_workspace_lyrics(workspace) if workspace else read_lyrics(source)
            if workspace is None and (manifest := document.metadata.get("workspace_manifest")):
                manifest_path = Path(manifest)
                if not manifest_path.is_absolute():
                    manifest_path = source.parent / manifest_path
                try:
                    workspace = load_workspace_project(manifest_path)
                except (OSError, TypeError, ValueError):
                    pass
                else:
                    if source.resolve() == workspace.lyrics_project.resolve():
                        document = read_workspace_lyrics(workspace)
            audio = self.audio_picker.value()
            name = source.stem
            if workspace:
                document.metadata["workspace_manifest"] = str(workspace.manifest)
                audio = str(workspace.audio or workspace.video or "")
                name = workspace.name
            self.load_document(document, audio or None, name)
            self.source_picker.set_value(path)
            if warning := document.metadata.get("legacy_timing_warning"):
                self._report(warning)

        self._guard(load)

    def adopt_saved_revision(self, document: LyricsDocument) -> bool:
        """Accept a saved manifest location without clearing selection or undo history."""
        self._commit_pending()
        current = copy.deepcopy(self.current_document().to_dict())
        saved = copy.deepcopy(document.to_dict())
        for payload in (current, saved):
            payload.get("metadata", {}).pop("workspace_manifest", None)
        if current != saved:
            return False
        self._document.metadata = copy.deepcopy(document.metadata)
        self.mark_saved()
        return True

    def mark_saved(self) -> None:
        self._commit_pending()
        self._saved_document = copy.deepcopy(self._document.to_dict())
        self._saved_audio = self.audio_picker.value()
        self._saved_name = self.name_edit.text()
        self._sync_dirty()

    def select_line(self, index: int, *, seek: bool = True) -> None:
        if self._rendering or not self._document.lines:
            return
        index = min(max(0, index), len(self._document.lines) - 1)
        if not self._guard(self._commit_pending):
            self.lines_table.blockSignals(True)
            self.lines_table.setCurrentCell(self._selected, 0)
            self.lines_table.blockSignals(False)
            return
        self._selected = index
        self._loop_index = index
        self._rendering = True
        self.lines_table.setCurrentCell(index, 0)
        self._rendering = False
        self._render_line()
        self.timeline.set_document(self._document, index)
        if seek and self._document.lines[index].start is not None:
            self.seek(self._document.lines[index].start)

    def _table_selection_changed(self, row, _column, _old_row, _old_column) -> None:
        if row >= 0 and not self._rendering and row != self._selected:
            self.select_line(row)

    def undo(self) -> None:
        self._flush_active_cell()
        if self._has_pending():
            try:
                self._commit_pending()
            except (ValueError, TypeError):
                self._render()
                self._report("已撤销尚未应用的表格草稿。")
                return
        restored, history = travel_history(self._history, self._snapshot())
        if restored is not None:
            self.stop_playback()
            self._history = history
            self._document = _history_document(restored)
            self._selected = restored["selected"]
            self._render()

    def redo(self) -> None:
        if not self._guard(self._commit_pending):
            return
        restored, history = travel_history(self._history, self._snapshot(), redo=True)
        if restored is not None:
            self.stop_playback()
            self._history = history
            self._document = _history_document(restored)
            self._selected = restored["selected"]
            self._render()

    def _mutate(self, operation: Callable[[LyricsDocument], LyricsDocument], selected=None) -> None:
        def apply():
            self._commit_pending()
            self.stop_playback()
            document = self.current_document()
            self._replace(operation(document), selected)

        self._guard(apply)

    def toggle_hidden(self) -> None:
        def toggle(document):
            document.lines[self._selected].hidden = not document.lines[self._selected].hidden
            return document

        self._mutate(toggle)

    def delete_line(self) -> None:
        def delete(document):
            if len(document.lines) <= 1:
                raise ValueError("至少保留一行歌词；可使用隐藏功能。")
            document.lines.pop(self._selected)
            return document

        self._mutate(delete)

    def insert_line(self, after: bool = True) -> None:
        selected = self._selected + int(after)

        def insert(document):
            current = document.lines[self._selected]
            if after:
                start = current.end or current.start or 0.0
                following = (
                    document.lines[selected].start if selected < len(document.lines) else None
                )
                end = following if following is not None and following > start else start + 2.0
            else:
                end = current.start or 0.0
                previous = document.lines[self._selected - 1].end if self._selected else 0.0
                start = max(0.0, previous or end - 2.0)
                if end <= start:
                    start, end = max(0.0, end - 0.5), max(0.5, end)
            document.lines.insert(selected, LyricLine("新歌词", start, end))
            return document

        self._mutate(insert, selected)

    def nudge(self, edge: str, delta: float) -> None:
        def change(document):
            original = document.lines[self._selected]
            previous_end = original.end
            validated = nudge_editor_line_timing(
                document,
                document_to_editor_rows(document),
                self._selected + 1,
                start_delta=delta if edge == "start" else 0,
                end_delta=delta if edge == "end" else 0,
                ripple_following=False,
            )
            updated = validated.lines[self._selected]
            original.start, original.end = updated.start, updated.end
            if original.tokens:
                original.tokens[0].start = updated.start
                original.tokens[-1].end = updated.end
            document.metadata["word_timing"] = "manual"
            if self.ripple.isChecked() and previous_end is not None:
                ripple_following_line_timing(
                    document, self._selected + 1, previous_end=previous_end
                )
            return document

        self._mutate(change)

    def shift_timing(self) -> None:
        def shift(document):
            start_line = self._selected + 1 if self.shift_scope.currentIndex() else 1
            _validated, applied = shift_editor_timeline(
                document,
                document_to_editor_rows(document),
                self.shift_seconds.value(),
                start_line=start_line,
            )
            for line in document.lines[start_line - 1 :]:
                if line.start is not None and line.end is not None:
                    line.start += applied
                    line.end += applied
                    for token in line.tokens:
                        token.start += applied
                        token.end += applied
            if applied:
                document.metadata["word_timing"] = "manual"
            return document

        self._mutate(shift)

    def _boundary_changed(self, index: int, edge: str, seconds: float) -> None:
        if not self._guard(self._commit_pending):
            return
        self.select_line(index, seek=False)
        current = getattr(self._document.lines[self._selected], edge, None)
        if current is not None:
            self.nudge(edge, seconds - current)

    def _tokens_changed(self, entries: list[dict]) -> None:
        self._fill(
            self.tokens_table, [[item["text"], item["start"], item["end"]] for item in entries]
        )
        self._draft_changed()
        if not self.apply_pending():
            self.token_timeline.set_line(self._document.lines[self._selected])

    def add_token(self) -> None:
        rows = self._values(self.tokens_table)
        try:
            start = float(rows[-1][2]) if rows else 0.0
        except ValueError:
            self._report("请先修正现有词块时间。")
            return
        rows.append(["新词", start, start + 0.5])
        self._fill(self.tokens_table, rows)
        self._draft_changed()

    def delete_token(self) -> None:
        row = self.tokens_table.currentRow()
        if self.tokens_table.rowCount() <= 1:
            self._report("至少保留一个词块；删除整句请使用歌词行删除按钮。")
        elif row >= 0:
            self.tokens_table.removeRow(row)
            self._draft_changed()

    def add_pronunciation(self) -> None:
        rows = self._values(self.pronunciation_table)
        rows.append(["", "", "0", "1"])
        self._fill(self.pronunciation_table, rows)
        self._draft_changed()

    def delete_pronunciation(self) -> None:
        row = self.pronunciation_table.currentRow()
        if row >= 0:
            self.pronunciation_table.removeRow(row)
            self._draft_changed()

    def _set_snap(self, enabled: bool) -> None:
        if hasattr(self.timeline, "set_snap_enabled"):
            self.timeline.set_snap_enabled(enabled)
        if hasattr(self.token_timeline, "set_snap_enabled"):
            self.token_timeline.set_snap_enabled(enabled)

    def _configure_preferences(self) -> None:
        """Restore first, then attach writes so opening a page never rewrites settings."""
        preferences = load_preferences()
        rate = float(preferences.get("playback_rate", 1.0))
        rate_index = self.speed.findData(rate)
        if rate_index < 0:
            self.speed.addItem(f"{rate:g}×", rate)
            rate_index = self.speed.count() - 1
        self.speed.setCurrentIndex(rate_index)
        self.ripple.setChecked(bool(preferences.get("ripple_following", True)))
        self.snap.setChecked(bool(preferences.get("snap_enabled", True)))
        self.follow_playback.setChecked(bool(preferences.get("follow_playback", True)))
        zoom = max(1.0, float(preferences.get("global_zoom", 1.0)))
        zoom_index = self.zoom.findData(zoom)
        if zoom_index < 0:
            self.zoom.addItem(f"时间轴 {zoom:g}×", zoom)
            zoom_index = self.zoom.count() - 1
        self.zoom.setCurrentIndex(zoom_index)
        self.speed.currentIndexChanged.connect(
            lambda _: self._persist_preference("playback_rate", float(self.speed.currentData()))
        )
        self.ripple.toggled.connect(
            lambda value: self._persist_preference("ripple_following", value)
        )
        self.snap.toggled.connect(lambda value: self._persist_preference("snap_enabled", value))
        self.follow_playback.toggled.connect(
            lambda value: self._persist_preference("follow_playback", value)
        )
        self.zoom.currentIndexChanged.connect(
            lambda _: self._persist_preference("global_zoom", float(self.zoom.currentData()))
        )

    def _persist_preference(self, key: str, value: object) -> None:
        try:
            save_preferences({key: value})
        except OSError as exc:
            self._report(f"设置已生效，但暂时无法保存到本机：{exc}")

    def return_to_playhead(self) -> None:
        seconds = self.player.position() / 1000
        self.timeline.reveal_position(seconds, center=True)
        self.token_timeline.reveal_position(seconds, center=True)

    def _audio_changed(self, value: str) -> None:
        if self._rendering:
            return
        self.stop_playback()
        self.player.setSource(QUrl.fromLocalFile(str(Path(value).resolve())) if value else QUrl())
        self._sync_dirty()

    def seek(self, seconds: float) -> None:
        self.player.setPosition(round(max(0, seconds) * 1000))
        self.preview.set_position(seconds)
        self.timeline.set_position(seconds)
        self.token_timeline.set_position(seconds)

    def toggle_playback(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            return
        if not self._guard(self._commit_pending):
            return
        if self.player.source().isEmpty():
            self._report("请先选择校准音频或有声视频。")
            return
        self._loop_index = self._selected
        if self.loop.isChecked() and self._document.lines:
            line = self._document.lines[self._selected]
            if line.start is not None and line.end is not None:
                current = self.player.position() / 1000
                if not line.start <= current < line.end:
                    self.seek(line.start)
        self.player.play()

    def stop_playback(self) -> None:
        self.player.stop()

    def _playback_state_changed(self, state) -> None:
        self.play_button.setText(
            "暂停" if state == QMediaPlayer.PlaybackState.PlayingState else "播放"
        )

    def _position_changed(self, milliseconds: int) -> None:
        seconds = milliseconds / 1000
        self.position_label.setText(f"{int(seconds // 60):02}:{seconds % 60:05.2f}")
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(milliseconds)
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        if playing and self.loop.isChecked() and self._document.lines:
            line = self._document.lines[min(self._loop_index, len(self._document.lines) - 1)]
            if line.end is not None and line.start is not None and seconds >= line.end:
                self.player.setPosition(round(line.start * 1000))
                return
        if playing and not self.loop.isChecked() and not self._has_pending():
            active = next(
                (
                    index
                    for index, line in enumerate(self._document.lines)
                    if not line.hidden
                    and line.start is not None
                    and line.end is not None
                    and line.start <= seconds < line.end
                ),
                None,
            )
            if active is not None and active != self._selected:
                self.select_line(active, seek=False)
        self.preview.set_position(seconds)
        self.timeline.set_position(seconds)
        self.token_timeline.set_position(seconds)
        if playing and self.follow_playback.isChecked():
            self.timeline.reveal_position(seconds)
            self.token_timeline.reveal_position(seconds)

    def export_to(self, directory: str) -> list[str]:
        """Synchronous export for tests/automation; the interactive UI uses a worker."""
        self._commit_pending()
        document, files = _write_exports(
            self.current_document(), directory, self.name_edit.text(), self.audio_picker.value()
        )
        self._document = document
        self._last_export_dir = directory
        self._render()
        self.mark_saved()
        return files

    def export_dialog(self) -> bool:
        """Choose a destination and submit a background save, returning acceptance."""
        return self._export()

    def _export(self, *, after_saved: Callable | None = None) -> bool:
        if not self._guard(self._commit_pending) or not self._document.lines:
            return False
        directory = QFileDialog.getExistingDirectory(
            self, "选择保存工程和导出歌词的目录", self._last_export_dir
        )
        if not directory:
            return False
        self.stop_playback()
        source = self.current_document()
        audio, name = self.audio_picker.value(), self.name_edit.text()

        def task(log):
            log("正在保存工程与歌词格式……")
            return _write_exports(source, directory, name, audio)

        def saved(result):
            document, files = result
            matches_export = False
            try:
                matches_export = (
                    self.name_edit.text() == name
                    and self.audio_picker.value() == audio
                    and self.current_document().to_dict() == source.to_dict()
                )
            except (ValueError, TypeError, IndexError):
                # A new, invalid form draft still belongs to the user. The
                # background job saved its earlier snapshot, not this draft.
                pass
            if matches_export:
                self._document = document
                self._render()
                self.mark_saved()
            self._last_export_dir = directory
            message = f"已保存 {len(files)} 个文件至 {directory}"
            if not matches_export:
                self._sync_dirty()
                message += "；已导出开始保存时的版本，之后的修改仍保留在编辑器中，尚未保存。"
            self._report(message)
            if after_saved and matches_export and not self.is_dirty:
                after_saved()

        return self.runner.submit("保存歌词工程", task, saved)

    def _handoff(self) -> None:
        def send():
            self._commit_pending()
            self.stop_playback()
            self.handoff.emit(self.current_document())
            self._report("已将当前歌词交给制作页，修改仍保留在编辑器中。")

        self._guard(send)

    def _open_export_directory(self) -> None:
        if self._last_export_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_export_dir))
        else:
            self._report("保存导出后可打开输出目录。")
