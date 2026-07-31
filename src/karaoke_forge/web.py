from __future__ import annotations

import html
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .ass import AssStyle
from .editor import (
    apply_editor_rows,
    apply_pronunciation_rows,
    apply_token_timing,
    document_from_payload,
    document_to_editor_rows,
    editor_preview_html,
    editor_token_timeline_html,
    nudge_editor_line_timing,
    pronunciation_to_editor_rows,
    token_timing_to_json,
)
from .formats import (
    attach_reference_translation,
    export_formats,
    read_lyrics,
    write_format,
)
from .models import LyricsDocument, PronunciationSpan
from .netease import (
    NeteaseAlignOptions,
    align_netease_song,
    download_netease_track,
    fetch_public_netease_info,
)
from .pipeline import (
    AlignOptions,
    align_audio_and_lyrics,
    normalize_timing_refinement,
    refine_audio_word_timing,
    should_refine_timing,
)
from .pronunciation import generate_pronunciation
from .workflows import MakeOptions, make_karaoke_video

WEB_CSS = """
:root {
  --kf-ink: #162033;
  --kf-muted: #697386;
  --kf-paper: #f7f3ea;
  --kf-card: rgba(255, 255, 255, 0.92);
  --kf-line: #e3ded2;
  --kf-orange: #ffad1f;
  --kf-orange-dark: #d87800;
  --kf-teal: #0b6671;
  --kf-teal-soft: #dceff0;
}

.gradio-container {
  background:
    radial-gradient(circle at 8% 3%, rgba(255, 173, 31, 0.16), transparent 22rem),
    radial-gradient(circle at 92% 10%, rgba(11, 102, 113, 0.12), transparent 26rem),
    var(--kf-paper) !important;
  color: var(--kf-ink) !important;
  min-height: 100vh;
}

.kf-shell {
  max-width: 1240px;
  margin: 0 auto;
}

.kf-hero {
  position: relative;
  overflow: hidden;
  padding: 34px 38px;
  border-radius: 30px;
  background: var(--kf-ink);
  color: #fff;
  box-shadow: 0 22px 60px rgba(22, 32, 51, 0.18);
  margin: 10px 0 22px;
}

.kf-hero::after {
  content: "";
  position: absolute;
  right: -45px;
  top: -62px;
  width: 230px;
  height: 230px;
  border-radius: 50%;
  border: 42px solid rgba(255, 173, 31, 0.88);
  box-shadow: 0 0 0 18px rgba(255, 255, 255, 0.08);
}

.kf-kicker {
  color: #ffd27a;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: .18em;
  text-transform: uppercase;
  margin-bottom: 10px;
}

.kf-title {
  font-size: clamp(28px, 4vw, 52px);
  line-height: 1.05;
  font-weight: 900;
  letter-spacing: -.04em;
  margin: 0;
  max-width: 760px;
}

.kf-subtitle {
  color: rgba(255,255,255,.72);
  font-size: 16px;
  margin: 14px 0 0;
  max-width: 690px;
}

.kf-steps {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 22px;
}

.kf-step {
  padding: 8px 12px;
  border: 1px solid rgba(255,255,255,.16);
  border-radius: 999px;
  color: rgba(255,255,255,.82);
  font-size: 13px;
  background: rgba(255,255,255,.06);
}

.kf-step b {
  color: #ffd27a;
  margin-right: 5px;
}

.kf-card {
  background: var(--kf-card) !important;
  border: 1px solid var(--kf-line) !important;
  border-radius: 22px !important;
  box-shadow: 0 8px 30px rgba(22, 32, 51, .06) !important;
  padding: 18px !important;
}

.kf-card h2, .kf-card h3 {
  color: var(--kf-ink);
  letter-spacing: -.02em;
}

.kf-section-label {
  color: var(--kf-teal);
  font-size: 12px;
  font-weight: 800;
  letter-spacing: .12em;
  text-transform: uppercase;
  margin-bottom: 3px;
}

.kf-tip {
  border-left: 4px solid var(--kf-orange);
  background: #fff8e9;
  padding: 12px 14px;
  border-radius: 4px 12px 12px 4px;
  color: #6f5525;
  font-size: 13px;
}

.kf-primary button {
  min-height: 52px !important;
  border: 0 !important;
  border-radius: 14px !important;
  color: #172033 !important;
  font-size: 16px !important;
  font-weight: 850 !important;
  background: linear-gradient(135deg, #ffc44f, #ff9e12) !important;
  box-shadow: 0 10px 24px rgba(216, 120, 0, .22) !important;
}

.kf-primary button:hover {
  transform: translateY(-1px);
  box-shadow: 0 13px 28px rgba(216, 120, 0, .28) !important;
}

.kf-status {
  border-radius: 16px;
  min-height: 54px;
}

.kf-subtitle-preview {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  overflow: hidden;
  border-radius: 18px;
  background:
    linear-gradient(180deg, rgba(10,18,30,.08), rgba(5,10,18,.5)),
    radial-gradient(circle at 72% 32%, rgba(255,190,92,.55), transparent 18%),
    linear-gradient(135deg, #537f91 0%, #28495d 42%, #101d2b 100%);
  box-shadow: inset 0 0 70px rgba(0,0,0,.32);
}

.kf-preview-vignette {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse at 50% 40%, transparent 28%, rgba(0,0,0,.42) 100%),
    linear-gradient(155deg, transparent 45%, rgba(255,255,255,.08) 46%, transparent 48%);
}

.kf-preview-badge {
  position: absolute;
  top: 12px;
  left: 12px;
  padding: 5px 9px;
  border-radius: 999px;
  color: rgba(255,255,255,.8);
  background: rgba(5,10,18,.38);
  border: 1px solid rgba(255,255,255,.16);
  font-size: 11px;
}

.kf-token-editor {
  padding: 14px;
  border: 1px solid #cfd8e6;
  border-radius: 16px;
  background: #eef3f9;
  color: #172238;
}

.kf-token-help {
  color: #42526b;
  font-size: 13px;
  line-height: 1.55;
}

.kf-token-toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 10px;
}

.kf-token-actions {
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
}

.kf-token-actions button {
  padding: 5px 9px;
  border: 1px solid #b9c5d6;
  border-radius: 8px;
  background: #fff;
  color: #26364e;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
}

.kf-token-actions button:hover {
  border-color: #d87800;
  color: #a65d00;
}

.kf-token-scroll {
  overflow-x: auto;
  padding: 2px 2px 9px;
}

.kf-token-canvas {
  position: relative;
  width: 100%;
}

.kf-token-ruler {
  display: flex;
  justify-content: space-between;
  color: #65758c;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  padding: 0 2px 5px;
}

.kf-token-track {
  position: relative;
  height: 102px;
  border-radius: 12px;
  background:
    repeating-linear-gradient(
      90deg,
      rgba(255,255,255,.08) 0,
      rgba(255,255,255,.08) 1px,
      transparent 1px,
      transparent 5%
    ),
    #17243b;
  box-shadow: inset 0 0 0 1px rgba(255,255,255,.08);
}

.kf-token-block {
  position: absolute;
  top: 20px;
  height: 62px;
  overflow: hidden;
  padding: 7px 4px;
  border: 1px solid #7dd3fc;
  border-radius: 8px;
  background: linear-gradient(180deg, #155e75, #164e63);
  color: #fff;
  cursor: pointer;
  z-index: 2;
}

.kf-token-block:hover,
.kf-token-block:focus-visible,
.kf-token-block.is-playing {
  background: linear-gradient(180deg, #0e7490, #155e75);
  outline: 2px solid #fbbf24;
  outline-offset: -2px;
}

.kf-token-label,
.kf-token-time {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kf-token-label {
  font-size: 14px;
  font-weight: 800;
}

.kf-token-time {
  margin-top: 4px;
  color: #bae6fd;
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}

.kf-token-boundary {
  position: absolute;
  inset: 0;
  z-index: 4;
  width: 100%;
  height: 102px;
  margin: 0;
  appearance: none;
  pointer-events: none;
  background: transparent;
}

.kf-token-boundary::-webkit-slider-runnable-track {
  height: 100%;
  background: transparent;
}

.kf-token-boundary::-webkit-slider-thumb {
  width: 7px;
  height: 102px;
  margin-top: 0;
  appearance: none;
  pointer-events: auto;
  cursor: ew-resize;
  border: 0;
  border-radius: 4px;
  background: rgba(251, 191, 36, .86);
  box-shadow: 0 0 0 1px rgba(17,24,39,.35);
}

.kf-token-boundary::-moz-range-track {
  height: 100%;
  background: transparent;
}

.kf-token-boundary::-moz-range-thumb {
  width: 7px;
  height: 102px;
  pointer-events: auto;
  cursor: ew-resize;
  border: 0;
  border-radius: 4px;
  background: rgba(251, 191, 36, .86);
}

.kf-token-playhead {
  position: absolute;
  top: -8px;
  bottom: -8px;
  z-index: 7;
  width: 3px;
  pointer-events: none;
  background: #fb3b3b;
  box-shadow: 0 0 0 1px rgba(255,255,255,.65), 0 0 8px rgba(251,59,59,.75);
  transform: translateX(-1px);
}

.kf-token-playhead::before {
  content: "";
  position: absolute;
  top: -1px;
  left: -5px;
  width: 0;
  height: 0;
  border-left: 6px solid transparent;
  border-right: 6px solid transparent;
  border-top: 8px solid #fb3b3b;
}

.kf-token-playtime {
  color: #d12626;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

#kf-token-json {
  display: none !important;
}

#kf-line-context-action,
#kf-line-context-apply {
  display: none !important;
}

#kf-line-context-menu {
  position: fixed;
  z-index: 4000;
  display: none;
  min-width: 210px;
  padding: 6px;
  border: 1px solid #cfd8e6;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 16px 42px rgba(22, 32, 51, .28);
}

#kf-line-context-menu.is-open {
  display: grid;
  gap: 3px;
}

#kf-line-context-menu button {
  width: 100%;
  padding: 9px 11px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: #26364e;
  text-align: left;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
}

#kf-line-context-menu button:hover {
  background: #edf3f8;
}

#kf-line-context-menu button[data-action="delete"] {
  color: #c62828;
}

#editor-workspace {
  position: relative;
  display: flex !important;
  flex-direction: column;
  gap: 8px;
  width: 100% !important;
  min-width: 0 !important;
  max-width: none !important;
  padding: 8px 0;
}

#editor-main-grid {
  align-items: stretch;
  min-height: 0;
  overflow: visible;
  flex-wrap: nowrap;
}

#editor-topbar {
  position: sticky;
  top: 4px;
  z-index: 50;
  flex: 0 0 auto;
  min-width: 0;
  align-items: center;
  flex-wrap: nowrap;
  padding: 8px;
  border: 1px solid var(--kf-line);
  border-radius: 14px;
  background: rgba(247, 243, 234, .98);
  box-shadow: 0 6px 18px rgba(22, 32, 51, .1);
}

#editor-topbar > * {
  min-width: 0;
}

#editor-overview-toggle,
#editor-exit-workspace {
  flex: 0 0 auto !important;
}

#editor-zoom-help {
  flex: 0 1 auto !important;
  color: var(--kf-muted);
  font-size: 12px;
}

#editor-status {
  flex: 1 1 auto !important;
  max-height: 48px;
  overflow: auto;
  font-size: 12px;
}

#editor-status h3,
#editor-status p {
  margin: 0;
  font-size: 13px;
}

#editor-timing-panel,
#editor-side-panel {
  height: auto;
  min-height: 0;
  overflow: visible;
}

#editor-timing-card,
#editor-side-card {
  display: flex !important;
  flex-direction: column;
  gap: 6px;
  height: auto;
  min-height: 0;
  overflow: visible;
  padding: 10px !important;
}

#editor-preview {
  flex: 0 0 auto;
  min-width: 0;
  min-height: 220px;
  overflow: hidden;
}

#editor-preview.kf-sticky-preview {
  position: relative !important;
  top: auto;
  z-index: 1;
  height: auto;
  padding: 0;
  box-shadow: none;
  backdrop-filter: none;
}

.kf-editor-preview-stage {
  position: relative;
  width: 100%;
  min-height: 220px;
  overflow: hidden;
  border-radius: 16px;
  background: linear-gradient(150deg, #142038, #263b58);
  color: #fff;
  font-size: var(--kf-preview-font-size, 28px);
  line-height: 1.3;
}

.kf-editor-preview-info {
  position: absolute;
  top: 8px;
  left: 14px;
  color: #ffd27a;
  font-size: 12px;
}

.kf-editor-preview-translation {
  position: absolute;
  top: 29px;
  left: 8%;
  right: 8%;
  overflow: hidden;
  color: #d9efff;
  font-size: calc(var(--kf-preview-font-size, 28px) * .58);
  text-align: center;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kf-editor-preview-row {
  position: absolute;
  overflow: hidden;
  padding-top: .72em;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kf-editor-preview-row ruby {
  ruby-position: over;
  ruby-align: center;
}

.kf-editor-preview-row rt {
  font-size: .5em;
  line-height: 1;
}

.kf-editor-preview-upper {
  top: 66px;
  left: 7%;
  right: 16%;
  text-align: left;
}

.kf-editor-preview-lower {
  right: 7%;
  bottom: 20px;
  left: 16%;
  text-align: right;
}

#editor-line-controls {
  flex: 0 0 auto;
  min-width: 0;
  padding: 4px;
  border-radius: 14px;
  background: #f3f6fa;
}

#editor-line-controls h3 {
  margin: 0;
  font-size: 15px;
}

#editor-audio-panel {
  flex: 0 0 auto;
  min-width: 0;
  min-height: 0;
  overflow: visible;
}

#editor-line-audio {
  min-height: 0 !important;
}

#editor-timing-status {
  max-height: 34px;
  overflow: auto;
  font-size: 12px;
}

#editor-timing-status p {
  margin: 0;
}

#editor-token-timeline {
  flex: 0 0 auto;
  min-width: 0;
  min-height: 0;
  overflow: visible;
}

#editor-timing-actions {
  flex: 0 0 auto;
  min-width: 0;
  flex-wrap: nowrap;
}

#editor-timing-actions button {
  min-width: 0;
  padding-inline: 8px;
}

#editor-pronunciation-panel {
  flex: 0 0 auto;
  display: flex !important;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  overflow: visible;
  padding: 6px;
  border-radius: 14px;
  background: #f3f6fa;
}

#editor-pronunciation-units {
  flex: 0 0 auto;
  min-height: 0;
  overflow: visible;
}

#editor-lines table,
#editor-lines input,
#editor-lines textarea {
  font-size: var(--kf-overview-font-size, 13px) !important;
}

#editor-overview-panel {
  position: fixed;
  top: 12px;
  bottom: 12px;
  left: 12px;
  z-index: 2001;
  width: min(760px, calc(100vw - 24px)) !important;
  min-width: 0 !important;
  max-width: 760px !important;
  overflow-y: auto;
  padding: 8px;
  border: 1px solid rgba(216, 222, 232, .9);
  border-radius: 20px;
  background: #f7f3ea;
  box-shadow: 0 24px 70px rgba(22, 32, 51, .32);
  opacity: 0;
  pointer-events: none;
  transform: translateX(calc(-100% - 32px));
  transition: transform .2s ease, opacity .2s ease;
  backdrop-filter: none;
}

#editor-overview-panel.is-open {
  opacity: 1;
  pointer-events: auto;
  transform: translateX(0);
}

#kf-editor-drawer-backdrop {
  position: fixed;
  inset: 0;
  z-index: 2000;
  display: none;
  background: rgba(15, 23, 42, .2);
  backdrop-filter: none;
}

#kf-editor-drawer-backdrop.is-open {
  display: block;
}

#editor-overview-toggle button {
  border-color: #d87800 !important;
  background: #fff8e8 !important;
  color: #9a5700 !important;
  font-weight: 800 !important;
}

#editor-overview-close button {
  min-width: 110px;
}

.kf-sticky-preview {
  position: sticky !important;
  top: 8px;
  z-index: 20;
  padding: 6px;
  border-radius: 18px;
  background: rgba(247, 243, 234, .96);
  box-shadow: 0 10px 26px rgba(22, 32, 51, .16);
  backdrop-filter: blur(8px);
}

.kf-footer {
  color: var(--kf-muted);
  text-align: center;
  padding: 22px 0 8px;
  font-size: 12px;
}

@media (max-width: 720px) {
  .kf-hero { padding: 26px 22px; border-radius: 22px; }
  .kf-hero::after { opacity: .32; right: -105px; }
  .kf-card { padding: 12px !important; border-radius: 17px !important; }
}
"""

