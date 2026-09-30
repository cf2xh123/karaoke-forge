"""Exercise drawer and keyboard lifecycle using the shipped browser handlers."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from karaoke_forge.web import TOKEN_TIMELINE_JS


def run_javascript(body: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for JavaScript interaction probes")
    result = subprocess.run(
        [node, "-"],
        input="const assert = require('node:assert/strict');\n" + body,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def js_helper(name: str) -> str:
    start = TOKEN_TIMELINE_JS.index(f"  const {name} =")
    end = TOKEN_TIMELINE_JS.index("\n  };", start) + len("\n  };")
    return TOKEN_TIMELINE_JS[start:end]


DRAWER_ENVIRONMENT = r"""
class Element {
  constructor(parent = null) {
    this.parent = parent;
    this.attributes = new Map();
    this.classes = new Set();
    this.classList = {
      toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name),
      contains: name => this.classes.has(name),
    };
    this.tabIndex = 0;
    this.isConnected = true;
  }
  setAttribute(name, value) { this.attributes.set(name, value); }
  removeAttribute(name) { this.attributes.delete(name); }
  getAttribute(name) { return this.attributes.get(name); }
  getClientRects() { return [{}]; }
  focus() { document.activeElement = this; }
  closest(selector) {
    if (selector === '[inert]') {
      return this.attributes.has('inert') ? this : this.parent?.closest(selector);
    }
    return null;
  }
  contains(element) { return element === this || element?.parent === this; }
}
const drawer = new Element();
let drawerMounted = true;
const toggle = new Element();
const first = new Element(drawer);
const last = new Element(drawer);
drawer.querySelectorAll = () => [first, last];
const listeners = {};
const styles = new Map([['overflow', ['clip', 'important']]]);
const document = {
  activeElement: toggle,
  createElement: () => new Element(),
  querySelector: selector => selector === '#editor-overview-panel' ?
    (drawerMounted ? drawer : null) :
    selector.includes('editor-overview-toggle') ? toggle : null,
  addEventListener: (name, callback) => { listeners[name] = callback; },
  body: {
    appendChild() {},
    style: {
      getPropertyValue: name => styles.get(name)?.[0] || '',
      getPropertyPriority: name => styles.get(name)?.[1] || '',
      setProperty: (name, value, priority = '') => styles.set(name, [value, priority]),
      removeProperty: name => styles.delete(name),
    },
  },
};
const window = {};
let editorVisible = true;
const visibleElement = () => editorVisible ? {} : null;
"""


def drawer_setup(*, mounted: bool = True) -> str:
    start = TOKEN_TIMELINE_JS.index('  const drawerBackdrop = document.createElement("div");')
    end = TOKEN_TIMELINE_JS.index('\n  document.addEventListener("click",', start)
    initial_mount = "" if mounted else "drawerMounted = false;\n"
    return DRAWER_ENVIRONMENT + initial_mount + TOKEN_TIMELINE_JS[start:end]


def test_lazily_mounted_drawer_starts_closed_and_initializes_only_once() -> None:
    run_javascript(
        drawer_setup(mounted=False)
        + """
assert.equal(drawer.attributes.has('inert'), false);
drawerMounted = true;
window.__kfSyncOverviewState();
assert.equal(drawer.attributes.has('inert'), true);
assert.equal(drawer.getAttribute('aria-hidden'), 'true');
assert.equal(drawer.getAttribute('role'), 'dialog');
assert.equal(toggle.getAttribute('aria-expanded'), 'false');
assert.equal(document.activeElement, toggle);
assert.deepEqual(styles.get('overflow'), ['clip', 'important']);
const originalSetter = drawer.setAttribute;
drawer.setAttribute = () => assert.fail('unchanged drawer must not be rewritten');
window.__kfSyncOverviewState();
window.__kfSyncOverviewState();
drawer.setAttribute = originalSetter;
setOverviewOpen(true);
assert.equal(drawer.attributes.has('inert'), false);
assert.equal(document.activeElement, first);
"""
    )


def test_drawer_restores_focus_and_existing_scroll_style_on_close() -> None:
    run_javascript(
        drawer_setup()
        + """
