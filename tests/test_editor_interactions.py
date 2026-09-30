"""Behavioral probes of the editor's actual JavaScript and navigation callbacks."""

from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from karaoke_forge.editor import document_to_editor_rows, token_timing_to_json
from karaoke_forge.models import KaraokeToken, LyricLine, LyricsDocument
from karaoke_forge.web import TOKEN_TIMELINE_JS, create_web_app


def js_helper(name: str, *, indent: int = 2) -> str:
    """Extract an existing helper verbatim; the probes do not reimplement it."""

    prefix = " " * indent
    start = TOKEN_TIMELINE_JS.index(f"{prefix}const {name} =")
    end = TOKEN_TIMELINE_JS.index(f"\n{prefix}}};", start) + len(prefix) + 4
    return TOKEN_TIMELINE_JS[start:end]


def run_javascript(body: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the browser-independent JavaScript probes")
    completed = subprocess.run(
        [node, "-"],
        input="const assert = require('node:assert/strict');\n" + body,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


PREVIEW_ENVIRONMENT = r"""
const hiddenClasses = new Set();
const stage = {
  dataset: {lineNumber:'1', lineStart:'1', lineEnd:'2'},
  innerHTML: '<ruby>A<rt>UNSAVED ei</rt></ruby>',
  classList: {toggle(name, enabled) {
    if (enabled) hiddenClasses.add(name); else hiddenClasses.delete(name);
  }},
};
let replaced = false;
const timeline = {
  dataset:{lineCount:'2'},
  __kfPreviewLines:[
    {number:1,start:1,end:2,ruby:'COMMITTED A',translation:'first',tokens:[{text:'A'}]},
    {number:2,start:2,end:3,ruby:'NEXT B',translation:'second',tokens:[{text:'B'}]},
  ],
};
const host = {querySelector:()=>stage, replaceChildren:()=>{replaced=true;}};
const document = {querySelector:()=>host};
const escapeEditorText = value => String(value ?? '');
const visibleElement = selector => selector === '.kf-editor-preview-stage' ? stage : {};
const workspaceLinesMatch = () => true;
const lineBlock = number => ({
  dataset:{lineNumber:String(number),start:String(number),end:String(number+1)},
  closest:()=>timeline,
});
"""


def test_global_preview_preserves_same_line_unsaved_pronunciation() -> None:
    run_javascript(
        PREVIEW_ENVIRONMENT
        + js_helper("showGlobalPreview")
        + """
showGlobalPreview(lineBlock(1));
assert.match(stage.innerHTML, /UNSAVED ei/);
assert.equal(replaced, false);
"""
    )


def test_adjacent_playback_lines_keep_preview_visible_without_server_response() -> None:
    run_javascript(
        PREVIEW_ENVIRONMENT
        + js_helper("showGlobalPreview")
        + js_helper("updateGlobalKaraokeAt")
        + """
updateGlobalKaraokeAt(lineBlock(1), 1.999);
assert.equal(hiddenClasses.has('is-global-gap'), false);
// The backend has not returned another preview or token editor yet.
updateGlobalKaraokeAt(lineBlock(2), 2.0);
assert.equal(stage.dataset.lineNumber, '2');
assert.match(stage.innerHTML, /NEXT B/);
assert.equal(hiddenClasses.has('is-global-gap'), false);
assert.equal(replaced, false);
updateGlobalKaraokeAt(null, 3.5);
assert.equal(hiddenClasses.has('is-global-gap'), true);
"""
    )


def drag_environment() -> str:
    grab_start = TOKEN_TIMELINE_JS.index("    const grabOffset =")
    grab_end = TOKEN_TIMELINE_JS.index(";", grab_start) + 1
    return (
        """
function moveEdge({edgeName='end', raw=2.43, initial=2, candidates=[], bypass=false,
                   enabled=true, firstTokenEnd=1.4, lastTokenStart=1.4}={}) {
  const duration=10, baseStart=1, baseEnd=2, lineNumber=1;
  const track={getBoundingClientRect:()=>({left:100,width:140})};
  const pointerStartX=100 + initial / duration * 140 + 10;
  let previewSeconds=initial, snapTarget=null;
  const editorPreferences={snap_enabled:enabled};
  const timeline={querySelectorAll:()=>candidates.map(item=>({dataset:item}))};
"""
        + TOKEN_TIMELINE_JS[grab_start:grab_end]
        + js_helper("secondsFromPointer", indent=4)
        + """
  const seconds=secondsFromPointer(100 + raw / duration * 140 + 10, bypass);
  return {seconds,snapTarget};
}
"""
    )


def test_edge_drag_keeps_the_pointer_grab_offset() -> None:
    run_javascript(
        drag_environment()
        + """
// A one-pixel move at 14 pixels/s must not include the 10-pixel handle grab offset.
assert.equal(moveEdge({raw:2 + 1/14}).seconds, 2.07);
assert.equal(moveEdge({raw:2}).seconds, 2.0);
"""
    )


def test_edge_snap_is_precise_and_can_be_temporarily_or_persistently_disabled() -> None:
    run_javascript(
        drag_environment()
        + """
const candidates=[
  {lineNumber:'1',edge:'end',end:'2.43'},
  {lineNumber:'2',edge:'start',start:'2.503'},
];
const snapped=moveEdge({candidates});
assert.equal(snapped.seconds,2.503);
assert.equal(snapped.snapTarget.line,2);
assert.equal(moveEdge({candidates,bypass:true}).seconds,2.43);
assert.equal(moveEdge({candidates,enabled:false}).seconds,2.43);
assert.equal(moveEdge({candidates,raw:2.2}).snapTarget,null);
"""
    )


def test_edge_snap_never_crosses_the_first_or_last_token() -> None:
    run_javascript(
        drag_environment()
        + """
const tail=moveEdge({raw:1.4,lastTokenStart:1.4,
  candidates:[{lineNumber:'2',edge:'start',start:'1.4'}]});
assert.equal(tail.seconds,1.41);
assert.equal(tail.snapTarget,null);
const head=moveEdge({edgeName:'start',initial:1,raw:1.4,firstTokenEnd:1.3,
  candidates:[{lineNumber:'2',edge:'end',end:'1.4'}]});
assert.equal(head.seconds,1.29);
assert.equal(head.snapTarget,null);
"""
    )


def test_navigation_serializes_requests_and_ignores_old_acknowledgements() -> None:
    poll_start = TOKEN_TIMELINE_JS.index("    const mutation = window.__kfEditorMutationPending;")
    poll_end = TOKEN_TIMELINE_JS.index("    const globalMode =", poll_start)
    run_javascript(
        """
const fields={}, sent=[], timers=[], busy=[];
let displayed=1;
const window={setTimeout:fn=>timers.push(fn)};
const setEditorBusy=value=>busy.push(value);
const setHiddenInput=(selector,value)=>{fields[selector]=value;return true;};
const displayedEditorLine=()=>displayed;
const document={querySelector(selector) {
  if(selector.includes('kf-editor-mutation-ack')) return {value:fields.mutationAck||''};
  if(selector.includes('kf-global-navigation-ack')) return {value:fields.navAck||''};
  if(selector === '#kf-global-select-line') return {
    matches:()=>true,
    click:()=>sent.push({line:Number(fields['#kf-global-line-request']),
                        id:fields['#kf-global-navigation-id']}),
  };
  return null;
}};
const flush=()=>{while(timers.length)timers.shift()();};
"""
        + js_helper("selectGlobalLine")
        + "function acknowledge(){\n"
        + TOKEN_TIMELINE_JS[poll_start:poll_end]
        + "}\n"
        + """
selectGlobalLine(2); flush();
selectGlobalLine(3); selectGlobalLine(4); flush();
assert.deepEqual(sent.map(request=>request.line),[2]);
fields.navAck=sent[0].id;
acknowledge(); flush();
assert.deepEqual(sent.map(request=>request.line),[2,4]);
const pending=window.__kfGlobalNavigation.pending.id;
// An old acknowledgement must not unlock the still-pending latest selection.
acknowledge();
assert.equal(window.__kfGlobalNavigation.pending.id,pending);
displayed=4; fields.navAck=sent[1].id;
acknowledge();
assert.equal(window.__kfGlobalNavigation.pending,null);
assert.equal(window.__kfGlobalNavigation.latest,null);
// Undo restores focus to line 1. There must be no leftover selection of line 4.
displayed=1; acknowledge(); flush();
assert.deepEqual(sent.map(request=>request.line),[2,4]);
assert.equal(busy.at(-1),false);
"""
    )


def test_late_navigation_response_does_not_seek_the_audio_backwards() -> None:
    start = TOKEN_TIMELINE_JS.index("      const displayedLine = displayedEditorLine();")
    end = TOKEN_TIMELINE_JS.index("      const manualSelectionActive", start)
    run_javascript(
        """
const window={__karaokeForgeLastDisplayedEditorLine:1,__karaokeForgeGlobalFollowLine:3};
const displayedEditorLine=()=>2;
const navigation={pending:{id:'latest',line:3}};
const globalTimeline={querySelector:()=>({dataset:{start:'3'}})};
const globalParts={}, playbackActive=true;
const markGlobalLineSelected=()=>{};
const queueGlobalSeek=()=>assert.fail('An old navigation response must not seek playback');
const seekGlobalTimeline=()=>assert.fail('An old navigation response must not seek playback');
"""
        + TOKEN_TIMELINE_JS[start:end]
    )


def test_click_during_blur_save_runs_once_after_the_saved_timeline_arrives() -> None:
    click_start = TOKEN_TIMELINE_JS.index(
        '    const block = event.target.closest?.(".kf-global-line-block");'
    )
    click_end = TOKEN_TIMELINE_JS.index("    clearTokenStopTimer();", click_start)
    poll_start = TOKEN_TIMELINE_JS.index("    const mutation = window.__kfEditorMutationPending;")
    poll_end = TOKEN_TIMELINE_JS.index("    const globalMode =", poll_start)
    run_javascript(
        """
const clicked=[];
let ack='before-save';
const window={__kfEditorMutationPending:{acknowledgment:ack}};
const document={querySelector:selector=>selector.includes('mutation-ack') ? {value:ack} : null};
const setEditorBusy=()=>{};
const visibleElement=()=>({querySelector:selector=>({click:()=>clicked.push(selector)})});
function userClick(line) {
  const event={target:{closest:()=>({dataset:{lineNumber:String(line)}})},
    preventDefault(){},stopPropagation(){}};
"""
        + TOKEN_TIMELINE_JS[click_start:click_end]
        + "}\nfunction poll(){\n"
        + TOKEN_TIMELINE_JS[poll_start:poll_end]
        + "}\n"
        + """
userClick(2); userClick(3);
assert.equal(window.__kfDeferredGlobalClick,3);
poll(); assert.equal(clicked.length,0);
ack='saved'; poll();
assert.deepEqual(clicked,['.kf-global-line-block[data-line-number="3"]']);
assert.equal(window.__kfDeferredGlobalClick,null);
poll(); assert.equal(clicked.length,1);
"""
    )


def test_user_mode_change_locks_the_editor_until_its_acknowledgement() -> None:
    start = TOKEN_TIMELINE_JS.index('  document.addEventListener("change", (event) => {')
    end = TOKEN_TIMELINE_JS.index("\n  }, true);", start) + len("\n  }, true);")
    run_javascript(
        """
let handler, paused=0;
const busy=[];
const window={};
const document={addEventListener:(_type,callback)=>{handler=callback;},
  querySelector:()=>({value:'current-ack'})};
const pauseForEditorMutation=()=>{paused++;};
const setEditorBusy=value=>busy.push(value);
"""
        + js_helper("beginEditorMutation")
        + TOKEN_TIMELINE_JS[start:end]
        + """
handler({target:{matches:()=>false}});
assert.equal(paused,0);
handler({target:{matches:()=>true}});
assert.equal(window.__kfEditorMutationPending.acknowledgment,'current-ack');
assert.deepEqual(busy,[true]);
handler({target:{matches:()=>true}});
assert.equal(paused,1);
"""
    )


def test_project_shortcuts_preserve_native_text_undo_and_route_global_redo() -> None:
    start = TOKEN_TIMELINE_JS.rindex('  document.addEventListener("keydown", (event) => {')
    end = TOKEN_TIMELINE_JS.index("\n  });", start) + len("\n  });")
    run_javascript(
        """
let handler, workspaceVisible=true;
const clicks=[];
const document={
  addEventListener:(_type,callback)=>{handler=callback;},
  querySelector:selector=>({click:()=>clicks.push(selector)}),
};
const visibleElement=()=>workspaceVisible ? {} : null;
const target=kind=>({closest(selector) {
  if(selector === 'input' && ['text','number','range'].includes(kind)) return {type:kind};
  if(selector.includes('textarea') && ['textarea','contenteditable'].includes(kind)) return {};
  return null;
}});
function key(kind,letter='z',shift=false,meta=false) {
  const event={target:target(kind),key:letter,ctrlKey:!meta,metaKey:meta,shiftKey:shift,
               prevented:false,preventDefault(){this.prevented=true;}};
  handler(event); return event;
}
"""
        + js_helper("isTextEntry")
        + TOKEN_TIMELINE_JS[start:end]
        + """
for(const kind of ['text','textarea','contenteditable','number']) {
  assert.equal(key(kind).prevented,false);
}
assert.equal(clicks.length,0);
assert.equal(key('range').prevented,true);
assert.match(clicks.at(-1),/editor-undo/);
key('outside','z',true); assert.match(clicks.at(-1),/editor-redo/);
key('outside','y'); assert.match(clicks.at(-1),/editor-redo/);
key('outside','Z',false,true); assert.match(clicks.at(-1),/editor-undo/);
workspaceVisible=false;
const count=clicks.length;
assert.equal(key('outside').prevented,false);
assert.equal(clicks.length,count);
"""
    )


@pytest.fixture(scope="module")
def navigation_callbacks(tmp_path_factory):
    root = tmp_path_factory.mktemp("editor-interactions")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(root / "outputs"))
        patch.setenv("KARAOKE_FORGE_SETTINGS_DIR", str(root / "settings"))
        patch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
        app = create_web_app()
        yield {
            **{getattr(fn.fn, "__name__", ""): fn.fn for fn in app.fns.values()},
            "__app__": app,
        }
        app.close()


def interaction_document() -> LyricsDocument:
    return LyricsDocument(
        lines=[
            LyricLine(
                text="A", start=1.0, end=2.0, tokens=[KaraokeToken(text="A", start=1.0, end=2.0)]
            ),
            LyricLine(
                text="B", start=2.5, end=3.5, tokens=[KaraokeToken(text="B", start=2.5, end=3.5)]
            ),
            LyricLine(
                text="C", start=5.0, end=6.0, tokens=[KaraokeToken(text="C", start=5.0, end=6.0)]
            ),
        ]
    )


@pytest.mark.parametrize(
    "index, selected, should_acknowledge",
    [([1, 4], True, False), ([1, 0], False, False), ([1, 0], True, True), ([99, 0], True, True)],
)
def test_only_first_column_row_navigation_acknowledges_the_mutation_gate(
    navigation_callbacks, index, selected, should_acknowledge
) -> None:
    document = interaction_document()
    result = navigation_callbacks["select_editor_row"](
        None,
        document.to_dict(),
        document_to_editor_rows(document),
        1,
        token_timing_to_json(document.lines[0]),
        "",
        [],
        {},
        "global",
        False,
        SimpleNamespace(index=index, selected=selected),
    )
    assert len(result) == 12
    if should_acknowledge:
        assert isinstance(result[11], str) and len(result[11]) == 32
    else:
        assert all(value == {"__type__": "update"} for value in result)


def test_first_column_validation_failure_still_releases_its_mutation_gate(
    navigation_callbacks,
) -> None:
    document = interaction_document()
    result = navigation_callbacks["select_editor_row"](
        None,
        document.to_dict(),
        "invalid table",
        1,
        token_timing_to_json(document.lines[0]),
        "",
        [],
        {},
        "global",
        False,
        SimpleNamespace(index=[0, 0], selected=True),
    )
    assert all(value == {"__type__": "update"} for value in result[:11])
    assert isinstance(result[11], str) and len(result[11]) == 32


def test_row_selection_has_no_unconditional_delayed_mutation_ack(navigation_callbacks) -> None:
    app = navigation_callbacks["__app__"]
    callback_id, callback = next(
        (function_id, fn)
        for function_id, fn in app.fns.items()
        if getattr(fn.fn, "__name__", "") == "select_editor_row"
    )
    assert callback.outputs[-1].elem_id == "kf-editor-mutation-ack"
    acknowledgement_id = callback.outputs[-1]._id
    assert not any(
        dependency.get("trigger_after") == callback_id
        and acknowledgement_id in dependency["outputs"]
        for dependency in app.config["dependencies"]
    )


def navigate(callback, document, requested, token_json=None):
    return callback(
        None,
        document.to_dict(),
        document_to_editor_rows(document),
        1,
        token_json or token_timing_to_json(document.lines[0]),
        "",
        [],
        {},
        True,
        requested,
    )


def test_same_line_global_selection_skips_all_workspace_outputs(navigation_callbacks) -> None:
    result = navigate(
        navigation_callbacks["select_global_editor_workspace"], interaction_document(), 1
    )
    assert all(value == {"__type__": "update"} for value in result[:11])


def test_clean_global_navigation_keeps_audio_document_and_preview_loaded(
    navigation_callbacks,
) -> None:
    document = interaction_document()
    result = navigate(navigation_callbacks["select_global_editor_workspace"], document, 2)
    for index in [0, 1, 5, 8]:
        assert result[index] == {"__type__": "update"}
    assert result[2] == 2
    assert 'data-line-number="2"' in result[6]
    assert json.loads(result[7]) == [{"text": "B", "start": 2.5, "end": 3.5}]


def test_global_navigation_commits_previous_draft_and_emits_selected_tokens(
    navigation_callbacks,
) -> None:
    document = interaction_document()
    result = navigate(
        navigation_callbacks["select_global_editor_workspace"],
        document,
        2,
        '[{"text":"NEW","start":1.0,"end":2.2}]',
    )
    assert result[0]["lines"][0]["text"] == "NEW"
    assert result[1][0][4] == "NEW"
    assert result[5] == result[8] == {"__type__": "update"}
    assert json.loads(result[7])[0]["text"] == "B"
    assert result[10]["past"][-1]["document"] == document.to_dict()


def test_saving_only_token_text_keeps_line_padding_and_following_sentence_times(
    navigation_callbacks,
) -> None:
    document = LyricsDocument(
        lines=[
            LyricLine(
                text="花開",
                start=1.0,
                end=2.8,
                tokens=[
                    KaraokeToken(text="花", start=1.2, end=2.0),
                    KaraokeToken(text="開", start=2.0, end=2.6),
                ],
            ),
            LyricLine(
                text="B",
                start=5.0,
                end=6.0,
                tokens=[
                    KaraokeToken(text="B", start=5.0, end=6.0),
                ],
            ),
        ]
    )
    entries = json.loads(token_timing_to_json(document.lines[0]))
    entries[0]["text"] = "華"
    result = navigation_callbacks["save_editor_token_timing_workspace"](
        document.to_dict(),
        document_to_editor_rows(document),
        1,
        json.dumps(entries),
        "",
        [],
        {},
        True,
    )
    assert result[0]["lines"][0]["text"] == "華開"
    assert result[0]["lines"][0]["start"] == pytest.approx(1.0)
    assert result[0]["lines"][0]["end"] == pytest.approx(2.8)
    assert result[0]["lines"][1] == document.to_dict()["lines"][1]


@pytest.mark.parametrize("snap_line, expected_next_start", [(2, 2.5), (3, 2.52)])
def test_snapped_join_avoids_the_ripple_gap_only_for_a_real_matching_boundary(
    navigation_callbacks, snap_line, expected_next_start
) -> None:
    document = interaction_document()
    result = navigation_callbacks["apply_global_line_edge_workspace"](
        document.to_dict(),
        document_to_editor_rows(document),
        1,
        token_timing_to_json(document.lines[0]),
        "",
        [],
        {},
        True,
        None,
        json.dumps(
            {
                "line": 1,
                "edge": "end",
                "seconds": 2.5,
                "base_start": 1.0,
                "base_end": 2.0,
                "text": "A",
                "snap": {"line": snap_line, "edge": "start", "seconds": 2.5},
            }
        ),
    )
    assert result[0]["lines"][0]["end"] == pytest.approx(2.5)
    assert result[0]["lines"][1]["start"] == pytest.approx(expected_next_start)
    assert result[0]["lines"][1]["end"] == pytest.approx(expected_next_start + 1.0)
    assert result[0]["lines"][2] == document.to_dict()["lines"][2]
