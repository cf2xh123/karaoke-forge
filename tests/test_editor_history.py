from __future__ import annotations

import json

import pytest

from karaoke_forge.editor import (
    document_from_payload,
    document_pronunciation_to_editor_rows,
    document_to_editor_rows,
    token_timing_to_json,
)
from karaoke_forge.editor_history import EDITOR_HISTORY_LIMIT, record_history
from karaoke_forge.models import KaraokeToken, LyricLine, LyricsDocument
from karaoke_forge.web import (
    _editor_undo_snapshot,
    create_web_app,
    redo_editor_line_action,
    undo_editor_line_action,
)


def make_document() -> LyricsDocument:
    return LyricsDocument(
        lines=[
            LyricLine(
                text=text,
                start=start,
                end=start + 1,
                tokens=[KaraokeToken(text=text, start=start, end=start + 1)],
            )
            for text, start in [("A", 1.0), ("B", 3.0), ("C", 5.0)]
        ]
    )


def travel(document, history, *, redo=False, selected=1, token_json=None, whole=None, units=None):
    callback = redo_editor_line_action if redo else undo_editor_line_action
    result = callback(
        document.to_dict(),
        document_to_editor_rows(document),
        selected,
        history,
        token_json,
        whole,
        units,
    )
    return document_from_payload(result[0]), result[9], result


def test_multiple_undos_and_redos_restore_documents_and_focus() -> None:
    original = make_document()
    first = document_from_payload(original.to_dict())
    first.lines[0].hidden = True
    second = document_from_payload(first.to_dict())
    second.lines[1].hidden = True
    history = record_history({}, _editor_undo_snapshot(original, 1))
    history = record_history(history, _editor_undo_snapshot(first, 2))

    current, history, result = travel(second, history, selected=3)
    assert current.to_dict() == first.to_dict()
    assert result[2] == 2
    current, history, result = travel(current, history, selected=2)
    assert current.to_dict() == original.to_dict()
    assert result[2] == 1
    current, history, result = travel(current, history, redo=True)
    assert current.to_dict() == first.to_dict()
    assert result[2] == 2
    current, history, result = travel(current, history, redo=True, selected=2)
    assert current.to_dict() == second.to_dict()
    assert result[2] == 3
    assert not history["future"]


def test_unsaved_token_and_pronunciation_draft_is_recoverable() -> None:
    original = make_document()
    committed = document_from_payload(original.to_dict())
    committed.lines[2].hidden = True
    history = record_history({}, _editor_undo_snapshot(original, 3))
    draft_json = json.dumps([{"text": "A", "start": 1.1, "end": 1.9}])

    current, history, result = travel(
        committed, history, token_json=draft_json, whole="ei", units=[["A", "ei", 0, 1]]
    )
    assert current.to_dict() == committed.to_dict()
    assert len(history["past"]) == 1
    assert result[3] == ""
    current, history, result = travel(current, history, redo=True)
    assert current.lines[0].tokens[0].start == pytest.approx(1.1)
    assert current.lines[0].tokens[0].end == pytest.approx(1.9)
    assert current.lines[0].pronunciation == "ei"
    assert current.lines[0].pronunciation_units[0].reading == "ei"
    assert current.lines[2].hidden


def test_pending_text_draft_can_be_undone_with_empty_history() -> None:
    original = make_document()
    current, history, _ = travel(original, {}, token_json='[{"text":"NEW","start":1.0,"end":2.0}]')
    assert current.to_dict() == original.to_dict()
    current, _history, _ = travel(current, history, redo=True)
    assert current.lines[0].text == "NEW"


def test_pending_overview_draft_is_undone_before_the_previous_committed_edit() -> None:
    original = make_document()
    committed = document_from_payload(original.to_dict())
    committed.lines[2].hidden = True
    history = record_history({}, _editor_undo_snapshot(original, 3))
    rows = document_to_editor_rows(committed)
    rows[1][3] = 4.4
    result = undo_editor_line_action(committed.to_dict(), rows, 2, history)
    assert result[0] == committed.to_dict()
    current, history, _ = travel(document_from_payload(result[0]), result[9])
    assert current.to_dict() == original.to_dict()
    current, history, _ = travel(current, history, redo=True)
    current, _, _ = travel(current, history, redo=True)
    assert current.lines[1].end == pytest.approx(4.4)
    assert current.lines[2].hidden


