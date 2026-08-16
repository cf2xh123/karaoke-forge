import json

import pytest

from karaoke_forge.ass import write_ass
from karaoke_forge.editor import (
    LINE_STATUS_DELETED,
    LINE_STATUS_HIDDEN,
    LINE_STATUS_VISIBLE,
    apply_editor_rows,
    apply_pronunciation_rows,
    apply_token_timing,
    document_from_payload,
    document_to_editor_rows,
    editor_global_timeline_html,
    editor_preview_html,
    editor_token_timeline_html,
    nudge_editor_line_timing,
    ripple_following_line_timing,
    shift_editor_timeline,
    token_timing_to_json,
)
from karaoke_forge.formats import parse_lrc, parse_yrc, write_json, write_lrc, write_srt


def test_editor_hides_and_deletes_complete_lyric_rows() -> None:
    document = parse_lrc("[00:01.00]Credit\n[00:03.00]Wrong duplicate\n[00:05.00]Original line\n")
    document.lines[0].translation = "署名"
    rows = document_to_editor_rows(document)
    rows[0][1] = LINE_STATUS_HIDDEN
    rows[1][1] = LINE_STATUS_DELETED
    rows[2][1] = LINE_STATUS_VISIBLE
    rows[2][4] = "Corrected line"
    rows[2][5] = "修正后的歌词"

    edited = apply_editor_rows(document, rows)

    assert len(edited.lines) == 2
    assert edited.lines[0].hidden
    assert edited.lines[0].translation == "署名"
    assert edited.lines[1].text == "Corrected line"
    assert edited.lines[1].translation == "修正后的歌词"
    assert edited.lines[1].tokens
    assert "Credit" not in write_lrc(edited)
    assert "Wrong duplicate" not in write_lrc(edited)
    assert "Corrected line" in write_lrc(edited)
    assert "Credit" not in write_srt(edited)
    assert "Credit" not in write_ass(edited)

    restored = document_from_payload(edited.to_dict())
    assert restored.lines[0].hidden
    assert '"hidden": true' in write_json(restored)


def test_editor_preserves_a_source_timed_blank_interlude() -> None:
    document = parse_lrc("[00:01.00]First\n[00:03.00]\n[00:05.00]Third\n")

    edited = apply_editor_rows(document, document_to_editor_rows(document))

    assert [line.text for line in edited.lines] == ["First", "", "Third"]
    assert edited.lines[1].start == document.lines[1].start
    assert edited.lines[1].end == document.lines[1].end
    assert edited.lines[1].tokens == []


def test_editor_rejects_a_cleared_nonempty_line_until_it_is_deleted() -> None:
    document = parse_lrc("[00:01.00]First\n[00:03.00]Second\n")
    rows = document_to_editor_rows(document)
    rows[1][4] = ""

    with pytest.raises(ValueError, match="🗑 删除"):
        apply_editor_rows(document, rows)


def test_token_delete_preserves_an_untouched_source_overlap() -> None:
    document = parse_yrc(
        "[1000,3000](1000,900,0)A(1900,110,0),(2000,1000,0)B\n"
    )
    entries = json.loads(token_timing_to_json(document.lines[0]))
    entries.pop(0)

    edited = apply_token_timing(
        document,
        document_to_editor_rows(document),
        1,
        json.dumps(entries),
    )

    assert edited.lines[0].text == ",B"
    assert edited.lines[0].tokens[0].end == 2.01
    assert edited.lines[0].tokens[1].start == 2.0


def test_token_edit_still_rejects_a_new_overlap() -> None:
    document = parse_yrc(
        "[1000,3000](1000,900,0)A(1900,110,0),(2000,1000,0)B\n"
    )
    entries = json.loads(token_timing_to_json(document.lines[0]))
    entries[-1]["start"] = 1.98

    with pytest.raises(ValueError, match="时间发生重叠"):
        apply_token_timing(
            document,
            document_to_editor_rows(document),
            1,
            json.dumps(entries),
        )