TOKEN_TIMELINE_JS = r"""
() => {
  if (window.__karaokeForgeTokenTimelineInstalled) {
    return [];
  }
  window.__karaokeForgeTokenTimelineInstalled = true;

  const setTokenJson = (payload) => {
    const input = document.querySelector("#kf-token-json textarea, #kf-token-json input");
    if (!input) return;
    const prototype = input instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
    if (setter) setter.call(input, JSON.stringify(payload));
    else input.value = JSON.stringify(payload);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  };

  const setHiddenInput = (selector, value) => {
    const input = document.querySelector(`${selector} textarea, ${selector} input`);
    if (!input) return false;
    const prototype = input instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
    if (setter) setter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  };

  const timelineHandles = (timeline) =>
    Array.from(timeline.querySelectorAll(".kf-token-boundary"))
      .sort((left, right) =>
        Number(left.dataset.boundaryIndex) - Number(right.dataset.boundaryIndex)
      );

  const boundarySnapshot = (timeline) =>
    timelineHandles(timeline).map((handle) => Number(handle.value));

  const refreshTimeline = (timeline) => {
    const clipStart = Number(timeline.dataset.clipStart);
    const clipEnd = Number(timeline.dataset.clipEnd);
    const duration = Math.max(0.01, clipEnd - clipStart);
    const handles = timelineHandles(timeline);
    const boundaries = handles.map((handle) => Number(handle.value));
    const blocks = Array.from(timeline.querySelectorAll(".kf-token-block"))
      .sort((left, right) =>
        Number(left.dataset.tokenIndex) - Number(right.dataset.tokenIndex)
      );
    const payload = blocks.map((block, index) => {
      const start = boundaries[index];
      const end = boundaries[index + 1];
      block.style.left = `${((start - clipStart) / duration) * 100}%`;
      block.style.width = `${Math.max(0.35, ((end - start) / duration) * 100)}%`;
      block.dataset.start = start.toFixed(3);
      block.dataset.end = end.toFixed(3);
      const time = block.querySelector(".kf-token-time");
      if (time) time.textContent = `${start.toFixed(2)}–${end.toFixed(2)}s`;
      return {
        text: block.dataset.token || "",
        start: Number(start.toFixed(3)),
        end: Number(end.toFixed(3)),
      };
    });
    setTokenJson(payload);
  };

  const restoreSnapshot = (timeline, snapshot) => {
    const handles = timelineHandles(timeline);
    if (!Array.isArray(snapshot) || snapshot.length !== handles.length) return;
    handles.forEach((handle, index) => {
      handle.value = String(snapshot[index]);
    });
    refreshTimeline(timeline);
  };

  const visibleElement = (selector) =>
    Array.from(document.querySelectorAll(selector))
      .find((element) => element.offsetParent !== null);

  const applyTimelineZoom = (timeline, mode) => {
    const scrollArea = timeline?.querySelector(".kf-token-scroll");
    const canvas = timeline?.querySelector(".kf-token-canvas");
    if (!scrollArea || !canvas) return;
    const baseWidth = Math.max(1, Number(canvas.dataset.baseWidth) || 760);
    const oldWidth = Math.max(1, canvas.getBoundingClientRect().width);
    const fitZoom = Math.max(0.2, scrollArea.clientWidth / baseWidth);
    const currentZoom = Number(
      canvas.dataset.zoom || Math.max(fitZoom, oldWidth / baseWidth)
    );
    let nextZoom = currentZoom;
    if (mode === "in") nextZoom = Math.min(8, currentZoom * 1.4);
    if (mode === "out") nextZoom = Math.max(fitZoom, currentZoom / 1.4);
    if (mode === "fit") nextZoom = fitZoom;
    const anchor = Math.min(
      1,
      Math.max(0, (scrollArea.scrollLeft + scrollArea.clientWidth / 2) / oldWidth)
    );
    const nextWidth = Math.max(scrollArea.clientWidth, baseWidth * nextZoom);
    canvas.dataset.zoom = String(nextZoom);
    canvas.style.width = `${nextWidth}px`;
    canvas.style.minWidth = `${nextWidth}px`;
    requestAnimationFrame(() => {
      scrollArea.scrollLeft = Math.max(
        0,
        anchor * nextWidth - scrollArea.clientWidth / 2
      );
    });
  };

  const applyPreviewZoom = (stage, direction) => {
    if (!stage) return;
    const host = stage.closest("#editor-preview") || stage;
    const current = Number(
      host.dataset.fontSize || Number.parseFloat(getComputedStyle(stage).fontSize)
    );
    const next = Math.min(
      56,
      Math.max(16, current * (direction === "in" ? 1.1 : 0.9))
    );
    host.dataset.fontSize = String(next);
    host.style.setProperty("--kf-preview-font-size", `${next}px`);
  };

  const applyOverviewZoom = (overview, direction) => {
    if (!overview) return;
    const current = Number(overview.dataset.fontSize || 13);
    const next = Math.min(
      24,
      Math.max(10, current + (direction === "in" ? 1 : -1))
    );
    overview.dataset.fontSize = String(next);
    overview.style.setProperty("--kf-overview-font-size", `${next}px`);
  };

  document.addEventListener("wheel", (event) => {
    if (!(event.ctrlKey || event.metaKey)) return;
    const timeline = event.target.closest?.(".kf-token-editor");
    const previewStage = event.target.closest?.(".kf-editor-preview-stage");
    const overview = event.target.closest?.("#editor-lines");
    if (!timeline && !previewStage && !overview) return;
    event.preventDefault();
    const direction = event.deltaY < 0 ? "in" : "out";
    if (timeline) applyTimelineZoom(timeline, direction);
    if (previewStage) applyPreviewZoom(previewStage, direction);
    if (overview) applyOverviewZoom(overview, direction);
  }, { passive: false });

  const updatePlaybackAt = (localTime) => {
    const timeline = visibleElement(".kf-token-editor");
    if (!timeline) return;
    const clipStart = Number(timeline.dataset.clipStart);
    const clipEnd = Number(timeline.dataset.clipEnd);
    const duration = Math.max(0.01, clipEnd - clipStart);
    const absoluteTime = clipStart + Number(localTime || 0);
    const percent = Math.min(
      100,
      Math.max(0, ((absoluteTime - clipStart) / duration) * 100)
    );
    const playhead = timeline.querySelector(".kf-token-playhead");
    if (playhead) playhead.style.left = `${percent}%`;
    const playtime = timeline.querySelector(".kf-token-playtime");
    if (playtime) playtime.textContent = `${absoluteTime.toFixed(2)}s`;
    const scrollArea = timeline.querySelector(".kf-token-scroll");
    const canvas = timeline.querySelector(".kf-token-canvas");
    if (scrollArea && canvas && canvas.scrollWidth > scrollArea.clientWidth) {
      const target = (percent / 100) * canvas.scrollWidth;
      const safeLeft = scrollArea.scrollLeft + scrollArea.clientWidth * 0.18;
      const safeRight = scrollArea.scrollLeft + scrollArea.clientWidth * 0.82;
      const lastTarget = Number(timeline.__kfLastFollowTarget ?? -100000);
      if (
        (target < safeLeft || target > safeRight) &&
        Math.abs(target - lastTarget) > scrollArea.clientWidth * 0.18
      ) {
        timeline.__kfLastFollowTarget = target;
        scrollArea.scrollTo({
          left: Math.max(0, target - scrollArea.clientWidth / 2),
          behavior: "smooth",
        });
      }
    }
    const tokenBlocks = Array.from(timeline.querySelectorAll(".kf-token-block"))
      .sort((left, right) =>
        Number(left.dataset.tokenIndex) - Number(right.dataset.tokenIndex)
      );
    tokenBlocks.forEach((block) => {
      const start = Number(block.dataset.start);
      const end = Number(block.dataset.end);
      block.classList.toggle(
        "is-playing",
        absoluteTime >= start && absoluteTime < end
      );
    });

    const karaoke = visibleElement(".kf-live-karaoke-current");
    if (karaoke) {
      const lineStart = Number(karaoke.dataset.lineStart);
      const lineEnd = Math.max(lineStart + 0.01, Number(karaoke.dataset.lineEnd));
      const weights = tokenBlocks.map((block) =>
        Math.max(1, Array.from(block.dataset.token || "").length)
      );
      const totalWeight = Math.max(1, weights.reduce((sum, value) => sum + value, 0));
      let completedWeight = 0;
      let lyricProgress = absoluteTime >= lineEnd ? 100 : 0;
      for (let index = 0; index < tokenBlocks.length; index += 1) {
        const block = tokenBlocks[index];
        const start = Number(block.dataset.start);
        const end = Math.max(start + 0.01, Number(block.dataset.end));
        if (absoluteTime >= end) {
          completedWeight += weights[index];
          lyricProgress = (completedWeight / totalWeight) * 100;
          continue;
        }
        if (absoluteTime >= start) {
          const inside = (absoluteTime - start) / (end - start);
          lyricProgress = (
            (completedWeight + weights[index] * inside) / totalWeight
          ) * 100;
        }
        break;
      }
      const fill = karaoke.querySelector(".kf-live-karaoke-fill");
      if (fill) {
        fill.style.clipPath = `inset(0 ${100 - lyricProgress}% 0 0)`;
      }
    }
  };

  const waveSurferParts = () => {
    const host = document.querySelector("#editor-line-audio");
    if (!host) return null;
    const queue = [host];
    const visited = new Set();
    let media = host.querySelector("audio, video");
    while (queue.length) {
      const root = queue.shift();
      if (!root || visited.has(root)) continue;
      visited.add(root);
      media ||= root.querySelector?.("audio, video");
      const progress = root.querySelector?.('[part="progress"]');
      const wrapper = root.querySelector?.('[part="wrapper"]');
      if (progress && wrapper) {
        const controls = host.querySelector('[data-testid="waveform-controls"]');
        const playButton = controls?.querySelector(".play-pause-button");
        return { host, progress, wrapper, playButton, media };
      }
      root.querySelectorAll?.("*").forEach((element) => {
        if (element.shadowRoot) queue.push(element.shadowRoot);
      });
    }
    return null;
  };

  const waveProgressRatio = (parts) => {
    const width = Number.parseFloat(parts?.progress?.style?.width || "");
    return Number.isFinite(width) ? Math.min(1, Math.max(0, width / 100)) : null;
  };

  const seekWaveSurfer = (parts, ratio) => {
    if (!parts?.wrapper) return false;
    const bounds = parts.wrapper.getBoundingClientRect();
    const clientX = bounds.left + Math.min(1, Math.max(0, ratio)) * bounds.width;
    const options = {
      bubbles: true,
      composed: true,
      clientX,
      clientY: bounds.top + bounds.height / 2,
      button: 0,
    };
    parts.wrapper.dispatchEvent(new PointerEvent("pointerdown", options));
    parts.wrapper.dispatchEvent(new PointerEvent("pointerup", options));
    parts.wrapper.dispatchEvent(new MouseEvent("click", options));
    return true;
  };

  const buttonIsPause = (button) =>
    /pause|\u6682\u505c/i.test(button?.getAttribute("aria-label") || "");

  const selectedPlaybackRate = () => {
    const input = document.querySelector(
      "#editor-playback-rate input[type='range'], #editor-playback-rate input"
    );
    const rate = Number.parseFloat(input?.value || "1");
    return Number.isFinite(rate) ? Math.min(2, Math.max(0.5, rate)) : 1;
  };

  const loopCurrentLine = () => Boolean(
    document.querySelector("#editor-loop-line input[type='checkbox']")?.checked
  );

  const applyPlaybackRate = (media) => {
    if (!media) return;
    const rate = selectedPlaybackRate();
    if (Math.abs(Number(media.playbackRate || 1) - rate) > 0.001) {
      media.playbackRate = rate;
    }
    for (const property of [
      "preservesPitch",
      "mozPreservesPitch",
      "webkitPreservesPitch",
    ]) {
      if (property in media) media[property] = true;
    }
  };

  const finishCurrentLine = (
    timeline,
    parts,
    absoluteTime,
    clipStart,
    clipEnd
  ) => {
    const preview = visibleElement(".kf-editor-preview-stage");
    if (!timeline || !preview || !parts) return;
    const lineStart = Number(preview.dataset.lineStart);
    const lineEnd = Number(preview.dataset.lineEnd);
    const lineNumber = Number(preview.dataset.lineNumber);
    const lineCount = Number(preview.dataset.lineCount);
    if (![lineStart, lineEnd, lineNumber, lineCount].every(Number.isFinite)) return;
    if (absoluteTime < lineEnd - 0.06) {
      timeline.__kfCompletionHandled = false;
      return;
    }
    const isPlaying = parts.media
      ? !parts.media.paused && !parts.media.ended
      : buttonIsPause(parts.playButton);
    if (!isPlaying || timeline.__kfCompletionHandled || absoluteTime < lineEnd) {
      return;
    }
    timeline.__kfCompletionHandled = true;
    if (loopCurrentLine()) {
      const duration = Math.max(0.01, clipEnd - clipStart);
      seekWaveSurfer(parts, (lineStart - clipStart) / duration);
      if (parts.media?.paused) {
        parts.media.play().catch(() => parts.playButton?.click());
      }
      return;
    }
    if (buttonIsPause(parts.playButton)) {
      parts.playButton.click();
    } else {
      parts.media?.pause();
    }
    if (lineNumber < lineCount) {
      window.setTimeout(() => {
        document.querySelector("#editor-next-line button")?.click();
      }, 80);
    }
  };

  const pollWaveSurfer = () => {
    const timeline = visibleElement(".kf-token-editor");
    const parts = waveSurferParts();
    const ratio = waveProgressRatio(parts);
    applyPlaybackRate(parts?.media);
    if (timeline && ratio !== null) {
      const clipStart = Number(timeline.dataset.clipStart);
      const clipEnd = Number(timeline.dataset.clipEnd);
      const duration = Math.max(0.01, clipEnd - clipStart);
      const localTime = ratio * duration;
      updatePlaybackAt(localTime);
      finishCurrentLine(
        timeline,
        parts,
        clipStart + localTime,
        clipStart,
        clipEnd
      );
    }
    window.__karaokeForgeWavePollFrame = requestAnimationFrame(pollWaveSurfer);
  };
  if (window.__karaokeForgeWavePollFrame) {
    cancelAnimationFrame(window.__karaokeForgeWavePollFrame);
  }
  pollWaveSurfer();

  document.addEventListener("pointerdown", (event) => {
    const handle = event.target.closest?.(".kf-token-boundary");
    if (!handle) return;
    const timeline = handle.closest(".kf-token-editor");
    timeline.__kfUndoHistory ||= [];
    timeline.__kfRedoHistory = [];
    const snapshot = boundarySnapshot(timeline);
    const previous = timeline.__kfUndoHistory.at(-1);
    if (!previous || JSON.stringify(previous) !== JSON.stringify(snapshot)) {
      timeline.__kfUndoHistory.push(snapshot);
    }
  });

  document.addEventListener("input", (event) => {
    const handle = event.target.closest?.(".kf-token-boundary");
    if (!handle) return;
    const timeline = handle.closest(".kf-token-editor");
    const handles = timelineHandles(timeline);
    const index = handles.indexOf(handle);
    const lower = index > 0 ? Number(handles[index - 1].value) + 0.01 : Number(handle.min);
    const upper = index + 1 < handles.length
      ? Number(handles[index + 1].value) - 0.01
      : Number(handle.max);
    handle.value = String(Math.min(upper, Math.max(lower, Number(handle.value))));
    refreshTimeline(timeline);
  });

  document.addEventListener("click", (event) => {
    const zoomOut = event.target.closest?.(".kf-token-zoom-out");
    const zoomFit = event.target.closest?.(".kf-token-zoom-fit");
    const zoomIn = event.target.closest?.(".kf-token-zoom-in");
    if (zoomOut || zoomFit || zoomIn) {
      const timeline = event.target.closest(".kf-token-editor");
      applyTimelineZoom(
        timeline,
        zoomIn ? "in" : zoomOut ? "out" : "fit"
      );
      return;
    }

    const pageLeft = event.target.closest?.(".kf-token-page-left");
    const pageRight = event.target.closest?.(".kf-token-page-right");
    if (pageLeft || pageRight) {
      const timeline = event.target.closest(".kf-token-editor");
      const scrollArea = timeline.querySelector(".kf-token-scroll");
      if (scrollArea) {
        scrollArea.scrollBy({
          left: scrollArea.clientWidth * (pageLeft ? -0.82 : 0.82),
          behavior: "smooth",
        });
      }
      return;
    }

    const undo = event.target.closest?.(".kf-token-undo");
    const redo = event.target.closest?.(".kf-token-redo");
    if (undo || redo) {
      const timeline = event.target.closest(".kf-token-editor");
      timeline.__kfUndoHistory ||= [];
      timeline.__kfRedoHistory ||= [];
      if (undo && timeline.__kfUndoHistory.length) {
        timeline.__kfRedoHistory.push(boundarySnapshot(timeline));
        restoreSnapshot(timeline, timeline.__kfUndoHistory.pop());
      } else if (redo && timeline.__kfRedoHistory.length) {
        timeline.__kfUndoHistory.push(boundarySnapshot(timeline));
        restoreSnapshot(timeline, timeline.__kfRedoHistory.pop());
      }
      return;
    }

    const block = event.target.closest?.(".kf-token-block");
    if (!block) return;
    const timeline = block.closest(".kf-token-editor");
    const parts = waveSurferParts();
    if (!timeline || !parts) return;
    const clipStart = Number(timeline.dataset.clipStart);
    const clipEnd = Number(timeline.dataset.clipEnd);
    const start = Number(block.dataset.start);
    const end = Number(block.dataset.end);
    const duration = Math.max(0.01, clipEnd - clipStart);
    const scrollArea = timeline.querySelector(".kf-token-scroll");
    if (scrollArea) {
      scrollArea.scrollTo({
        left: Math.max(
          0,
          block.offsetLeft + block.offsetWidth / 2 - scrollArea.clientWidth / 2
        ),
        behavior: "smooth",
      });
    }
    if (buttonIsPause(parts.playButton)) parts.playButton.click();
    seekWaveSurfer(parts, (start - clipStart) / duration);
    requestAnimationFrame(() => parts.playButton?.click());
    if (window.__karaokeForgeTokenStopTimer) {
      clearTimeout(window.__karaokeForgeTokenStopTimer);
    }
    window.__karaokeForgeTokenStopTimer = setTimeout(
      () => {
        const current = waveSurferParts();
        if (buttonIsPause(current?.playButton)) current.playButton.click();
      },
      Math.max(80, ((end - start) * 1000) / selectedPlaybackRate())
    );
  });

  const drawerBackdrop = document.createElement("div");
  drawerBackdrop.id = "kf-editor-drawer-backdrop";
  document.body.appendChild(drawerBackdrop);

  const setOverviewOpen = (open) => {
    const drawer = document.querySelector("#editor-overview-panel");
    drawer?.classList.toggle("is-open", open);
    drawerBackdrop.classList.toggle("is-open", open);
    document.body.style.overflow = open ? "hidden" : "";
  };

  document.addEventListener("click", (event) => {
    if (event.target.closest?.("#editor-overview-toggle")) {
      setOverviewOpen(true);
      return;
    }
    if (
      event.target.closest?.("#editor-overview-close") ||
      event.target === drawerBackdrop
    ) {
      setOverviewOpen(false);
      return;
    }
    const lyricCell = event.target.closest?.(
      "#editor-lines tbody td, #editor-lines [role='gridcell']"
    );
    const lyricColumn = lyricCell?.cellIndex ??
      (Number(lyricCell?.getAttribute("aria-colindex")) - 1);
    if (lyricCell && lyricColumn === 0) {
      window.setTimeout(() => setOverviewOpen(false), 120);
    }
  });

  const lineContextMenu = document.createElement("div");
  lineContextMenu.id = "kf-line-context-menu";
  lineContextMenu.innerHTML = `
    <button type="button" data-action="toggle-hidden">👁 隐藏 / 显示这句</button>
    <button type="button" data-action="insert-before">＋ 在上方插入一行</button>
    <button type="button" data-action="insert-after">＋ 在下方插入一行</button>
    <button type="button" data-action="delete">🗑 删除这句</button>
  `;
  document.body.appendChild(lineContextMenu);

  const closeLineContextMenu = () => {
    lineContextMenu.classList.remove("is-open");
    lineContextMenu.removeAttribute("data-row");
  };

  const lyricRowIndex = (target) => {
    const cell = target.closest?.(
      "#editor-lines td, #editor-lines [role='gridcell']"
    );
    const row = cell?.closest?.("tr, [role='row']");
    if (!cell || !row) return null;
    const cells = Array.from(
      row.querySelectorAll("td, [role='gridcell']")
    );
    const displayedNumber = Number.parseInt(cells[0]?.textContent?.trim() || "", 10);
    if (Number.isInteger(displayedNumber) && displayedNumber > 0) {
      return displayedNumber - 1;
    }
    const body = row.closest("tbody");
    if (body) {
      const rows = Array.from(body.querySelectorAll(":scope > tr"));
      const index = rows.indexOf(row);
      if (index >= 0) return index;
    }
    const ariaIndex = Number.parseInt(row.getAttribute("aria-rowindex") || "", 10);
    return Number.isInteger(ariaIndex) ? Math.max(0, ariaIndex - 2) : null;
  };

  const showLineContextMenu = (event) => {
    if (event.button !== 2 || !event.target.closest?.("#editor-lines")) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const rowIndex = lyricRowIndex(event.target);
    if (!Number.isInteger(rowIndex)) {
      closeLineContextMenu();
      return;
    }
    lineContextMenu.dataset.row = String(rowIndex);
    lineContextMenu.style.left = `${Math.max(
      4,
      Math.min(event.clientX, window.innerWidth - 224)
    )}px`;
    lineContextMenu.style.top = `${Math.max(
      4,
      Math.min(event.clientY, window.innerHeight - 178)
    )}px`;
    lineContextMenu.classList.add("is-open");
  };
  document.addEventListener("pointerdown", showLineContextMenu, true);
  document.addEventListener("contextmenu", (event) => {
    if (!event.target.closest?.("#editor-lines")) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);

  lineContextMenu.addEventListener("click", (event) => {
    const button = event.target.closest?.("button[data-action]");
    const row = Number(lineContextMenu.dataset.row);
    if (!button || !Number.isInteger(row)) return;
    const request = JSON.stringify({ row, action: button.dataset.action });
    closeLineContextMenu();
    if (!setHiddenInput("#kf-line-context-action", request)) return;
    window.setTimeout(() => {
      const root = document.querySelector("#kf-line-context-apply");
      const applyButton = root?.matches?.("button")
        ? root
        : root?.querySelector("button");
      applyButton?.click();
    }, 20);
  });

  document.addEventListener("pointerdown", (event) => {
    if (!event.target.closest?.("#kf-line-context-menu")) {
      closeLineContextMenu();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      setOverviewOpen(false);
      closeLineContextMenu();
    }
  });

  const syncAudioPlayback = (event) => {
    const audio = event.target;
    if (!(audio instanceof HTMLAudioElement) || !audio.closest("#editor-line-audio")) {
      return;
    }
    applyPlaybackRate(audio);
    updatePlaybackAt(audio.currentTime);
  };
  for (const eventName of ["play", "pause", "timeupdate", "seeked", "loadedmetadata"]) {
    document.addEventListener(eventName, syncAudioPlayback, true);
  }

  document.addEventListener("keydown", (event) => {
    if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "z") return;
    const timeline = document.activeElement?.closest?.(".kf-token-editor");
    if (!timeline) return;
    event.preventDefault();
    const button = timeline.querySelector(
      event.shiftKey ? ".kf-token-redo" : ".kf-token-undo"
    );
    button?.click();
  });
  return [];
}
"""


