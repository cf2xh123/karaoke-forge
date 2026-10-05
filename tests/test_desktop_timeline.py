from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("karaoke_forge.desktop")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QFont, QFontDatabase, QImage, QMouseEvent, QRawFont, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from karaoke_forge.ass import AssStyle, write_ass
from karaoke_forge.desktop.timeline import (
    LyricPreviewWidget,
    TimelineWidget,
    TokenTimelineWidget,
)
from karaoke_forge.models import KaraokeToken, LyricLine, LyricsDocument, PronunciationSpan


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    # The Windows offscreen plugin does not enumerate installed system fonts.
    font = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts/msyh.ttc"
    if not QFontDatabase.families() and font.is_file():
        QFontDatabase.addApplicationFont(str(font))
    yield instance


@pytest.fixture
def document():
    return LyricsDocument(
        [
            LyricLine(
                "春の歌",
                1.0,
                5.0,
                [KaraokeToken("春", 1, 2), KaraokeToken("の歌", 3, 5)],
                translation="春天的歌",
                pronunciation_units=[PronunciationSpan("春", "はる", 0, 1)],
            ),
            LyricLine("次の歌", 6.0, 9.0, [KaraokeToken("次の歌", 6, 9)]),
        ]
    )


def show(widget, app, width=700, height=230):
    widget.resize(width, height)
    widget.show()
    app.processEvents()
    return widget


def test_timeline_selects_document_index_and_seeks(app, document):
    widget = show(TimelineWidget(), app)
    widget.set_document(document)
    selections, seeks = [], []
    widget.lineSelected.connect(selections.append)
    widget.seekRequested.connect(seeks.append)
    target = widget.line_rect(1).center().toPoint()
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=target)
    assert selections == [1]
    assert seeks[0] == pytest.approx(7.5, abs=0.03)
    widget.close()


def test_sentence_edge_drag_is_transactional_and_cannot_cross_other_edge(app, document):
    widget = show(TimelineWidget(), app)
    widget.set_document(document)
    widget.set_snap_enabled(False)
    changes = []
    widget.boundaryChanged.connect(lambda *args: changes.append(args))
    rect = widget.line_rect(0)
    initial = QPoint(round(rect.right()), round(rect.center().y()))
    target = QPoint(round(widget.time_to_x(0)), initial.y())
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=initial)
    QTest.mouseMove(widget, target)
    assert changes == []
    assert document.lines[0].end == 5
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=target)
    assert len(changes) == 1
    assert changes[0][:2] == (0, "end")
    assert changes[0][2] == pytest.approx(1.01)
    assert document.lines[0].end == 5
    widget.close()


def test_sentence_edge_snap_uses_other_sentence_boundary(app, document):
    widget = show(TimelineWidget(), app)
    widget.set_document(document)
    changes = []
    widget.boundaryChanged.connect(lambda *args: changes.append(args))
    rect = widget.line_rect(0)
    initial = QPoint(round(rect.right()), round(rect.center().y()))
    target = QPoint(round(widget.time_to_x(6)) - 3, initial.y())
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=initial)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=target)
    assert changes[0][2] == 6.0
    widget.close()


def test_clicking_handle_tolerance_does_not_change_sentence_or_token_timing(app, document):
    document.lines[1].start = 5.02
    document.lines[0].tokens[0].end = 2.99
    timeline = show(TimelineWidget(), app)
    timeline.set_document(document)
    changes = []
    timeline.boundaryChanged.connect(lambda *args: changes.append(args))
    point = timeline.line_rect(0).topRight().toPoint() + QPoint(4, 20)
    QTest.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=point)
    assert changes == []
    timeline.close()
    tokens = show(TokenTimelineWidget(), app)
    tokens.set_line(document.lines[0])
    tokens.timingChanged.connect(changes.append)
    point = tokens.token_rect(0).topRight().toPoint() + QPoint(4, 20)
    QTest.mouseClick(tokens, Qt.MouseButton.LeftButton, pos=point)
    assert changes == []
    tokens.close()


def test_overlapping_sentences_have_separate_hit_targets_and_hidden_lines_are_absent(app):
    document = LyricsDocument(
        [
            LyricLine("one", 1, 4),
            LyricLine("hidden", 1, 4, hidden=True),
            LyricLine("chorus", 2, 5),
        ]
    )
    widget = show(TimelineWidget(), app)
    widget.set_document(document)
    assert widget.line_rect(1).isNull()
    assert not widget.line_rect(0).intersects(widget.line_rect(2))
    selected = []
    widget.lineSelected.connect(selected.append)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=widget.line_rect(2).center().toPoint())
    assert selected == [2]
    widget.close()