def test_editor_scales_existing_tokens_when_line_time_changes() -> None:
    document = parse_lrc("[00:01.00]Hello world\n[00:03.00]Next\n")
    rows = document_to_editor_rows(document)
    rows[0][2] = 2.0
    rows[0][3] = 6.0

    edited = apply_editor_rows(document, rows)

    assert edited.lines[0].start == 2.0
    assert edited.lines[0].end == 6.0
    assert edited.lines[0].tokens[0].start == 2.0
    assert edited.lines[0].tokens[-1].end == 6.0


def test_editor_nudges_only_edge_tokens_and_keeps_the_interior_fixed() -> None:
    document = parse_lrc("[00:01.00]Hello brave world\n[00:04.00]Next\n")
    rows = document_to_editor_rows(document)
    original = [
        (token.start, token.end)
        for token in document.lines[0].tokens
    ]

    edited = nudge_editor_line_timing(
        document,
        rows,
        1,
        start_delta=0.1,
        end_delta=-0.1,
    )

    assert edited.lines[0].start == 1.1
    assert edited.lines[0].end == 3.88
    assert edited.lines[0].tokens[0].start == 1.1
    assert edited.lines[0].tokens[0].end == original[0][1]
    assert (edited.lines[0].tokens[1].start, edited.lines[0].tokens[1].end) == original[1]
    assert edited.lines[0].tokens[-1].start == original[-1][0]
    assert edited.lines[0].tokens[-1].end == 3.88


def test_editor_ripple_pushes_following_lines_without_retiming_their_tokens() -> None:
    document = parse_yrc(
        "[1000,2000](1000,1000,0)A(2000,1000,0)B\n"
        "[3050,1000](3050,500,0)C(3550,500,0)D\n"
        "[4100,1000](4100,500,0)E(4600,500,0)F\n"
        "[7000,1000](7000,1000,0)G\n"
    )
    rows = document_to_editor_rows(document)
    second_offsets = [
        (token.start - document.lines[1].start, token.end - document.lines[1].start)
        for token in document.lines[1].tokens
    ]

    edited = nudge_editor_line_timing(document, rows, 1, end_delta=0.2)

    assert edited.lines[0].end == pytest.approx(3.2)
    assert edited.lines[1].start == pytest.approx(3.22)
    assert edited.lines[2].start == pytest.approx(4.24)
    assert edited.lines[3].start == pytest.approx(7.0)
    shifted_offsets = [
        (token.start - edited.lines[1].start, token.end - edited.lines[1].start)
        for token in edited.lines[1].tokens
    ]
    for shifted, original in zip(shifted_offsets, second_offsets):
        assert shifted == pytest.approx(original)


def test_editor_ripple_can_be_disabled_for_intentional_overlaps() -> None:
    document = parse_lrc("[00:01.00]First\n[00:03.00]Second\n[00:05.00]Third\n")
    rows = document_to_editor_rows(document)

    edited = nudge_editor_line_timing(
        document,
        rows,
        1,
        end_delta=1.0,
        ripple_following=False,
    )

    assert edited.lines[0].end > edited.lines[1].start
    assert edited.lines[1].start == document.lines[1].start


def test_ripple_preserves_an_existing_overlap_instead_of_cleaning_the_document() -> None:
    document = parse_lrc("[00:01.00]First\n[00:03.00]Second\n[00:05.00]Third\n")
    document.lines[0].end = 3.5
    document.lines[0].tokens[-1].end = 3.5
    document.lines[1].end = 5.0
    document.lines[1].tokens[-1].end = 5.0
    old_overlap = document.lines[0].end - document.lines[1].start
    document.lines[0].end += 0.2
    document.lines[0].tokens[-1].end += 0.2

    shifted = ripple_following_line_timing(document, 1, previous_end=3.5)

    assert shifted == (2, 3)
    assert document.lines[0].end - document.lines[1].start == pytest.approx(old_overlap)