@pytest.mark.parametrize("draft_kind", ["empty_text", "invalid_time", "pronunciation", "overlap"])
def test_undo_discards_invalid_draft_before_consuming_committed_history(draft_kind) -> None:
    original = make_document()
    committed = document_from_payload(original.to_dict())
    committed.lines[2].hidden = True
    history = record_history({}, _editor_undo_snapshot(original, 3))
    history["future"] = [_editor_undo_snapshot(original, 2)]
    rows = document_to_editor_rows(committed)
    token_json = token_timing_to_json(committed.lines[0])
    units = None
    if draft_kind == "empty_text":
        rows[0][4] = ""
    elif draft_kind == "invalid_time":
        rows[0][3] = 0.5
    elif draft_kind == "pronunciation":
        units = [["A", "ei", 0, 99]]
    else:
        token_json = json.dumps(
            [
                {"text": "A", "start": 1.0, "end": 1.8},
                {"text": "X", "start": 1.5, "end": 2.0},
            ]
        )

    result = undo_editor_line_action(committed.to_dict(), rows, 1, history, token_json, "", units)
    assert result[0] == committed.to_dict()
    assert result[1] == document_to_editor_rows(committed)
    assert result[2] == 1
    assert result[4] == document_pronunciation_to_editor_rows(committed, committed.lines[0])
    assert result[7] == token_timing_to_json(committed.lines[0])
    assert "已撤销无效草稿" in result[8]
    assert result[9] == history
    assert len(result[9]["past"]) == len(result[9]["future"]) == 1

    restored, _, _ = travel(document_from_payload(result[0]), result[9])
    assert restored.to_dict() == original.to_dict()


def test_undo_discards_invalid_draft_even_without_history() -> None:
    document = make_document()
    rows = document_to_editor_rows(document)
    rows[0][4] = ""
    result = undo_editor_line_action(document.to_dict(), rows, 1, {})
    assert result[0] == document.to_dict()
    assert result[9] == {}
    assert "已撤销无效草稿" in result[8]


def test_redo_does_not_discard_invalid_drafts() -> None:
    document = make_document()
    rows = document_to_editor_rows(document)
    rows[0][4] = ""
    history = {"past": [], "future": [_editor_undo_snapshot(document, 1)]}
    with pytest.raises(ValueError, match="原文已清空"):
        redo_editor_line_action(document.to_dict(), rows, 1, history)
    assert len(history["future"]) == 1


def test_new_draft_after_undo_clears_redo_and_remains_undoable() -> None:
    original = make_document()
    changed = document_from_payload(original.to_dict())
    changed.lines[1].hidden = True
    history = record_history({}, _editor_undo_snapshot(original, 2))
    current, history, _ = travel(changed, history)
    current, history, result = travel(
        current,
        history,
        redo=True,
        token_json='[{"text":"NEW","start":1.0,"end":2.0}]',
    )
    assert current.lines[0].text == "NEW"
    assert not current.lines[1].hidden
    assert not history["future"]
    assert "暂无可重做" in result[8]
    current, _, _ = travel(current, history)
    assert current.to_dict() == original.to_dict()


def test_noop_preserves_redo_and_history_is_bounded_and_isolated() -> None:
    document = make_document()
    history = {}
    for index in range(EDITOR_HISTORY_LIMIT + 8):
        document.lines[0].text = str(index)
        snapshot = _editor_undo_snapshot(document, 1)
        history = record_history(history, snapshot)
    assert len(history["past"]) == EDITOR_HISTORY_LIMIT
    assert history["past"][0]["document"]["lines"][0]["text"] == "8"
    snapshot["document"]["lines"][0]["text"] = "mutated"
    assert history["past"][-1]["document"]["lines"][0]["text"] != "mutated"
    history["future"] = [_editor_undo_snapshot(make_document(), 1)]
    assert record_history(history, None) == history


@pytest.fixture(scope="module")
def editor_callbacks(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path_factory.mktemp("history-output")))
        patch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
        app = create_web_app()
        yield {
            **{getattr(fn.fn, "__name__", ""): fn for fn in app.fns.values()},
            "__app__": app,
        }
        app.close()