def test_token_drag_emits_once_preserves_document_and_prevents_overlap(app, document):
    widget = show(TokenTimelineWidget(), app)
    widget.set_line(document.lines[0])
    widget.set_snap_enabled(False)
    changes = []
    widget.timingChanged.connect(changes.append)
    rect = widget.token_rect(0)
    initial = QPoint(round(rect.right()), round(rect.center().y()))
    target = QPoint(round(widget.time_to_x(4)), initial.y())
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=initial)
    QTest.mouseMove(widget, target)
    assert changes == []
    assert document.lines[0].tokens[0].end == 2
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=target)
    assert len(changes) == 1
    assert changes[0][0]["end"] == 3
    assert changes[0][0]["end"] <= changes[0][1]["start"]
    assert document.lines[0].tokens[0].end == 2
    widget.close()


def test_escape_cancels_token_drag_without_emitting(app, document):
    widget = show(TokenTimelineWidget(), app)
    widget.set_line(document.lines[0])
    changes = []
    widget.timingChanged.connect(changes.append)
    initial = widget.token_rect(0).topRight().toPoint() + QPoint(0, 20)
    target = initial + QPoint(70, 0)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=initial)
    QTest.mouseMove(widget, target)
    QTest.keyClick(widget, Qt.Key.Key_Escape)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=target)
    assert changes == []
    assert widget.token_rect(0).right() == pytest.approx(widget.time_to_x(2))
    widget.close()


def test_shared_token_boundary_moves_both_tokens_without_overlap(app):
    line = LyricLine("ab", 0, 4, [KaraokeToken("a", 0, 2), KaraokeToken("b", 2, 4)])
    widget = show(TokenTimelineWidget(), app)
    widget.set_line(line)
    widget.set_linked_boundaries(True)
    changes = []
    widget.timingChanged.connect(changes.append)
    initial = widget.token_rect(0).topRight().toPoint() + QPoint(0, 20)
    target = QPoint(round(widget.time_to_x(3)), initial.y())
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=initial)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=target)
    assert len(changes) == 1
    assert changes[0][0]["end"] == pytest.approx(3, abs=0.01)
    assert changes[0][0]["end"] == changes[0][1]["start"]
    assert changes[0][1]["end"] == 4
    widget.close()


def test_touching_tokens_can_be_shortened_independently_to_create_a_pause(app):
    line = LyricLine("ab", 0, 4, [KaraokeToken("a", 0, 2), KaraokeToken("b", 2, 4)])
    widget = show(TokenTimelineWidget(), app)
    widget.set_line(line)
    changes = []
    widget.timingChanged.connect(changes.append)
    initial = widget.token_rect(0).topRight().toPoint() + QPoint(0, 20)
    target = QPoint(round(widget.time_to_x(1.5)), initial.y())
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=initial)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=target)
    assert changes[0][0]["end"] == pytest.approx(1.5, abs=0.01)
    assert changes[0][1]["start"] == 2
    assert line.tokens[0].end == 2
    widget.close()


@pytest.mark.parametrize("widget_type", [TimelineWidget, TokenTimelineWidget])
def test_ruler_drag_seeks_continuously_and_never_edits(app, document, widget_type):
    widget = show(widget_type(), app)
    if isinstance(widget, TimelineWidget):
        widget.set_document(document)
    else:
        widget.set_line(document.lines[0])
    seeks = []
    widget.seekRequested.connect(seeks.append)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(150, 15))
    QTest.mouseMove(widget, QPoint(220, 15))
    QTest.mouseMove(widget, QPoint(280, 15))
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(320, 15))
    assert len(seeks) >= 4
    assert seeks == sorted(seeks)
    assert seeks[-1] == pytest.approx(widget.x_to_time(320))
    widget.close()