def test_ripple_keeps_hidden_and_timed_blank_rows_on_the_same_timeline() -> None:
    document = parse_lrc(
        "[00:01.00]First\n[00:03.00]\n[00:04.00]Hidden cue\n[00:05.00]Last\n"
    )
    document.lines[2].hidden = True
    old_starts = [line.start for line in document.lines]
    assert document.lines[0].end is not None
    previous_end = document.lines[0].end
    document.lines[0].end += 2.5
    document.lines[0].tokens[-1].end = document.lines[0].end

    shifted = ripple_following_line_timing(
        document,
        1,
        previous_end=previous_end,
    )

    assert 2 in shifted
    assert 3 in shifted
    assert document.lines[1].start > old_starts[1]
    assert document.lines[2].start > old_starts[2]


def test_global_shift_moves_line_and_token_times_by_the_same_offset() -> None:
    document = parse_yrc(
        "[1000,1000](1000,500,0)A(1500,500,0)B\n"
        "[3000,1000](3000,1000,0)C\n"
    )

    shifted, applied = shift_editor_timeline(
        document,
        document_to_editor_rows(document),
        -5.0,
        start_line=2,
    )

    assert applied == pytest.approx(-3.0)
    assert shifted.lines[0].start == 1.0
    assert shifted.lines[1].start == 0.0
    assert shifted.lines[1].tokens[0].start == 0.0
    assert shifted.lines[1].end - shifted.lines[1].start == pytest.approx(1.0)


def test_global_timeline_renders_all_playable_lines_and_selected_line() -> None:
    document = parse_lrc("[00:01.00]A\n[00:03.00]B\n[00:05.00]C\n")

    timeline = editor_global_timeline_html(document, 2)

    assert timeline.count("kf-global-line-block") == 3
    assert 'data-line-number="2"' in timeline
    assert "is-selected" in timeline
    assert "kf-global-playhead" in timeline


def test_global_timeline_uses_real_media_duration_for_intro_and_outro() -> None:
    document = parse_lrc("[00:10.00]A\n[00:20.00]B\n")

    timeline = editor_global_timeline_html(document, 1, media_duration=180.0)

    assert 'data-duration="180.000000"' in timeline


def test_editor_saves_word_pronunciation_and_ass_uses_it() -> None:
    document = parse_lrc("[00:01.00]花が咲く\n[00:04.00]次\n")

    edited = apply_pronunciation_rows(
        document,
        1,
        [
            ["花", "はな", 0, 1],
            ["咲", "さ", 2, 3],
        ],
        "",
    )

    assert [unit.reading for unit in edited.lines[0].pronunciation_units] == [
        "はな",
        "さ",
    ]
    output = write_ass(edited)
    assert "はな" in output
    assert "さ" in output
    preview = editor_preview_html(edited, 1)
    assert "<ruby " in preview
    assert "color:inherit !important" in preview
    assert '"pronunciation_units"' in write_json(edited)


def test_whole_line_pronunciation_remains_an_independent_fallback() -> None:
    document = parse_lrc("[00:01.00]Hello\n")
    document.lines[0].pronunciation = "ハロー"

    edited = apply_pronunciation_rows(document, 1, [], "ヘロー")

    assert not edited.lines[0].pronunciation_units
    assert "ヘロー" in write_ass(edited)
    assert "ヘロー" in editor_preview_html(edited, 1)


def test_editor_preview_shows_current_and_next_ktv_rows() -> None:
    document = parse_lrc("[00:01.00]A\n[00:03.00]B\n[00:05.00]C\n")

    first = editor_preview_html(document, 1)
    second = editor_preview_html(document, 2)

    assert "A" in first and "B" in first
    assert "B" in second and "C" in second
    assert "当前句黄色 / 下一句白色" in second