def test_mixed_global_drag_and_line_action_share_history(editor_callbacks) -> None:
    document = make_document()
    edge = editor_callbacks["apply_global_line_edge_workspace"].fn
    action = editor_callbacks["apply_editor_line_action_workspace"].fn
    first = edge(
        document.to_dict(),
        document_to_editor_rows(document),
        1,
        token_timing_to_json(document.lines[0]),
        "",
        [],
        {},
        False,
        None,
        json.dumps(
            {
                "line": 1,
                "edge": "end",
                "seconds": 2.5,
                "base_start": 1.0,
                "base_end": 2.0,
                "text": "A",
            }
        ),
    )
    second = action(
        first[0],
        first[1],
        first[2],
        first[7],
        first[3],
        first[4],
        '{"row":1,"action":"delete"}',
        False,
        first[10],
    )
    assert len(second[9]["past"]) == 2
    current, history, _ = travel(document_from_payload(second[0]), second[9])
    assert current.to_dict() == first[0]
    current, history, _ = travel(current, history)
    assert current.to_dict() == document.to_dict()
    current, history, _ = travel(current, history, redo=True)
    current, history, _ = travel(current, history, redo=True)
    assert current.to_dict() == second[0]


def test_saved_tokens_form_steps_and_noop_save_keeps_redo(editor_callbacks) -> None:
    save = editor_callbacks["save_editor_token_timing_workspace"].fn
    document = make_document()
    history = {}
    for end in [2.2, 2.4]:
        result = save(
            document.to_dict(),
            document_to_editor_rows(document),
            1,
            json.dumps([{"text": "A", "start": 1.0, "end": end}]),
            "",
            [],
            history,
            False,
        )
        document, history = document_from_payload(result[0]), result[9]
    assert len(history["past"]) == 2
    document, history, _ = travel(document, history)
    noop = save(
        document.to_dict(),
        document_to_editor_rows(document),
        1,
        token_timing_to_json(document.lines[0]),
        "",
        [],
        history,
        False,
    )
    assert noop[9] == history
    document, history, _ = travel(document, noop[9], redo=True)
    assert document.lines[0].end == pytest.approx(2.4)


def test_navigation_keeps_existing_history_and_commits_drafts(editor_callbacks) -> None:
    load = editor_callbacks["load_editor_line_workspace"].fn
    document = make_document()
    history = {"past": [], "future": [_editor_undo_snapshot(document, 1)]}
    clean = load(
        document.to_dict(),
        document_to_editor_rows(document),
        1,
        token_timing_to_json(document.lines[0]),
        "",
        [],
        history,
        False,
    )
    assert clean[8] == history
    dirty = load(
        document.to_dict(),
        document_to_editor_rows(document),
        1,
        '[{"text":"NEW","start":1.0,"end":2.0}]',
        "",
        [],
        history,
        False,
    )
    assert len(dirty[8]["past"]) == 1
    assert not dirty[8]["future"]


def test_undo_redo_callbacks_accept_all_draft_inputs(editor_callbacks) -> None:
    for name in ["undo_editor_line_action", "redo_editor_line_action"]:
        callback = editor_callbacks[name]
        assert len(callback.inputs) == 8
        assert len(callback.outputs) == 10
        assert callback.inputs[4].elem_id == "kf-token-json"


def test_mutations_acknowledge_success_and_failure(editor_callbacks) -> None:
    app = editor_callbacks["__app__"]
    component_ids = {
        component.elem_id: component._id
        for component in app.blocks.values()
        if getattr(component, "elem_id", None)
    }
    ack_id = component_ids["kf-editor-mutation-ack"]
    dependencies = app.config["dependencies"]
    for elem_id in [
        "kf-global-edge-apply",
        "editor-save-global-rows",
        "editor-shift-global",
        "kf-line-context-apply",
        "editor-toggle-line",
        "editor-delete-line",
        "editor-undo",
        "editor-redo",
        "editor-start-earlier",
        "editor-start-later",
        "editor-end-earlier",
        "editor-end-later",
        "editor-save-tokens",
        "editor-save-pronunciation",
        "editor-export",
        "editor-handoff",
    ]:
        mutation = next(
            dependency
            for dependency in dependencies
            if (component_ids[elem_id], "click") in dependency["targets"]
            and dependency["backend_fn"]
            and dependency["outputs"]
        )
        acknowledgement = next(
            dependency
            for dependency in dependencies
            if dependency.get("trigger_after") == mutation["id"]
            and dependency["outputs"] == [ack_id]
        )
        assert acknowledgement["trigger_only_on_success"] is False
        assert acknowledgement["show_progress"] == "hidden"


def test_redo_restores_an_all_hidden_editor_state() -> None:
    document = make_document()
    hidden = document_from_payload(document.to_dict())
    for line in hidden.lines:
        line.hidden = True
    history = record_history({}, _editor_undo_snapshot(document, 1))
    current, history, _ = travel(hidden, history)
    current, history, _ = travel(current, history, redo=True)
    assert all(line.hidden for line in current.lines)
