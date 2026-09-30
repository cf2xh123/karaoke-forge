"""Native, paint-based karaoke timelines and lyric preview widgets.

All timing values exposed by these widgets are absolute seconds. Editing is
transactional: dragging paints a provisional value; only release emits a change.
"""

from __future__ import annotations

import math
from dataclasses import fields
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTextLayout,
    QTextOption,
    QTransform,
)
from PySide6.QtWidgets import QScrollBar, QWidget

from ..ass import (
    AssStyle,
    _countdown_position,
    _estimated_display_end,
    _inactive_display_windows,
    _karaoke_upper_margin,
    _line_pronunciation,
    _pronunciation_clearance,
    _pronunciation_source_timing,
    document_auto_pronunciation,
)
from ..models import LyricLine, LyricsDocument
from ..text import split_edge_whitespace

MIN_TOKEN = 0.01


def _finite(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError):
        return fallback
    return number if math.isfinite(number) else fallback


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), max(low, high))


def _progress(start: float, end: float, position: float) -> float:
    return _clamp((position - start) / max(end - start, MIN_TOKEN), 0.0, 1.0)


def _clock(seconds: float) -> str:
    minutes, remainder = divmod(max(0.0, seconds), 60)
    return f"{int(minutes)}:{remainder:04.1f}"