def test_editor_applies_visual_per_token_timing() -> None:
    document = parse_yrc("[1000,2000](1000,1000,0)slow(2000,1000,0) word\n")
    rows = document_to_editor_rows(document)
    timing = '[{"text":"slow","start":1.0,"end":2.5},{"text":" word","start":2.5,"end":3.0}]'

    edited = apply_token_timing(document, rows, 1, timing)

    assert edited.lines[0].start == 1.0
    assert edited.lines[0].end == 3.0
    assert edited.lines[0].tokens[0].end == 2.5
    assert edited.lines[0].tokens[1].start == 2.5
    assert edited.metadata["word_timing"] == "manual"
    assert '"end": 2.5' in token_timing_to_json(edited.lines[0])

    timeline = editor_token_timeline_html(edited, 1)
    assert timeline.count("kf-token-block") == 2
    assert timeline.count("kf-token-boundary") == 4
    assert timeline.count("kf-token-text") == 2
    assert "点击词块空白处可试听" in timeline
    assert "右键词块可立即移除" in timeline
    assert "按住时间轴空白处左右拖动" in timeline
    assert "kf-token-playhead" in timeline
    assert "前一段" in timeline
    assert "后一段" in timeline
    assert "缩小" in timeline
    assert "适应全句" in timeline
    assert "放大" in timeline
    assert "data-base-width" in timeline
    assert "撤销拖动" in timeline
    assert "重做" in timeline
    assert 'data-line-number="1"' in timeline
    assert 'data-line-start="1.000"' in timeline
    assert 'data-line-end="3.000"' in timeline

    preview = editor_preview_html(edited, 1)
    assert "kf-live-karaoke-current" in preview
    assert "kf-live-karaoke-fill" in preview
    assert "kf-live-karaoke-measure" in preview
    assert preview.count("kf-karaoke-token-core") == 2
    assert "kf-editor-preview-stage" in preview
    assert "kf-editor-preview-upper" in preview
    assert "kf-editor-preview-lower" in preview
    assert 'data-line-number="1"' in preview
    assert 'data-line-count="1"' in preview
    assert "--kf-preview-font-size" in preview


def test_editor_deletes_token_text_without_changing_other_token_times() -> None:
    document = parse_yrc("[1000,3000](1000,1000,0)A(2000,1000,0)B(3000,1000,0)C\n")
    document = apply_pronunciation_rows(
        document,
        1,
        [["A", "a", 0, 1], ["C", "c", 2, 3]],
        "a b c",
    )
    rows = document_to_editor_rows(document)
    timing = '[{"text":"A","start":1.0,"end":2.0},{"text":"C","start":3.0,"end":4.0}]'

    edited = apply_token_timing(document, rows, 1, timing)

    assert edited.lines[0].text == "AC"
    assert [(token.text, token.start, token.end) for token in edited.lines[0].tokens] == [
        ("A", 1.0, 2.0),
        ("C", 3.0, 4.0),
    ]
    assert edited.lines[0].pronunciation is None
    assert [
        (unit.source, unit.reading, unit.start, unit.end)
        for unit in edited.lines[0].pronunciation_units
    ] == [("A", "a", 0, 1), ("C", "c", 1, 2)]
    timeline = editor_token_timeline_html(edited, 1)
    assert 'data-token-index="1" data-edge="start"' in timeline
    assert 'value="3.0"' in timeline


def test_editor_deletion_preserves_untouched_submillisecond_times() -> None:
    document = parse_yrc("[1000,3000](1000,1000,0)A(2000,1000,0)B(3000,1000,0)C\n")
    document.lines[0].tokens[0].start = 1.123456789
    document.lines[0].tokens[0].end = 1.987654321
    document.lines[0].tokens[2].start = 3.111111111
    document.lines[0].tokens[2].end = 4.222222222
    rows = document_to_editor_rows(document)
    entries = json.loads(token_timing_to_json(document.lines[0]))
    entries.pop(1)

    edited = apply_token_timing(document, rows, 1, json.dumps(entries))

    assert (edited.lines[0].tokens[0].start, edited.lines[0].tokens[0].end) == (
        1.123456789,
        1.987654321,
    )
    assert (edited.lines[0].tokens[1].start, edited.lines[0].tokens[1].end) == (
        3.111111111,
        4.222222222,
    )


def test_editor_rows_do_not_retime_tokens_when_line_bounds_are_unchanged() -> None:
    document = parse_yrc("[70,6390](70,477,0)A(547,478,0)B(1025,477,0)C\n")
    document.lines[0].tokens[1].start = 0.5476030927834948
    document.lines[0].tokens[1].end = 1.0252061855669896

    edited = apply_editor_rows(document, document_to_editor_rows(document))

    assert edited.lines[0].tokens == document.lines[0].tokens
