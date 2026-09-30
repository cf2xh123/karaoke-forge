"""Native, paint-based karaoke timelines and lyric preview widgets.

All timing values exposed by these widgets are absolute seconds. Editing is
transactional: dragging paints a provisional value; only release emits a change.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QScrollBar, QWidget

from ..models import LyricLine, LyricsDocument

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
    """Two native karaoke rows, including character-aligned ruby readings."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._document = LyricsDocument([])
        self._current = 0
        self._position = 0.0
        self._style: dict[str, Any] = {}
        self._background = QPixmap()
        self.setMinimumSize(200, 250)
        self.setAccessibleName("卡拉 OK 双行字幕预览")

    def set_document(self, doc: LyricsDocument) -> None:
        self._document = doc
        self._current = max(0, min(self._current, len(doc.lines) - 1))
        self.update()

    def set_current_line(self, index: int) -> None:
        self._current = max(0, min(int(index), len(self._document.lines) - 1))
        self.update()

    def set_position(self, sec: float) -> None:
        self._position = max(0.0, _finite(sec))
        timed = [
            (i, line)
            for i, line in enumerate(self._document.lines)
            if line.is_timed and not line.hidden
        ]
        active = [
            i for i, line in timed if _finite(line.start) <= self._position < _finite(line.end)
        ]
        if active:
            if self._current not in active:
                self._current = active[0]
        elif timed:
            preceding = [i for i, line in timed if _finite(line.start) <= self._position]
            self._current = preceding[-1] if preceding else timed[0][0]
        self.update()

    def set_style(self, style: dict[str, Any]) -> None:
        self._style = dict(style)
        self.update()

    def set_background(self, path: str | Path | None) -> None:
        self._background = QPixmap(str(path)) if path else QPixmap()
        self.update()

    def highlight_fractions(self, line_index: int | None = None) -> list[float]:
        index = self._current if line_index is None else line_index
        if not 0 <= index < len(self._document.lines):
            return []
        line = self._document.lines[index]
        if line.tokens:
            return [_progress(token.start, token.end, self._position) for token in line.tokens]
        if line.is_timed:
            return [_progress(_finite(line.start), _finite(line.end), self._position)]
        return []

    def preview_rows(self) -> list[tuple[int, int]]:
        """Return (visual row, document index), preserving hidden-line indices."""
        visible = [i for i, line in enumerate(self._document.lines) if not line.hidden]
        if not visible:
            return []
        index = visible.index(self._current) if self._current in visible else 0
        rows = [(index % 2, visible[index])]
        if index + 1 < len(visible):
            rows.append(((index + 1) % 2, visible[index + 1]))
        return rows

    def _color(self, key: str, fallback: str, alias: str = "") -> QColor:
        color = QColor(str(self._style.get(key, self._style.get(alias, fallback))))
        return color if color.isValid() else QColor(fallback)

    def _font(self, pixels: float) -> QFont:
        font = QFont(str(self._style.get("font", "Microsoft YaHei")))
        font.setPixelSize(max(9, round(pixels)))
        font.setWeight(QFont.Weight.DemiBold)
        return font

    def _character_progress(self, line: LyricLine, index: int) -> list[float]:
        values = [0.0] * len(line.text)
        fractions = self.highlight_fractions(index)
        if not line.tokens:
            fraction = fractions[0] if fractions else 0
            return [_clamp(fraction * len(line.text) - i, 0, 1) for i in range(len(line.text))]
        cursor = 0
        for token, fraction in zip(line.tokens, fractions):
            offset = line.text.find(token.text, cursor)
            if offset < 0:
                continue
            end = min(len(line.text), offset + len(token.text))
            for char in range(offset, end):
                values[char] = _clamp(fraction * (end - offset) - (char - offset), 0, 1)
            cursor = end
        return values

    def _draw_outlined(self, painter: QPainter, path: QPainterPath, color: QColor) -> None:
        # Paint the fill last: drawPath strokes over the fill, which can cover
        # thin strokes of small CJK glyphs and turn an entire preview dark.
        pen = QPen(
            self._color("outline_color", "#101820"),
            2.5,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.strokePath(path, pen)
        painter.fillPath(path, color)

    def _draw_line(
        self, painter: QPainter, line: LyricLine, index: int, area: QRectF, align_right: bool
    ) -> None:
        resolution = self._style.get("resolution", (1920, 1080))
        source_width = (
            _finite(resolution[0], 1920) if isinstance(resolution, (list, tuple)) else 1920
        )
        size = _clamp(
            _finite(self._style.get("font_size", 58), 58) * self.width() / max(1, source_width),
            17,
            44,
        )
        font = self._font(size)
        metrics = QFontMetricsF(font)
        reading_size = max(
            10,
            size
            * _finite(self._style.get("pronunciation_font_size", 26), 26)
            / max(1, _finite(self._style.get("font_size", 58), 58)),
        )
        reading_font = self._font(reading_size)
        reading_metrics = QFontMetricsF(reading_font)
        advances = [metrics.horizontalAdvance(char) for char in line.text]
        units = []
        if self._style.get("show_pronunciation", True):
            units = [
                (unit.start, min(len(line.text), unit.end), unit.reading)
                for unit in line.pronunciation_units
                if unit.reading and 0 <= unit.start < unit.end <= len(line.text)
            ]
            if not units and line.pronunciation and line.text:
                units = [(0, len(line.text), line.pronunciation)]
        for start, end, reading in units:
            required = reading_metrics.horizontalAdvance(reading) + 8
            extra = max(0.0, required - sum(advances[start:end])) / (end - start)
            for char in range(start, end):
                advances[char] += extra
        positions = [0.0]
        for advance in advances:
            positions.append(positions[-1] + advance)
        width = positions[-1]
        scale = min(1.0, area.width() / max(1, width))
        x = area.right() - width * scale if align_right else area.left()
        y = area.top() + 25
        painter.save()
        painter.translate(x, y)
        painter.scale(scale, scale)
        baseline = reading_metrics.height() + metrics.ascent() + 5
        fractions = self._character_progress(line, index)
        waiting = self._color("text_color", "#ffffff", "secondary_color")
        sung = self._color("highlight_color", "#ffd54a", "primary_color")
        for char, text in enumerate(line.text):
            glyph_x = positions[char] + (advances[char] - metrics.horizontalAdvance(text)) / 2
            path = QPainterPath()
            path.addText(QPointF(glyph_x, baseline), font, text)
            self._draw_outlined(painter, path, waiting)
            if fractions[char] > 0:
                painter.save()
                painter.setClipRect(
                    QRectF(
                        glyph_x - 1,
                        baseline - metrics.ascent() - 2,
                        (metrics.horizontalAdvance(text) + 2) * fractions[char],
                        metrics.height() + 4,
                    )
                )
                self._draw_outlined(painter, path, sung)
                painter.restore()
        for start, end, reading in units:
            reading_width = reading_metrics.horizontalAdvance(reading)
            reading_x = (positions[start] + positions[end] - reading_width) / 2
            path = QPainterPath()
            path.addText(QPointF(reading_x, reading_metrics.ascent()), reading_font, reading)
            self._draw_outlined(painter, path, self._color("pronunciation_color", "#ffffff"))
            progress = sum(fractions[start:end]) / (end - start)
            if progress:
                painter.save()
                painter.setClipRect(
                    QRectF(
                        reading_x - 1,
                        -2,
                        (reading_width + 2) * progress,
                        reading_metrics.height() + 4,
                    )
                )
                self._draw_outlined(painter, path, sung)
                painter.restore()
        painter.restore()
        if self._style.get("show_translation", True) and line.translation:
            translation_font = self._font(max(11, size * 0.65))
            painter.setFont(translation_font)
            painter.setPen(self._color("translation_color", "#eaf4ff"))
            translation_y = y + (baseline + metrics.descent() + 8) * scale
            translation_rect = QRectF(
                area.left(), translation_y, area.width(), max(20, area.bottom() - translation_y)
            )
            alignment = Qt.AlignmentFlag.AlignRight if align_right else Qt.AlignmentFlag.AlignLeft
            painter.drawText(
                translation_rect, alignment | Qt.TextFlag.TextWordWrap, line.translation
            )

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#142438"))
        if not self._background.isNull():
            scaled = self._background.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(
                (self.width() - scaled.width()) // 2, (self.height() - scaled.height()) // 2, scaled
            )
            painter.fillRect(self.rect(), QColor(0, 0, 0, 100))
        painter.setPen(QColor("#9fb5c8"))
        painter.setFont(self._font(11))
        painter.drawText(QRectF(16, 10, self.width() - 32, 20), "字幕预览")
        rows = self.preview_rows()
        if not rows:
            painter.setPen(QColor("#b5c5d4"))
            painter.drawText(
                self.rect().adjusted(20, 35, -20, -20),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                "载入歌词后，在这里预览逐字高亮、翻译和读音",
            )
        else:
            row_height = (self.height() - 36) / 2
            for row, index in rows:
                area = QRectF(22, 22 + row * row_height, self.width() - 44, row_height)
                self._draw_line(painter, self._document.lines[index], index, area, row == 1)
        painter.end()