def test_selecting_next_word_exposes_its_own_start_at_a_shared_boundary(app):
    line = LyricLine("ab", 0, 4, [KaraokeToken("a", 0, 2), KaraokeToken("b", 2, 4)])
    widget = show(TokenTimelineWidget(), app)
    widget.set_line(line)
    changes, selections = [], []
    widget.timingChanged.connect(changes.append)
    widget.tokenSelected.connect(selections.append)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=widget.token_rect(1).center().toPoint())
    assert selections == [1]
    assert changes == []
    initial = QPoint(round(widget.time_to_x(2)), 60)
    target = QPoint(round(widget.time_to_x(2.5)), 60)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=initial)
    assert widget._drag == (1, "start")
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=target)
    assert len(changes) == 1
    assert changes[0][0] == {"text": "a", "start": 0, "end": 2}
    assert changes[0][1]["start"] == pytest.approx(2.5, abs=0.01)
    widget.close()


def test_dense_word_centre_selects_and_zoom_keeps_both_handles_reachable(app):
    line = LyricLine("ab", 0, 10, [KaraokeToken("a", 1, 1.1), KaraokeToken("b", 1.1, 1.2)])
    widget = show(TokenTimelineWidget(), app, width=320)
    widget.set_line(line)
    selections = []
    widget.tokenSelected.connect(selections.append)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=widget.token_rect(1).center().toPoint())
    assert selections == [1]
    widget.set_zoom(16)
    widget.reveal_position(1.15, center=True)
    for edge in ("start", "end"):
        assert widget._edge_at(QPointF(widget.time_to_x(getattr(line.tokens[1], edge)), 60)) == (1, edge)
    widget.close()


def test_dragging_word_body_preserves_duration_and_clamps_at_neighbors(app):
    line = LyricLine("abc", 0, 6, [
        KaraokeToken("a", 0, 1), KaraokeToken("b", 2, 3), KaraokeToken("c", 5, 6),
    ])
    widget = show(TokenTimelineWidget(), app)
    widget.set_line(line)
    widget.set_snap_enabled(False)
    changes = []
    widget.timingChanged.connect(changes.append)
    initial = widget.token_rect(1).center().toPoint()
    target = initial + QPoint(round(3 * widget.pixels_per_second), 0)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=initial)
    QTest.mouseMove(widget, target)
    assert not changes
    assert line.tokens[1].start == 2
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=target)
    assert len(changes) == 1
    assert changes[0][1] == {"text": "b", "start": 4, "end": 5}
    assert changes[0][0]["end"] == 1 and changes[0][2]["start"] == 5
    widget.close()


def test_nudge_shares_independent_and_linked_constraints_without_mouse_rounding(app):
    widget = show(TokenTimelineWidget(), app)
    line = LyricLine("ab", 0, 4, [KaraokeToken("a", 0, 2), KaraokeToken("b", 2, 4)])
    widget.set_line(line)
    widget.set_selected_token(1)
    changes = []
    widget.timingChanged.connect(changes.append)
    widget.adjust_selected("start", 0.005)
    assert changes[-1][1]["start"] == 2.005
    assert changes[-1][0]["end"] == 2
    widget.set_line(line)
    widget.set_linked_boundaries(True)
    widget.adjust_selected("start", 0.1)
    assert changes[-1][0]["end"] == changes[-1][1]["start"] == 2.1
    widget.set_line(line)
    widget.set_linked_boundaries(False)
    before = len(changes)
    widget.adjust_selected("move", -1)
    assert len(changes) == before
    widget.adjust_selected("start", 20)
    assert changes[-1][1]["end"] - changes[-1][1]["start"] == pytest.approx(0.01)
    assert changes[-1][0]["end"] == 2
    widget.close()


@pytest.mark.parametrize("widget_type", [TimelineWidget, TokenTimelineWidget])
def test_gesture_prevents_playback_follow_and_data_replacement_until_release(app, document, widget_type):
    widget = show(widget_type(), app, width=400)
    if isinstance(widget, TimelineWidget):
        widget.set_document(document)
        rect = widget.line_rect(0)
        replace = lambda: widget.set_document(LyricsDocument([LyricLine("other", 20, 30)]))
    else:
        widget.set_line(document.lines[0])
        rect = widget.token_rect(0)
        replace = lambda: widget.set_line(document.lines[1])
    initial = rect.topRight().toPoint() + QPoint(0, 20)
    events = []
    widget.interactionStarted.connect(events.append)
    widget.interactionFinished.connect(lambda: events.append("finished"))
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=initial)
    assert widget.is_interacting
    original_duration = widget._duration
    replace()
    widget.set_zoom(16)
    widget.reveal_position(30, center=True)
    assert widget._duration == original_duration
    assert widget._zoom == 1
    assert widget._scroll.value() == 0
    QTest.keyClick(widget, Qt.Key.Key_Escape)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=initial + QPoint(40, 0))
    assert not widget.is_interacting
    assert events == ["edit", "finished"]
    replace()
    assert widget._duration != original_duration
    widget.close()