class _ScrollableTimeline(QWidget):
    """Timeline canvas with its own horizontal scroll bar and anchored zoom."""

    seekRequested = Signal(float)
    LEFT = 16.0
    TOP = 40.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._start = 0.0
        self._duration = 10.0
        self._position = 0.0
        self._zoom = 1.0
        self.snap_enabled = True
        self._scroll = QScrollBar(Qt.Orientation.Horizontal, self)
        self._scroll.valueChanged.connect(lambda _: self.update())
        self.setMinimumSize(160, 176)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("歌词时间轴")
        self.setToolTip("点击跳转；拖动边缘调整时间；Ctrl + 滚轮缩放；滚轮横向滚动")
        self._update_scroll()

    @property
    def pixels_per_second(self) -> float:
        return max(1.0, self.width() - 2 * self.LEFT) * self._zoom / self._duration

    def time_to_x(self, seconds: float) -> float:
        return self.LEFT + (seconds - self._start) * self.pixels_per_second - self._scroll.value()

    def x_to_time(self, x: float) -> float:
        seconds = self._start + (x - self.LEFT + self._scroll.value()) / self.pixels_per_second
        return _clamp(seconds, self._start, self._start + self._duration)

    def set_position(self, seconds: float) -> None:
        self._position = max(0.0, _finite(seconds))
        self.update()

    def set_zoom(self, zoom: float) -> None:
        self._zoom_at(_finite(zoom, 1.0), self.width() / 2)

    def set_snap_enabled(self, enabled: bool) -> None:
        self.snap_enabled = bool(enabled)

    def reveal_position(self, seconds: float | None = None, *, center: bool = False) -> None:
        """Reveal the playhead without seeking, changing selection or zooming."""
        if getattr(self, "_drag", None):
            return
        value = self._position if seconds is None else _finite(seconds, self._position)
        value = _clamp(value, self._start, self._start + self._duration)
        x = self.time_to_x(value)
        if center or x < self.LEFT or x > self.width() - self.LEFT:
            self._scroll.setValue(round(self._scroll.value() + x - self.width() / 2))

    def _zoom_at(self, zoom: float, x: float) -> None:
        anchor = self.x_to_time(x)
        self._zoom = _clamp(zoom, 1.0, 64.0)
        self._update_scroll()
        offset = self.LEFT + (anchor - self._start) * self.pixels_per_second - x
        self._scroll.setValue(round(offset))
        self.update()

    def _update_scroll(self) -> None:
        width = max(1, self.width() - int(2 * self.LEFT))
        self._scroll.setGeometry(0, self.height() - 16, self.width(), 16)
        self._scroll.setPageStep(width)
        self._scroll.setRange(0, max(0, round(width * (self._zoom - 1))))
        self._scroll.setSingleStep(40)
        self._scroll.setVisible(self._zoom > 1.0)

    def resizeEvent(self, event: Any) -> None:
        self._update_scroll()
        super().resizeEvent(event)

    def wheelEvent(self, event: Any) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120
            self._zoom_at(self._zoom * (1.25**steps), event.position().x())
        else:
            delta = event.pixelDelta().x() or event.pixelDelta().y()
            if not delta:
                delta = (event.angleDelta().x() or event.angleDelta().y()) / 3
            self._scroll.setValue(self._scroll.value() - round(delta))
        event.accept()

    def _paint_base(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#f5f8fb"))
        painter.fillRect(QRectF(0, 0, self.width(), self.TOP - 5), QColor("#edf2f7"))
        minimum_step = 70 / self.pixels_per_second
        scale = 10 ** math.floor(math.log10(max(minimum_step, 0.01)))
        step = next(
            (factor * scale for factor in (1, 2, 5, 10) if factor * scale >= minimum_step),
            10 * scale,
        )
        first = math.floor(self.x_to_time(0) / step) * step
        last = self.x_to_time(self.width())
        font = QFont(self.font())
        font.setPointSizeF(9)
        painter.setFont(font)
        tick = first
        while tick <= last + step / 2:
            x = self.time_to_x(tick)
            painter.setPen(QColor("#dce4ee"))
            painter.drawLine(QPointF(x, self.TOP - 8), QPointF(x, self.height() - 17))
            painter.setPen(QColor("#66758a"))
            painter.drawText(QRectF(x + 4, 6, 85, 20), _clock(tick))
            tick += step

    def _paint_playhead(self, painter: QPainter) -> None:
        x = self.time_to_x(self._position)
        if 0 <= x <= self.width():
            painter.setPen(QPen(QColor("#dc4b42"), 2))
            painter.drawLine(QPointF(x, self.TOP - 7), QPointF(x, self.height() - 17))
            painter.setBrush(QColor("#dc4b42"))
            painter.drawEllipse(QPointF(x, self.TOP - 9), 4, 4)

    def _empty(self, painter: QPainter, message: str) -> None:
        painter.setPen(QColor("#677589"))
        painter.drawText(
            QRectF(14, 45, self.width() - 28, self.height() - 65),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            message,
        )


class TimelineWidget(_ScrollableTimeline):
    lineSelected = Signal(int)
    boundaryChanged = Signal(int, str, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._document = LyricsDocument([])
        self._selected = 0
        self._lanes: dict[int, int] = {}
        self._drag: tuple[int, str, float] | None = None
        self._drag_original = 0.0
        self._drag_origin_x = 0.0

    def set_document(self, doc: LyricsDocument, selected: int = 0) -> None:
        self._document = doc
        self._selected = max(0, min(int(selected), len(doc.lines) - 1))
        timed = [
            (index, line)
            for index, line in enumerate(doc.lines)
            if line.is_timed and not line.hidden
        ]
        self._duration = max(10.0, max((_finite(line.end) for _, line in timed), default=0) + 1)
        lane_ends: list[float] = []
        self._lanes = {}
        for index, line in sorted(timed, key=lambda item: _finite(item[1].start)):
            start = max(0.0, _finite(line.start))
            lane = next((i for i, end in enumerate(lane_ends) if end <= start), len(lane_ends))
            if lane == len(lane_ends):
                lane_ends.append(0.0)
            lane_ends[lane] = _finite(line.end)
            self._lanes[index] = lane
        self.setMinimumHeight(max(166, 68 + len(lane_ends) * 46))
        self._update_scroll()
        self.update()

    def line_rect(self, index: int) -> QRectF:
        if index not in self._lanes:
            return QRectF()
        line = self._document.lines[index]
        start, end = _finite(line.start), _finite(line.end)
        if self._drag and self._drag[0] == index:
            if self._drag[1] == "start":
                start = self._drag[2]
            else:
                end = self._drag[2]
        x = self.time_to_x(start)
        return QRectF(x, self.TOP + self._lanes[index] * 46, max(3.0, self.time_to_x(end) - x), 34)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        self._paint_base(painter)
        if not self._lanes:
            self._empty(painter, "载入带时间轴的歌词后，可在这里选句、跳转和拖动句子边缘。")
        for index in self._lanes:
            rect = self.line_rect(index)
            if not rect.intersects(QRectF(self.rect())):
                continue
            selected = index == self._selected
            color = "#0b6671" if selected else "#41778d"
            painter.setBrush(QColor(color))
            painter.setPen(QPen(QColor("#eba23a" if selected else "#34677d"), 2 if selected else 1))
            painter.drawRoundedRect(rect, 5, 5)
            painter.save()
            painter.setClipRect(rect.adjusted(6, 0, -6, 0))
            painter.setPen(QColor("#ffffff"))
            painter.drawText(
                rect.adjusted(8, 0, -8, 0),
                Qt.AlignmentFlag.AlignVCenter,
                f"{index + 1}  {self._document.lines[index].text}",
            )
            painter.restore()
            painter.setPen(QPen(QColor("#e4f4f5"), 2))
            for x in (rect.left() + 3, rect.right() - 3):
                painter.drawLine(QPointF(x, rect.top() + 10), QPointF(x, rect.bottom() - 10))
        self._paint_playhead(painter)
        painter.end()

    def _hit(self, point: QPointF) -> tuple[int, str | None] | None:
        indices = sorted(self._lanes, key=lambda index: index == self._selected, reverse=True)
        for index in indices:
            rect = self.line_rect(index)
            if rect.adjusted(-5, 0, 5, 0).contains(point):
                if abs(point.x() - rect.left()) <= 6:
                    return index, "start"
                if abs(point.x() - rect.right()) <= 6:
                    return index, "end"
                return index, None
        return None

    def mousePressEvent(self, event: Any) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        hit = self._hit(event.position())
        if hit:
            index, edge = hit
            self._selected = index
            if edge:
                value = _finite(getattr(self._document.lines[index], edge))
                self._drag = (index, edge, value)
                self._drag_original = value
                self._drag_origin_x = event.position().x()
            self.lineSelected.emit(index)
            if edge is None:
                self.seekRequested.emit(self.x_to_time(event.position().x()))
        else:
            self.seekRequested.emit(self.x_to_time(event.position().x()))
        self.update()
        event.accept()

    def _drag_to(self, x: float) -> None:
        if not self._drag:
            return
        index, edge, _ = self._drag
        line = self._document.lines[index]
        if abs(x - self._drag_origin_x) < 0.5:
            self._drag = (index, edge, self._drag_original)
            self.update()
            return
        value = self._drag_original + (x - self._drag_origin_x) / self.pixels_per_second
        if self.snap_enabled:
            candidates = [self._position]
            candidates += [
                max(0.0, _finite(getattr(other, boundary)))
                for i, other in enumerate(self._document.lines)
                if i != index and other.is_timed and not other.hidden
                for boundary in ("start", "end")
            ]
            nearest = min(candidates, key=lambda candidate: abs(candidate - value))
            if abs(nearest - value) * self.pixels_per_second <= 7:
                value = nearest
        if edge == "start":
            value = _clamp(value, 0.0, _finite(line.end) - MIN_TOKEN)
        else:
            value = _clamp(value, _finite(line.start) + MIN_TOKEN, self._duration)
        self._drag = (index, edge, round(value, 3))
        self.update()

    def mouseMoveEvent(self, event: Any) -> None:
        if self._drag:
            self._drag_to(event.position().x())
        else:
            hit = self._hit(event.position())
            self.setCursor(
                Qt.CursorShape.SizeHorCursor
                if hit and hit[1]
                else Qt.CursorShape.PointingHandCursor
            )
        event.accept()

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag:
            self._drag_to(event.position().x())
            index, edge, value = self._drag
            self._drag = None
            if abs(value - self._drag_original) >= 0.0005:
                self.boundaryChanged.emit(index, edge, value)
            self.update()
        event.accept()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Escape and self._drag:
            self._drag = None
            self.update()
        else:
            super().keyPressEvent(event)


class TokenTimelineWidget(_ScrollableTimeline):
    timingChanged = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._line: LyricLine | None = None
        self._tokens: list[dict[str, Any]] = []
        self._drag: tuple[int, str] | None = None
        self._before_drag: list[dict[str, Any]] = []
        self._drag_origin_x = 0.0
        self.setMinimumHeight(150)
        self.setAccessibleName("逐词时间轴")

    def set_line(self, line: LyricLine | None) -> None:
        self._line = line
        self._drag = None
        self._tokens = (
            [{"text": token.text, "start": token.start, "end": token.end} for token in line.tokens]
            if line
            else []
        )
        starts = [token["start"] for token in self._tokens]
        ends = [token["end"] for token in self._tokens]
        self._start = max(0.0, _finite(line.start, min(starts, default=0))) if line else 0.0
        self._end = (
            max(self._start + MIN_TOKEN, _finite(line.end, max(ends, default=self._start + 5)))
            if line
            else 5.0
        )
        self._duration = max(0.1, self._end - self._start)
        self._update_scroll()
        self.update()

    def token_rect(self, index: int) -> QRectF:
        token = self._tokens[index]
        left = self.time_to_x(token["start"])
        return QRectF(left, self.TOP + 5, max(3.0, self.time_to_x(token["end"]) - left), 62)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        self._paint_base(painter)
        if not self._tokens:
            self._empty(painter, "选中一句带逐词时间的歌词，即可拖动词块边缘精修。")
        for index, token in enumerate(self._tokens):
            rect = self.token_rect(index)
            if not rect.intersects(QRectF(self.rect())):
                continue
            active = token["start"] <= self._position < token["end"]
            painter.setBrush(QColor("#126c77" if active else "#325b77"))
            painter.setPen(QPen(QColor("#edac43" if active else "#7796ad"), 2 if active else 1))
            painter.drawRoundedRect(rect.adjusted(1, 0, -1, 0), 5, 5)
            painter.save()
            painter.setClipRect(rect.adjusted(4, 3, -4, -3))
            painter.setPen(QColor("#ffffff"))
            painter.drawText(
                rect.adjusted(4, 6, -4, -26), Qt.AlignmentFlag.AlignCenter, token["text"]
            )
            painter.setPen(QColor("#d0e6ed"))
            painter.drawText(
                rect.adjusted(4, 32, -4, -3),
                Qt.AlignmentFlag.AlignCenter,
                f"{token['start']:.2f}–{token['end']:.2f}",
            )
            painter.restore()
        self._paint_playhead(painter)
        painter.end()

    def _edge_at(self, point: QPointF) -> tuple[int, str] | None:
        candidates = []
        for index in range(len(self._tokens)):
            rect = self.token_rect(index)
            if not rect.top() <= point.y() <= rect.bottom():
                continue
            for edge, x in (("start", rect.left()), ("end", rect.right())):
                if abs(x - point.x()) <= 7:
                    candidates.append((abs(x - point.x()), index, edge))
        if candidates:
            _, index, edge = min(candidates)
            return index, edge
        return None

    def mousePressEvent(self, event: Any) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._drag = self._edge_at(event.position())
        if self._drag:
            self._before_drag = [dict(token) for token in self._tokens]
            self._drag_origin_x = event.position().x()
        else:
            self.seekRequested.emit(self.x_to_time(event.position().x()))
        event.accept()

    def _drag_to(self, x: float) -> None:
        if not self._drag:
            return
        index, edge = self._drag
        if abs(x - self._drag_origin_x) < 0.5:
            self._tokens = [dict(token) for token in self._before_drag]
            self.update()
            return
        token = self._tokens[index]
        value = self._before_drag[index][edge] + (x - self._drag_origin_x) / self.pixels_per_second
        neighbor: tuple[int, str] | None = None
        if edge == "start":
            low = self._tokens[index - 1]["end"] if index else self._start
            high = token["end"] - MIN_TOKEN
            if (
                index
                and abs(self._before_drag[index - 1]["end"] - self._before_drag[index]["start"])
                < 0.001
            ):
                neighbor = (index - 1, "end")
                low = self._tokens[index - 1]["start"] + MIN_TOKEN
        else:
            low = token["start"] + MIN_TOKEN
            high = self._tokens[index + 1]["start"] if index + 1 < len(self._tokens) else self._end
            if (
                index + 1 < len(self._tokens)
                and abs(self._before_drag[index]["end"] - self._before_drag[index + 1]["start"])
                < 0.001
            ):
                neighbor = (index + 1, "start")
                high = self._tokens[index + 1]["end"] - MIN_TOKEN
        if self.snap_enabled:
            for candidate in (low, high, self._position):
                if abs(candidate - value) * self.pixels_per_second <= 6:
                    value = candidate
                    break
        token[edge] = round(_clamp(value, low, high), 3)
        if neighbor:
            self._tokens[neighbor[0]][neighbor[1]] = token[edge]
        self.update()

    def mouseMoveEvent(self, event: Any) -> None:
        if self._drag:
            self._drag_to(event.position().x())
        else:
            self.setCursor(
                Qt.CursorShape.SizeHorCursor
                if self._edge_at(event.position())
                else Qt.CursorShape.PointingHandCursor
            )
        event.accept()

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag:
            self._drag_to(event.position().x())
            self._drag = None
            if self._tokens != self._before_drag:
                self.timingChanged.emit([dict(token) for token in self._tokens])
            self.update()
        event.accept()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Escape and self._drag:
            self._tokens = self._before_drag
            self._drag = None
            self.update()
        else:
            super().keyPressEvent(event)


class LyricPreviewWidget(QWidget):
    """Paint the ASS rows and display windows in a scaled video coordinate space."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._document = LyricsDocument([])
        self._current = 0
        self._position = 0.0
        self._style: dict[str, Any] = {}
        self._ass_style = AssStyle()
        self._background = QPixmap()
        self._manual_selection = True
        self._indices: list[int] = []
        self._render_ends: dict[int, float] = {}
        self._waiting: dict[int, list[tuple[float, float]]] = {}
        self._cues: dict[int, tuple[float, float]] = {}
        self._pronunciations: dict[int, Any] = {}
        self._pronunciation_cache: dict[tuple, Any] = {}
        self.setMinimumSize(200, 170)
        self.setAccessibleName("卡拉 OK 成片字幕预览")

    def set_document(self, doc: LyricsDocument) -> None:
        self._document = doc
        self._current = max(0, min(self._current, len(doc.lines) - 1))
        self._manual_selection = True
        self._rebuild_display()
        self.update()

    def set_current_line(self, index: int) -> None:
        self._current = max(0, min(int(index), len(self._document.lines) - 1))
        self._manual_selection = True
        self.update()

    def set_position(self, sec: float) -> None:
        self._position = max(0.0, _finite(sec))
        self._manual_selection = False
        active = [
            index
            for index in self._indices
            if self._document.lines[index].is_timed
            and _finite(self._document.lines[index].start)
            <= self._position
            < self._render_ends[index]
        ]
        if active and self._current not in active:
            self._current = active[0]
        self.update()

    def set_style(self, style: dict[str, Any]) -> None:
        self._style = dict(style)
        values = {
            field.name: style[field.name] for field in fields(AssStyle) if field.name in style
        }
        for key, alias in (("highlight_color", "primary_color"), ("text_color", "secondary_color")):
            if key not in values and alias in style:
                values[key] = style[alias]
        resolution = values.get("resolution", (1920, 1080))
        if not isinstance(resolution, (tuple, list)) or len(resolution) != 2:
            resolution = (1920, 1080)
        values["resolution"] = tuple(
            max(1, round(_finite(value, fallback)))
            for value, fallback in zip(resolution, (1920, 1080))
        )
        resolved = AssStyle(**values)
        if resolved != self._ass_style:
            self._ass_style = resolved
            self._rebuild_display()
        self.update()

    def _rebuild_display(self) -> None:
        style = self._ass_style
        # Blank interlude markers and hidden rows never consume a KTV row in ASS.
        self._indices = [
            index
            for index, line in enumerate(self._document.lines)
            if not line.hidden and line.text.strip()
        ]
        timed_indices = [index for index in self._indices if self._document.lines[index].is_timed]
        lines = [self._document.lines[index] for index in timed_indices]
        gap = max(1.0, float(style.countdown_gap_threshold))
        lead_in = max(0.5, float(style.countdown_lead_in))
        ends = [
            _estimated_display_end(line, lines[index + 1] if index + 1 < len(lines) else None, gap)
            for index, line in enumerate(lines)
        ]
        for index, line in enumerate(lines):
            if index + 2 < len(lines):
                ends[index] = max(
                    float(line.start), min(ends[index], float(lines[index + 2].start))
                )
        breaks = {}
        preceding_end = 0.0
        for index, line in enumerate(lines):
            if float(line.start) - preceding_end >= gap:
                breaks[index] = (preceding_end, float(line.start))
            preceding_end = max(preceding_end, ends[index])
        self._render_ends = dict(zip(timed_indices, ends))
        self._waiting = {
            original: _inactive_display_windows(lines, ends, breaks, index, lead_in)
            for index, original in enumerate(timed_indices)
        }
        self._cues = {
            timed_indices[index]: (max(gap_start, start - lead_in), start)
            for index, (gap_start, start) in breaks.items()
        }
        generated = document_auto_pronunciation(self._document, style.auto_pronunciation)
        cache = {}
        self._pronunciations = {}
        for index in self._indices:
            line = self._document.lines[index]
            key = (
                line.text,
                line.pronunciation,
                tuple(
                    (unit.source, unit.reading, unit.start, unit.end)
                    for unit in line.pronunciation_units
                ),
                generated,
                style.auto_english_pronunciation,
                style.show_pronunciation,
            )
            if key in self._pronunciation_cache:
                reading = self._pronunciation_cache[key]
            else:
                reading = (
                    _line_pronunciation(
                        line,
                        auto_pronunciation=generated,
                        auto_english_pronunciation=style.auto_english_pronunciation,
                    )
                    if style.show_pronunciation
                    else None
                )
            cache[key] = self._pronunciations[index] = reading
        # Keep only this revision, so repeated edits do not accumulate old lyric text.
        self._pronunciation_cache = cache

    def set_background(self, path: str | Path | None) -> None:
        self._background = QPixmap(str(path)) if path else QPixmap()
        self.update()

    def preview_rect(self) -> QRectF:
        """Return the letterboxed video viewport, keeping all ASS coordinates proportional."""
        width, height = self._ass_style.resolution
        scale = min(self.width() / width, self.height() / height)
        return QRectF(
            (self.width() - width * scale) / 2,
            (self.height() - height * scale) / 2,
            width * scale,
            height * scale,
        )

    def preview_rows(self) -> list[tuple[int, int]]:
        """Return (ASS row, original document index) for active and upcoming lyrics."""
        if self._manual_selection:
            if self._current not in self._indices:
                return []
            ordinal = self._indices.index(self._current)
            return [
                (position % 2, self._indices[position])
                for position in range(ordinal, min(ordinal + 2, len(self._indices)))
            ]
        rows = []
        for ordinal, index in enumerate(self._indices):
            line = self._document.lines[index]
            active = (
                line.is_timed and float(line.start) <= self._position < self._render_ends[index]
            )
            waiting = any(
                start <= self._position < end for start, end in self._waiting.get(index, [])
            )
            if active or waiting:
                rows.append((ordinal % 2, index))
        return rows

    def countdown_state(self) -> tuple[int, int, int] | None:
        """Return the upcoming row/index and 1–3 filled notes, absent at the singing boundary."""
        if self._manual_selection or not self._ass_style.show_countdown:
            return None
        for index, (start, end) in self._cues.items():
            if end - start >= 0.03 and start <= self._position < end:
                filled = min(3, 1 + int((self._position - start) / ((end - start) / 3)))
                return self._indices.index(index) % 2, index, filled
        return None

    def _sung_position(self, index: int) -> float:
        # ASS compresses token timing only when trimming an estimated instrumental gap
        # or when the next line on the same row must replace an overlapping lyric.
        line = self._document.lines[index]
        end = self._render_ends.get(index, line.end)
        if line.is_timed and end is not None and end < float(line.end) - 0.01:
            scale = max(0.01, end - float(line.start)) / max(
                0.01, float(line.end) - float(line.start)
            )
            return float(line.start) + (self._position - float(line.start)) / scale
        return self._position

    def highlight_fractions(self, line_index: int | None = None) -> list[float]:
        index = self._current if line_index is None else line_index
        if not 0 <= index < len(self._document.lines):
            return []
        line = self._document.lines[index]
        if line.tokens:
            position = self._sung_position(index)
            return [_progress(token.start, token.end, position) for token in line.tokens]
        if line.is_timed:
            # Without karaoke tags ASS paints active plain text in the sung color.
            return [float(self._position >= float(line.start))]
        return []

    def _character_progress(self, line: LyricLine, index: int) -> list[float]:
        values = [0.0] * len(line.text)
        fractions = self.highlight_fractions(index)
        if not line.tokens:
            return [fractions[0] if fractions else 0.0] * len(line.text)
        cursor = 0
        for token, fraction in zip(line.tokens, fractions):
            offset = line.text.find(token.text, cursor)
            if offset < 0:
                continue
            leading, core, _trailing = split_edge_whitespace(token.text)
            start = offset + len(leading)
            end = min(len(line.text), start + len(core))
            for char in range(start, end):
                values[char] = _clamp(fraction * (end - start) - (char - start), 0, 1)
            cursor = offset + len(token.text)
        return values

    def _color(self, key: str, fallback: str) -> QColor:
        color = QColor(str(getattr(self._ass_style, key, fallback)))
        return color if color.isValid() else QColor(fallback)

    def _font(self, size: float) -> QFont:
        font = QFont(self._ass_style.font)
        # Match the ASS font measurement convention used by ass._text_width.
        font.setPixelSize(max(1, round(size * 0.75)))
        font.setWeight(QFont.Weight.Bold)
        return font

    def _draw_outlined(
        self, painter: QPainter, path: QPainterPath, color: QColor, *, reading=False
    ) -> None:
        style = self._ass_style
        if style.shadow:
            shadow = QTransform().translate(style.shadow, style.shadow).map(path)
            painter.fillPath(shadow, QColor(0, 0, 0, 128))
        outline = min(style.outline, 2.0) if reading else style.outline
        if outline > 0:
            painter.strokePath(
                path,
                QPen(
                    self._color("outline_color", "#111111"),
                    outline * 2,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                ),
            )
        painter.fillPath(path, color)

    def _draw_line(self, painter: QPainter, index: int, row: int) -> None:
        line = self._document.lines[index]
        style = self._ass_style
        width, height = style.resolution
        font = self._font(style.font_size)
        metrics = QFontMetricsF(font)
        advances = [metrics.horizontalAdvance(char) for char in line.text]
        positions = [0.0]
        for advance in advances:
            positions.append(positions[-1] + advance)
        x = (
            float(style.karaoke_margin_h)
            if row == 0
            else width - style.karaoke_margin_h - positions[-1]
        )
        margin = _karaoke_upper_margin(style) if row == 0 else style.margin_v
        baseline = height - margin - metrics.descent()
        fractions = self._character_progress(line, index)
        waiting = self._color("text_color", "#ffffff")
        sung = self._color("highlight_color", "#ffd54a")
        for char, text in enumerate(line.text):
            glyph_x = x + positions[char]
            path = QPainterPath()
            path.addText(QPointF(glyph_x, baseline), font, text)
            self._draw_outlined(painter, path, waiting)
            if fractions[char] > 0:
                painter.save()
                painter.setClipRect(
                    QRectF(
                        glyph_x - style.outline,
                        baseline - metrics.ascent() - style.outline,
                        (advances[char] + 2 * style.outline) * fractions[char],
                        metrics.height() + 2 * style.outline,
                    ),
                    Qt.ClipOperation.IntersectClip,
                )
                self._draw_outlined(painter, path, sung)
                painter.restore()
        pronunciation = self._pronunciations.get(index)
        if pronunciation is None:
            return
        reading_font = self._font(style.pronunciation_font_size)
        reading_metrics = QFontMetricsF(reading_font)
        reading_bottom = height - margin - style.font_size - _pronunciation_clearance(style)
        for unit in pronunciation.units:
            start, end = unit.start, unit.end
            if not 0 <= start < end <= len(line.text):
                continue
            reading_width = reading_metrics.horizontalAdvance(unit.reading)
            reading_x = x + (positions[start] + positions[end] - reading_width) / 2
            reading_y = reading_bottom - reading_metrics.descent()
            path = QPainterPath()
            path.addText(QPointF(reading_x, reading_y), reading_font, unit.reading)
            self._draw_outlined(
                painter, path, self._color("pronunciation_color", "#ffffff"), reading=True
            )
            timing = _pronunciation_source_timing(unit, line)
            progress = (
                _progress(*timing, self._position)
                if timing
                else sum(fractions[start:end]) / (end - start)
            )
            if progress:
                painter.save()
                painter.setClipRect(
                    QRectF(
                        reading_x - style.outline,
                        reading_y - reading_metrics.ascent() - style.outline,
                        (reading_width + style.outline * 2) * progress,
                        reading_metrics.height() + 2 * style.outline,
                    ),
                    Qt.ClipOperation.IntersectClip,
                )
                self._draw_outlined(painter, path, sung, reading=True)
                painter.restore()

    def _draw_translation(self, painter: QPainter) -> None:
        style = self._ass_style
        if not style.show_translation:
            return
        for ordinal, index in enumerate(self._indices):
            line = self._document.lines[index]
            if not line.translation:
                continue
            visible = self._manual_selection and index == self._current
            if not self._manual_selection and line.is_timed:
                end = self._render_ends[index]
                if ordinal + 1 < len(self._indices):
                    following = self._document.lines[self._indices[ordinal + 1]]
                    if following.start is not None:
                        end = min(end, following.start)
                visible = float(line.start) <= self._position < end
            if not visible:
                continue
            width, _height = style.resolution
            font = self._font(style.translation_font_size)
            metrics = QFontMetricsF(font)
            top = float(style.translation_margin_v)
            for paragraph in line.translation.splitlines():
                utf16 = paragraph.encode("utf-16-le")
                layout = QTextLayout(paragraph, font)
                option = QTextOption()
                option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
                layout.setTextOption(option)
                layout.beginLayout()
                while True:
                    text_line = layout.createLine()
                    if not text_line.isValid():
                        break
                    text_line.setLineWidth(max(1, width - 120))
                    start = text_line.textStart() * 2
                    end = start + text_line.textLength() * 2
                    text = utf16[start:end].decode("utf-16-le").rstrip()
                    path = QPainterPath()
                    path.addText(
                        QPointF(
                            (width - metrics.horizontalAdvance(text)) / 2, top + metrics.ascent()
                        ),
                        font,
                        text,
                    )
                    self._draw_outlined(painter, path, self._color("translation_color", "#eaf4ff"))
                    top += text_line.height()
                layout.endLayout()

    def _draw_countdown(self, painter: QPainter) -> None:
        state = self.countdown_state()
        if state is None:
            return
        row, index, filled = state
        style = self._ass_style
        height = max(34, round(style.font_size * 0.72))
        note_width = round(height * 26 / 34)
        spacing = max(12, round(height * 0.38))
        width = note_width * 3 + spacing * 2
        x, y = _countdown_position(
            self._document.lines[index], self._pronunciations.get(index), row, style, width, height
        )
        note = QPainterPath()
        note.moveTo(14, 1)
        note.lineTo(17, 1)
        note.cubicTo(18, 5, 24, 6, 24, 13)
        note.cubicTo(24, 16, 23, 18, 21, 20)
        note.cubicTo(23, 12, 19, 11, 17, 11)
        note.lineTo(17, 24)
        note.cubicTo(17, 28, 13, 31, 8, 31)
        note.cubicTo(3, 31, 1, 28, 3, 25)
        note.cubicTo(5, 22, 10, 20, 14, 22)
        note.lineTo(14, 1)
        for index in range(3):
            painter.save()
            painter.translate(x - width / 2 + index * (note_width + spacing), y - height / 2)
            painter.scale(height / 34, height / 34)
            color = (
                self._color("highlight_color", "#ffd54a")
                if index < filled
                else self._color("text_color", "#ffffff")
            )
            if index >= filled:
                color.setAlpha(85)
            painter.fillPath(note, color)
            painter.restore()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0c1522"))
        viewport = self.preview_rect()
        width, height = self._ass_style.resolution
        painter.save()
        painter.setClipRect(viewport)
        painter.translate(viewport.topLeft())
        painter.scale(viewport.width() / width, viewport.height() / height)
        painter.fillRect(QRectF(0, 0, width, height), QColor("#142438"))
        if not self._background.isNull():
            scale = max(width / self._background.width(), height / self._background.height())
            target = QRectF(
                (width - self._background.width() * scale) / 2,
                (height - self._background.height() * scale) / 2,
                self._background.width() * scale,
                self._background.height() * scale,
            )
            painter.drawPixmap(target, self._background, QRectF(self._background.rect()))
            painter.fillRect(QRectF(0, 0, width, height), QColor(0, 0, 0, 100))
        for row, index in self.preview_rows():
            self._draw_line(painter, index, row)
        self._draw_translation(painter)
        self._draw_countdown(painter)
        painter.restore()
        if not self._indices:
            painter.setPen(QColor("#b5c5d4"))
            font = QFont(self.font())
            font.setPointSizeF(10)
            painter.setFont(font)
            painter.drawText(
                self.rect().adjusted(20, 20, -20, -20),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                "载入歌词后，在这里预览逐字高亮、翻译和读音",
            )
        painter.end()
