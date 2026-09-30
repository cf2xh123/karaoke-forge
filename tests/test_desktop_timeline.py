from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("karaoke_forge.desktop")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QFontDatabase, QImage, QWheelEvent
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


def test_native_preview_paints_readings_and_highlight_and_accepts_background_reset(app, document):
    widget = show(LyricPreviewWidget(), app, height=300)
    widget.set_document(document)
    widget.set_style({"show_pronunciation": True, "show_translation": True})
    widget.set_background(None)
    widget.set_position(1)
    before = widget.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
    widget.set_position(4)
    after = widget.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
    assert before.size() == after.size()
    changed = sum(
        before.pixel(x, y) != after.pixel(x, y)
        for x in range(before.width())
        for y in range(before.height())
    )
    assert changed > 30
    # Thin CJK glyphs must retain a light fill after their outline is painted.
    light_pixels = sum(
        before.pixelColor(x, y).red() > 180
        and before.pixelColor(x, y).green() > 180
        and before.pixelColor(x, y).blue() > 180
        for x in range(before.width())
        for y in range(before.height() // 2, before.height())
    )
    sung_pixels = sum(
        after.pixelColor(x, y).red() > 180
        and after.pixelColor(x, y).green() > 130
        and after.pixelColor(x, y).blue() < 140
        for x in range(after.width())
        for y in range(after.height() // 2, after.height())
    )
    assert light_pixels > 20
    assert sung_pixels > 20
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