def test_start_callback_can_commit_new_data_or_cancel_invalid_draft(app):
    widget = show(TokenTimelineWidget(), app)
    original = LyricLine("a", 0, 5, [KaraokeToken("a", 1, 3)])
    committed = LyricLine("a", 0, 5, [KaraokeToken("a", 1, 4)])
    widget.set_line(original)
    changes = []
    widget.timingChanged.connect(changes.append)

    def commit(_kind):
        assert widget.is_interacting
        widget.set_line(committed)

    widget.interactionStarted.connect(commit)
    widget.adjust_selected("end", 0.1)
    assert changes[-1][0]["end"] == 4.1
    widget.interactionStarted.disconnect(commit)
    widget.interactionStarted.connect(lambda _: widget.cancel_interaction())
    widget.adjust_selected("end", 0.1)
    assert len(changes) == 1
    assert not widget.is_interacting
    widget.close()


def test_snap_does_not_jump_seconds_at_overview_scale_or_chase_a_moving_playhead(app):
    widget = show(TimelineWidget(), app, width=700)
    widget.set_document(LyricsDocument([LyricLine("a", 0, 100), LyricLine("b", 102, 600)]))
    changes = []
    widget.boundaryChanged.connect(lambda *args: changes.append(args))
    initial = QPoint(round(widget.time_to_x(100)), 60)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=initial)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=initial + QPoint(1, 0))
    # At this scale one pixel is ~0.9s; the nearby 102s boundary is not a magnet.
    assert changes[-1][2] == pytest.approx(100 + 1 / widget.pixels_per_second, abs=0.001)
    widget.close()

    widget = show(TokenTimelineWidget(), app)
    widget.set_line(LyricLine("a", 0, 4, [KaraokeToken("a", 1, 2)]))
    widget.set_position(0)
    changes = []
    widget.timingChanged.connect(changes.append)
    initial = QPoint(round(widget.time_to_x(2)), 60)
    target = initial + QPoint(20, 0)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=initial)
    widget.set_position(2 + 20 / widget.pixels_per_second + 0.02)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=target)
    assert changes[-1][0]["end"] == pytest.approx(2 + 20 / widget.pixels_per_second, abs=0.001)
    widget.close()


def test_nearest_snap_target_wins_and_original_join_does_not_trap_small_gap(app):
    widget = show(TokenTimelineWidget(), app)
    widget.set_line(LyricLine("ab", 0, 40, [KaraokeToken("a", 1, 1.12), KaraokeToken("b", 1.18, 2)]))
    widget.set_position(1.16)
    widget._begin_interaction("edit")
    widget._before_drag = [dict(token) for token in widget._tokens]
    widget._apply_delta(0, "end", 0.03)
    assert widget._tokens[0]["end"] == 1.16
    widget.cancel_interaction()
    widget.set_line(LyricLine("ab", 0, 4, [KaraokeToken("a", 0, 2), KaraokeToken("b", 2, 4)]))
    changes = []
    widget.timingChanged.connect(changes.append)
    initial = QPoint(round(widget.time_to_x(2)), 60)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=initial)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=initial - QPoint(1, 0))
    assert changes[-1][0]["end"] < 2
    assert changes[-1][1]["start"] == 2
    widget.close()


def test_line_edge_minimum_duration_survives_submillisecond_source_times(app):
    widget = show(TimelineWidget(), app)
    widget.set_document(LyricsDocument([LyricLine("a", 1.0005, 3)]))
    changes = []
    widget.boundaryChanged.connect(lambda *args: changes.append(args))
    initial = QPoint(round(widget.time_to_x(3)), 60)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=initial)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(0, 60))
    assert changes[-1][2] - 1.0005 == pytest.approx(0.01)
    widget.close()