assert.equal(drawer.getAttribute('aria-hidden'), 'true');
assert.equal(drawer.attributes.has('inert'), true);
assert.deepEqual(styles.get('overflow'), ['clip', 'important']);
setOverviewOpen(true);
assert.equal(document.activeElement, first);
assert.equal(drawer.attributes.has('inert'), false);
assert.equal(drawer.getAttribute('aria-modal'), 'true');
assert.equal(toggle.getAttribute('aria-expanded'), 'true');
assert.equal(styles.get('overflow')[0], 'hidden');
// Calling open twice must not overwrite the original scrolling state or opener.
setOverviewOpen(true);
setOverviewOpen(false);
assert.equal(document.activeElement, toggle);
assert.equal(drawer.attributes.has('inert'), true);
assert.equal(drawer.getAttribute('aria-hidden'), 'true');
assert.equal(drawer.attributes.has('aria-modal'), false);
assert.equal(toggle.getAttribute('aria-expanded'), 'false');
assert.deepEqual(styles.get('overflow'), ['clip', 'important']);
assert.equal(drawerBackdrop.classList.contains('is-open'), false);
"""
    )


def test_tab_change_releases_the_drawer_without_stealing_focus() -> None:
    run_javascript(
        drawer_setup()
        + """
setOverviewOpen(true);
const destination = new Element();
document.activeElement = destination;
editorVisible = false;
window.__kfSyncOverviewState();
assert.equal(document.activeElement, destination);
assert.equal(drawerBackdrop.classList.contains('is-open'), false);
assert.equal(drawer.attributes.has('inert'), true);
assert.deepEqual(styles.get('overflow'), ['clip', 'important']);
"""
    )


def test_drawer_tab_navigation_stays_within_the_open_panel() -> None:
    run_javascript(
        drawer_setup()
        + """
setOverviewOpen(true);
let prevented = 0;
const tab = shiftKey => listeners.keydown({
  key: 'Tab', shiftKey, preventDefault() { prevented += 1; },
});
document.activeElement = last;
tab(false);
assert.equal(document.activeElement, first);
tab(true);
assert.equal(document.activeElement, last);
document.activeElement = toggle;
tab(false);
assert.equal(document.activeElement, first);
assert.equal(prevented, 3);
setOverviewOpen(false);
tab(false);
assert.equal(prevented, 3);
"""
    )


def test_finishing_an_edit_does_not_make_the_closed_drawer_focusable() -> None:
    run_javascript(
        drawer_setup()
        + js_helper("setEditorBusy")
        + """
setOverviewOpen(true);
setEditorBusy(true);
setOverviewOpen(false);
setEditorBusy(false);
assert.equal(drawer.attributes.has('inert'), true);
setOverviewOpen(true);
setEditorBusy(true);
assert.equal(drawer.attributes.has('inert'), true);
setEditorBusy(false);
assert.equal(drawer.attributes.has('inert'), false);
"""
    )


def test_space_preserves_native_controls_and_toggles_unfocused_playback() -> None:
    run_javascript(
        """
const window = {};
let played = 0;
const isTextEntry = () => false;
const waveSurferParts = () => ({});
const clearTokenStopTimer = () => {};
const playbackIsActive = () => false;
const playPlayback = () => { played += 1; };
const pausePlayback = () => {};
"""
        + js_helper("handlePlaybackSpace")
        + """
for (const control of ['button', 'input', "[role='checkbox']", "[role='tab']"]) {
  handlePlaybackSpace({
    code: 'Space', type: 'keydown',
    target: {closest: selector => selector.includes(control) ? {} : null},
    preventDefault() { assert.fail('native controls must retain Space'); },
    stopImmediatePropagation() { assert.fail('native controls must retain Space'); },
  });
}
assert.equal(played, 0);
let prevented = false;
handlePlaybackSpace({
  code: 'Space', type: 'keydown', target: {closest: () => null},
  preventDefault() { prevented = true; }, stopImmediatePropagation() {},
});
assert.equal(played, 1);
assert.equal(prevented, true);
"""
    )