@dataclass(frozen=True)
class UiJobResult:
    status: str
    video: str | None
    files: list[str]
    log: str
    output_dir: str | None


@dataclass(frozen=True)
class UiEditorPreparationResult:
    status: str
    payload: dict[str, Any]
    rows: list[list[object]]
    line_number: int
    whole_pronunciation: str
    pronunciation_rows: list[list[object]]
    preview: str
    project: str | None
    audio: str | None
    files: list[str]
    log: str
    output_dir: str | None


def _file_path(value: object | None) -> Path | None:
    if value is None:
        return None
    if isinstance(value, (str, os.PathLike)):
        return Path(value)
    for attribute in ("path", "name"):
        candidate = getattr(value, attribute, None)
        if candidate:
            return Path(candidate)
    return None


def _safe_stem(value: str | None, fallback: str = "karaoke") -> str:
    stem = Path((value or "").strip()).stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", stem)
    stem = re.sub(r"\s+", "-", stem).strip(" .-_")
    return stem[:80] or fallback


def _default_output_root() -> Path:
    configured = os.environ.get("KARAOKE_FORGE_OUTPUT_DIR")
    root = Path(configured).expanduser() if configured else Path.cwd() / "outputs"
    return root.resolve()


def _new_job_dir(kind: str, output_root: str | Path | None = None) -> Path:
    root = Path(output_root).expanduser() if output_root else _default_output_root()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    directory = root / f"{kind}-{stamp}-{uuid4().hex[:6]}"
    directory.mkdir(parents=True, exist_ok=False)
    return directory.resolve()


def _prepare_lyrics(
    lyrics_file: object | None,
    pasted_lyrics: str | None,
    job_dir: Path,
) -> Path:
    if pasted_lyrics and pasted_lyrics.strip():
        target = job_dir / "lyrics.txt"
        target.write_text(pasted_lyrics.strip() + "\n", encoding="utf-8")
        return target
    source = _file_path(lyrics_file)
    if source is None or not source.is_file():
        raise ValueError("请上传歌词文件，或在歌词框中直接粘贴歌词。")
    return source


def _quality_settings(label: str) -> tuple[int, str]:
    options = {
        "快速预览": (24, "veryfast"),
        "推荐质量": (18, "medium"),
        "高质量": (16, "slow"),
    }
    return options.get(label, options["推荐质量"])


def _build_style(
    font: str,
    font_size: float,
    text_color: str,
    highlight_color: str,
    margin_v: float,
    show_translation: bool = True,
    translation_font_size: float = 38,
    translation_color: str = "#EAF4FF",
    show_pronunciation: bool = True,
    pronunciation_font_size: float = 26,
    pronunciation_color: str = "#FFFFFF",
) -> AssStyle:
    return AssStyle(
        font=font or "Microsoft YaHei",
        font_size=int(font_size),
        text_color=text_color,
        highlight_color=highlight_color,
        margin_v=int(margin_v),
        show_translation=show_translation,
        translation_font_size=int(translation_font_size),
        translation_color=translation_color,
        show_pronunciation=show_pronunciation,
        pronunciation_font_size=int(pronunciation_font_size),
        pronunciation_color=pronunciation_color,
    )


def _lyrics_with_translation(
    lyrics_path: Path,
    translated_lrc: str | None,
    job_dir: Path,
    original_lrc: str | None = None,
) -> Path:
    if not translated_lrc:
        return lyrics_path
    document = read_lyrics(lyrics_path)
    attached = attach_reference_translation(document, original_lrc, translated_lrc)
    if not attached:
        return lyrics_path
    target = job_dir / "lyrics-bilingual.json"
    target.write_text(write_format(document, "json"), encoding="utf-8")
    return target


def subtitle_preview_html(
    font: str,
    font_size: float,
    text_color: str,
    highlight_color: str,
    margin_v: float,
    show_translation: bool,
    translation_font_size: float,
    translation_color: str,
    show_pronunciation: bool,
    pronunciation_font_size: float,
    pronunciation_color: str,
    sample_text: str = "让每一句歌词，都踩准拍子。",
    sample_translation: str = "让歌声与画面在这里相遇。",
) -> str:
    """Return a browser-native preview of the current ASS subtitle style."""

    safe_font = html.escape(font or "Microsoft YaHei", quote=True)
    raw_lines = [
        line.strip()
        for line in (sample_text or "让每一句歌词，都踩准拍子。").splitlines()
        if line.strip()
    ]
    if not raw_lines:
        raw_lines = ["让每一句歌词，都踩准拍子。"]
    if len(raw_lines) >= 2:
        upper_text, lower_text = raw_lines[-2:]
    else:
        upper_text = "The lyric before this line"
        lower_text = raw_lines[0]
    split_at = max(1, round(len(lower_text) * 0.4))
    safe_translation = html.escape(sample_translation or "让歌声与画面在这里相遇。")
    main_size = max(16, min(48, round(float(font_size) * 0.55)))
    translated_size = max(13, min(36, round(float(translation_font_size) * 0.55)))
    pronunciation_size = max(10, min(24, round(float(pronunciation_font_size) * 0.55)))
    bottom = max(12, min(92, round(float(margin_v) * 0.42)))
    translation_html = ""
    if show_translation:
        translation_html = (
            '<div style="position:absolute;left:15%;right:15%;top:8%;'
            f"text-align:center;font-family:'{safe_font}',sans-serif;"
            f"font-size:{translated_size}px;color:{translation_color};font-weight:700;"
            "text-shadow:-1px -1px 0 #111,1px -1px 0 #111,"
            '-1px 1px 0 #111,1px 1px 0 #111,0 2px 6px #000;">'
            f"{safe_translation}</div>"
        )

    def coloured_source(value: str, start: int, *, active: bool) -> str:
        if not active:
            return html.escape(value)
        local_split = split_at - start
        if local_split <= 0:
            return html.escape(value)
        if local_split >= len(value):
            return f'<span style="color:{highlight_color};">{html.escape(value)}</span>'
        return (
            f'<span style="color:{highlight_color};">'
            f"{html.escape(value[:local_split])}</span>{html.escape(value[local_split:])}"
        )

    def preview_line(value: str, *, active: bool) -> str:
        pronunciation = generate_pronunciation(value) if show_pronunciation else None
        if pronunciation is None:
            return coloured_source(value, 0, active=active)
        parts: list[str] = []
        cursor = 0
        for unit in pronunciation.units:
            start = max(cursor, min(len(value), unit.start))
            end = max(start, min(len(value), unit.end or start + len(unit.source)))
            parts.append(coloured_source(value[cursor:start], cursor, active=active))
            reading = unit.reading
            reading_color = pronunciation_color
            if active and start < split_at:
                reading_color = highlight_color
            parts.append(
                '<ruby style="ruby-position:over;ruby-align:center;">'
                f"{coloured_source(value[start:end], start, active=active)}"
                f'<rt style="font-size:{pronunciation_size}px;color:{reading_color};'
                "font-weight:700;text-shadow:-1px -1px 0 #111,1px -1px 0 #111,"
                '-1px 1px 0 #111,1px 1px 0 #111,0 2px 5px #000;">'
                f"{html.escape(reading)}</rt></ruby>"
            )
            cursor = end
        parts.append(coloured_source(value[cursor:], cursor, active=active))
        return "".join(parts)

    upper_line_html = preview_line(upper_text, active=False)
    lower_line_html = preview_line(lower_text, active=True)
    return f"""
    <div class="kf-subtitle-preview" data-kf-layout="ktv-split">
      <div class="kf-preview-vignette"></div>
      {translation_html}
      <div style="position:absolute;left:6%;right:16%;
                  bottom:{bottom + main_size + 28}px;text-align:left;
                  font-family:'{safe_font}',sans-serif;font-size:{main_size}px;
                  color:{text_color};font-weight:800;
                  text-shadow:-2px -2px 0 #111,2px -2px 0 #111,
                              -2px 2px 0 #111,2px 2px 0 #111,0 3px 8px #000;">
        {upper_line_html}
      </div>
      <div style="position:absolute;left:16%;right:6%;bottom:{bottom}px;
                  text-align:right;font-family:'{safe_font}',sans-serif;
                  font-size:{main_size}px;color:{text_color};font-weight:800;
                  text-shadow:-2px -2px 0 #111,2px -2px 0 #111,
                              -2px 2px 0 #111,2px 2px 0 #111,0 3px 8px #000;">
        {lower_line_html}
      </div>
      <div class="kf-preview-badge">实时字幕预览 · KTV 双行布局</div>
    </div>
    """


def _build_align_options(
    language: str,
    model: str,
    device: str,
    separate_vocals: bool,
) -> AlignOptions:
    return AlignOptions(
        model=model,
        language=None if language == "自动识别" else language,
        device=device,
        compute_type="int8" if device == "cpu" else "default",
        separate_vocals=separate_vocals,
    )


def _web_timing_refinement(value: str | bool | None) -> str:
    if isinstance(value, bool):
        return "auto" if value else "off"
    return normalize_timing_refinement(value)