def test_nudge_start_callback_may_remove_all_tokens_without_crashing(app):
    widget = show(TokenTimelineWidget(), app)
    widget.set_line(LyricLine("a", 0, 3, [KaraokeToken("a", 1, 2)]))
    widget.interactionStarted.connect(lambda _: widget.set_line(LyricLine("a", 0, 3)))
    changes = []
    widget.timingChanged.connect(changes.append)
    widget.adjust_selected("end", 0.1)
    assert changes == []
    assert not widget.is_interacting
    widget.close()


@pytest.mark.parametrize("widget_type", [TimelineWidget, TokenTimelineWidget])
@pytest.mark.parametrize("interruption", [
    QEvent.Type.WindowDeactivate, QEvent.Type.UngrabMouse, QEvent.Type.FocusOut,
    QEvent.Type.ApplicationDeactivate,
])
def test_focus_or_capture_loss_cancels_provisional_drag_once(app, document, widget_type, interruption):
    widget = show(widget_type(), app)
    changes, finished = [], []
    if isinstance(widget, TimelineWidget):
        widget.set_document(document)
        rect = lambda: widget.line_rect(0)
        widget.boundaryChanged.connect(lambda *args: changes.append(args))
    else:
        widget.set_line(document.lines[0])
        rect = lambda: widget.token_rect(0)
        widget.timingChanged.connect(changes.append)
    widget.interactionFinished.connect(lambda: finished.append(True))
    original = rect().right()
    initial = rect().topRight().toPoint() + QPoint(0, 20)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=initial)
    QTest.mouseMove(widget, initial - QPoint(35, 0))
    assert rect().right() < original
    receiver = app if interruption == QEvent.Type.ApplicationDeactivate else widget
    app.sendEvent(receiver, QEvent(interruption))
    assert not widget.is_interacting
    assert rect().right() == original
    QTest.mouseMove(widget, initial - QPoint(80, 0))
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=initial - QPoint(80, 0))
    assert changes == [] and finished == [True]
    widget.close()


def test_hover_after_a_missed_mouse_release_cancels_instead_of_editing(app, document):
    widget = show(TokenTimelineWidget(), app)
    widget.set_line(document.lines[0])
    original = widget.token_rect(0).right()
    initial = widget.token_rect(0).topRight().toPoint() + QPoint(0, 20)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=initial)
    QTest.mouseMove(widget, initial + QPoint(30, 0))
    point = QPointF(initial + QPoint(50, 0))
    event = QMouseEvent(
        QEvent.Type.MouseMove, point, widget.mapToGlobal(point.toPoint()),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
    )
    app.sendEvent(widget, event)
    assert not widget.is_interacting
    assert widget.token_rect(0).right() == original
    widget.close()


def test_ctrl_wheel_zoom_preserves_pointer_time_and_scrolls_inside_small_widget(app, document):
    widget = show(TokenTimelineWidget(), app, width=280)
    widget.set_line(document.lines[0])
    point = QPointF(150, 70)
    anchor = widget.x_to_time(point.x())
    event = QWheelEvent(
        point,
        widget.mapToGlobal(point.toPoint()),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)
    assert widget._zoom == pytest.approx(1.25)
    assert widget.x_to_time(point.x()) == pytest.approx(anchor, abs=0.02)
    assert widget._scroll.maximum() > 0
    assert widget.width() == 280
    widget.close()


def test_reveal_playhead_scrolls_only_when_outside_visible_area(app, document):
    widget = show(TimelineWidget(), app, width=320)
    widget.set_document(document, selected=0)
    widget.set_zoom(8)
    seeks, selections = [], []
    widget.seekRequested.connect(seeks.append)
    widget.lineSelected.connect(selections.append)
    widget.set_position(8)
    widget.reveal_position()
    assert widget.LEFT <= widget.time_to_x(8) <= widget.width() - widget.LEFT
    offset = widget._scroll.value()
    widget.reveal_position(8.1)
    assert widget._scroll.value() == offset
    widget.reveal_position(8.1, center=True)
    assert widget.time_to_x(8.1) == pytest.approx(widget.width() / 2, abs=1)
    assert widget._selected == 0
    assert seeks == [] and selections == []
    widget.close()


def test_follow_playback_does_not_scroll_while_dragging_a_boundary(app, document):
    widget = show(TimelineWidget(), app, width=320)
    widget.set_document(document)
    widget.set_zoom(2)
    initial = widget.line_rect(0).topRight().toPoint() + QPoint(0, 20)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=initial)
    offset = widget._scroll.value()
    widget.reveal_position(9, center=True)
    assert widget._scroll.value() == offset
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=initial)
    widget.reveal_position(9)
    assert widget._scroll.value() != offset
    widget.close()


