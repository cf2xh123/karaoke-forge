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
    editor_preview_html,
    editor_token_timeline_html,
    nudge_editor_line_timing,
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


def test_editor_nudges_line_bounds_and_keeps_word_timing_in_sync() -> None:
    document = parse_lrc("[00:01.00]Hello world\n[00:03.00]Next\n")
    rows = document_to_editor_rows(document)

    edited = nudge_editor_line_timing(
        document,
        rows,
        1,
        start_delta=0.1,
        end_delta=-0.1,
    )

    assert edited.lines[0].start == 1.1
    assert edited.lines[0].end == 2.88
    assert edited.lines[0].tokens[0].start == 1.1
    assert edited.lines[0].tokens[-1].end == 2.88


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
    assert timeline.count("kf-token-boundary") == 3
    assert "点击词块可单独试听" in timeline
    assert "kf-token-playhead" in timeline
    assert "前一段" in timeline
    assert "后一段" in timeline
    assert "缩小" in timeline
    assert "适应全句" in timeline
    assert "放大" in timeline
    assert "data-base-width" in timeline
    assert "撤销拖动" in timeline
    assert "重做" in timeline

    preview = editor_preview_html(edited, 1)
    assert "kf-live-karaoke-current" in preview
    assert "kf-live-karaoke-fill" in preview
    assert "kf-editor-preview-stage" in preview
    assert "kf-editor-preview-upper" in preview
    assert "kf-editor-preview-lower" in preview
    assert 'data-line-number="1"' in preview
    assert 'data-line-count="1"' in preview
    assert "--kf-preview-font-size" in preview