def _materialize_auto_pronunciation(document: LyricsDocument) -> int:
    """Persist generated readings so the editor can adjust them before rendering."""

    generated_count = 0
    for line in document.lines:
        if line.pronunciation or line.pronunciation_units:
            continue
        generated = generate_pronunciation(line.text)
        if generated is None:
            continue
        line.pronunciation_units = [
            PronunciationSpan(
                source=unit.source,
                reading=unit.reading,
                start=unit.start,
                end=unit.end,
            )
            for unit in generated.units
            if unit.reading.strip() and unit.end > unit.start
        ]
        if line.pronunciation_units:
            generated_count += 1
    return generated_count


def prepare_make_editor_job(
    audio_file: object | None,
    video_file: object | None,
    lyrics_file: object | None,
    pasted_lyrics: str,
    output_name: str,
    language: str,
    model: str,
    device: str,
    separate_vocals: bool,
    netease_link: str = "",
    use_netease_lyrics: bool = True,
    rights_confirmed: bool = False,
    timing_refinement: str | bool = "auto",
    output_root: str = "",
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> UiEditorPreparationResult:
    """Build an editable timed-lyrics project from the MV page's existing inputs."""

    logs: list[str] = []

    def report(message: str) -> None:
        logs.append(message)
        if progress_callback:
            progress_callback(message)

    job_dir: Path | None = None
    try:
        audio = _file_path(audio_file)
        video = _file_path(video_file)
        if audio is None or not audio.is_file():
            raise ValueError("请先在制作页上传歌曲音频；校准将直接使用这份音频。")
        if video is None or not video.is_file():
            raise ValueError("请先在制作页上传对应的 MV；之后无需再次上传。")
        if audio.suffix.lower() == ".ncm":
            raise ValueError(
                "不支持转换或解密 NCM 文件；请上传官方允许导出的 MP3、FLAC、WAV 或 M4A。"
            )

        job_dir = _new_job_dir("rehearsal", output_root.strip() or None)
        report("已沿用制作页上传的音频和 MV，无需重复上传")

        netease_info = None
        link = (netease_link or "").strip()
        if link:
            if not rights_confirmed:
                raise PermissionError("请勾选版权与使用权确认后再使用网易云链接。")
            netease_info = fetch_public_netease_info(link)
            report("仅从网易云读取公开歌曲信息、歌词和翻译，不下载音频")

        if lyrics_file is not None or (pasted_lyrics and pasted_lyrics.strip()):
            lyrics_path = _prepare_lyrics(lyrics_file, pasted_lyrics, job_dir)
            if netease_info is not None:
                lyrics_path = _lyrics_with_translation(
                    lyrics_path,
                    netease_info.translated_lyrics,
                    job_dir,
                    netease_info.page_lyrics,
                )
        elif (
            link
            and use_netease_lyrics
            and netease_info
            and (netease_info.word_lyrics or netease_info.page_lyrics)
        ):
            if netease_info.word_lyrics:
                lyrics_path = job_dir / "netease-lyrics.yrc"
                lyrics_path.write_text(netease_info.word_lyrics, encoding="utf-8")
                report("已使用网易云公开的真实逐字时间轴")
            else:
                lyrics_path = job_dir / "netease-lyrics.lrc"
                lyrics_path.write_text(netease_info.page_lyrics or "", encoding="utf-8")
                report("已使用网易云公开的行级时间轴")
            lyrics_path = _lyrics_with_translation(
                lyrics_path,
                netease_info.translated_lyrics,
                job_dir,
                netease_info.page_lyrics,
            )
            if netease_info.translated_lyrics and lyrics_path.suffix == ".json":
                report("已附加网易云中文翻译")
        else:
            raise ValueError("请上传/粘贴歌词，或填写网易云链接并勾选使用公开歌词。")

        source = read_lyrics(lyrics_path)
        if source.is_timed:
            timing_mode = _web_timing_refinement(timing_refinement)
            if should_refine_timing(source, timing_mode):
                refined = refine_audio_word_timing(
                    audio,
                    source,
                    options=_build_align_options(language, model, device, separate_vocals),
                    work_dir=job_dir / ".work",
                    progress=report,
                )
                document = refined.document
                timing_summary = (
                    f"时间轴已按上传音频精修，匹配覆盖率 **{refined.report.coverage:.1%}**。"
                )
            else:
                document = source
                if timing_mode == "off":
                    timing_summary = "已保留输入歌词的原始时间轴。"
                    report("逐字时间精修已关闭")
                else:
                    timing_summary = "检测到可信逐字时间，已直接用于校准。"
                    report("已有可信逐字时间，跳过重复识别")
        else:
            aligned = align_audio_and_lyrics(
                audio,
                lyrics_path,
                options=_build_align_options(language, model, device, separate_vocals),
                work_dir=job_dir / ".work",
                progress=report,
            )
            document = aligned.document
            timing_summary = (
                f"已按上传音频生成时间轴，匹配覆盖率 **{aligned.report.coverage:.1%}**。"
            )

        generated_count = _materialize_auto_pronunciation(document)
        report(f"已为 {generated_count} 行歌词生成可编辑注音")
        document.require_timed()
        fallback_stem = (
            f"{netease_info.title}-校准工程"
            if netease_info is not None
            else f"{video.stem}-校准工程"
        )
        stem = _safe_stem(output_name, fallback=fallback_stem)
        exports = export_formats(
            document,
            job_dir,
            stem,
            ["lrc", "elrc", "srt", "vtt", "ass", "json"],
        )
        project = exports["json"]
        files = [str(path) for path in exports.values()]
        line_number = 1
        line = document.lines[0]
        status = (
            "### ✅ 可校准 KTV 工程已生成\n"
            f"{timing_summary}\n\n"
            f"共 {len(document.lines)} 行，自动注音 {generated_count} 行。"
            "已沿用制作页的音频和 MV；现在可逐句试听和微调，完成后再渲染视频。"
        )
        report("校准工程已生成并送入编辑器")
        return UiEditorPreparationResult(
            status=status,
            payload=document.to_dict(),
            rows=document_to_editor_rows(document),
            line_number=line_number,
            whole_pronunciation=line.pronunciation or "",
            pronunciation_rows=pronunciation_to_editor_rows(line),
            preview=editor_preview_html(document, line_number),
            project=str(project),
            audio=str(audio),
            files=files,
            log="\n".join(logs),
            output_dir=str(job_dir),
        )
    except Exception as exc:
        logs.append(f"失败：{exc}")
        return UiEditorPreparationResult(
            status=f"### ⚠️ 没有生成校准工程\n{exc}",
            payload={},
            rows=[],
            line_number=1,
            whole_pronunciation="",
            pronunciation_rows=[],
            preview=f'<div class="kf-tip">生成失败：{html.escape(str(exc))}</div>',
            project=None,
            audio=None,
            files=[],
            log="\n".join(logs),
            output_dir=str(job_dir) if job_dir else None,
        )


def run_make_job(
    audio_file: object | None,
    video_file: object | None,
    lyrics_file: object | None,
    pasted_lyrics: str,
    output_name: str,
    language: str,
    model: str,
    device: str,
    separate_vocals: bool,
    quality: str,
    audio_offset: float,
    font: str,
    font_size: int,
    text_color: str,
    highlight_color: str,
    margin_v: int,
    netease_link: str = "",
    use_netease_lyrics: bool = True,
    rights_confirmed: bool = False,
    cookie_browser: str = "",
    cookie_browser_profile: str = "",
    auto_sync: bool = True,
    timing_refinement: str | bool = "auto",
    show_translation: bool = True,
    translation_font_size: float = 38,
    translation_color: str = "#EAF4FF",
    show_pronunciation: bool = True,
    pronunciation_font_size: float = 26,
    pronunciation_color: str = "#FFFFFF",
    output_root: str = "",
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> UiJobResult:
    logs: list[str] = []

    def report(message: str) -> None:
        logs.append(message)
        if progress_callback:
            progress_callback(message)

    job_dir: Path | None = None
    temporary_audio: Path | None = None
    try:
        audio = _file_path(audio_file)
        video = _file_path(video_file)
        if video is None or not video.is_file():
            raise ValueError("请先上传对应的 MV 视频。")

        job_dir = _new_job_dir("mv", output_root.strip() or None)
        netease_info = None
        link = (netease_link or "").strip()
        if link:
            if not rights_confirmed:
                raise PermissionError("请勾选版权与使用权确认后再使用网易云链接。")
            if audio is None:
                track = download_netease_track(
                    link,
                    job_dir / ".source",
                    cookie_browser=cookie_browser,
                    cookie_browser_profile=cookie_browser_profile,
                    progress=report,
                )
                netease_info = track
                if track.is_preview:
                    track.audio_path.unlink(missing_ok=True)
                    audio = video
                    report("网易云只返回试听片段，已自动改用 MV 内嵌的完整音轨")
                else:
                    audio = track.audio_path
                    temporary_audio = track.audio_path
            else:
                netease_info = fetch_public_netease_info(link)
                report("已使用本地音频，仅从网易云读取公开歌曲信息和歌词")

        if audio is None or not audio.is_file():
            raise ValueError("请上传歌曲音频，或提供可公开播放的网易云单曲链接。")
        if audio.suffix.lower() == ".ncm":
            raise ValueError(
                "不支持转换或解密 NCM 文件；请上传官方允许导出的 MP3、FLAC、WAV 或 M4A。"
            )

        if lyrics_file is not None or (pasted_lyrics and pasted_lyrics.strip()):
            lyrics = _prepare_lyrics(lyrics_file, pasted_lyrics, job_dir)
            if netease_info is not None:
                lyrics = _lyrics_with_translation(
                    lyrics,
                    netease_info.translated_lyrics,
                    job_dir,
                    netease_info.page_lyrics,
                )
        elif (
            link
            and use_netease_lyrics
            and netease_info
            and (netease_info.word_lyrics or netease_info.page_lyrics)
        ):
            if netease_info.word_lyrics:
                lyrics = job_dir / "netease-lyrics.yrc"
                lyrics.write_text(netease_info.word_lyrics, encoding="utf-8")
            else:
                lyrics = job_dir / "netease-lyrics.lrc"
                lyrics.write_text(netease_info.page_lyrics or "", encoding="utf-8")
            lyrics = _lyrics_with_translation(
                lyrics,
                netease_info.translated_lyrics,
                job_dir,
                netease_info.page_lyrics,
            )
            timing_detail = "逐字时间轴" if netease_info.word_lyrics else "行级时间轴"
            report(f"已使用网易云页面公开歌词和{timing_detail}")
            if netease_info.translated_lyrics and lyrics.suffix == ".json":
                report("已附加网易云中文翻译，将固定显示在画面顶部")
        else:
            raise ValueError("请提供歌词，或勾选使用网易云页面公开歌词。")

        fallback_stem = (
            f"{netease_info.title}-karaoke" if netease_info is not None else f"{video.stem}-karaoke"
        )
        stem = _safe_stem(output_name, fallback=fallback_stem)
        output = job_dir / f"{stem}.mp4"
        assets = job_dir / f"{stem}.assets"
        crf, preset = _quality_settings(quality)
        report("素材检查完成")

        result = make_karaoke_video(
            audio,
            video,
            lyrics,
            output,
            assets,
            options=MakeOptions(
                align=_build_align_options(language, model, device, separate_vocals),
                style=_build_style(
                    font,
                    font_size,
                    text_color,
                    highlight_color,
                    margin_v,
                    show_translation,
                    translation_font_size,
                    translation_color,
                    show_pronunciation,
                    pronunciation_font_size,
                    pronunciation_color,
                ),
                audio_offset=float(audio_offset),
                crf=crf,
                preset=preset,
                overwrite=False,
                auto_sync=auto_sync,
                timing_refinement=_web_timing_refinement(timing_refinement),
            ),
            progress=report,
        )
        if temporary_audio:
            temporary_audio.unlink(missing_ok=True)
            report("本次获取的临时音频已清理")
        files = [str(result.video), *(str(path) for path in result.exports.values())]
        if result.alignment_report:
            alignment = (
                f"歌词匹配覆盖率 **{result.alignment_report.coverage:.1%}**，"
                f"匹配 {result.alignment_report.matched_units}/"
                f"{result.alignment_report.target_units} 个词元。"
            )
        else:
            alignment = "检测到已有时间轴歌词，因此跳过了自动对齐。"
        sync_result = getattr(result, "sync_result", None)
        if sync_result is not None:
            sync_text = (
                f"\n\n自动定位到歌曲从 MV 第 **{sync_result.offset:.2f} 秒**开始，"
                f"置信度 **{sync_result.confidence:.0%}**。"
            )
        elif audio.resolve() == video.resolve():
            sync_text = "\n\n已直接使用 MV 内嵌完整音轨。"
        else:
            sync_text = ""
        status = (
            f"### ✅ 卡拉 OK MV 已生成\n{alignment}{sync_text}"
            "\n\n成品和所有歌词格式已保存，可以预览或下载。"
        )
        report("全部完成")
        return UiJobResult(status, str(result.video), files, "\n".join(logs), str(job_dir))
    except Exception as exc:
        if temporary_audio:
            temporary_audio.unlink(missing_ok=True)
        logs.append(f"失败：{exc}")
        return UiJobResult(
            "### ⚠️ 没有生成成功\n"
            f"{exc}\n\n请检查素材是否匹配；如果是首次使用，也可以到“环境检查”页面查看依赖。",
            None,
            [],
            "\n".join(logs),
            str(job_dir) if job_dir else None,
        )


def run_align_job(
    audio_file: object | None,
    lyrics_file: object | None,
    pasted_lyrics: str,
    output_name: str,
    language: str,
    model: str,
    device: str,
    separate_vocals: bool,
    timing_refinement: str | bool = "off",
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> UiJobResult:
    logs: list[str] = []

    def report(message: str) -> None:
        logs.append(message)
        if progress_callback:
            progress_callback(message)

    job_dir: Path | None = None
    try:
        audio = _file_path(audio_file)
        if audio is None or not audio.is_file():
            raise ValueError("请先上传歌曲音频。")
        job_dir = _new_job_dir("lyrics")
        lyrics_path = _prepare_lyrics(lyrics_file, pasted_lyrics, job_dir)
        source = read_lyrics(lyrics_path)
        report("素材检查完成")

        alignment_text: str
        if source.is_timed:
            timing_mode = _web_timing_refinement(timing_refinement)
            if should_refine_timing(source, timing_mode):
                refined = refine_audio_word_timing(
                    audio,
                    source,
                    options=_build_align_options(language, model, device, separate_vocals),
                    work_dir=job_dir / ".work",
                    progress=report,
                )
                document = refined.document
                alignment_text = (
                    f"逐字时间已按“{'强制' if timing_mode == 'force' else '自动'}”"
                    f"策略精修，匹配覆盖率 **{refined.report.coverage:.1%}**。"
                )
            else:
                document = source
                if timing_mode == "off":
                    alignment_text = "检测到已有时间轴；时间精修已关闭，完整保留输入文件时间。"
                    report("逐字时间精修已关闭")
                else:
                    alignment_text = "检测到可信逐字时间，已直接进行格式导出。"
                    report("已有可信逐字时间，跳过识别")
        else:
            result = align_audio_and_lyrics(
                audio,
                lyrics_path,
                options=_build_align_options(language, model, device, separate_vocals),
                work_dir=job_dir / ".work",
                progress=report,
            )
            document = result.document
            alignment_text = (
                f"匹配覆盖率 **{result.report.coverage:.1%}**，"
                f"匹配 {result.report.matched_units}/{result.report.target_units} 个词元。"
            )

        stem = _safe_stem(output_name, fallback=lyrics_path.stem)
        exports = export_formats(
            document,
            job_dir,
            stem,
            ["lrc", "elrc", "srt", "vtt", "ass", "json"],
        )
        report("全部歌词格式已导出")
        return UiJobResult(
            f"### ✅ 时间轴歌词已生成\n{alignment_text}",
            None,
            [str(path) for path in exports.values()],
            "\n".join(logs),
            str(job_dir),
        )
    except Exception as exc:
        logs.append(f"失败：{exc}")
        return UiJobResult(
            f"### ⚠️ 没有生成成功\n{exc}",
            None,
            [],
            "\n".join(logs),
            str(job_dir) if job_dir else None,
        )


def run_netease_align_job(
    link: str,
    local_audio_file: object | None,
    lyrics_file: object | None,
    pasted_lyrics: str,
    output_name: str,
    language: str,
    model: str,
    device: str,
    separate_vocals: bool,
    use_page_lyrics: bool,
    keep_audio: bool,
    rights_confirmed: bool,
    cookie_browser: str = "",
    cookie_browser_profile: str = "",
    timing_refinement: str | bool = "auto",
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> UiJobResult:
    logs: list[str] = []

    def report(message: str) -> None:
        logs.append(message)
        if progress_callback:
            progress_callback(message)

    job_dir: Path | None = None
    try:
        if not (link or "").strip():
            raise ValueError("请粘贴网易云音乐单曲链接。")
        job_dir = _new_job_dir("netease")
        local_audio = _file_path(local_audio_file)
        if lyrics_file is not None or (pasted_lyrics and pasted_lyrics.strip()):
            effective_lyrics: Path | None = _prepare_lyrics(
                lyrics_file,
                pasted_lyrics,
                job_dir,
            )
        else:
            effective_lyrics = None

        result = align_netease_song(
            link,
            effective_lyrics,
            job_dir,
            local_audio_path=local_audio,
            name=_safe_stem(output_name, fallback="netease-lyrics"),
            options=NeteaseAlignOptions(
                align=_build_align_options(
                    language,
                    model,
                    device,
                    separate_vocals,
                ),
                use_page_lyrics=use_page_lyrics,
                keep_audio=keep_audio,
                rights_confirmed=rights_confirmed,
                cookie_browser=cookie_browser or None,
                cookie_browser_profile=cookie_browser_profile or None,
                timing_refinement=_web_timing_refinement(timing_refinement),
            ),
            progress=report,
        )
        files = [str(path) for path in result.exports.values()]
        if result.kept_audio:
            files.append(str(result.kept_audio))
        if result.alignment_report:
            timing = (
                f"重新对齐覆盖率 **{result.alignment_report.coverage:.1%}**，"
                f"匹配 {result.alignment_report.matched_units}/"
                f"{result.alignment_report.target_units} 个词元。"
            )
        else:
            timing = (
                "使用了网易云提供的真实逐字时间轴。"
                if result.track.word_lyrics
                else "使用了歌词文件中已有的行级时间轴。"
            )
        access = f"账号与音质：{result.track.access_text}  \n" if result.track.access_text else ""
        status = (
            f"### ✅ {result.track.title} 的时间轴已生成\n"
            f"歌手：{result.track.artist_text}  \n"
            f"{access}"
            f"{timing}"
        )
        report("全部歌词格式已导出")
        return UiJobResult(status, None, files, "\n".join(logs), str(job_dir))
    except Exception as exc:
        logs.append(f"失败：{exc}")
        return UiJobResult(
            f"### ⚠️ 没有生成成功\n{exc}",
            None,
            [],
            "\n".join(logs),
            str(job_dir) if job_dir else None,
        )


def run_convert_job(
    lyrics_file: object | None,
    output_format: str,
) -> UiJobResult:
    job_dir: Path | None = None
    try:
        source = _file_path(lyrics_file)
        if source is None or not source.is_file():
            raise ValueError("请上传一个带时间轴的歌词文件。")
        document = read_lyrics(source)
        document.require_timed()
        job_dir = _new_job_dir("convert")
        suffix = "lrc" if output_format == "elrc" else output_format
        label = "enhanced" if output_format == "elrc" else output_format
        target = job_dir / f"{source.stem}.{label}.{suffix}"
        target.write_text(write_format(document, output_format), encoding="utf-8")
        return UiJobResult(
            f"### ✅ 已转换为 {output_format.upper()}\n文件可以直接下载。",
            None,
            [str(target)],
            f"读取：{source.name}\n输出：{target.name}",
            str(job_dir),
        )
    except Exception as exc:
        return UiJobResult(
            f"### ⚠️ 转换失败\n{exc}",
            None,
            [],
            f"失败：{exc}",
            str(job_dir) if job_dir else None,
        )


def load_editor_project(
    lyrics_file: object | None,
) -> tuple[dict[str, Any], list[list[object]], str, int, str, list[list[object]], str]:
    source = _file_path(lyrics_file)
    if source is None or not source.is_file():
        raise ValueError("请先上传带时间轴的歌词或 Karaoke Forge JSON。")
    document = read_lyrics(source)
    document.require_timed()
    line_number = 1
    line = document.lines[0]
    return (
        document.to_dict(),
        document_to_editor_rows(document),
        f"### ✅ 已载入 {source.name}\n共 {len(document.lines)} 行，可编辑后导出。",
        line_number,
        line.pronunciation or "",
        pronunciation_to_editor_rows(line),
        editor_preview_html(document, line_number),
    )


def load_editor_line(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
) -> tuple[dict[str, Any], list[list[object]], str, list[list[object]], str]:
    document = apply_editor_rows(document_from_payload(payload), line_table)
    index = int(line_number) - 1
    if index < 0 or index >= len(document.lines):
        raise ValueError(f"行号应在 1 到 {len(document.lines)} 之间。")
    line = document.lines[index]
    return (
        document.to_dict(),
        document_to_editor_rows(document),
        line.pronunciation or "",
        pronunciation_to_editor_rows(line),
        editor_preview_html(document, int(line_number)),
    )


def _editor_selected_line_outputs(
    document: LyricsDocument,
    line_number: int,
) -> tuple[
    dict[str, Any],
    list[list[object]],
    int,
    str,
    list[list[object]],
    str,
    str,
    str,
]:
    selected = min(max(1, int(line_number)), len(document.lines))
    line = document.lines[selected - 1]
    return (
        document.to_dict(),
        document_to_editor_rows(document),
        selected,
        line.pronunciation or "",
        pronunciation_to_editor_rows(line),
        editor_preview_html(document, selected),
        editor_token_timeline_html(document, selected),
        token_timing_to_json(line),
    )


def apply_editor_line_action(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    action_request: str,
) -> tuple[object, ...]:
    document = apply_editor_rows(document_from_payload(payload), line_table)
    try:
        request = json.loads(str(action_request or ""))
        row_index = int(request["row"])
        action = str(request["action"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("歌词行操作无效，请重新选择歌词行。") from exc
    if row_index < 0 or row_index >= len(document.lines):
        raise ValueError("选择的歌词行已经变化，请重新选择。")

    undo_payload = document.to_dict()
    rows = document_to_editor_rows(document)
    selected = int(line_number)
    if action == "toggle-hidden":
        is_hidden = rows[row_index][1] == "隐藏"
        rows[row_index][1] = "显示" if is_hidden else "隐藏"
        selected = row_index + 1
        action_label = "恢复显示" if is_hidden else "隐藏"
    elif action == "delete":
        if len(rows) <= 1:
            raise ValueError("项目至少需要保留一行歌词，不能删除最后一行。")
        rows.pop(row_index)
        if row_index + 1 < selected:
            selected -= 1
        elif row_index + 1 == selected:
            selected = min(row_index + 1, len(rows))
        action_label = "删除"
    elif action in {"insert-before", "insert-after"}:
        current = document.lines[row_index]
        if current.start is None or current.end is None:
            raise ValueError("当前歌词没有完整时间，无法自动插入新行。")
        if action == "insert-before":
            insert_at = row_index
            previous_end = document.lines[row_index - 1].end if row_index > 0 else None
            end = current.start
            start = max(0.0, previous_end if previous_end is not None else end - 2.0)
            if end <= start + 0.01:
                start = max(0.0, end - 0.5)
                end = max(start + 0.5, end)
            position_label = "上方"
        else:
            insert_at = row_index + 1
            next_start = (
                document.lines[row_index + 1].start if row_index + 1 < len(document.lines) else None
            )
            start = current.end
            end = next_start if next_start is not None else start + 2.0
            if end <= start + 0.01:
                end = start + 0.5
            position_label = "下方"
        rows.insert(
            insert_at,
            [len(document.lines) + 1, "显示", start, end, "新歌词", ""],
        )
        selected = insert_at + 1
        action_label = f"在第 {row_index + 1} 行{position_label}插入新行"
    else:
        raise ValueError("不支持的歌词行操作。")

    edited = apply_editor_rows(document, rows)
    workspace = _editor_selected_line_outputs(edited, selected)
    if action in {"insert-before", "insert-after"}:
        headline = f"### ✅ 已{action_label}"
    else:
        headline = f"### ✅ 已{action_label}第 {row_index + 1} 行"
    status = f"{headline}\n如有误操作，点击右侧“撤销 / 重做”。"
    return (*workspace, status, undo_payload)


def apply_editor_current_line_action(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    action: str,
) -> tuple[object, ...]:
    request = json.dumps(
        {"row": int(line_number) - 1, "action": action},
        ensure_ascii=False,
    )
    return apply_editor_line_action(payload, line_table, line_number, request)


def undo_editor_line_action(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    undo_payload: dict[str, Any],
) -> tuple[object, ...]:
    if not undo_payload or not undo_payload.get("lines"):
        raise ValueError("目前没有可撤销的隐藏或删除操作。")
    current = apply_editor_rows(document_from_payload(payload), line_table)
    restored = document_from_payload(undo_payload)
    restored.require_timed()
    workspace = _editor_selected_line_outputs(restored, int(line_number))
    status = "### ✅ 已撤销上次歌词行操作\n再次点击可以重做。"
    return (*workspace, status, current.to_dict())


def save_editor_pronunciation(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    whole_line: str,
    pronunciation_table: object,
) -> tuple[dict[str, Any], list[list[object]], str, list[list[object]], str, str]:
    document = apply_editor_rows(document_from_payload(payload), line_table)
    document = apply_pronunciation_rows(
        document,
        int(line_number),
        pronunciation_table,
        whole_line,
    )
    line = document.lines[int(line_number) - 1]
    return (
        document.to_dict(),
        document_to_editor_rows(document),
        line.pronunciation or "",
        pronunciation_to_editor_rows(line),
        editor_preview_html(document, int(line_number)),
        f"### ✅ 已保存第 {int(line_number)} 行注音",
    )


def preview_editor_changes(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    whole_line: str,
    pronunciation_table: object,
) -> str:
    try:
        document = apply_editor_rows(document_from_payload(payload), line_table)
        document = apply_pronunciation_rows(
            document,
            int(line_number),
            pronunciation_table,
            whole_line,
        )
        return editor_preview_html(document, int(line_number))
    except Exception as exc:
        return f'<div class="kf-tip">预览暂不可用：{html.escape(str(exc))}</div>'


def nudge_editor_timing(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    *,
    start_delta: float = 0.0,
    end_delta: float = 0.0,
) -> tuple[dict[str, Any], list[list[object]], str, str]:
    document = nudge_editor_line_timing(
        document_from_payload(payload),
        line_table,
        int(line_number),
        start_delta=start_delta,
        end_delta=end_delta,
    )
    line = document.lines[int(line_number) - 1]
    assert line.start is not None and line.end is not None
    status = (
        f"第 {int(line_number)} 行：**{line.start:.2f}s → {line.end:.2f}s**"
        f"（时长 {line.end - line.start:.2f}s）"
    )
    return (
        document.to_dict(),
        document_to_editor_rows(document),
        editor_preview_html(document, int(line_number)),
        status,
    )


def editor_token_workspace(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
) -> tuple[str, str]:
    document = apply_editor_rows(document_from_payload(payload), line_table)
    index = int(line_number) - 1
    if index < 0 or index >= len(document.lines):
        raise ValueError(f"行号应在 1 到 {len(document.lines)} 之间。")
    return (
        editor_token_timeline_html(document, int(line_number)),
        token_timing_to_json(document.lines[index]),
    )


def save_editor_token_timing(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    token_timing_json: str,
) -> tuple[dict[str, Any], list[list[object]], str, str, str, str]:
    document = apply_token_timing(
        document_from_payload(payload),
        line_table,
        int(line_number),
        token_timing_json,
    )
    line = document.lines[int(line_number) - 1]
    assert line.start is not None and line.end is not None
    return (
        document.to_dict(),
        document_to_editor_rows(document),
        editor_preview_html(document, int(line_number)),
        editor_token_timeline_html(document, int(line_number)),
        token_timing_to_json(line),
        (
            f"### ✅ 已保存第 {int(line_number)} 行逐词时间\n"
            f"整句范围：**{line.start:.2f}s → {line.end:.2f}s**，"
            f"共 {len(line.tokens)} 个词块。"
        ),
    )


def preview_editor_audio_line(
    audio_file: object | None,
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    lead_in: float = 1.0,
    tail: float = 1.0,
) -> tuple[str, str]:
    audio = _file_path(audio_file)
    if audio is None or not audio.is_file():
        raise ValueError("请先上传用于校准的歌曲音频。")
    document = apply_editor_rows(document_from_payload(payload), line_table)
    index = int(line_number) - 1
    if index < 0 or index >= len(document.lines):
        raise ValueError(f"行号应在 1 到 {len(document.lines)} 之间。")
    line = document.lines[index]
    if line.start is None or line.end is None:
        raise ValueError("当前歌词行没有完整时间，无法试听。")
    clip_start = max(0.0, line.start - max(0.0, float(lead_in)))
    clip_end = line.end + max(0.0, float(tail))
    cache_root = Path(
        os.environ.get("KARAOKE_FORGE_CACHE_DIR")
        or (_default_output_root().parent / "KaraokeForgeCache")
    ).expanduser()
    clip_dir = cache_root / "editor-clips"
    clip_dir.mkdir(parents=True, exist_ok=True)
    target = clip_dir / (
        f"{_safe_stem(audio.stem)}-line-{index + 1}-{line.start:.3f}-{line.end:.3f}.m4a"
    )
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("没有找到 FFmpeg，无法截取当前句试听片段。")
    if not target.is_file():
        completed = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{clip_start:.3f}",
                "-i",
                str(audio),
                "-t",
                f"{clip_end - clip_start:.3f}",
                "-vn",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(target),
            ],
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )
        if completed.returncode != 0:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"当前句试听片段生成失败：{completed.stderr.strip()}")
    status = (
        f"试听片段从 **{clip_start:.2f}s** 开始；当前歌词应在 "
        f"**{line.start:.2f}s → {line.end:.2f}s** 之间。"
    )
    return str(target), status


def export_editor_project(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    whole_line: str,
    pronunciation_table: object,
    output_name: str,
) -> tuple[dict[str, Any], list[list[object]], str, list[str], str]:
    document = apply_editor_rows(document_from_payload(payload), line_table)
    document = apply_pronunciation_rows(
        document,
        int(line_number),
        pronunciation_table,
        whole_line,
    )
    job_dir = _new_job_dir("editor")
    stem = _safe_stem(output_name, fallback="edited-lyrics")
    formats = ["lrc", "elrc", "srt", "vtt", "ass", "json"] if document.visible_lines else ["json"]
    exports = export_formats(
        document,
        job_dir,
        stem,
        formats,
    )
    hidden_count = sum(line.hidden for line in document.lines)
    visibility_note = (
        "；当前没有可见行，因此只导出了可恢复的 JSON 项目文件" if not document.visible_lines else ""
    )
    status = (
        f"### ✅ 编辑结果已导出\n保留 {len(document.lines)} 行，"
        f"其中 {hidden_count} 行暂时隐藏；隐藏行仅保留在 JSON 项目文件中"
        f"{visibility_note}。"
    )
    return (
        document.to_dict(),
        document_to_editor_rows(document),
        status,
        [str(path) for path in exports.values()],
        str(job_dir),
    )


def handoff_editor_to_make(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    whole_line: str,
    pronunciation_table: object,
    output_name: str,
    audio_file: object | None,
) -> tuple[
    dict[str, Any],
    list[list[object]],
    str,
    list[str],
    str,
    str,
    str | None,
]:
    updated_payload, rows, status, files, output_dir = export_editor_project(
        payload,
        line_table,
        line_number,
        whole_line,
        pronunciation_table,
        output_name,
    )
    project = next((path for path in files if path.lower().endswith(".json")), None)
    if project is None:
        raise RuntimeError("没有生成可交给 MV 制作的 JSON 项目文件。")
    audio = _file_path(audio_file)
    audio_value = str(audio) if audio is not None and audio.is_file() else None
    status += (
        "\n\n### ✅ 已交给“制作卡拉 OK MV”\n"
        "歌词工程和校准音频已经带入；制作页原来上传的 MV 会继续保留，"
        "现在可以直接生成最终视频。"
    )
    return (
        updated_payload,
        rows,
        status,
        files,
        output_dir,
        project,
        audio_value,
    )


def environment_markdown() -> str:
    checks = [
        ("Python 3.10+", sys.version_info >= (3, 10), sys.version.split()[0]),
        ("FFmpeg", shutil.which("ffmpeg") is not None, shutil.which("ffmpeg") or "未找到"),
        (
            "faster-whisper",
            importlib.util.find_spec("faster_whisper") is not None,
            "已安装" if importlib.util.find_spec("faster_whisper") else "未安装",
        ),
        (
            "Demucs（可选）",
            importlib.util.find_spec("demucs") is not None,
            "已安装" if importlib.util.find_spec("demucs") else "未安装",
        ),
        (
            "Gradio 网页",
            importlib.util.find_spec("gradio") is not None,
            "已安装" if importlib.util.find_spec("gradio") else "未安装",
        ),
        (
            "网易云链接适配器",
            importlib.util.find_spec("yt_dlp") is not None,
            "已安装" if importlib.util.find_spec("yt_dlp") else "未安装",
        ),
        (
            "日语/英语注音",
            importlib.util.find_spec("pykakasi") is not None
            and importlib.util.find_spec("alkana") is not None,
            "已安装"
            if importlib.util.find_spec("pykakasi") and importlib.util.find_spec("alkana")
            else "未安装；请重新运行首次安装.bat",
        ),
    ]
    rows = ["### 本机环境"]
    for name, ok, detail in checks:
        icon = "✅" if ok else "⚪"
        rows.append(f"- {icon} **{name}**：{detail}")
    rows.extend(
        [
            "",
            (
                "> faster-whisper 用于从无时间轴歌词生成时间；Demucs 只在勾选"
                "“先分离人声”时需要；yt-dlp 用于获取当前匿名或已登录账号有权播放的"
                "网易云音频；pykakasi 与 alkana 用于离线生成日语和英语注音。"
            ),
            "",
            "输出默认保存在项目的 `outputs` 目录。页面运行在本机，素材不会自动上传到公网。",
        ]
    )
    return "\n".join(rows)


def _open_output_directory(path: str | None) -> str:
    if not path:
        return "还没有可打开的输出目录。"
    directory = Path(path)
    if not directory.is_dir():
        return "输出目录已经不存在。"
    try:
        if sys.platform == "win32":
            os.startfile(directory)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(directory)])
        else:
            subprocess.Popen(["xdg-open", str(directory)])
    except Exception as exc:
        return f"无法打开目录：{exc}"
    return f"已打开：`{directory}`"


def create_web_app() -> object:
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError(
            '网页依赖尚未安装。请运行 `pip install -e ".[web]"`，'
            '需要自动对齐和网易云链接时安装 `pip install -e ".[web,align,netease]"`。'
        ) from exc

    with gr.Blocks(
        title="Karaoke Forge｜本地卡拉 OK 工作台",
        fill_width=True,
        delete_cache=(3600, 86400),
    ) as app:
        make_output_directory = gr.State()
        align_output_directory = gr.State()
        netease_output_directory = gr.State()
        editor_payload = gr.State({})
        editor_line_undo_payload = gr.State({})
        editor_output_directory = gr.State()
        gr.HTML(
            """
            <div class="kf-shell">
              <section class="kf-hero">
                <div class="kf-kicker">Karaoke Forge · Local Studio</div>
                <h1 class="kf-title">让每一句歌词，<br>都踩准拍子。</h1>
                <p class="kf-subtitle">
                  上传歌曲、MV 和歌词，剩下的交给本地工作台。
                  自动生成时间轴、逐字高亮字幕和卡拉 OK 成片。
                </p>
                <div class="kf-steps">
                  <span class="kf-step"><b>01</b>选择素材</span>
                  <span class="kf-step"><b>02</b>调整效果</span>
                  <span class="kf-step"><b>03</b>点击生成</span>
                  <span class="kf-step"><b>04</b>预览下载</span>
                </div>
              </section>
            </div>
            """
        )

        with gr.Tabs() as main_tabs:
            with gr.Tab("制作卡拉 OK MV", id="make"), gr.Row(equal_height=False):
                with gr.Column(scale=7, min_width=340):
                    with gr.Group(elem_classes="kf-card"):
                        gr.HTML('<div class="kf-section-label">Step 01 · 素材</div>')
                        gr.Markdown("## 把三个文件放进来")
                        with gr.Row():
                            make_audio = gr.File(
                                label="① 歌曲音频（或使用网易云公开音频）",
                                file_types=["audio"],
                                type="filepath",
                            )
                            make_video = gr.File(
                                label="② 对应 MV",
                                file_types=["video"],
                                type="filepath",
                            )
                            make_lyrics = gr.File(
                                label="③ 歌词文件",
                                file_types=[".txt", ".lrc", ".srt", ".vtt", ".ass", ".json"],
                                type="filepath",
                            )
                        with gr.Accordion("没有歌词文件？直接粘贴歌词", open=False):
                            make_pasted = gr.Textbox(
                                label="一行一句",
                                lines=8,
                                placeholder="第一句歌词\n第二句歌词\n第三句歌词",
                            )
                        with gr.Accordion("使用网易云链接补充音频或歌词", open=False):
                            make_netease_link = gr.Textbox(
                                label="网易云单曲链接",
                                placeholder="https://music.163.com/song?id=...",
                            )
                            with gr.Row():
                                make_cookie_browser = gr.Dropdown(
                                    label="账号权限",
                                    choices=[
                                        ("匿名（仅公开音频）", ""),
                                        ("Chrome 已登录账号", "chrome"),
                                        ("Edge 已登录账号", "edge"),
                                        ("Firefox 已登录账号", "firefox"),
                                        ("Brave 已登录账号", "brave"),
                                    ],
                                    value="",
                                )
                                make_cookie_profile = gr.Textbox(
                                    label="浏览器配置（可选）",
                                    placeholder="留空使用默认配置，如 Profile 1",
                                )
                            make_use_netease_lyrics = gr.Checkbox(
                                label="没有上传歌词时，使用网易云页面公开歌词",
                                value=True,
                            )
                            make_rights = gr.Checkbox(
                                label="我确认账号和歌曲归我合法使用，且不会绕过地区、版权或 DRM 限制",
                                value=False,
                            )
                            gr.Markdown(
                                "> 选择浏览器后，会检测其中的网易云登录状态和本曲 VIP/SVIP"
                                "音质权限，并使用账号实际有权播放的最高音质。Cookie 只在本机"
                                "内存中读取，不会保存；本工具不接收密码，也不转换 NCM。"
                            )
                        gr.HTML(
                            '<div class="kf-tip">歌曲和 MV 应是同一版本。'
                            "已有 LRC/SRT 等时间轴歌词时，系统会自动跳过识别。</div>"
                        )

                    with gr.Group(elem_classes="kf-card"):
                        gr.HTML('<div class="kf-section-label">Step 02 · 效果</div>')
                        gr.Markdown("## 选择字幕外观")
                        with gr.Row():
                            make_name = gr.Textbox(
                                label="成品名称",
                                value="我的卡拉OK",
                            )
                            make_language = gr.Dropdown(
                                label="歌曲语言",
                                choices=[
                                    ("自动识别", "自动识别"),
                                    ("中文", "zh"),
                                    ("英语", "en"),
                                    ("日语", "ja"),
                                    ("韩语", "ko"),
                                    ("粤语", "yue"),
                                ],
                                value="自动识别",
                            )
                            make_quality = gr.Radio(
                                label="视频质量",
                                choices=["快速预览", "推荐质量", "高质量"],
                                value="推荐质量",
                            )
                        with gr.Row():
                            make_font = gr.Dropdown(
                                label="字幕字体",
                                choices=[
                                    "Microsoft YaHei",
                                    "PingFang SC",
                                    "Noto Sans CJK SC",
                                    "Arial",
                                ],
                                value="Microsoft YaHei",
                                allow_custom_value=True,
                            )
                            make_font_size = gr.Slider(32, 88, value=58, step=1, label="字号")
                            make_margin = gr.Slider(30, 180, value=72, step=2, label="底部距离")
                        with gr.Row():
                            make_text_color = gr.ColorPicker(label="未唱颜色", value="#FFFFFF")
                            make_highlight_color = gr.ColorPicker(
                                label="唱到的颜色", value="#FFD54A"
                            )
                        with gr.Row():
                            make_show_translation = gr.Checkbox(
                                label="有中文翻译时固定显示在画面顶部",
                                value=True,
                            )
                            make_translation_size = gr.Slider(
                                24,
                                58,
                                value=38,
                                step=1,
                                label="翻译字号",
                            )
                            make_translation_color = gr.ColorPicker(
                                label="翻译颜色",
                                value="#EAF4FF",
                            )
                        with gr.Row():
                            make_show_pronunciation = gr.Checkbox(
                                label="显示日语振假名和英语片假名读音",
                                value=True,
                            )
                            make_pronunciation_size = gr.Slider(
                                18,
                                40,
                                value=26,
                                step=1,
                                label="注音字号",
                            )
                            make_pronunciation_color = gr.ColorPicker(
                                label="注音颜色",
                                value="#FFFFFF",
                            )

                        with gr.Accordion("字幕实时预览", open=True):
                            with gr.Row():
                                make_preview_text = gr.Textbox(
                                    label="原文双行预览（上一行 + 当前行）",
                                    value=(
                                        "I hear the flowers whisper.\n"
                                        "Let me bloom inside your garden."
                                    ),
                                    lines=2,
                                )
                                make_preview_translation = gr.Textbox(
                                    label="中文翻译预览",
                                    value="让我在你的花园里盛放。",
                                )
                            make_style_preview = gr.HTML(
                                subtitle_preview_html(
                                    "Microsoft YaHei",
                                    58,
                                    "#FFFFFF",
                                    "#FFD54A",
                                    72,
                                    True,
                                    38,
                                    "#EAF4FF",
                                    True,
                                    26,
                                    "#FFFFFF",
                                    (
                                        "I hear the flowers whisper.\n"
                                        "Let me bloom inside your garden."
                                    ),
                                    "让我在你的花园里盛放。",
                                )
                            )

                        with gr.Accordion("高级设置", open=False):
                            with gr.Row():
                                make_model = gr.Dropdown(
                                    label="识别模型",
                                    choices=["tiny", "base", "small", "medium", "large-v3"],
                                    value="small",
                                )
                                make_device = gr.Radio(
                                    label="运行设备",
                                    choices=[
                                        ("自动选择", "auto"),
                                        ("只用 CPU", "cpu"),
                                        ("NVIDIA 显卡", "cuda"),
                                    ],
                                    value="auto",
                                )
                            with gr.Row():
                                make_separate = gr.Checkbox(
                                    label="先分离人声（更慢，复杂伴奏可尝试）",
                                    value=False,
                                )
                                make_auto_sync = gr.Checkbox(
                                    label="自动定位 MV 中歌曲开始位置",
                                    value=True,
                                )
                                make_timing_refinement = gr.Dropdown(
                                    label="逐字时间精修",
                                    choices=[
                                        ("关闭：完全保留输入时间", "off"),
                                        ("自动：只精修行级/合成时间", "auto"),
                                        ("强制：重新检查所有逐字时间", "force"),
                                    ],
                                    value="auto",
                                )
                                make_offset = gr.Number(
                                    label="定位后的手动微调（秒）",
                                    value=0.0,
                                    precision=2,
                                )
                            make_output_root = gr.Textbox(
                                label="输出目录",
                                value=str(_default_output_root()),
                                placeholder=r"例如 D:\KaraokeForgeOutputs",
                                info="视频较大时请选择剩余空间充足的磁盘，建议至少预留 2 GB。",
                            )

                    gr.Markdown(
                        "> 推荐先生成校准工程：这里上传的音频、MV、歌词和网易云链接会"
                        "直接带入编辑器，不需要重复上传。确认歌词后再生成最终视频。"
                    )
                    with gr.Row():
                        make_prepare_button = gr.Button(
                            "① 生成可校准 KTV 工程",
                            variant="primary",
                            elem_classes="kf-primary",
                        )
                        make_button = gr.Button(
                            "② 生成最终卡拉 OK MV",
                            variant="secondary",
                        )

                with gr.Column(scale=5, min_width=320):
                    with gr.Group(elem_classes="kf-card"):
                        gr.HTML('<div class="kf-section-label">Step 03 · 成品</div>')
                        make_status = gr.Markdown(
                            "### 等待开始\n选择素材后点击生成，这里会显示结果。",
                            elem_classes="kf-status",
                        )
                        make_preview = gr.Video(label="成品预览")
                        make_downloads = gr.File(
                            label="下载视频和歌词文件",
                            file_count="multiple",
                        )
                        make_log = gr.Textbox(
                            label="处理记录",
                            lines=8,
                            interactive=False,
                        )
                        open_make_dir = gr.Button("在电脑中打开输出文件夹")
                        open_make_message = gr.Markdown()

            with gr.Tab("只生成时间轴歌词", id="align"), gr.Row(equal_height=False):
                with gr.Column(scale=7), gr.Group(elem_classes="kf-card"):
                    gr.HTML('<div class="kf-section-label">Lyrics Lab</div>')
                    gr.Markdown("## 从歌曲得到时间轴歌词")
                    with gr.Row():
                        align_audio = gr.File(
                            label="歌曲音频",
                            file_types=["audio"],
                            type="filepath",
                        )
                        align_lyrics = gr.File(
                            label="原始歌词",
                            file_types=[".txt", ".lrc", ".srt", ".vtt", ".ass", ".json"],
                            type="filepath",
                        )
                    align_pasted = gr.Textbox(
                        label="或者直接粘贴歌词",
                        lines=9,
                        placeholder="一行一句；上传文件和粘贴内容二选一即可",
                    )
                    with gr.Row():
                        align_name = gr.Textbox(label="输出名称", value="歌词时间轴")
                        align_language = gr.Dropdown(
                            label="语言",
                            choices=[
                                ("自动识别", "自动识别"),
                                ("中文", "zh"),
                                ("英语", "en"),
                                ("日语", "ja"),
                                ("韩语", "ko"),
                                ("粤语", "yue"),
                            ],
                            value="自动识别",
                        )
                        align_model = gr.Dropdown(
                            label="识别模型",
                            choices=["tiny", "base", "small", "medium", "large-v3"],
                            value="small",
                        )
                    with gr.Accordion("高级设置", open=False):
                        align_device = gr.Radio(
                            label="运行设备",
                            choices=[
                                ("自动选择", "auto"),
                                ("只用 CPU", "cpu"),
                                ("NVIDIA 显卡", "cuda"),
                            ],
                            value="auto",
                        )
                        align_separate = gr.Checkbox(
                            label="先分离人声",
                            value=False,
                        )
                        align_timing_refinement = gr.Dropdown(
                            label="已有时间轴时的逐字精修",
                            choices=[
                                ("关闭：完全保留输入时间", "off"),
                                ("自动：只精修行级/合成时间", "auto"),
                                ("强制：重新检查所有逐字时间", "force"),
                            ],
                            value="auto",
                        )
                    align_button = gr.Button(
                        "生成全部歌词格式",
                        variant="primary",
                        elem_classes="kf-primary",
                    )
                with gr.Column(scale=5), gr.Group(elem_classes="kf-card"):
                    align_status = gr.Markdown("### 等待开始")
                    align_downloads = gr.File(
                        label="下载时间轴歌词",
                        file_count="multiple",
                    )
                    align_log = gr.Textbox(
                        label="处理记录",
                        lines=10,
                        interactive=False,
                    )
                    open_align_dir = gr.Button("在电脑中打开输出文件夹")
                    open_align_message = gr.Markdown()

            with gr.Tab("网易云链接生成歌词", id="netease"), gr.Row(equal_height=False):
                with gr.Column(scale=7), gr.Group(elem_classes="kf-card"):
                    gr.HTML('<div class="kf-section-label">NetEase Link</div>')
                    gr.Markdown("## 从网易云单曲链接生成时间轴歌词")
                    netease_link = gr.Textbox(
                        label="网易云单曲链接",
                        placeholder="可直接粘贴整段分享文字或 https://music.163.com/song?id=...",
                    )
                    with gr.Row():
                        netease_local_audio = gr.File(
                            label="本地音频（会员歌曲建议上传）",
                            file_types=["audio"],
                            type="filepath",
                        )
                        netease_lyrics = gr.File(
                            label="自己的歌词（可选）",
                            file_types=[".txt", ".lrc", ".srt", ".vtt", ".ass", ".json"],
                            type="filepath",
                        )
                    netease_pasted = gr.Textbox(
                        label="或者粘贴自己的歌词",
                        lines=7,
                        placeholder="自己的歌词优先；留空则可使用网易云页面公开歌词",
                    )
                    with gr.Row():
                        netease_name = gr.Textbox(label="输出名称", value="网易云歌词时间轴")
                        netease_language = gr.Dropdown(
                            label="语言",
                            choices=[
                                ("自动识别", "自动识别"),
                                ("中文", "zh"),
                                ("英语", "en"),
                                ("日语", "ja"),
                                ("韩语", "ko"),
                            ],
                            value="自动识别",
                        )
                        netease_model = gr.Dropdown(
                            label="识别模型",
                            choices=["tiny", "base", "small", "medium", "large-v3"],
                            value="small",
                        )
                    netease_use_page_lyrics = gr.Checkbox(
                        label="没有提供自己的歌词时，使用网易云页面公开 LRC",
                        value=True,
                    )
                    with gr.Row():
                        netease_cookie_browser = gr.Dropdown(
                            label="账号权限",
                            choices=[
                                ("匿名（仅公开音频）", ""),
                                ("Chrome 已登录账号", "chrome"),
                                ("Edge 已登录账号", "edge"),
                                ("Firefox 已登录账号", "firefox"),
                                ("Brave 已登录账号", "brave"),
                            ],
                            value="",
                        )
                        netease_cookie_profile = gr.Textbox(
                            label="浏览器配置（可选）",
                            placeholder="留空使用默认配置，如 Profile 1",
                        )
                    netease_rights = gr.Checkbox(
                        label="我确认账号和歌曲归我合法使用，且不会绕过地区、版权或 DRM 限制",
                        value=False,
                    )
                    with gr.Accordion("高级设置", open=False):
                        netease_device = gr.Radio(
                            label="运行设备",
                            choices=[
                                ("自动选择", "auto"),
                                ("只用 CPU", "cpu"),
                                ("NVIDIA 显卡", "cuda"),
                            ],
                            value="auto",
                        )
                        netease_separate = gr.Checkbox(label="先分离人声", value=False)
                        netease_keep_audio = gr.Checkbox(
                            label="保留本次获取的音频文件",
                            value=False,
                        )
                        netease_timing_refinement = gr.Dropdown(
                            label="逐字时间精修",
                            choices=[
                                ("关闭：完全保留网易云时间", "off"),
                                ("自动：保留 YRC，精修普通 LRC", "auto"),
                                ("强制：重新检查全部逐字时间", "force"),
                            ],
                            value="auto",
                        )
                    gr.Markdown(
                        "> 选择浏览器后，会读取其中现有的网易云登录会话，自动检测本曲"
                        " VIP/SVIP 权限并获取账号有权播放的最高音质。Cookie 只在本机内存"
                        "中使用，不会保存；不接收密码、不提升账号权限，也不会转换 NCM。"
                    )
                    netease_button = gr.Button(
                        "读取链接并生成时间轴",
                        variant="primary",
                        elem_classes="kf-primary",
                    )
                with gr.Column(scale=5), gr.Group(elem_classes="kf-card"):
                    netease_status = gr.Markdown("### 等待网易云单曲链接")
                    netease_downloads = gr.File(
                        label="下载时间轴歌词",
                        file_count="multiple",
                    )
                    netease_log = gr.Textbox(
                        label="处理记录",
                        lines=11,
                        interactive=False,
                    )
                    open_netease_dir = gr.Button("在电脑中打开输出文件夹")
                    open_netease_message = gr.Markdown()

            with gr.Tab("歌词与注音编辑", id="editor"):
                with gr.Column(elem_id="editor-workspace"):
                    with gr.Row(elem_id="editor-topbar"):
                        gr.Button(
                            "☰ 歌词总览",
                            elem_id="editor-overview-toggle",
                        )
                        gr.Markdown(
                            "`Ctrl + 滚轮`：缩放鼠标所在的歌词或时间轴",
                            elem_id="editor-zoom-help",
                        )
                        editor_status = gr.Markdown(
                            "### 等待载入歌词项目",
                            elem_id="editor-status",
                        )
                        editor_exit_workspace = gr.Button(
                            "← 返回制作页",
                            elem_id="editor-exit-workspace",
                        )
                    with gr.Row(equal_height=True, elem_id="editor-main-grid"):
                        with (
                            gr.Column(
                                scale=1,
                                min_width=0,
                                elem_id="editor-overview-panel",
                            ),
                            gr.Group(elem_classes="kf-card"),
                        ):
                            with gr.Row():
                                gr.Markdown("## 歌词总览")
                                gr.Button(
                                    "收起总览",
                                    elem_id="editor-overview-close",
                                )
                            gr.Markdown(
                                "点击序号会选择、试听并收起窗口；点击其他单元格可继续编辑。"
                                "右键任意歌词行可隐藏、插入或删除；也可使用右侧当前句操作。"
                            )
                            with gr.Accordion("载入或更换工程", open=False):
                                with gr.Row():
                                    editor_audio = gr.Audio(
                                        label="校准用歌曲音频",
                                        sources=["upload"],
                                        type="filepath",
                                    )
                                    editor_source = gr.File(
                                        label="带时间轴歌词或项目 JSON",
                                        file_types=[
                                            ".lrc",
                                            ".yrc",
                                            ".srt",
                                            ".vtt",
                                            ".ass",
                                            ".json",
                                        ],
                                        type="filepath",
                                    )
                                editor_load = gr.Button("载入编辑器", variant="primary")
                            editor_lines = gr.Dataframe(
                                headers=["序号", "状态", "开始秒", "结束秒", "原文", "翻译"],
                                datatype=["number", "str", "number", "number", "str", "str"],
                                value=[],
                                row_count=(1, "dynamic"),
                                column_count=(6, "fixed"),
                                interactive=True,
                                wrap=True,
                                label="歌词总览（点序号试听并收起；其他列可编辑）",
                                elem_id="editor-lines",
                            )
                            with gr.Row():
                                editor_name = gr.Textbox(
                                    label="导出名称",
                                    value="编辑后的歌词",
                                )
                                editor_export = gr.Button(
                                    "保存并导出全部格式",
                                    variant="primary",
                                    elem_classes="kf-primary",
                                )
                                editor_handoff = gr.Button(
                                    "确认校准，交给 MV 制作",
                                    variant="primary",
                                    elem_classes="kf-primary",
                                )
                            editor_downloads = gr.File(
                                label="下载编辑后的歌词",
                                file_count="multiple",
                            )
                            open_editor_dir = gr.Button("在电脑中打开输出文件夹")
                            open_editor_message = gr.Markdown()
                        with (
                            gr.Column(
                                scale=8,
                                min_width=560,
                                elem_id="editor-timing-panel",
                            ),
                            gr.Group(
                                elem_classes="kf-card",
                                elem_id="editor-timing-card",
                            ),
                        ):
                            editor_preview = gr.HTML(
                                '<div class="kf-tip">载入项目后，这里会实时预览当前行。</div>',
                                elem_classes="kf-sticky-preview",
                                elem_id="editor-preview",
                            )
                            with gr.Column(elem_id="editor-audio-panel"):
                                editor_line_audio = gr.Audio(
                                    label="当前句试听（前后各保留 1 秒）",
                                    interactive=False,
                                    autoplay=True,
                                    elem_id="editor-line-audio",
                                )
                                editor_timing_status = gr.Markdown(
                                    "从歌词总览选择一句即可自动播放。",
                                    elem_id="editor-timing-status",
                                )
                            editor_token_timeline = gr.HTML(
                                '<div class="kf-tip">选择歌词后，这里会显示可拖动的逐词时间条。</div>',
                                elem_id="editor-token-timeline",
                            )
                            editor_token_json = gr.Textbox(
                                value="[]",
                                show_label=False,
                                container=False,
                                elem_id="kf-token-json",
                            )
                            with gr.Row(elem_id="editor-timing-actions"):
                                editor_save_tokens = gr.Button(
                                    "保存逐词时间",
                                    variant="primary",
                                    elem_classes="kf-primary",
                                )
                                editor_start_earlier = gr.Button("开始 −0.1s")
                                editor_start_later = gr.Button("开始 +0.1s")
                                editor_end_earlier = gr.Button("结束 −0.1s")
                                editor_end_later = gr.Button("结束 +0.1s")

                        with (
                            gr.Column(
                                scale=4,
                                min_width=340,
                                elem_id="editor-side-panel",
                            ),
                            gr.Group(
                                elem_classes="kf-card",
                                elem_id="editor-side-card",
                            ),
                        ):
                            with gr.Column(elem_id="editor-line-controls"):
                                gr.Markdown("### 当前句操作")
                                with gr.Row():
                                    editor_line_number = gr.Number(
                                        label="行号",
                                        value=1,
                                        precision=0,
                                        minimum=1,
                                    )
                                    editor_load_line = gr.Button("载入行")
                                    editor_listen_line = gr.Button("试听")
                            with gr.Row():
                                editor_toggle_line_hidden = gr.Button("👁 隐藏 / 显示")
                                editor_delete_line = gr.Button("🗑 删除")
                                editor_undo_line_action = gr.Button("↶ 撤销 / 重做")
                            with gr.Row():
                                editor_previous_line = gr.Button(
                                    "← 上一句",
                                    elem_id="editor-previous-line",
                                )
                                editor_next_line = gr.Button(
                                    "下一句 →",
                                    elem_id="editor-next-line",
                                )
                            with gr.Row():
                                gr.Checkbox(
                                    label="循环当前句",
                                    value=False,
                                    interactive=True,
                                    elem_id="editor-loop-line",
                                )
                                gr.Slider(
                                    minimum=0.5,
                                    maximum=2.0,
                                    value=1.0,
                                    step=0.25,
                                    label="播放倍速",
                                    interactive=True,
                                    elem_id="editor-playback-rate",
                                )
                            editor_line_context_action = gr.Textbox(
                                value="",
                                show_label=False,
                                container=False,
                                elem_id="kf-line-context-action",
                            )
                            editor_apply_context_action = gr.Button(
                                "应用歌词行菜单",
                                elem_id="kf-line-context-apply",
                            )
                            with gr.Column(elem_id="editor-pronunciation-panel"):
                                editor_whole_pronunciation = gr.Textbox(
                                    label="整行注音（可选）",
                                    placeholder="逐词表格为空时可用整行读音",
                                )
                                editor_pronunciation_units = gr.Dataframe(
                                    headers=["原文片段", "读音", "起始字符", "结束字符"],
                                    datatype=["str", "str", "number", "number"],
                                    value=[],
                                    row_count=(1, "dynamic"),
                                    column_count=(4, "fixed"),
                                    interactive=True,
                                    wrap=True,
                                    label="逐词注音（修改“读音”列）",
                                    elem_id="editor-pronunciation-units",
                                )
                                editor_save_pronunciation = gr.Button(
                                    "保存本行注音",
                                    variant="primary",
                                )

            with gr.Tab("歌词格式转换", id="convert"), gr.Row():
                with gr.Column(scale=6), gr.Group(elem_classes="kf-card"):
                    gr.HTML('<div class="kf-section-label">Format Desk</div>')
                    gr.Markdown("## 在常见歌词格式之间转换")
                    convert_source = gr.File(
                        label="上传带时间轴歌词",
                        file_types=[".lrc", ".srt", ".vtt", ".ass", ".json"],
                        type="filepath",
                    )
                    convert_format = gr.Dropdown(
                        label="转换成",
                        choices=[
                            ("普通 LRC", "lrc"),
                            ("增强 LRC（逐词）", "elrc"),
                            ("SRT", "srt"),
                            ("WebVTT", "vtt"),
                            ("ASS 卡拉 OK 字幕", "ass"),
                            ("Karaoke Forge JSON", "json"),
                        ],
                        value="srt",
                    )
                    convert_button = gr.Button(
                        "开始转换",
                        variant="primary",
                        elem_classes="kf-primary",
                    )
                with gr.Column(scale=6), gr.Group(elem_classes="kf-card"):
                    convert_status = gr.Markdown("### 等待文件")
                    convert_download = gr.File(
                        label="下载转换结果",
                        file_count="multiple",
                    )
                    convert_log = gr.Textbox(
                        label="转换记录",
                        lines=6,
                        interactive=False,
                    )

            with gr.Tab("环境检查与帮助", id="doctor"), gr.Row():
                with gr.Column(scale=7), gr.Group(elem_classes="kf-card"):
                    environment = gr.Markdown(environment_markdown())
                    refresh_environment = gr.Button("重新检查")
                with gr.Column(scale=5), gr.Group(elem_classes="kf-card"):
                    gr.Markdown(
                        """
                                ### 第一次使用

                                1. Windows 用户先双击项目根目录的 `首次安装.bat`。
                                2. 安装完成后双击 `启动网页版.bat`。
                                3. 首次自动对齐会下载 Whisper 模型，等待时间取决于网络。

                                ### 常见情况

                                - **只有格式转换需求**：不需要 Whisper。
                                - **歌词已有时间轴**：制作 MV 时不会运行 Whisper。
                                - **网易云会员歌曲**：可选择已登录浏览器来使用账号现有权限；
                                  也可上传官方允许导出的标准音频；不支持 NCM。
                                - **匹配率低**：确认歌词与歌曲是同一版本，或尝试分离人声。
                                - **字幕没有中文字体**：在样式里换成本机已安装字体。
                                """
                    )

        gr.HTML(
            '<div class="kf-footer">Karaoke Forge · 本地处理 · '
            "请确保你拥有歌曲、歌词和视频的使用权</div>"
        )
        app.load(fn=None, js=TOKEN_TIMELINE_JS, queue=False)

        def make_wrapper(
            audio: object,
            video: object,
            lyrics: object,
            pasted: str,
            name: str,
            language: str,
            model: str,
            device: str,
            separate: bool,
            quality: str,
            offset: float,
            font: str,
            font_size: int,
            text_color: str,
            highlight_color: str,
            margin: int,
            netease_link: str,
            use_netease_lyrics: bool,
            rights_confirmed: bool,
            cookie_browser: str,
            cookie_browser_profile: str,
            auto_sync: bool,
            timing_refinement: str,
            show_translation: bool,
            translation_font_size: float,
            translation_color: str,
            show_pronunciation: bool,
            pronunciation_font_size: float,
            pronunciation_color: str,
            output_root: str,
            progress: object = gr.Progress(),
        ) -> tuple[str, str | None, list[str], str, str | None]:
            def update(message: str) -> None:
                progress(0.5, desc=message)

            result = run_make_job(
                audio,
                video,
                lyrics,
                pasted,
                name,
                language,
                model,
                device,
                separate,
                quality,
                offset,
                font,
                font_size,
                text_color,
                highlight_color,
                margin,
                netease_link,
                use_netease_lyrics,
                rights_confirmed,
                cookie_browser,
                cookie_browser_profile,
                auto_sync,
                timing_refinement,
                show_translation,
                translation_font_size,
                translation_color,
                show_pronunciation,
                pronunciation_font_size,
                pronunciation_color,
                output_root,
                progress_callback=update,
            )
            progress(1.0, desc="完成" if result.video else "未完成")
            return (
                result.status,
                result.video,
                result.files,
                result.log,
                result.output_dir,
            )

        def prepare_make_editor_wrapper(
            audio: object,
            video: object,
            lyrics: object,
            pasted: str,
            name: str,
            language: str,
            model: str,
            device: str,
            separate: bool,
            netease_link: str,
            use_netease_lyrics: bool,
            rights_confirmed: bool,
            timing_refinement: str,
            output_root: str,
            progress: object = gr.Progress(),
        ) -> tuple[object, ...]:
            def update(message: str) -> None:
                progress(0.5, desc=message)

            result = prepare_make_editor_job(
                audio,
                video,
                lyrics,
                pasted,
                name,
                language,
                model,
                device,
                separate,
                netease_link,
                use_netease_lyrics,
                rights_confirmed,
                timing_refinement,
                output_root,
                progress_callback=update,
            )
            progress(1.0, desc="校准工程已就绪" if result.project else "未完成")
            if result.project:
                token_timeline, token_json = editor_token_workspace(
                    result.payload,
                    result.rows,
                    result.line_number,
                )
            else:
                token_timeline, token_json = result.preview, "[]"
            return (
                result.payload,
                result.rows,
                result.status,
                result.line_number,
                result.whole_pronunciation,
                result.pronunciation_rows,
                result.preview,
                token_timeline,
                token_json,
                {},
                result.project,
                result.audio,
                result.files,
                result.output_dir,
                result.status,
                result.log,
                gr.update(selected="editor" if result.project else "make"),
            )

        make_prepare_button.click(
            prepare_make_editor_wrapper,
            inputs=[
                make_audio,
                make_video,
                make_lyrics,
                make_pasted,
                make_name,
                make_language,
                make_model,
                make_device,
                make_separate,
                make_netease_link,
                make_use_netease_lyrics,
                make_rights,
                make_timing_refinement,
                make_output_root,
            ],
            outputs=[
                editor_payload,
                editor_lines,
                editor_status,
                editor_line_number,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_preview,
                editor_token_timeline,
                editor_token_json,
                editor_line_undo_payload,
                editor_source,
                editor_audio,
                editor_downloads,
                editor_output_directory,
                make_status,
                make_log,
                main_tabs,
            ],
            show_progress="full",
        )

        make_button.click(
            make_wrapper,
            inputs=[
                make_audio,
                make_video,
                make_lyrics,
                make_pasted,
                make_name,
                make_language,
                make_model,
                make_device,
                make_separate,
                make_quality,
                make_offset,
                make_font,
                make_font_size,
                make_text_color,
                make_highlight_color,
                make_margin,
                make_netease_link,
                make_use_netease_lyrics,
                make_rights,
                make_cookie_browser,
                make_cookie_profile,
                make_auto_sync,
                make_timing_refinement,
                make_show_translation,
                make_translation_size,
                make_translation_color,
                make_show_pronunciation,
                make_pronunciation_size,
                make_pronunciation_color,
                make_output_root,
            ],
            outputs=[
                make_status,
                make_preview,
                make_downloads,
                make_log,
                make_output_directory,
            ],
            show_progress="full",
        )

        preview_inputs = [
            make_font,
            make_font_size,
            make_text_color,
            make_highlight_color,
            make_margin,
            make_show_translation,
            make_translation_size,
            make_translation_color,
            make_show_pronunciation,
            make_pronunciation_size,
            make_pronunciation_color,
            make_preview_text,
            make_preview_translation,
        ]
        for preview_input in preview_inputs:
            preview_input.change(
                subtitle_preview_html,
                inputs=preview_inputs,
                outputs=make_style_preview,
                queue=False,
            )

        def align_wrapper(
            audio: object,
            lyrics: object,
            pasted: str,
            name: str,
            language: str,
            model: str,
            device: str,
            separate: bool,
            timing_refinement: str,
            progress: object = gr.Progress(),
        ) -> tuple[str, list[str], str, str | None]:
            def update(message: str) -> None:
                progress(0.5, desc=message)

            result = run_align_job(
                audio,
                lyrics,
                pasted,
                name,
                language,
                model,
                device,
                separate,
                timing_refinement,
                progress_callback=update,
            )
            progress(1.0, desc="完成" if result.files else "未完成")
            return result.status, result.files, result.log, result.output_dir

        align_button.click(
            align_wrapper,
            inputs=[
                align_audio,
                align_lyrics,
                align_pasted,
                align_name,
                align_language,
                align_model,
                align_device,
                align_separate,
                align_timing_refinement,
            ],
            outputs=[align_status, align_downloads, align_log, align_output_directory],
            show_progress="full",
        )

        def netease_wrapper(
            link: str,
            local_audio: object,
            lyrics: object,
            pasted: str,
            name: str,
            language: str,
            model: str,
            device: str,
            separate: bool,
            use_page_lyrics: bool,
            keep_audio: bool,
            rights_confirmed: bool,
            cookie_browser: str,
            cookie_browser_profile: str,
            timing_refinement: str,
            progress: object = gr.Progress(),
        ) -> tuple[str, list[str], str, str | None]:
            def update(message: str) -> None:
                progress(0.5, desc=message)

            result = run_netease_align_job(
                link,
                local_audio,
                lyrics,
                pasted,
                name,
                language,
                model,
                device,
                separate,
                use_page_lyrics,
                keep_audio,
                rights_confirmed,
                cookie_browser,
                cookie_browser_profile,
                timing_refinement,
                progress_callback=update,
            )
            progress(1.0, desc="完成" if result.files else "未完成")
            return result.status, result.files, result.log, result.output_dir

        netease_button.click(
            netease_wrapper,
            inputs=[
                netease_link,
                netease_local_audio,
                netease_lyrics,
                netease_pasted,
                netease_name,
                netease_language,
                netease_model,
                netease_device,
                netease_separate,
                netease_use_page_lyrics,
                netease_keep_audio,
                netease_rights,
                netease_cookie_browser,
                netease_cookie_profile,
                netease_timing_refinement,
            ],
            outputs=[
                netease_status,
                netease_downloads,
                netease_log,
                netease_output_directory,
            ],
            show_progress="full",
        )

        def load_editor_project_workspace(
            source: object,
        ) -> tuple[object, ...]:
            result = load_editor_project(source)
            token_timeline, token_json = editor_token_workspace(
                result[0],
                result[1],
                result[3],
            )
            return (*result, token_timeline, token_json, {})

        def load_editor_line_workspace(
            payload: dict[str, Any],
            table: object,
            line_number: int,
        ) -> tuple[object, ...]:
            result = load_editor_line(payload, table, line_number)
            token_timeline, token_json = editor_token_workspace(
                result[0],
                result[1],
                int(line_number),
            )
            return (*result, token_timeline, token_json)

        def select_editor_row(
            audio: object,
            payload: dict[str, Any],
            table: object,
            event: gr.SelectData,
        ) -> tuple[object, ...]:
            skipped = tuple(gr.skip() for _ in range(10))
            if not getattr(event, "selected", True) or event.index is None:
                return skipped
            selected = (
                event.index[0]
                if isinstance(event.index, (tuple, list)) and event.index
                else event.index
            )
            try:
                selected_row = int(selected)
            except (TypeError, ValueError):
                return skipped
            if selected_row < 0:
                return skipped
            line_number = selected_row + 1
            try:
                loaded = load_editor_line(payload, table, line_number)
                token_timeline, token_json = editor_token_workspace(
                    loaded[0],
                    loaded[1],
                    line_number,
                )
            except (TypeError, ValueError, IndexError):
                return skipped
            try:
                clip, timing_status = preview_editor_audio_line(
                    audio,
                    loaded[0],
                    loaded[1],
                    line_number,
                )
            except (ValueError, RuntimeError) as exc:
                clip = None
                timing_status = f"已选择第 {line_number} 行；暂时无法自动播放：{exc}"
            return (
                loaded[0],
                loaded[1],
                line_number,
                loaded[2],
                loaded[3],
                loaded[4],
                token_timeline,
                token_json,
                clip,
                timing_status,
            )

        select_editor_row.__annotations__["event"] = gr.SelectData

        def step_editor_line_workspace(
            audio: object,
            payload: dict[str, Any],
            table: object,
            line_number: int,
            delta: int,
        ) -> tuple[object, ...]:
            document = apply_editor_rows(document_from_payload(payload), table)
            target = min(
                max(1, int(line_number) + int(delta)),
                len(document.lines),
            )
            loaded = load_editor_line(
                document.to_dict(),
                document_to_editor_rows(document),
                target,
            )
            token_timeline, token_json = editor_token_workspace(
                loaded[0],
                loaded[1],
                target,
            )
            try:
                clip, timing_status = preview_editor_audio_line(
                    audio,
                    loaded[0],
                    loaded[1],
                    target,
                )
            except (ValueError, RuntimeError) as exc:
                clip = None
                timing_status = f"已选择第 {target} 行；暂时无法自动播放：{exc}"
            return (
                loaded[0],
                loaded[1],
                target,
                loaded[2],
                loaded[3],
                loaded[4],
                token_timeline,
                token_json,
                clip,
                timing_status,
            )

        editor_exit_workspace.click(
            lambda: gr.update(selected="make"),
            outputs=main_tabs,
            queue=False,
        )
        editor_load.click(
            load_editor_project_workspace,
            inputs=editor_source,
            outputs=[
                editor_payload,
                editor_lines,
                editor_status,
                editor_line_number,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_preview,
                editor_token_timeline,
                editor_token_json,
                editor_line_undo_payload,
            ],
            queue=False,
        )
        editor_load_line.click(
            load_editor_line_workspace,
            inputs=[editor_payload, editor_lines, editor_line_number],
            outputs=[
                editor_payload,
                editor_lines,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_preview,
                editor_token_timeline,
                editor_token_json,
            ],
            queue=False,
        )
        editor_lines.select(
            select_editor_row,
            inputs=[editor_audio, editor_payload, editor_lines],
            outputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_preview,
                editor_token_timeline,
                editor_token_json,
                editor_line_audio,
                editor_timing_status,
            ],
            queue=False,
        )
        editor_line_workspace_outputs = [
            editor_payload,
            editor_lines,
            editor_line_number,
            editor_whole_pronunciation,
            editor_pronunciation_units,
            editor_preview,
            editor_token_timeline,
            editor_token_json,
            editor_line_audio,
            editor_timing_status,
        ]
        editor_previous_line.click(
            lambda audio, payload, table, line_number: step_editor_line_workspace(
                audio,
                payload,
                table,
                line_number,
                -1,
            ),
            inputs=[editor_audio, editor_payload, editor_lines, editor_line_number],
            outputs=editor_line_workspace_outputs,
            queue=False,
        )
        editor_next_line.click(
            lambda audio, payload, table, line_number: step_editor_line_workspace(
                audio,
                payload,
                table,
                line_number,
                1,
            ),
            inputs=[editor_audio, editor_payload, editor_lines, editor_line_number],
            outputs=editor_line_workspace_outputs,
            queue=False,
        )
        editor_line_action_outputs = [
            editor_payload,
            editor_lines,
            editor_line_number,
            editor_whole_pronunciation,
            editor_pronunciation_units,
            editor_preview,
            editor_token_timeline,
            editor_token_json,
            editor_status,
            editor_line_undo_payload,
        ]
        editor_apply_context_action.click(
            apply_editor_line_action,
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_line_context_action,
            ],
            outputs=editor_line_action_outputs,
            queue=False,
        )
        editor_toggle_line_hidden.click(
            lambda payload, table, line_number: apply_editor_current_line_action(
                payload,
                table,
                line_number,
                "toggle-hidden",
            ),
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
            ],
            outputs=editor_line_action_outputs,
            queue=False,
        )
        editor_delete_line.click(
            lambda payload, table, line_number: apply_editor_current_line_action(
                payload,
                table,
                line_number,
                "delete",
            ),
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
            ],
            outputs=editor_line_action_outputs,
            queue=False,
        )
        editor_undo_line_action.click(
            undo_editor_line_action,
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_line_undo_payload,
            ],
            outputs=editor_line_action_outputs,
            queue=False,
        )
        editor_listen_line.click(
            preview_editor_audio_line,
            inputs=[
                editor_audio,
                editor_payload,
                editor_lines,
                editor_line_number,
            ],
            outputs=[editor_line_audio, editor_timing_status],
            queue=False,
        )

        def nudge_editor_timing_workspace(
            payload: dict[str, Any],
            table: object,
            line_number: int,
            *,
            start_delta: float = 0.0,
            end_delta: float = 0.0,
        ) -> tuple[object, ...]:
            result = nudge_editor_timing(
                payload,
                table,
                line_number,
                start_delta=start_delta,
                end_delta=end_delta,
            )
            token_timeline, token_json = editor_token_workspace(
                result[0],
                result[1],
                int(line_number),
            )
            return (*result, token_timeline, token_json)

        for timing_button, timing_function in [
            (
                editor_start_earlier,
                lambda payload, table, line_number: nudge_editor_timing_workspace(
                    payload,
                    table,
                    line_number,
                    start_delta=-0.1,
                ),
            ),
            (
                editor_start_later,
                lambda payload, table, line_number: nudge_editor_timing_workspace(
                    payload,
                    table,
                    line_number,
                    start_delta=0.1,
                ),
            ),
            (
                editor_end_earlier,
                lambda payload, table, line_number: nudge_editor_timing_workspace(
                    payload,
                    table,
                    line_number,
                    end_delta=-0.1,
                ),
            ),
            (
                editor_end_later,
                lambda payload, table, line_number: nudge_editor_timing_workspace(
                    payload,
                    table,
                    line_number,
                    end_delta=0.1,
                ),
            ),
        ]:
            timing_button.click(
                timing_function,
                inputs=[editor_payload, editor_lines, editor_line_number],
                outputs=[
                    editor_payload,
                    editor_lines,
                    editor_preview,
                    editor_timing_status,
                    editor_token_timeline,
                    editor_token_json,
                ],
                queue=False,
            )
        editor_save_tokens.click(
            save_editor_token_timing,
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_token_json,
            ],
            outputs=[
                editor_payload,
                editor_lines,
                editor_preview,
                editor_token_timeline,
                editor_token_json,
                editor_timing_status,
            ],
            queue=False,
        )
        editor_save_pronunciation.click(
            save_editor_pronunciation,
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_whole_pronunciation,
                editor_pronunciation_units,
            ],
            outputs=[
                editor_payload,
                editor_lines,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_preview,
                editor_status,
            ],
            queue=False,
        )
        editor_preview_inputs = [
            editor_payload,
            editor_lines,
            editor_line_number,
            editor_whole_pronunciation,
            editor_pronunciation_units,
        ]
        for editor_preview_input in [
            editor_lines,
            editor_line_number,
            editor_whole_pronunciation,
            editor_pronunciation_units,
        ]:
            editor_preview_input.change(
                preview_editor_changes,
                inputs=editor_preview_inputs,
                outputs=editor_preview,
                queue=False,
            )
        editor_export.click(
            export_editor_project,
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_name,
            ],
            outputs=[
                editor_payload,
                editor_lines,
                editor_status,
                editor_downloads,
                editor_output_directory,
            ],
        )

        def handoff_editor_wrapper(
            payload: dict[str, Any],
            line_table: object,
            line_number: int,
            whole_line: str,
            pronunciation_table: object,
            output_name: str,
            audio_file: object | None,
            token_timing_json: str,
        ) -> tuple[object, ...]:
            timed_document = apply_token_timing(
                document_from_payload(payload),
                line_table,
                int(line_number),
                token_timing_json,
            )
            timed_payload = timed_document.to_dict()
            timed_rows = document_to_editor_rows(timed_document)
            result = handoff_editor_to_make(
                timed_payload,
                timed_rows,
                line_number,
                whole_line,
                pronunciation_table,
                output_name,
                audio_file,
            )
            return (*result, gr.update(selected="make"))

        editor_handoff.click(
            handoff_editor_wrapper,
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_name,
                editor_audio,
                editor_token_json,
            ],
            outputs=[
                editor_payload,
                editor_lines,
                editor_status,
                editor_downloads,
                editor_output_directory,
                make_lyrics,
                make_audio,
                main_tabs,
            ],
        )

        def convert_wrapper(
            source: object,
            output_format: str,
        ) -> tuple[str, list[str], str]:
            result = run_convert_job(source, output_format)
            return result.status, result.files, result.log

        convert_button.click(
            convert_wrapper,
            inputs=[convert_source, convert_format],
            outputs=[convert_status, convert_download, convert_log],
        )
        refresh_environment.click(
            environment_markdown,
            outputs=environment,
            queue=False,
        )
        open_make_dir.click(
            _open_output_directory,
            inputs=make_output_directory,
            outputs=open_make_message,
            queue=False,
        )
        open_align_dir.click(
            _open_output_directory,
            inputs=align_output_directory,
            outputs=open_align_message,
            queue=False,
        )
        open_netease_dir.click(
            _open_output_directory,
            inputs=netease_output_directory,
            outputs=open_netease_message,
            queue=False,
        )
        open_editor_dir.click(
            _open_output_directory,
            inputs=editor_output_directory,
            outputs=open_editor_message,
            queue=False,
        )

    return app


def launch_web_app(
    *,
    host: str = "127.0.0.1",
    port: int = 7860,
    open_browser: bool = True,
) -> None:
    import gradio as gr

    output_root = _default_output_root()
    configured_cache = os.environ.get("KARAOKE_FORGE_CACHE_DIR")
    cache_root = (
        Path(configured_cache).expanduser()
        if configured_cache
        else output_root.parent / "KaraokeForgeCache"
    ).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("GRADIO_TEMP_DIR", str(cache_root))

    app = create_web_app()
    theme = gr.themes.Base(
        primary_hue="orange",
        secondary_hue="teal",
        neutral_hue="slate",
        radius_size="lg",
    )
    app.queue(default_concurrency_limit=1).launch(
        server_name=host,
        server_port=port,
        inbrowser=open_browser,
        share=False,
        show_error=True,
        theme=theme,
        css=WEB_CSS,
        allowed_paths=[str(output_root)],
    )