def test_highlighting_obeys_individual_token_times_and_gaps(app, document):
    widget = show(LyricPreviewWidget(), app, height=300)
    widget.set_document(document)
    widget.set_position(1.5)
    assert widget.highlight_fractions(0) == [0.5, 0.0]
    widget.set_position(2.5)
    assert widget.highlight_fractions(0) == [1.0, 0.0]
    widget.set_position(4)
    assert widget.highlight_fractions(0) == [1.0, 0.5]
    assert widget._character_progress(document.lines[0], 0) == [1.0, 1.0, 0.0]
    widget.set_position(7)
    assert widget.preview_rows() == [(1, 1)]
    assert widget.highlight_fractions(1) == pytest.approx([1 / 3])
    widget.close()


def _available_font_for(text):
    families = QFontDatabase.families()
    preferred = ["DejaVu Sans", "Liberation Sans", "Arial", "Microsoft YaHei"]
    for family in [name for name in preferred if name in families] + families:
        raw = QRawFont.fromFont(QFont(family, 20))
        if raw.isValid() and all(raw.supportsCharacter(ord(char)) for char in set(text)):
            return family
    return None


def _preview_fill_counts(image):
    counts = {"waiting": 0, "sung": 0, "reading": 0}
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            red, green, blue = color.red(), color.green(), color.blue()
            counts["waiting"] += red > 180 and green > 180 and blue > 180
            counts["sung"] += red > 180 and green > 130 and blue < 140
            counts["reading"] += red < 80 and green > 180 and blue > 180
    return counts


def test_native_preview_paints_readings_and_highlight_and_accepts_background_reset(app):
    # Exercise real glyph fills on every platform, without depending on a CJK
    # font being installed or counting tiny fallback boxes in a scaled preview.
    font = _available_font_for("Morning songMORNING")
    assert font is not None, "The Qt test environment needs a font with Latin glyphs"
    source = LyricsDocument(
        [
            LyricLine(
                "Morning song", 1, 5,
                [KaraokeToken("Morning ", 1, 2), KaraokeToken("song", 3, 5)],
                pronunciation_units=[PronunciationSpan("Morning", "MORNING", 0, 7)],
            )
        ],
        metadata={"auto_pronunciation": "false"},
    )
    widget = show(LyricPreviewWidget(), app, width=960, height=540)
    widget.set_document(source)
    style = {
        "font": font, "resolution": (960, 540), "font_size": 88,
        "pronunciation_font_size": 40, "pronunciation_color": "#00FFFF",
        "show_pronunciation": True, "show_translation": False,
    }
    widget.set_style(style)
    widget.set_background(None)
    widget.set_position(1)
    before = widget.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
    widget.set_position(4)
    after = widget.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
    assert before.size() == after.size()
    initial, progressed = _preview_fill_counts(before), _preview_fill_counts(after)
    assert initial["waiting"] > 100
    assert initial["reading"] > 100, "The saved reading must actually be painted"
    assert initial["sung"] == 0
    assert progressed["sung"] > initial["waiting"] * 0.4
    assert progressed["reading"] < initial["reading"] * 0.1, "The reading must also highlight"
    widget.set_position(1)
    widget.set_style({**style, "show_pronunciation": False})
    assert _preview_fill_counts(widget.grab().toImage())["reading"] == 0
    widget.close()


def test_native_preview_cjk_glyph_fills_survive_outlines_when_font_is_available(app, document):
    font = _available_font_for("春の歌次")
    if font is None:
        pytest.skip("No installed Qt font provides the CJK glyphs used by this raster test")
    widget = show(LyricPreviewWidget(), app, width=960, height=540)
    widget.set_document(document)
    widget.set_style({
        "font": font, "resolution": (960, 540), "font_size": 88,
        "show_pronunciation": False, "show_translation": False,
    })
    widget.set_position(1)
    before = _preview_fill_counts(widget.grab().toImage())
    widget.set_position(4)
    after = _preview_fill_counts(widget.grab().toImage())
    assert before["waiting"] > 100
    assert before["sung"] == 0
    assert after["sung"] > before["waiting"] * 0.25
    widget.close()


def _ass_seconds(value):
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _visible_ass_rows(document, style, seconds):
    rows = set()
    index_by_text = {line.text: index for index, line in enumerate(document.lines)}
    for event in write_ass(document, style).splitlines():
        if not event.startswith("Dialogue:"):
            continue
        columns = event.split(",", 9)
        if columns[3] not in {"Karaoke", "KaraokeLower", "KaraokeInactive", "KaraokeLowerInactive"}:
            continue
        if _ass_seconds(columns[1]) <= seconds < _ass_seconds(columns[2]):
            text = re.sub(r"\{[^}]*\}", "", columns[9])
            rows.add((int("Lower" in columns[3]), index_by_text[text]))
    return rows


def test_preview_rows_follow_exported_ass_events_across_overlap_blank_rows_and_long_breaks(app):
    document = LyricsDocument(
        [
            LyricLine("", 0, 0.5),
            LyricLine("first", 1, 5, [KaraokeToken("first", 1, 5)]),
            LyricLine("hidden", 2, 4, hidden=True),
            LyricLine("overlap", 3, 8, [KaraokeToken("overlap", 3, 8)]),
            LyricLine("third", 6, 9, [KaraokeToken("third", 6, 9)]),
            LyricLine("after break", 20, 22, [KaraokeToken("after break", 20, 22)]),
        ]
    )
    style = AssStyle(auto_pronunciation=False)
    widget = show(LyricPreviewWidget(), app, height=400)
    widget.set_document(document)
    widget.set_style({"auto_pronunciation": False})
    for seconds in (0, 1, 2, 3.5, 5.5, 6.1, 8.5, 9.5, 16.9, 17, 18.1, 19.9, 20, 22):
        widget.set_position(seconds)
        assert set(widget.preview_rows()) == _visible_ass_rows(document, style, seconds), seconds
    widget.close()


def test_countdown_stages_and_gap_threshold_match_the_exported_cues(app):
    source = LyricsDocument([LyricLine("before", 0, 2), LyricLine("after", 20, 23)])
    widget = show(LyricPreviewWidget(), app)
    widget.set_document(source)
    widget.set_style({"auto_pronunciation": False})
    widget.set_position(10)
    assert widget.preview_rows() == []
    assert widget.countdown_state() is None
    for seconds, filled in ((17, 1), (18, 2), (19, 3)):
        widget.set_position(seconds)
        assert widget.preview_rows() == [(1, 1)]
        assert widget.countdown_state() == (1, 1, filled)
    with_cue = widget.grab().toImage()
    widget.set_style({"auto_pronunciation": False, "show_countdown": False})
    assert widget.preview_rows() == [(1, 1)]
    assert widget.countdown_state() is None
    assert widget.grab().toImage() != with_cue
    widget.set_position(20)
    assert widget.countdown_state() is None
    widget.set_style({"auto_pronunciation": False, "countdown_gap_threshold": 20})
    widget.set_position(10)
    assert widget.preview_rows() == [(1, 1)]
    assert _visible_ass_rows(source, AssStyle(countdown_gap_threshold=20), 10) == {(1, 1)}
    widget.close()


def _color_bounds(image, channel):
    points = []
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if channel == "green":
                selected = color.green() > 120 and color.red() < 70 and color.blue() < 70
            else:
                selected = color.red() > 180 and color.green() > 180 and color.blue() > 180
            if selected:
                points.append((x, y))
    assert points, channel
    return (
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    )


def test_translation_size_position_and_lyric_bottom_margin_change_actual_pixels(app):
    widget = show(LyricPreviewWidget(), app, width=768, height=432)
    widget.set_document(
        LyricsDocument([LyricLine("Lyric text", 0, 10, translation="Translated words")])
    )
    style = {
        "font_size": 80,
        "translation_font_size": 24,
        "translation_margin_v": 16,
        "margin_v": 30,
        "translation_color": "#00FF00",
        "show_pronunciation": False,
    }
    widget.set_style(style)
    widget.set_position(0)
    # Plain ASS text uses the sung color when active; choose white for this geometry probe.
    style["highlight_color"] = "#FFFFFF"
    widget.set_style(style)
    small = widget.grab().toImage()
    small_translation = _color_bounds(small, "green")
    first_main = _color_bounds(small, "white")
    widget.set_style(
        {**style, "translation_font_size": 58, "translation_margin_v": 400, "margin_v": 180}
    )
    large = widget.grab().toImage()
    large_translation = _color_bounds(large, "green")
    second_main = _color_bounds(large, "white")
    assert large_translation[2] - large_translation[0] > 1.8 * (
        small_translation[2] - small_translation[0]
    )
    assert large_translation[1] - small_translation[1] == pytest.approx((400 - 16) * 0.4, abs=8)
    assert first_main[1] - second_main[1] == pytest.approx((180 - 30) * 0.4, abs=2)
    widget.close()


def test_english_pronunciation_switch_filters_saved_spans_and_legacy_whole_readings(app):
    mixed = LyricLine(
        "春hello",
        0,
        5,
        pronunciation_units=[
            PronunciationSpan("春", "はる", 0, 1),
            PronunciationSpan("hello", "ハロー", 1, 6),
        ],
    )
    source = LyricsDocument([mixed, LyricLine("hello", 6, 9, pronunciation="ハロー")])
    before = source.to_dict()
    widget = show(LyricPreviewWidget(), app)
    widget.set_document(source)
    widget.set_style({"auto_english_pronunciation": True})
    widget.set_position(1)
    with_english = widget.grab().toImage()
    widget.set_style({"auto_english_pronunciation": False})
    assert [unit.reading for unit in widget._pronunciations[0].units] == ["はる"]
    assert widget._pronunciations[1] is None
    assert widget.grab().toImage() != with_english
    assert source.to_dict() == before
    widget.close()


def test_plain_lrc_estimated_gap_and_same_row_replacement_use_ass_timing(app):
    source = LyricsDocument([LyricLine("a", 0, 20), LyricLine("b", 20, 22)])
    widget = show(LyricPreviewWidget(), app)
    widget.set_document(source)
    widget.set_style({"auto_pronunciation": False})
    widget.set_position(10)
    assert widget.preview_rows() == []
    assert _visible_ass_rows(source, AssStyle(auto_pronunciation=False), 10) == set()
    source = LyricsDocument(
        [
            LyricLine("first", 0, 12, [KaraokeToken("first", 0, 12)]),
            LyricLine("second", 2, 8),
            LyricLine("third", 6, 10),
        ]
    )
    widget.set_document(source)
    widget.set_position(3)
    assert widget.highlight_fractions(0) == [0.5]
    assert set(widget.preview_rows()) == _visible_ass_rows(
        source, AssStyle(auto_pronunciation=False), 3
    )
    widget.close()


def test_preview_respects_video_aspect_ratio_at_small_window_sizes(app):
    widget = show(LyricPreviewWidget(), app, width=260, height=170)
    frame = widget.preview_rect()
    assert frame.width() / frame.height() == pytest.approx(16 / 9)
    assert widget.rect().contains(frame.toAlignedRect())
    widget.set_style({"resolution": (1080, 1920)})
    frame = widget.preview_rect()
    assert frame.width() / frame.height() == pytest.approx(9 / 16)
    assert widget.width() == 260
    widget.close()


def test_preview_respects_official_only_pronunciation_policy_without_dropping_saved_readings(
    app, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        "karaoke_forge.ass.generate_pronunciation", lambda *args, **kwargs: calls.append(args)
    )
    source = LyricsDocument(
        [
            LyricLine("missing", 0, 2),
            LyricLine("saved", 3, 5, pronunciation="セーブド"),
        ],
        metadata={"auto_pronunciation": " false "},
    )
    widget = show(LyricPreviewWidget(), app)
    widget.set_document(source)
    widget.set_style({"font_size": 80})
    assert calls == []
    assert widget._pronunciations[0] is None
    assert widget._pronunciations[1].text == "セーブド"
    widget.close()


def test_style_adjustments_reuse_readings_and_translation_wraps_unicode(app, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "karaoke_forge.ass.generate_pronunciation", lambda *args, **kwargs: calls.append(args)
    )
    source = LyricsDocument([LyricLine("original", 0, 10, translation="🎤✨ 雨と星 " * 25)])
    widget = show(LyricPreviewWidget(), app, width=480, height=270)
    widget.set_document(source)
    widget.set_position(1)
    before = len(calls)
    widget.set_style({"font_size": 80, "translation_font_size": 58})
    widget.set_style({"font_size": 80, "translation_font_size": 58, "margin_v": 120})
    assert len(calls) == before
    assert not widget.grab().toImage().isNull()
    widget.close()
