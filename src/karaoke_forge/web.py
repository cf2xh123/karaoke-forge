from __future__ import annotations

import base64
import hashlib
import html
import importlib.util
import ipaddress
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import traceback
import wave
from array import array
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import __version__
from .artwork import ArtworkError, download_public_cover
from .ass import AssStyle
from .editor import (
    LINE_STATUS_DELETED,
    _table_rows,
    apply_editor_rows,
    apply_pronunciation_rows,
    apply_token_timing,
    document_from_payload,
    document_pronunciation_to_editor_rows,
    document_to_editor_rows,
    editor_global_timeline_html,
    editor_preview_html,
    editor_token_timeline_html,
    nudge_editor_line_timing,
    ripple_following_line_timing,
    shift_editor_timeline,
    token_timing_to_json,
)
from .formats import (
    attach_reference_translation,
    export_formats,
    parse_lrc,
    parse_plain,
    parse_yrc,
    read_lyrics,
    write_format,
)
from .media import (
    create_spinning_cover_video,
    extract_video_frame,
    probe_media_duration,
    probe_media_has_audio,
)
from .models import LyricsDocument, PronunciationSpan
from .netease import (
    NeteaseAlignOptions,
    align_netease_song,
    download_netease_track,
    fetch_public_netease_info,
)
from .netease_login import (
    NeteaseLoginError,
    acquire_netease_music_u,
    capture_netease_music_u,
    clear_netease_login_profile,
    managed_netease_profile_exists,
    try_reuse_netease_music_u,
)
from .network import (
    ModelDownloadSettings,
    NetworkSettingsError,
    auto_detect_local_proxies,
    configure_model_download_settings,
    load_model_download_settings,
    model_download_status_markdown,
    test_model_download_network,
)
from .pipeline import (
    AlignOptions,
    align_audio_and_lyrics,
    normalize_timing_refinement,
    refine_audio_word_timing_with_fallback,
    resolve_align_options,
    should_refine_timing,
)
from .projects import (
    PROJECT_FILENAME,
    WorkspaceProject,
    list_workspace_projects,
    load_recent_workspace,
    load_workspace_project,
    save_workspace_project,
)
from .pronunciation import generate_pronunciation
from .qqmusic import QQMusicSongInfo, fetch_public_qqmusic_info
from .runtime import find_runtime_executable, inspect_demucs_runtime
from .utaten import (
    UtaTenLyricsInfo,
    UtaTenPronunciationReport,
    apply_utaten_pronunciation,
    fetch_public_utaten_info,
)
from .workflows import MakeOptions, make_karaoke_video

_EDITOR_CLIP_LOCKS_GUARD = threading.Lock()
_EDITOR_CLIP_LOCKS: dict[str, threading.Lock] = {}
_EDITOR_PREFETCH_GUARD = threading.Lock()
_EDITOR_PREFETCH_IN_FLIGHT: set[str] = set()
_EDITOR_PREFETCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="karaoke-forge-prefetch",
)
_WEB_ERROR_LOG_LOCK = threading.Lock()
_MATERIAL_PREVIEW_LOCK = threading.Lock()


class _NeteaseSessionBroker:
    """Track managed NetEase sessions without placing secrets in component defaults."""

    def __init__(self, music_u: str = "") -> None:
        self._lock = threading.Lock()
        self._managed_fingerprints: dict[str, int] = {}
        self._active_fingerprint = ""
        self._profile_reuse_enabled = True
        self._generation = 0
        self.enable_managed(music_u)

    @staticmethod
    def _fingerprint(music_u: str) -> str:
        value = (music_u or "").strip()
        return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""

    def _remember_fingerprint(self, music_u: str) -> None:
        value = (music_u or "").strip()
        fingerprint = self._fingerprint(value)
        if fingerprint:
            self._managed_fingerprints[fingerprint] = self._generation
            self._active_fingerprint = fingerprint

    def enable_managed(self, music_u: str) -> None:
        with self._lock:
            self._generation += 1
            self._profile_reuse_enabled = True
            self._remember_fingerprint(music_u)

    def clear_managed(self) -> None:
        with self._lock:
            self._generation += 1
            self._profile_reuse_enabled = False

    def begin_profile_reuse(self) -> int | None:
        with self._lock:
            return self._generation if self._profile_reuse_enabled else None

    def commit_profile_reuse(self, music_u: str, generation: int) -> bool:
        with self._lock:
            if not self._profile_reuse_enabled or generation != self._generation:
                return False
            self._remember_fingerprint(music_u)
            return True

    def begin_explicit_login(self, *, disable_existing: bool = False) -> int:
        with self._lock:
            self._generation += 1
            if disable_existing:
                self._profile_reuse_enabled = False
            elif self._profile_reuse_enabled and self._active_fingerprint:
                self._managed_fingerprints[self._active_fingerprint] = self._generation
            return self._generation

    def commit_explicit_login(self, music_u: str, generation: int) -> bool:
        with self._lock:
            if generation != self._generation:
                return False
            self._profile_reuse_enabled = True
            self._remember_fingerprint(music_u)
            return True

    def managed_token_allowed(self, music_u: str) -> bool:
        fingerprint = self._fingerprint(music_u)
        with self._lock:
            if not fingerprint or fingerprint not in self._managed_fingerprints:
                return True
            return bool(
                self._profile_reuse_enabled
                and self._managed_fingerprints[fingerprint] == self._generation
            )

    def recognizes_managed(self, music_u: str) -> bool:
        fingerprint = self._fingerprint(music_u)
        with self._lock:
            return bool(fingerprint and fingerprint in self._managed_fingerprints)


_ALIGNMENT_MODEL_CHOICES = [
    ("快速（small，速度优先）", "profile:fast"),
    ("均衡（large-v3-turbo，推荐）", "profile:balanced"),
    ("KTV 精准（large-v3 + 逐行强制对齐）", "profile:precise"),
    ("tiny（自定义模型）", "tiny"),
    ("base（自定义模型）", "base"),
    ("small（自定义模型）", "small"),
    ("medium（自定义模型）", "medium"),
    ("large-v3（自定义模型）", "large-v3"),
    ("large-v3-turbo（自定义模型）", "large-v3-turbo"),
]
_ALIGNMENT_MODEL_INFO = (
    "均衡档为默认选择。KTV 精准会尝试人声分离和逐行强制对齐，"
    "任一步骤不可用时都会安全回退；首次使用新模型时下载可能较久。"
)

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
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  color: #ffd27a;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: .18em;
  text-transform: uppercase;
  margin-bottom: 10px;
}

.kf-version {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 2px 9px;
  border: 1px solid rgba(255, 210, 122, .42);
  border-radius: 999px;
  background: rgba(255, 210, 122, .12);
  color: #ffe6ad;
  font-size: 11px;
  letter-spacing: .04em;
  line-height: 1;
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

.kf-resume-card {
  max-width: 1240px;
  margin: 0 auto 22px !important;
  border-color: rgba(11, 102, 113, .34) !important;
  background: linear-gradient(135deg, rgba(255,255,255,.98), rgba(220,239,240,.88)) !important;
  box-shadow: 0 18px 44px rgba(11, 102, 113, .14) !important;
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

.kf-preview-background {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
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
  cursor: grab;
  touch-action: pan-y;
}

.kf-token-scroll.is-panning {
  cursor: grabbing;
  user-select: none;
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
  box-sizing: border-box;
}

.kf-token-block:hover,
.kf-token-block:focus-visible,
.kf-token-block.is-playing {
  background: linear-gradient(180deg, #0e7490, #155e75);
  outline: 2px solid #fbbf24;
  outline-offset: -2px;
}

.kf-token-time {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kf-token-text {
  display: block;
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  padding: 1px 3px;
  border: 0;
  border-bottom: 1px solid rgba(255,255,255,.42);
  border-radius: 3px;
  outline: 0;
  background: rgba(3, 25, 39, .2);
  color: #fff;
  text-align: center;
  font-size: 14px;
  font-weight: 800;
}

.kf-token-text:focus {
  border-bottom-color: #fbbf24;
  background: rgba(3, 25, 39, .58);
}

.kf-token-text::placeholder {
  color: #fecaca;
  opacity: .9;
}

.kf-token-block.is-empty {
  border-color: #fca5a5;
  background: linear-gradient(180deg, #7f1d1d, #641b1b);
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
  width: 11px;
  pointer-events: auto;
  cursor: ew-resize;
  touch-action: none;
  background: transparent;
  transform: translateX(-5px);
}

.kf-token-playhead::before {
  content: "";
  position: absolute;
  inset: 0 auto 0 4px;
  width: 3px;
  background: #fb3b3b;
  box-shadow: 0 0 0 1px rgba(255,255,255,.65), 0 0 8px rgba(251,59,59,.75);
}

.kf-token-playhead::after {
  content: "";
  position: absolute;
  top: -1px;
  left: -1px;
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
#kf-line-context-apply,
#kf-global-line-request,
#kf-global-select-line,
#kf-global-edge-request,
#kf-global-edge-apply {
  display: none !important;
}

#kf-line-context-menu,
#kf-token-context-menu {
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

#kf-line-context-menu.is-open,
#kf-token-context-menu.is-open {
  display: grid;
  gap: 3px;
}

#kf-line-context-menu button,
#kf-token-context-menu button {
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

#kf-line-context-menu button:hover,
#kf-token-context-menu button:hover {
  background: #edf3f8;
}

#kf-line-context-menu button[data-action="delete"],
#kf-token-context-menu button[data-action="delete"] {
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

#editor-mode-bar {
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border: 1px solid rgba(15, 23, 42, .08);
  border-radius: 14px;
  background: rgba(255, 255, 255, .78);
}
#editor-global-mode-panel { min-width: 0; }
#editor-global-audio { position: sticky; top: 8px; z-index: 19; }
#editor-token-tuning-panel {
  margin-top: 12px;
  padding: 12px;
  border: 1px solid rgba(15, 23, 42, .1);
  border-radius: 16px;
  background: rgba(255, 255, 255, .62);
}
#editor-token-tuning-panel h3 { margin-top: 0; }
.kf-global-timeline {
  --kf-global-zoom: 1;
  overflow: hidden;
  border: 1px solid rgba(15, 23, 42, .12);
  border-radius: 16px;
  background: #0f172a;
  color: #e2e8f0;
}
.kf-global-toolbar {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
  padding: 10px 12px;
  background: rgba(30, 41, 59, .96);
  font-size: 13px;
}
.kf-global-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.kf-global-actions button {
  border: 1px solid rgba(226, 232, 240, .2);
  border-radius: 8px;
  background: #334155;
  color: #f8fafc;
  padding: 4px 9px;
  cursor: pointer;
}
.kf-global-scroll { overflow-x: auto; overscroll-behavior-x: contain; }
.kf-global-canvas {
  position: relative;
  width: calc(var(--kf-global-zoom) * 100%);
  transition: width .12s ease;
}
.kf-global-ruler {
  position: relative;
  height: 30px;
  border-bottom: 1px solid rgba(148, 163, 184, .25);
  background: #111827;
}
.kf-global-tick {
  position: absolute;
  bottom: 4px;
  transform: translateX(-50%);
  color: #94a3b8;
  font-size: 11px;
}
.kf-global-track {
  position: relative;
  height: 164px;
  cursor: crosshair;
  background:
    linear-gradient(to bottom, transparent 33%, rgba(148,163,184,.09) 33%, rgba(148,163,184,.09) 34%, transparent 34%, transparent 66%, rgba(148,163,184,.09) 66%, rgba(148,163,184,.09) 67%, transparent 67%),
    repeating-linear-gradient(to right, rgba(148,163,184,.06) 0 1px, transparent 1px 5%);
}
.kf-global-line-block {
  position: absolute;
  height: 42px;
  overflow: hidden;
  border: 1px solid rgba(255,255,255,.22);
  border-radius: 8px;
  background: #2563eb;
  color: white;
  cursor: pointer;
  text-align: left;
  padding: 5px 7px;
  font-size: 12px;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.kf-global-line-block.lane-0 { top: 8px; }
.kf-global-line-block.lane-1 { top: 59px; background: #7c3aed; }
.kf-global-line-block.lane-2 { top: 110px; background: #0f766e; }
.kf-global-line-block:hover,
.kf-global-line-block.is-selected {
  outline: 3px solid #facc15;
  outline-offset: 1px;
  z-index: 4;
}
.kf-global-line-block.is-playing { filter: brightness(1.3); }
.kf-global-line-edge {
  position: absolute;
  z-index: 9;
  width: 12px;
  height: 42px;
  margin: 0;
  padding: 0;
  border: 0;
  border-radius: 4px;
  background: transparent;
  cursor: ew-resize;
  touch-action: none;
}
.kf-global-line-edge.lane-0 { top: 8px; }
.kf-global-line-edge.lane-1 { top: 59px; }
.kf-global-line-edge.lane-2 { top: 110px; }
.kf-global-line-edge.is-start { transform: translateX(-100%); }
.kf-global-line-edge.is-start.is-flipped { transform: none; }
.kf-global-line-edge.is-end.is-flipped { transform: translateX(-100%); }
.kf-global-line-edge::after {
  content: "";
  position: absolute;
  top: 4px;
  bottom: 4px;
  width: 2px;
  border-radius: 999px;
  background: rgba(255, 244, 138, .92);
  box-shadow: 0 0 0 1px rgba(17,24,39,.5), 0 0 7px rgba(250,204,21,.8);
}
.kf-global-line-edge.is-start::after { right: 0; }
.kf-global-line-edge.is-end::after { left: 0; }
.kf-global-line-edge.is-start.is-flipped::after { left: 0; right: auto; }
.kf-global-line-edge.is-end.is-flipped::after { left: auto; right: 0; }
.kf-global-line-edge:hover::after,
.kf-global-line-edge:focus-visible::after,
.kf-global-line-edge.is-dragging::after {
  width: 4px;
  background: #fef08a;
  box-shadow: 0 0 0 1px #111827, 0 0 12px rgba(250,204,21,.95);
}
.kf-global-timeline[data-edge-saving="true"] .kf-global-line-edge {
  cursor: wait;
  opacity: .55;
}
.kf-editor-preview-stage.is-global-gap .kf-editor-preview-translation,
.kf-editor-preview-stage.is-global-gap .kf-editor-preview-row {
  opacity: 0;
}
.kf-global-token {
  position: absolute;
  left: 0;
  bottom: 1px;
  height: 3px;
  border-radius: 999px;
  background: rgba(255,255,255,.78);
  pointer-events: none;
}
.kf-global-playhead {
  position: absolute;
  inset: 0 auto 0 0;
  width: 3px;
  z-index: 8;
  background: #ef4444;
  box-shadow: 0 0 0 1px rgba(255,255,255,.55), 0 0 12px rgba(239,68,68,.85);
  cursor: ew-resize;
  pointer-events: auto;
}
.kf-global-playhead::before {
  content: "";
  position: absolute;
  top: -4px;
  left: 50%;
  transform: translateX(-50%);
  border: 6px solid transparent;
  border-top-color: #ef4444;
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
    const blocks = Array.from(timeline.querySelectorAll(".kf-token-block"))
      .sort((left, right) =>
        Number(left.dataset.tokenIndex) - Number(right.dataset.tokenIndex)
      );
    const payload = blocks.map((block) => {
      const tokenIndex = Number(block.dataset.tokenIndex);
      const startHandle = handles.find((handle) =>
        Number(handle.dataset.tokenIndex) === tokenIndex &&
        handle.dataset.edge === "start"
      );
      const endHandle = handles.find((handle) =>
        Number(handle.dataset.tokenIndex) === tokenIndex &&
        handle.dataset.edge === "end"
      );
      const start = Number(startHandle?.value ?? block.dataset.start);
      const end = Number(endHandle?.value ?? block.dataset.end);
      block.style.left = `${((start - clipStart) / duration) * 100}%`;
      block.style.width = `${Math.max(0.35, ((end - start) / duration) * 100)}%`;
      block.dataset.start = String(start);
      block.dataset.end = String(end);
      const textInput = block.querySelector(".kf-token-text");
      const tokenText = textInput?.value ?? block.dataset.token ?? "";
      block.dataset.token = tokenText;
      block.classList.toggle("is-empty", tokenText.length === 0);
      const time = block.querySelector(".kf-token-time");
      if (time) time.textContent = `${start.toFixed(2)}–${end.toFixed(2)}s`;
      return {
        text: tokenText,
        start: Number(start.toFixed(3)),
        end: Number(end.toFixed(3)),
      };
    }).filter((entry) => entry.text.length > 0);
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

  const elementIsVisible = (element) => Boolean(
    element && (
      element.offsetParent !== null ||
      Number(element.getClientRects?.().length || 0) > 0
    )
  );

  const visibleElement = (selector) =>
    Array.from(document.querySelectorAll(selector)).find(elementIsVisible);

  const displayedEditorLine = () => {
    const input = document.querySelector("#editor-current-line input");
    return Number(input?.value);
  };

  const workspaceLinesMatch = (timeline, preview) => {
    const timelineLine = Number(timeline?.dataset.lineNumber);
    const previewLine = Number(preview?.dataset.lineNumber);
    const currentLine = displayedEditorLine();
    const timelineStart = Number(timeline?.dataset.lineStart);
    const timelineEnd = Number(timeline?.dataset.lineEnd);
    const previewStart = Number(preview?.dataset.lineStart);
    const previewEnd = Number(preview?.dataset.lineEnd);
    return (
      Number.isInteger(timelineLine) &&
      Number.isInteger(previewLine) &&
      Number.isInteger(currentLine) &&
      [timelineStart, timelineEnd, previewStart, previewEnd].every(Number.isFinite) &&
      timelineLine === previewLine &&
      previewLine === currentLine &&
      Math.abs(timelineStart - previewStart) < 0.0005 &&
      Math.abs(timelineEnd - previewEnd) < 0.0005
    );
  };

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
    const preview = visibleElement(".kf-editor-preview-stage");
    if (!timeline || !workspaceLinesMatch(timeline, preview)) return;
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
          behavior: window.__karaokeForgeDraggingPlayhead ? "auto" : "smooth",
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
      const measure = karaoke.querySelector(".kf-live-karaoke-measure");
      const measureBounds = measure?.getBoundingClientRect();
      const totalWidth = Math.max(0.01, Number(measureBounds?.width || 0));
      let completedWidth = 0;
      let lyricProgress = absoluteTime >= lineEnd ? 100 : 0;
      for (let index = 0; index < tokenBlocks.length; index += 1) {
        const block = tokenBlocks[index];
        const start = Number(block.dataset.start);
        const end = Math.max(start + 0.01, Number(block.dataset.end));
        const core = measure?.querySelector(
          `.kf-karaoke-token-core[data-token-index="${index}"]`
        );
        const coreBounds = core?.getBoundingClientRect();
        const coreStart = Math.max(
          0,
          Number(coreBounds?.left || measureBounds?.left || 0) -
            Number(measureBounds?.left || 0)
        );
        const coreEnd = Math.max(
          coreStart,
          Number(coreBounds?.right || measureBounds?.left || 0) -
            Number(measureBounds?.left || 0)
        );
        if (absoluteTime >= end) {
          completedWidth = coreEnd;
          lyricProgress = (completedWidth / totalWidth) * 100;
          continue;
        }
        if (absoluteTime >= start) {
          const inside = (absoluteTime - start) / (end - start);
          lyricProgress = (
            (coreStart + (coreEnd - coreStart) * inside) / totalWidth
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

  const waveSurferPartsFor = (selector) => {
    const host = visibleElement(selector);
    if (!host) return null;
    const queue = [host];
    const visited = new Set();
    let progress = null;
    let wrapper = null;
    let playButton = null;
    let rateButton = null;
    const mediaCandidates = new Set();
    while (queue.length) {
      const root = queue.shift();
      if (!root || visited.has(root)) continue;
      visited.add(root);
      progress ||= root.querySelector?.('[part="progress"]');
      wrapper ||= root.querySelector?.('[part="wrapper"]');
      const controls = root.querySelector?.('[data-testid="waveform-controls"]');
      playButton ||= controls?.querySelector(".play-pause-button");
      rateButton ||= controls?.querySelector(".control-wrapper > button:last-child");
      root.querySelectorAll?.("audio").forEach((candidate) => {
        mediaCandidates.add(candidate);
      });
      root.querySelectorAll?.("*").forEach((element) => {
        if (element.shadowRoot) queue.push(element.shadowRoot);
      });
    }
    const usableMedia = (candidate) => {
      const duration = Number(candidate?.duration);
      const hasSource = Boolean(
        candidate?.currentSrc || candidate?.getAttribute?.("src")
      );
      return hasSource || (Number.isFinite(duration) && duration > 0);
    };
    // Gradio keeps an empty native <audio> beside the real WaveSurfer player.
    // Once waveform parts exist, their progress/button state is authoritative.
    const media = progress && wrapper
      ? null
      : Array.from(mediaCandidates).find(usableMedia) || null;
    return (progress && wrapper) || media
      ? { host, progress, wrapper, playButton, rateButton, media }
      : null;
  };

  const globalEditorModeActive = () => {
    const panel = document.querySelector("#editor-global-mode-panel");
    return elementIsVisible(panel) || Boolean(visibleElement(".kf-global-timeline"));
  };

  const waveSurferParts = () => waveSurferPartsFor(
    globalEditorModeActive() ? "#editor-global-audio" : "#editor-line-audio"
  );

  const waveProgressRatio = (parts) => {
    if (
      Number.isFinite(parts?.media?.duration) &&
      parts.media.duration > 0 &&
      Number.isFinite(parts.media.currentTime)
    ) {
      return Math.min(1, Math.max(0, parts.media.currentTime / parts.media.duration));
    }
    const width = Number.parseFloat(parts?.progress?.style?.width || "");
    return Number.isFinite(width) ? Math.min(1, Math.max(0, width / 100)) : null;
  };

  const globalPlaybackDuration = (timeline, parts) => {
    const candidates = [
      Number(parts?.media?.duration),
      Number(timeline?.dataset.mediaDuration),
      Number(timeline?.dataset.duration),
    ];
    return candidates.find((duration) => Number.isFinite(duration) && duration > 0) || 0.01;
  };

  const playbackSeconds = (parts, duration) => {
    const mediaDuration = Number(parts?.media?.duration);
    const mediaTime = Number(parts?.media?.currentTime);
    if (
      Number.isFinite(mediaDuration) &&
      mediaDuration > 0 &&
      Number.isFinite(mediaTime)
    ) {
      return Math.min(mediaDuration, Math.max(0, mediaTime));
    }
    const ratio = waveProgressRatio(parts);
    return ratio === null ? null : ratio * Math.max(0.01, Number(duration) || 0);
  };

  const seekWaveSurfer = (parts, ratio) => {
    if (parts?.media && Number.isFinite(parts.media.duration) && parts.media.duration > 0) {
      parts.media.currentTime = Math.min(1, Math.max(0, ratio)) * parts.media.duration;
      return true;
    }
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
    parts.wrapper.dispatchEvent(new MouseEvent("click", options));
    return true;
  };

  const seekEditorAbsoluteTime = (timeline, parts, absoluteTime) => {
    if (!timeline || !parts || !Number.isFinite(absoluteTime)) return false;
    if (globalEditorModeActive()) {
      window.__karaokeForgePendingGlobalSeek = null;
      return seekGlobalTimeline(
        visibleElement(".kf-global-timeline"),
        parts,
        absoluteTime
      );
    }
    const lineStart = Number(timeline.dataset.lineStart);
    const lineEnd = Number(timeline.dataset.lineEnd);
    if (![lineStart, lineEnd].every(Number.isFinite) || lineEnd <= lineStart) {
      return false;
    }
    return seekWaveSurfer(parts, (absoluteTime - lineStart) / (lineEnd - lineStart));
  };

  const seekFromTimelinePointer = (timeline, clientX) => {
    const track = timeline?.querySelector(".kf-token-track");
    const preview = visibleElement(".kf-editor-preview-stage");
    const parts = waveSurferParts();
    if (!track || !preview || !parts || !workspaceLinesMatch(timeline, preview)) {
      return false;
    }
    const bounds = track.getBoundingClientRect();
    const clipStart = Number(timeline.dataset.clipStart);
    const clipEnd = Number(timeline.dataset.clipEnd);
    const lineStart = Number(preview.dataset.lineStart);
    const lineEnd = Number(preview.dataset.lineEnd);
    if (
      ![clipStart, clipEnd, lineStart, lineEnd].every(Number.isFinite) ||
      bounds.width <= 0 || lineEnd <= lineStart
    ) return false;
    const trackRatio = Math.min(1, Math.max(0, (clientX - bounds.left) / bounds.width));
    const requested = clipStart + trackRatio * (clipEnd - clipStart);
    const seekableEnd = Math.max(lineStart, lineEnd - 0.01);
    const absoluteTime = Math.min(seekableEnd, Math.max(lineStart, requested));
    seekEditorAbsoluteTime(timeline, parts, absoluteTime);
    updatePlaybackAt(absoluteTime - clipStart);
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

  const applyPlaybackRate = (parts) => {
    const button = parts?.rateButton;
    if (!button) return;
    const selected = selectedPlaybackRate();
    const current = Number.parseFloat(button.textContent || "1");
    if (!Number.isFinite(current) || Math.abs(current - selected) < 0.001) return;
    const now = performance.now();
    if (now - Number(button.__kfLastRateClick || 0) < 120) return;
    button.__kfLastRateClick = now;
    button.click();
  };

  const playbackIsActive = (parts) => (
    parts?.media ? !parts.media.paused && !parts.media.ended : buttonIsPause(parts?.playButton)
  );

  const clearTokenAuditionGuard = () => {
    window.__karaokeForgeTokenAuditionGuardUntil = 0;
    window.__karaokeForgeTokenAuditionGuardLine = null;
  };

  const clearEditorMutationGuard = () => {
    window.__karaokeForgeEditorMutationGuardUntil = 0;
    window.__karaokeForgeEditorMutationGuardLine = null;
  };

  const guardEditorMutationStop = (milliseconds = 160) => {
    const timeline = visibleElement(".kf-token-editor");
    window.__karaokeForgeEditorMutationGuardUntil = performance.now() + milliseconds;
    window.__karaokeForgeEditorMutationGuardLine = timeline?.dataset.lineNumber ?? null;
  };

  const guardTokenAuditionStop = (milliseconds = 220) => {
    const timeline = visibleElement(".kf-token-editor");
    window.__karaokeForgeTokenAuditionGuardUntil = performance.now() + milliseconds;
    window.__karaokeForgeTokenAuditionGuardLine = timeline?.dataset.lineNumber ?? null;
  };

  const playPlayback = (parts, preserveAuditionGuard = false) => {
    if (!parts) return false;
    if (!preserveAuditionGuard) clearTokenAuditionGuard();
    clearEditorMutationGuard();
    applyPlaybackRate(parts);
    if (parts.media) {
      parts.media.play().catch(() => parts.playButton?.click());
    } else if (!buttonIsPause(parts.playButton)) {
      parts.playButton?.click();
    }
    return true;
  };

  const pausePlayback = (parts) => {
    if (!parts) return;
    if (parts.media && !parts.media.paused) parts.media.pause();
    else if (buttonIsPause(parts.playButton)) parts.playButton.click();
  };

  const clearTokenStopTimer = () => {
    const auditionWasActive = Boolean(window.__karaokeForgeTokenAuditionActive);
    if (window.__karaokeForgeTokenStopTimer) {
      clearTimeout(window.__karaokeForgeTokenStopTimer);
    }
    window.__karaokeForgeTokenStopTimer = null;
    window.__karaokeForgeTokenAuditionActive = false;
    window.__karaokeForgeTokenAuditionStopAt = null;
    if (auditionWasActive) guardTokenAuditionStop();
  };

  const pauseForEditorMutation = () => {
    guardEditorMutationStop();
    clearTokenStopTimer();
    pausePlayback(waveSurferParts());
  };

  const selectGlobalLine = (lineNumber) => {
    if (!setHiddenInput("#kf-global-line-request", String(lineNumber))) return;
    window.setTimeout(() => {
      const root = document.querySelector("#kf-global-select-line");
      const button = root?.matches?.("button") ? root : root?.querySelector("button");
      button?.click();
    }, 15);
  };

  const applyGlobalLineEdge = (request) => {
    if (!setHiddenInput("#kf-global-edge-request", JSON.stringify(request))) return false;
    window.setTimeout(() => {
      const root = document.querySelector("#kf-global-edge-apply");
      const button = root?.matches?.("button") ? root : root?.querySelector("button");
      button?.click();
    }, 15);
    return true;
  };

  const markGlobalLineSelected = (timeline, lineNumber) => {
    const requested = Math.trunc(Number(lineNumber));
    if (!timeline || !Number.isFinite(requested) || requested < 1) return false;
    let matched = false;
    timeline.querySelectorAll(".kf-global-line-block").forEach((block) => {
      const selected = Number(block.dataset.lineNumber) === requested;
      block.classList.toggle("is-selected", selected);
      matched ||= selected;
    });
    return matched;
  };

  const updateGlobalKaraokeAt = (activeBlock, absoluteTime) => {
    const preview = visibleElement(".kf-editor-preview-stage");
    const activeLine = Number(activeBlock?.dataset.lineNumber);
    const previewLine = Number(preview?.dataset.lineNumber);
    const matches = Boolean(
      activeBlock && preview && Number.isFinite(activeLine) && activeLine === previewLine
    );
    preview?.classList.toggle("is-global-gap", !matches);
    if (!matches) return;
    const tokenTimeline = visibleElement(".kf-token-editor");
    if (workspaceLinesMatch(tokenTimeline, preview)) {
      // The shared token editor contains the newest unsaved drag/text draft.
      // pollWaveSurfer already used it to update the fill in this frame.
      return;
    }
    const karaoke = preview.querySelector(".kf-live-karaoke-current");
    const measure = karaoke?.querySelector(".kf-live-karaoke-measure");
    const measureBounds = measure?.getBoundingClientRect();
    const totalWidth = Math.max(0.01, Number(measureBounds?.width || 0));
    const tokenBlocks = Array.from(activeBlock.querySelectorAll(".kf-global-token"))
      .sort((left, right) =>
        Number(left.dataset.tokenIndex) - Number(right.dataset.tokenIndex)
      );
    let lyricProgress = absoluteTime >= Number(activeBlock.dataset.end) ? 100 : 0;
    for (let index = 0; index < tokenBlocks.length; index += 1) {
      const block = tokenBlocks[index];
      const start = Number(block.dataset.start);
      const end = Math.max(start + 0.01, Number(block.dataset.end));
      const core = measure?.querySelector(
        `.kf-karaoke-token-core[data-token-index="${index}"]`
      );
      const coreBounds = core?.getBoundingClientRect();
      const coreStart = Math.max(
        0,
        Number(coreBounds?.left || measureBounds?.left || 0) -
          Number(measureBounds?.left || 0)
      );
      const coreEnd = Math.max(
        coreStart,
        Number(coreBounds?.right || measureBounds?.left || 0) -
          Number(measureBounds?.left || 0)
      );
      if (absoluteTime >= end) {
        lyricProgress = coreEnd / totalWidth * 100;
        continue;
      }
      if (absoluteTime >= start) {
        const inside = (absoluteTime - start) / (end - start);
        lyricProgress = (coreStart + (coreEnd - coreStart) * inside) /
          totalWidth * 100;
      }
      break;
    }
    const fill = karaoke?.querySelector(".kf-live-karaoke-fill");
    if (fill) fill.style.clipPath = `inset(0 ${100 - lyricProgress}% 0 0)`;
  };

  const pollWaveSurfer = () => {
    window.__karaokeForgeWavePollFrame = requestAnimationFrame(pollWaveSurfer);
    const globalMode = globalEditorModeActive();
    const timeline = visibleElement(".kf-token-editor");
    const parts = waveSurferPartsFor(
      globalMode ? "#editor-global-audio" : "#editor-line-audio"
    );
    const ratio = waveProgressRatio(parts);
    applyPlaybackRate(parts);
    const preview = visibleElement(".kf-editor-preview-stage");
    if (
      timeline &&
      preview &&
      workspaceLinesMatch(timeline, preview) &&
      !window.__karaokeForgeDraggingPlayhead
    ) {
      const clipStart = Number(timeline.dataset.clipStart);
      const lineStart = Number(preview.dataset.lineStart);
      const lineEnd = Number(preview.dataset.lineEnd);
      const globalTimeline = visibleElement(".kf-global-timeline");
      const globalDuration = globalPlaybackDuration(globalTimeline, parts);
      const absoluteTime = globalMode
        ? playbackSeconds(parts, globalDuration)
        : (ratio === null
          ? null
          : lineStart + ratio * Math.max(0.01, lineEnd - lineStart));
      if (Number.isFinite(absoluteTime)) {
        updatePlaybackAt(absoluteTime - clipStart);
        const auditionStopAt = Number(window.__karaokeForgeTokenAuditionStopAt);
        if (
          window.__karaokeForgeTokenAuditionActive &&
          Number.isFinite(auditionStopAt) &&
          absoluteTime >= auditionStopAt
        ) {
          pausePlayback(parts);
          clearTokenStopTimer();
        }
      }
    }
    if (globalMode) {
      const globalTimeline = visibleElement(".kf-global-timeline");
      const globalParts = parts || waveSurferPartsFor("#editor-global-audio");
      const playbackDuration = globalPlaybackDuration(globalTimeline, globalParts);
      const canvasDuration = Math.max(
        Number(globalTimeline?.dataset.duration) || 0,
        playbackDuration,
        0.01
      );
      const playbackActive = playbackIsActive(globalParts);
      const displayedLine = displayedEditorLine();
      if (
        Number.isFinite(displayedLine) && displayedLine >= 1 &&
        displayedLine !== Number(window.__karaokeForgeLastDisplayedEditorLine)
      ) {
        window.__karaokeForgeLastDisplayedEditorLine = displayedLine;
        const followsPlayback = displayedLine ===
          Number(window.__karaokeForgeGlobalFollowLine);
        if (!followsPlayback) {
          window.__karaokeForgeGlobalFollowLine = displayedLine;
          markGlobalLineSelected(globalTimeline, displayedLine);
          const requestedBlock = globalTimeline?.querySelector(
            `.kf-global-line-block[data-line-number="${displayedLine}"]`
          );
          if (requestedBlock) {
            queueGlobalSeek(
              globalTimeline,
              globalParts,
              Number(requestedBlock.dataset.start),
              playbackActive
            );
          }
          window.__karaokeForgeGlobalManualSelectionUntil = performance.now() + 320;
        } else if (!playbackActive) {
          markGlobalLineSelected(globalTimeline, displayedLine);
        }
      }
      const manualSelectionActive = Boolean(window.__karaokeForgePendingGlobalSeek) ||
        Boolean(window.__karaokeForgeDraggingGlobalLineEdge) ||
        performance.now() < Number(window.__karaokeForgeGlobalManualSelectionUntil || 0);
      let currentTime = playbackSeconds(globalParts, playbackDuration);
      const pendingSeek = window.__karaokeForgePendingGlobalSeek;
      if (pendingSeek) {
        const now = performance.now();
        const pendingTarget = Math.min(
          playbackDuration,
          Math.max(0, Number(pendingSeek.seconds) || 0)
        );
        const reachedTarget = Number.isFinite(currentTime) &&
          Math.abs(currentTime - pendingTarget) <= 0.45;
        if (reachedTarget && (!pendingSeek.play || playbackActive)) {
          window.__karaokeForgePendingGlobalSeek = null;
        } else if (now >= Number(pendingSeek.expiresAt || 0)) {
          window.__karaokeForgePendingGlobalSeek = null;
        } else if (
          globalParts && now - Number(pendingSeek.lastAttempt || 0) >= 120
        ) {
          pendingSeek.lastAttempt = now;
          seekGlobalTimeline(globalTimeline, globalParts, pendingTarget);
          if (pendingSeek.play) playPlayback(globalParts);
          currentTime = playbackSeconds(globalParts, playbackDuration);
        }
      }
      if (Number.isFinite(currentTime)) {
        const percent = Math.min(100, Math.max(0, currentTime / canvasDuration * 100));
        const globalRatio = waveProgressRatio(globalParts);
        const justFinished = Boolean(window.__karaokeForgeGlobalPlaybackWasActive) &&
          !playbackActive && globalRatio !== null && globalRatio >= 0.99999;
        window.__karaokeForgeGlobalPlaybackWasActive = playbackActive;
        const playhead = globalTimeline?.querySelector(".kf-global-playhead");
        if (
          playhead &&
          !window.__karaokeForgeDraggingGlobalPlayhead &&
          !window.__karaokeForgeDraggingGlobalLineEdge
        ) {
          playhead.style.left = `${percent}%`;
          playhead.setAttribute("aria-valuenow", currentTime.toFixed(2));
        }
        const scrollArea = globalTimeline?.querySelector(".kf-global-scroll");
        const canvas = globalTimeline?.querySelector(".kf-global-canvas");
        if (
          playbackActive &&
          !window.__karaokeForgeDraggingGlobalPlayhead &&
          !window.__karaokeForgeDraggingGlobalLineEdge &&
          scrollArea && canvas &&
          canvas.scrollWidth > scrollArea.clientWidth
        ) {
          const target = percent / 100 * canvas.scrollWidth;
          const safeLeft = scrollArea.scrollLeft + scrollArea.clientWidth * 0.15;
          const safeRight = scrollArea.scrollLeft + scrollArea.clientWidth * 0.85;
          const lastTarget = Number(globalTimeline.__kfLastFollowTarget ?? -100000);
          if (
            (target < safeLeft || target > safeRight) &&
            Math.abs(target - lastTarget) > scrollArea.clientWidth * 0.16
          ) {
            globalTimeline.__kfLastFollowTarget = target;
            scrollArea.scrollTo({
              left: Math.max(0, target - scrollArea.clientWidth / 2),
              behavior: "smooth",
            });
          }
        }
        let activeBlock = null;
        globalTimeline?.querySelectorAll(".kf-global-line-block").forEach((block) => {
          const start = Number(block.dataset.start);
          const end = Number(block.dataset.end);
          const active = currentTime >= start && currentTime < end;
          block.classList.toggle("is-playing", active);
          if (active) {
            activeBlock ||= block;
            block.setAttribute("aria-current", "true");
          } else {
            block.removeAttribute("aria-current");
          }
        });
        const loopInput = document.querySelector("#editor-loop-line input[type='checkbox']");
        const selectedBlock = globalTimeline?.querySelector(".kf-global-line-block.is-selected");
        let looped = false;
        if (
          loopInput?.checked && selectedBlock &&
          !manualSelectionActive &&
          (playbackActive || justFinished) &&
          currentTime >= Number(selectedBlock.dataset.end) - 0.04
        ) {
          looped = seekGlobalTimeline(
            globalTimeline,
            globalParts,
            Number(selectedBlock.dataset.start)
          );
          if (looped && justFinished) playPlayback(globalParts);
        }
        if (
          playbackActive && !looped && activeBlock &&
          !manualSelectionActive &&
          !window.__karaokeForgeDraggingGlobalPlayhead &&
          !window.__karaokeForgeDraggingGlobalLineEdge
        ) {
          const activeLine = Number(activeBlock.dataset.lineNumber);
          if (window.__karaokeForgeGlobalFollowLine !== activeLine) {
            window.__karaokeForgeGlobalFollowLine = activeLine;
            markGlobalLineSelected(globalTimeline, activeLine);
            selectGlobalLine(activeLine);
          }
        }
        updateGlobalKaraokeAt(looped ? selectedBlock : activeBlock, currentTime);
      }
    } else {
      window.__karaokeForgeGlobalPlaybackWasActive = false;
    }
  };
  if (window.__karaokeForgeWavePollFrame) {
    cancelAnimationFrame(window.__karaokeForgeWavePollFrame);
  }
  pollWaveSurfer();

  let observedTimelineLine = null;
  const clearAuditionAfterLineChange = () => {
    const timeline = visibleElement(".kf-token-editor");
    const line = timeline?.dataset.lineNumber || null;
    if (observedTimelineLine !== null && line !== observedTimelineLine) {
      clearTokenStopTimer();
    }
    observedTimelineLine = line;
  };
  new MutationObserver(clearAuditionAfterLineChange).observe(document.body, {
    childList: true,
    subtree: true,
  });
  clearAuditionAfterLineChange();

  document.addEventListener("pointerdown", (event) => {
    const handle = event.target.closest?.(".kf-token-boundary");
    if (!handle) return;
    pauseForEditorMutation();
    const timeline = handle.closest(".kf-token-editor");
    timeline.__kfUndoHistory ||= [];
    timeline.__kfRedoHistory = [];
    const snapshot = boundarySnapshot(timeline);
    const previous = timeline.__kfUndoHistory.at(-1);
    if (!previous || JSON.stringify(previous) !== JSON.stringify(snapshot)) {
      timeline.__kfUndoHistory.push(snapshot);
    }
  });

  document.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const playhead = event.target.closest?.(".kf-token-playhead");
    const timeline = playhead?.closest?.(".kf-token-editor");
    if (!playhead || !timeline) return;
    event.preventDefault();
    event.stopPropagation();
    clearTokenStopTimer();
    const parts = waveSurferParts();
    const resumeAfterDrag = playbackIsActive(parts);
    pausePlayback(parts);
    window.__karaokeForgeDraggingPlayhead = true;
    const pointerId = event.pointerId;
    playhead.setPointerCapture?.(pointerId);
    seekFromTimelinePointer(timeline, event.clientX);
    const move = (moveEvent) => {
      if (moveEvent.pointerId !== pointerId) return;
      moveEvent.preventDefault();
      seekFromTimelinePointer(timeline, moveEvent.clientX);
    };
    const finish = (finishEvent) => {
      if (finishEvent.pointerId !== pointerId) return;
      window.__karaokeForgeDraggingPlayhead = false;
      playhead.releasePointerCapture?.(pointerId);
      playhead.removeEventListener("pointermove", move);
      playhead.removeEventListener("pointerup", finish);
      playhead.removeEventListener("pointercancel", finish);
      if (resumeAfterDrag) requestAnimationFrame(() => playPlayback(waveSurferParts()));
    };
    playhead.addEventListener("pointermove", move);
    playhead.addEventListener("pointerup", finish);
    playhead.addEventListener("pointercancel", finish);
  });

  document.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const scrollArea = event.target.closest?.(".kf-token-scroll");
    if (
      !scrollArea ||
      scrollArea.scrollWidth <= scrollArea.clientWidth ||
      event.target.closest?.(
        ".kf-token-block, .kf-token-boundary, .kf-token-playhead, button, input, textarea, select"
      )
    ) return;
    event.preventDefault();
    const pointerId = event.pointerId;
    const startX = event.clientX;
    const startScrollLeft = scrollArea.scrollLeft;
    scrollArea.classList.add("is-panning");
    scrollArea.setPointerCapture?.(pointerId);
    const move = (moveEvent) => {
      if (moveEvent.pointerId !== pointerId) return;
      scrollArea.scrollLeft = startScrollLeft - (moveEvent.clientX - startX);
    };
    const finish = (finishEvent) => {
      if (finishEvent.pointerId !== pointerId) return;
      scrollArea.classList.remove("is-panning");
      scrollArea.releasePointerCapture?.(pointerId);
      scrollArea.removeEventListener("pointermove", move);
      scrollArea.removeEventListener("pointerup", finish);
      scrollArea.removeEventListener("pointercancel", finish);
    };
    scrollArea.addEventListener("pointermove", move);
    scrollArea.addEventListener("pointerup", finish);
    scrollArea.addEventListener("pointercancel", finish);
  });

  document.addEventListener("input", (event) => {
    const textInput = event.target.closest?.(".kf-token-text");
    if (textInput) {
      pauseForEditorMutation();
      const timeline = textInput.closest(".kf-token-editor");
      if (timeline) refreshTimeline(timeline);
      return;
    }
    const handle = event.target.closest?.(".kf-token-boundary");
    if (!handle) return;
    const timeline = handle.closest(".kf-token-editor");
    const handles = timelineHandles(timeline);
    const tokenIndex = Number(handle.dataset.tokenIndex);
    const edge = handle.dataset.edge;
    const ownOther = handles.find((candidate) =>
      Number(candidate.dataset.tokenIndex) === tokenIndex &&
      candidate.dataset.edge === (edge === "start" ? "end" : "start")
    );
    const neighbor = handles.find((candidate) =>
      Number(candidate.dataset.tokenIndex) === tokenIndex + (edge === "start" ? -1 : 1) &&
      candidate.dataset.edge === (edge === "start" ? "end" : "start")
    );
    const lower = edge === "start"
      ? (neighbor ? Number(neighbor.value) + 0.01 : Number(handle.min))
      : Number(ownOther.value) + 0.01;
    const upper = edge === "start"
      ? Number(ownOther.value) - 0.01
      : (neighbor ? Number(neighbor.value) - 0.01 : Number(handle.max));
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
      pauseForEditorMutation();
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
    if (event.target.closest?.(".kf-token-text")) return;
    const timeline = block.closest(".kf-token-editor");
    const parts = waveSurferParts();
    if (!timeline || !parts) return;
    const preview = visibleElement(".kf-editor-preview-stage");
    const lineStart = Number(preview?.dataset.lineStart);
    const lineEnd = Number(preview?.dataset.lineEnd);
    const start = Number(block.dataset.start);
    const end = Number(block.dataset.end);
    if (![lineStart, lineEnd, start, end].every(Number.isFinite)) return;
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
    pausePlayback(parts);
    seekEditorAbsoluteTime(timeline, parts, start);
    clearTokenStopTimer();
    clearTokenAuditionGuard();
    const tokenDuration = Math.max(0.01, end - start);
    const stopSafety = Math.min(0.045, Math.max(0.008, tokenDuration * 0.12));
    const stopAt = Math.max(start + 0.001, end - stopSafety);
    const capturedTimeline = timeline;
    const capturedLine = timeline.dataset.lineNumber;
    window.__karaokeForgeTokenAuditionActive = true;
    window.__karaokeForgeTokenAuditionStopAt = stopAt;
    requestAnimationFrame(() => playPlayback(parts, true));
    window.__karaokeForgeTokenStopTimer = setTimeout(
      () => {
        const currentTimeline = visibleElement(".kf-token-editor");
        const sameLine = (
          capturedTimeline.isConnected &&
          currentTimeline === capturedTimeline &&
          currentTimeline?.dataset.lineNumber === capturedLine
        );
        if (!sameLine) {
          clearTokenStopTimer();
          return;
        }
        const current = waveSurferParts();
        pausePlayback(current);
        clearTokenStopTimer();
      },
      Math.max(1, ((stopAt - start) * 1000) / selectedPlaybackRate())
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

  const tokenContextMenu = document.createElement("div");
  tokenContextMenu.id = "kf-token-context-menu";
  tokenContextMenu.innerHTML = `
    <button type="button" data-action="delete">🗑 删除这个词块</button>
  `;
  document.body.appendChild(tokenContextMenu);

  const closeTokenContextMenu = () => {
    tokenContextMenu.classList.remove("is-open");
    tokenContextMenu.__kfTargetBlock = null;
  };

  const renumberTokenBlocks = (timeline) => {
    const blocks = Array.from(timeline.querySelectorAll(".kf-token-block"))
      .sort((left, right) =>
        Number(left.dataset.tokenIndex) - Number(right.dataset.tokenIndex)
      );
    const handles = timelineHandles(timeline);
    blocks.forEach((block, newIndex) => {
      const oldIndex = Number(block.dataset.tokenIndex);
      block.dataset.tokenIndex = String(newIndex);
      handles
        .filter((handle) => Number(handle.dataset.tokenIndex) === oldIndex)
        .forEach((handle, edgeIndex) => {
          handle.dataset.tokenIndex = String(newIndex);
          handle.dataset.boundaryIndex = String(newIndex * 2 + edgeIndex);
        });
    });
  };

  const deleteTokenBlock = (block) => {
    const timeline = block?.closest?.(".kf-token-editor");
    if (!timeline) return;
    const blocks = timeline.querySelectorAll(".kf-token-block");
    if (blocks.length <= 1) return;
    pauseForEditorMutation();
    const tokenIndex = Number(block.dataset.tokenIndex);
    timelineHandles(timeline)
      .filter((handle) => Number(handle.dataset.tokenIndex) === tokenIndex)
      .forEach((handle) => handle.remove());
    block.remove();
    renumberTokenBlocks(timeline);
    refreshTimeline(timeline);
    window.setTimeout(() => {
      const root = document.querySelector("#editor-save-tokens");
      const button = root?.matches?.("button") ? root : root?.querySelector("button");
      button?.click();
    }, 30);
  };

  const showTokenContextMenu = (event) => {
    const block = event.target.closest?.(".kf-token-block");
    if (!block) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    pauseForEditorMutation();
    closeLineContextMenu();
    tokenContextMenu.__kfTargetBlock = block;
    const text = block.dataset.token || "空白词块";
    const button = tokenContextMenu.querySelector('button[data-action="delete"]');
    if (button) {
      button.textContent = `🗑 删除“${Array.from(text).slice(0, 12).join("")}”`;
      button.disabled = block.closest(".kf-token-editor")
        ?.querySelectorAll(".kf-token-block").length <= 1;
    }
    tokenContextMenu.style.left = `${Math.max(
      4,
      Math.min(event.clientX, window.innerWidth - 224)
    )}px`;
    tokenContextMenu.style.top = `${Math.max(
      4,
      Math.min(event.clientY, window.innerHeight - 70)
    )}px`;
    tokenContextMenu.classList.add("is-open");
  };
  document.addEventListener("contextmenu", showTokenContextMenu, true);

  const editorDraftSelector =
    ".kf-token-text, #editor-lines, #editor-pronunciation-panel";
  const editorActionSelector =
    "#editor-line-controls button, #editor-overview-panel button, " +
    "#kf-line-context-menu button, #editor-exit-workspace";

  document.addEventListener("focusin", (event) => {
    if (event.target.closest?.(editorDraftSelector)) pauseForEditorMutation();
  });

  document.addEventListener("input", (event) => {
    if (event.target.closest?.(editorDraftSelector)) pauseForEditorMutation();
  });

  document.addEventListener("pointerdown", (event) => {
    if (
      event.target.closest?.(
        "#editor-save-tokens, #editor-save-tokens button, #editor-timing-actions button, " +
        "#editor-pronunciation-panel button"
      )
    ) {
      pauseForEditorMutation();
    }
    if (event.target.closest?.(editorActionSelector)) {
      pauseForEditorMutation();
    }
    if (event.target.closest?.("#editor-line-audio, #editor-global-audio")) {
      window.__karaokeForgePendingGlobalSeek = null;
      clearTokenStopTimer();
      clearTokenAuditionGuard();
      clearEditorMutationGuard();
    }
  });

  document.addEventListener("click", (event) => {
    if (event.target.closest?.(editorActionSelector)) pauseForEditorMutation();
  });

  tokenContextMenu.addEventListener("click", (event) => {
    const button = event.target.closest?.('button[data-action="delete"]');
    const block = tokenContextMenu.__kfTargetBlock;
    if (!button || button.disabled || !block) return;
    deleteTokenBlock(block);
    closeTokenContextMenu();
  });

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
    if (!event.target.closest?.("#kf-token-context-menu")) {
      closeTokenContextMenu();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      window.__karaokeForgeCancelGlobalLineEdgeDrag?.();
      setOverviewOpen(false);
      closeLineContextMenu();
      closeTokenContextMenu();
    }
  });

  const globalTimelineParts = () => ({
    timeline: visibleElement(".kf-global-timeline"),
    parts: waveSurferPartsFor("#editor-global-audio"),
  });

  function seekGlobalTimeline(timeline, parts, seconds) {
    if (!timeline || !parts) return false;
    const mediaDuration = Number(parts.media?.duration);
    const playbackDuration = globalPlaybackDuration(timeline, parts);
    const canvasDuration = Math.max(
      Number(timeline.dataset.duration) || 0,
      playbackDuration,
      0.01
    );
    const target = Math.min(playbackDuration, Math.max(0, Number(seconds) || 0));
    timeline.__kfLastSeekSeconds = target;
    if (parts.media && Number.isFinite(mediaDuration) && mediaDuration > 0) {
      parts.media.currentTime = Math.min(mediaDuration, target);
    } else {
      seekWaveSurfer(parts, target / playbackDuration);
    }
    const playhead = timeline.querySelector(".kf-global-playhead");
    if (playhead) playhead.style.left = `${target / canvasDuration * 100}%`;
    return true;
  }

  function queueGlobalSeek(timeline, parts, seconds, shouldPlay) {
    const requested = Math.max(0, Number(seconds) || 0);
    window.__karaokeForgePendingGlobalSeek = {
      seconds: requested,
      play: Boolean(shouldPlay),
      expiresAt: performance.now() + 2500,
      lastAttempt: performance.now(),
    };
    const moved = seekGlobalTimeline(timeline, parts, requested);
    if (shouldPlay) playPlayback(parts);
    return moved;
  }

  const seekGlobalFromPointer = (timeline, parts, clientX) => {
    const track = timeline?.querySelector(".kf-global-track");
    if (!track) return false;
    const bounds = track.getBoundingClientRect();
    if (bounds.width <= 0) return false;
    const ratio = Math.min(1, Math.max(0, (clientX - bounds.left) / bounds.width));
    const canvasDuration = Math.max(Number(timeline.dataset.duration) || 0, 0.01);
    return seekGlobalTimeline(timeline, parts, ratio * canvasDuration);
  };

  document.addEventListener("pointerdown", (event) => {
    const edge = event.target.closest?.(".kf-global-line-edge");
    if (
      !edge ||
      event.button !== 0 ||
      event.isPrimary === false ||
      window.__karaokeForgeDraggingGlobalLineEdge
    ) return;
    const timeline = edge.closest(".kf-global-timeline");
    const track = edge.closest(".kf-global-track");
    const lineNumber = Number(edge.dataset.lineNumber);
    const edgeName = edge.dataset.edge;
    const block = timeline?.querySelector(
      `.kf-global-line-block[data-line-number="${lineNumber}"]`
    );
    if (
      !timeline || !track || !block ||
      timeline.dataset.edgeSaving === "true" ||
      !Number.isInteger(lineNumber) ||
      !["start", "end"].includes(edgeName)
    ) return;
    const duration = Math.max(Number(timeline.dataset.duration) || 0, 0.01);
    const baseStart = Number(block.dataset.start);
    const baseEnd = Number(block.dataset.end);
    if (!Number.isFinite(baseStart) || !Number.isFinite(baseEnd) || baseEnd <= baseStart) {
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    const parts = waveSurferPartsFor("#editor-global-audio");
    const resumeAfterDrag = playbackIsActive(parts);
    window.__karaokeForgePendingGlobalSeek = null;
    pauseForEditorMutation();
    window.__karaokeForgeDraggingGlobalLineEdge = true;
    window.__karaokeForgeGlobalManualSelectionUntil = performance.now() + 10000;
    window.__karaokeForgeGlobalFollowLine = lineNumber;
    markGlobalLineSelected(timeline, lineNumber);
    edge.classList.add("is-dragging");

    const pointerId = event.pointerId;
    const originalBlockLeft = block.style.left;
    const originalBlockWidth = block.style.width;
    const originalEdgeLeft = edge.style.left;
    const originalFlipped = edge.classList.contains("is-flipped");
    const tokenBlocks = Array.from(block.querySelectorAll(".kf-global-token"))
      .sort((left, right) => Number(left.dataset.tokenIndex) - Number(right.dataset.tokenIndex));
    const firstTokenEnd = Number(tokenBlocks.at(0)?.dataset.end);
    const lastTokenStart = Number(tokenBlocks.at(-1)?.dataset.start);
    let previewSeconds = edgeName === "start" ? baseStart : baseEnd;
    const pointerStartX = event.clientX;
    let pointerMoved = false;
    let finished = false;

    const secondsFromPointer = (clientX) => {
      const bounds = track.getBoundingClientRect();
      if (bounds.width <= 0) return previewSeconds;
      const ratio = Math.min(1, Math.max(0, (clientX - bounds.left) / bounds.width));
      let seconds = Math.round(ratio * duration * 100) / 100;
      if (edgeName === "start") {
        const tokenLimit = Number.isFinite(firstTokenEnd)
          ? firstTokenEnd - 0.01
          : baseEnd - 0.01;
        seconds = Math.min(baseEnd - 0.01, tokenLimit, Math.max(0, seconds));
      } else {
        const tokenLimit = Number.isFinite(lastTokenStart)
          ? lastTokenStart + 0.01
          : baseStart + 0.01;
        seconds = Math.max(baseStart + 0.01, tokenLimit, Math.min(duration, seconds));
      }
      return Math.round(Math.max(0, Math.min(duration, seconds)) * 100) / 100;
    };

    const previewAt = (seconds) => {
      previewSeconds = seconds;
      const nextStart = edgeName === "start" ? seconds : baseStart;
      const nextEnd = edgeName === "end" ? seconds : baseEnd;
      block.style.left = `${nextStart / duration * 100}%`;
      block.style.width = `${Math.max(0.22, (nextEnd - nextStart) / duration * 100)}%`;
      edge.style.left = `${seconds / duration * 100}%`;
      edge.classList.toggle(
        "is-flipped",
        edgeName === "start" ? seconds <= 0.000001 : seconds >= duration - 0.000001
      );
      edge.setAttribute("aria-valuenow", seconds.toFixed(2));
      edge.title = `拖动${edgeName === "start" ? "句首" : "句尾"}：${seconds.toFixed(2)}s`;
      if (!seekGlobalTimeline(timeline, parts, seconds)) {
        const playhead = timeline.querySelector(".kf-global-playhead");
        if (playhead) playhead.style.left = `${seconds / duration * 100}%`;
      }
    };

    const restorePreview = () => {
      block.style.left = originalBlockLeft;
      block.style.width = originalBlockWidth;
      edge.style.left = originalEdgeLeft;
      edge.classList.toggle("is-flipped", originalFlipped);
      edge.classList.remove("is-dragging");
      edge.removeAttribute("aria-valuenow");
      edge.title = `拖动${edgeName === "start" ? "句首" : "句尾"}：${(
        edgeName === "start" ? baseStart : baseEnd
      ).toFixed(2)}s`;
    };

    const removeListeners = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", pointerUp);
      window.removeEventListener("pointercancel", pointerCancel);
      edge.removeEventListener("lostpointercapture", lostCapture);
      if (edge.hasPointerCapture?.(pointerId)) edge.releasePointerCapture?.(pointerId);
      if (window.__karaokeForgeCancelGlobalLineEdgeDrag === cancelFromOutside) {
        window.__karaokeForgeCancelGlobalLineEdgeDrag = null;
      }
    };

    const finish = (commit) => {
      if (finished) return;
      finished = true;
      removeListeners();
      restorePreview();
      window.__karaokeForgeDraggingGlobalLineEdge = false;
      window.__karaokeForgeGlobalManualSelectionUntil = performance.now() + 420;
      const original = edgeName === "start" ? baseStart : baseEnd;
      const changed = pointerMoved && Math.abs(previewSeconds - original) >= 0.005;
      if (!commit || !changed) {
        if (resumeAfterDrag) playPlayback(parts);
        return;
      }
      timeline.dataset.edgeSaving = "true";
      const submitted = applyGlobalLineEdge({
        line: lineNumber,
        edge: edgeName,
        seconds: previewSeconds,
        base_start: baseStart,
        base_end: baseEnd,
        text: block.dataset.text || "",
      });
      if (!submitted) delete timeline.dataset.edgeSaving;
      window.setTimeout(() => {
        if (document.body.contains(timeline)) delete timeline.dataset.edgeSaving;
      }, 4500);
    };

    const move = (moveEvent) => {
      if (moveEvent.pointerId !== pointerId || finished) return;
      moveEvent.preventDefault();
      pointerMoved ||= Math.abs(moveEvent.clientX - pointerStartX) >= 1;
      previewAt(secondsFromPointer(moveEvent.clientX));
    };
    const pointerUp = (finishEvent) => {
      if (finishEvent.pointerId !== pointerId) return;
      finishEvent.preventDefault();
      pointerMoved ||= Math.abs(finishEvent.clientX - pointerStartX) >= 1;
      if (pointerMoved) previewAt(secondsFromPointer(finishEvent.clientX));
      finish(true);
    };
    const pointerCancel = (finishEvent) => {
      if (finishEvent.pointerId !== pointerId) return;
      finish(false);
    };
    const lostCapture = (finishEvent) => {
      if (finishEvent.pointerId !== pointerId) return;
      finish(false);
    };
    function cancelFromOutside() {
      finish(false);
    }

    edge.setPointerCapture?.(pointerId);
    window.addEventListener("pointermove", move, { passive: false });
    window.addEventListener("pointerup", pointerUp, { passive: false });
    window.addEventListener("pointercancel", pointerCancel);
    edge.addEventListener("lostpointercapture", lostCapture);
    window.__karaokeForgeCancelGlobalLineEdgeDrag = cancelFromOutside;
    previewAt(previewSeconds);
  }, true);

  document.addEventListener("click", (event) => {
    const zoomButton = event.target.closest?.(
      ".kf-global-zoom-in, .kf-global-zoom-out, .kf-global-zoom-fit"
    );
    if (zoomButton) {
      const timeline = zoomButton.closest(".kf-global-timeline");
      const canvas = timeline?.querySelector(".kf-global-canvas");
      if (!canvas) return;
      const current = Number(canvas.dataset.zoom || "1");
      if (zoomButton.classList.contains("kf-global-zoom-fit")) {
        canvas.dataset.zoom = "0";
        canvas.style.minWidth = "100%";
      } else {
        const base = Number(canvas.dataset.baseWidth || "1400");
        const next = Math.min(8, Math.max(0.5,
          (current > 0 ? current : 1) *
          (zoomButton.classList.contains("kf-global-zoom-in") ? 1.35 : 0.75)
        ));
        canvas.dataset.zoom = String(next);
        canvas.style.minWidth = `${Math.max(700, base * next)}px`;
      }
      return;
    }
    const block = event.target.closest?.(".kf-global-line-block");
    if (!block) return;
    event.preventDefault();
    event.stopPropagation();
    clearTokenStopTimer();
    clearTokenAuditionGuard();
    clearEditorMutationGuard();
    const { timeline, parts } = globalTimelineParts();
    const lineNumber = Number(block.dataset.lineNumber);
    window.__karaokeForgeLastDisplayedEditorLine = displayedEditorLine();
    window.__karaokeForgeGlobalFollowLine = lineNumber;
    window.__karaokeForgeGlobalManualSelectionUntil = performance.now() + 320;
    markGlobalLineSelected(timeline, lineNumber);
    queueGlobalSeek(timeline, parts, Number(block.dataset.start), true);
    selectGlobalLine(lineNumber);
  }, true);

  document.addEventListener("pointerdown", (event) => {
    const track = event.target.closest?.(".kf-global-track");
    if (
      !track ||
      event.target.closest?.(".kf-global-line-block, .kf-global-line-edge")
    ) return;
    const timeline = track.closest(".kf-global-timeline");
    const parts = waveSurferPartsFor("#editor-global-audio");
    if (!timeline || !parts) return;
    event.preventDefault();
    clearTokenStopTimer();
    clearTokenAuditionGuard();
    clearEditorMutationGuard();
    window.__karaokeForgePendingGlobalSeek = null;
    const resumeAfterDrag = playbackIsActive(parts);
    pausePlayback(parts);
    window.__karaokeForgeDraggingGlobalPlayhead = true;
    const pointerId = event.pointerId;
    track.setPointerCapture?.(pointerId);
    seekGlobalFromPointer(timeline, parts, event.clientX);
    const move = (moveEvent) => {
      if (moveEvent.pointerId !== pointerId) return;
      moveEvent.preventDefault();
      seekGlobalFromPointer(timeline, parts, moveEvent.clientX);
    };
    let finished = false;
    const finish = (finishEvent) => {
      if (finished || finishEvent.pointerId !== pointerId) return;
      finished = true;
      track.removeEventListener("pointermove", move);
      track.removeEventListener("pointerup", finish);
      track.removeEventListener("pointercancel", finish);
      track.removeEventListener("lostpointercapture", finish);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
      if (track.hasPointerCapture?.(pointerId)) {
        track.releasePointerCapture?.(pointerId);
      }
      window.__karaokeForgeDraggingGlobalPlayhead = false;
      const requestedTime = Number(timeline.__kfLastSeekSeconds);
      const requestedBlock = Array.from(
        timeline.querySelectorAll(".kf-global-line-block")
      ).find((block) => (
        requestedTime >= Number(block.dataset.start) &&
        requestedTime < Number(block.dataset.end)
      ));
      if (requestedBlock) {
        const requestedLine = Number(requestedBlock.dataset.lineNumber);
        window.__karaokeForgeLastDisplayedEditorLine = displayedEditorLine();
        window.__karaokeForgeGlobalFollowLine = requestedLine;
        window.__karaokeForgeGlobalManualSelectionUntil = performance.now() + 320;
        markGlobalLineSelected(timeline, requestedLine);
        selectGlobalLine(requestedLine);
      }
      if (resumeAfterDrag) playPlayback(parts);
    };
    track.addEventListener("pointermove", move);
    track.addEventListener("pointerup", finish);
    track.addEventListener("pointercancel", finish);
    track.addEventListener("lostpointercapture", finish);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
  }, true);

  const isTextEntry = (target) => {
    if (target.closest?.("textarea, select, [contenteditable='true']")) return true;
    const input = target.closest?.("input");
    return Boolean(input && [
      "text", "search", "email", "url", "tel", "password", "number"
    ].includes(input.type));
  };

  const handlePlaybackSpace = (event) => {
    if (
      event.code !== "Space" ||
      event.ctrlKey ||
      event.metaKey ||
      event.altKey ||
      isTextEntry(event.target)
    ) return;
    if (window.__karaokeForgeDraggingGlobalLineEdge) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    const pendingSeek = window.__karaokeForgePendingGlobalSeek;
    const parts = waveSurferParts();
    if (!parts && !pendingSeek) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (event.type !== "keydown" || event.repeat) return;
    clearTokenStopTimer();
    if (pendingSeek) {
      pendingSeek.play = !Boolean(pendingSeek.play);
      if (pendingSeek.play) playPlayback(parts);
      else pausePlayback(parts);
    } else if (playbackIsActive(parts)) {
      pausePlayback(parts);
    } else {
      playPlayback(parts);
    }
  };
  document.addEventListener("keydown", handlePlaybackSpace, true);
  document.addEventListener("keyup", handlePlaybackSpace, true);

  document.addEventListener("change", (event) => {
    if (!event.target.closest?.("#editor-timing-mode")) return;
    window.__karaokeForgeCancelGlobalLineEdgeDrag?.();
    window.__karaokeForgePendingGlobalSeek = null;
    window.__karaokeForgeGlobalManualSelectionUntil = 0;
    window.__karaokeForgeGlobalPlaybackWasActive = false;
    ["#editor-line-audio", "#editor-global-audio"].forEach((selector) => {
      const parts = waveSurferPartsFor(selector);
      pausePlayback(parts);
      if (parts?.media) parts.media.pause();
    });
  }, true);

  document.addEventListener("keydown", (event) => {
    if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "z") return;
    if (event.target.closest?.("input, textarea, [contenteditable='true']")) return;
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

EDITOR_STOP_GATE_JS = r"""
async (...args) => {
  await new Promise((resolve) => window.setTimeout(resolve, 55));
  const timeline = Array.from(document.querySelectorAll(".kf-token-editor"))
    .find((element) => element.offsetParent !== null);
  const preview = Array.from(document.querySelectorAll(".kf-editor-preview-stage"))
    .find((element) => element.offsetParent !== null);
  const lineInput = document.querySelector("#editor-current-line input");
  const stoppedLine = Number(args[3]);
  const currentLine = Number(lineInput?.value);
  const timelineLine = Number(timeline?.dataset.lineNumber);
  const previewLine = Number(preview?.dataset.lineNumber);
  const auditionGuarded = (
    Boolean(window.__karaokeForgeTokenAuditionActive) ||
    (
      performance.now() < Number(window.__karaokeForgeTokenAuditionGuardUntil || 0) &&
      Number(window.__karaokeForgeTokenAuditionGuardLine) === stoppedLine
    )
  );
  const mutationGuarded = (
    performance.now() < Number(window.__karaokeForgeEditorMutationGuardUntil || 0) &&
    Number(window.__karaokeForgeEditorMutationGuardLine) === stoppedLine
  );
  const suppressed = (
    auditionGuarded ||
    mutationGuarded ||
    !Number.isInteger(stoppedLine) ||
    currentLine !== stoppedLine ||
    timelineLine !== stoppedLine ||
    previewLine !== stoppedLine
  );
  if (suppressed && args.length) {
    args[args.length - 1] = true;
    if (!window.__karaokeForgeTokenAuditionActive && auditionGuarded) {
      window.__karaokeForgeTokenAuditionGuardUntil = 0;
      window.__karaokeForgeTokenAuditionGuardLine = null;
    }
  }
  return args;
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
    project_name: str | None
    files: list[str]
    log: str
    output_dir: str | None


@dataclass(frozen=True)
class SubtitlePreviewSample:
    text: str
    translation: str
    timestamp: float | None
    highlight_progress: float
    active_row: int
    description: str


def _file_path(value: object | None) -> Path | None:
    if value is None:
        return None
    if isinstance(value, (str, os.PathLike)):
        return Path(value)
    if isinstance(value, dict):
        candidate = value.get("path") or value.get("name")
        if candidate:
            return Path(candidate)
    for attribute in ("path", "name"):
        candidate = getattr(value, attribute, None)
        if candidate:
            return Path(candidate)
    return None


def _is_empty_audio_placeholder(path: Path | None) -> bool:
    """Detect the tiny placeholder occasionally emitted by Gradio's Audio input."""

    if path is None or not path.is_file() or path.name.casefold() != "audio-song.wav":
        return False
    try:
        return path.stat().st_size <= 16 and path.read_bytes().strip().lower() == b"audio"
    except OSError:
        return False


def _file_paths(value: object | None) -> tuple[Path, ...]:
    if value is None:
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, os.PathLike)):
        return tuple(path for item in value if (path := _file_path(item)) is not None)
    path = _file_path(value)
    return (path,) if path is not None else ()


def _validated_cover(value: object | None) -> Path | None:
    cover = _file_path(value)
    if cover is None:
        return None
    if not cover.is_file():
        raise ValueError("选择的封面图片不存在，请重新上传。")
    if cover.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        raise ValueError("封面仅支持 JPG、PNG、WebP 或 BMP 图片。")
    return cover


def _validated_fonts(value: object | None) -> tuple[Path, ...]:
    fonts = _file_paths(value)
    for font in fonts:
        if not font.is_file():
            raise ValueError(f"字体文件不存在：{font.name}")
        if font.suffix.lower() not in {".ttf", ".otf", ".ttc"}:
            raise ValueError(f"字体仅支持 TTF、OTF 或 TTC：{font.name}")
    return fonts


def _workspace_for_lyrics_project(lyrics_file: object | None) -> WorkspaceProject | None:
    """Recover the saved workspace associated with an exported lyrics project."""

    lyrics = _file_path(lyrics_file)
    if lyrics is None or not lyrics.is_file() or lyrics.suffix.casefold() != ".json":
        return None
    try:
        document = read_lyrics(lyrics)
        manifest_value = document.metadata.get("workspace_manifest")
        if not isinstance(manifest_value, str) or not manifest_value.strip():
            return None
        manifest = Path(manifest_value)
        if not manifest.is_absolute():
            manifest = lyrics.parent / manifest
        return load_workspace_project(manifest)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _workspace_asset_fallbacks(
    audio_file: object | None,
    video_file: object | None,
    lyrics_file: object | None,
    cover_file: object | None,
    font_files: object | None = None,
) -> tuple[Path | None, Path | None, Path | None, tuple[Path, ...]]:
    """Fill missing browser file values from their persisted workspace assets."""

    audio = _file_path(audio_file)
    video = _file_path(video_file)
    cover = _file_path(cover_file)
    fonts = _file_paths(font_files)
    workspace = _workspace_for_lyrics_project(lyrics_file)
    if workspace is None:
        return audio, video, cover, fonts

    if audio is None or not audio.is_file() or _is_empty_audio_placeholder(audio):
        if workspace.audio is not None and workspace.audio.is_file():
            audio = workspace.audio
        elif (
            workspace.video is not None
            and workspace.video.is_file()
            and probe_media_has_audio(workspace.video) is True
        ):
            audio = workspace.video
        else:
            audio = None
    if video is None or not video.is_file():
        video = (
            workspace.video if workspace.video is not None and workspace.video.is_file() else None
        )
    if cover is None or not cover.is_file():
        cover = (
            workspace.cover if workspace.cover is not None and workspace.cover.is_file() else None
        )
    if not fonts or not any(font.is_file() for font in fonts):
        saved_fonts = tuple(font for font in workspace.font_files if font.is_file())
        fonts = saved_fonts
    return audio, video, cover, fonts


def _online_cover_for_job(
    local_cover: Path | None,
    cover_url: str | None,
    job_dir: Path,
    report: Callable[[str], None],
) -> Path | None:
    if local_cover is not None:
        report("已使用本地上传的专辑封面")
        return local_cover
    if not cover_url:
        return None
    try:
        cover = download_public_cover(cover_url, job_dir / "online-album-cover")
    except ArtworkError as exc:
        report(f"在线专辑封面读取失败，将等待本地封面：{exc}")
        return None
    report("已读取在线歌曲信息中的专辑封面")
    return cover


def _safe_stem(value: str | None, fallback: str = "karaoke") -> str:
    def normalize(raw: str | None) -> str:
        stem = (raw or "").strip()
        stem = re.sub(r"[\\/]+", "-", stem)
        suffix = Path(stem).suffix.casefold()
        if suffix in {
            ".mp4",
            ".mkv",
            ".webm",
            ".json",
            ".lrc",
            ".ass",
            ".srt",
            ".vtt",
            ".mp3",
            ".m4a",
            ".flac",
            ".wav",
        }:
            stem = stem[: -len(suffix)]
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", stem)
        stem = re.sub(r"\s+", "-", stem).strip(" .-_")
        stem = re.sub(r"-{2,}", "-", stem)[:80].rstrip(" .")
        if re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])", stem):
            stem = f"_{stem}"
        return stem

    return normalize(value) or normalize(fallback) or "karaoke"


def _workspace_source_settings(
    netease_info: object | None,
    qqmusic_info: object | None,
    utaten_info: object | None,
) -> dict[str, object]:
    refs: dict[str, dict[str, str]] = {}
    for provider, info, id_attribute in (
        ("netease", netease_info, "song_id"),
        ("qqmusic", qqmusic_info, "song_mid"),
        ("utaten", utaten_info, "lyric_id"),
    ):
        if info is None:
            continue
        source_id = str(getattr(info, id_attribute, "") or "").strip()
        source_url = str(getattr(info, "canonical_url", "") or "").strip()
        title = str(getattr(info, "title", "") or "").strip()
        if source_id or source_url:
            refs[provider] = {"id": source_id, "url": source_url, "title": title}
    return {"source_refs": refs} if refs else {}


def _default_output_root() -> Path:
    configured = os.environ.get("KARAOKE_FORGE_OUTPUT_DIR")
    root = Path(configured).expanduser() if configured else Path.cwd() / "outputs"
    return root.resolve()


def _allow_gradio_file_paths(app: object, *values: object) -> None:
    """Allow only concrete result files, including files created after launch."""

    allowed = getattr(app, "allowed_paths", None)
    if not isinstance(allowed, list):
        return
    known = {os.path.normcase(str(Path(value).resolve())) for value in allowed}
    pending = list(values)
    while pending:
        value = pending.pop()
        if value is None:
            continue
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, os.PathLike)):
            pending.extend(value)
            continue
        if not isinstance(value, (str, os.PathLike)):
            continue
        try:
            path = Path(value).resolve()
        except (OSError, TypeError, ValueError):
            continue
        if not path.is_file():
            continue
        key = os.path.normcase(str(path))
        if key not in known:
            allowed.append(str(path))
            known.add(key)


def _allow_gradio_workspace_paths(
    app: object,
    workspace_or_manifest: WorkspaceProject | str | os.PathLike[str],
) -> None:
    """Allow the exact persisted files belonging to one validated workspace."""

    try:
        workspace = (
            workspace_or_manifest
            if isinstance(workspace_or_manifest, WorkspaceProject)
            else load_workspace_project(workspace_or_manifest)
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return
    _allow_gradio_file_paths(
        app,
        workspace.manifest,
        workspace.lyrics_project,
        workspace.audio,
        workspace.video,
        workspace.cover,
        workspace.font_files,
    )


def _allow_gradio_result_workspaces(app: object, values: object) -> None:
    """Discover workspace manifests in a callback result without opening directories."""

    pending = list(values) if isinstance(values, Sequence) and not isinstance(
        values, (str, bytes, os.PathLike)
    ) else [values]
    for value in pending:
        if not isinstance(value, (str, os.PathLike)):
            continue
        path = Path(value)
        if path.name == PROJECT_FILENAME and path.is_file():
            _allow_gradio_workspace_paths(app, path)


def _new_job_dir(kind: str, output_root: str | Path | None = None) -> Path:
    root = Path(output_root).expanduser() if output_root else _default_output_root()
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    directory = root / f"{kind}-{stamp}-{uuid4().hex[:6]}"
    directory.mkdir(parents=True, exist_ok=False)
    return directory.resolve()


_PREVIEW_DEFAULT_TEXT = "I hear the flowers whisper.\nLet me bloom inside your garden."
_PREVIEW_DEFAULT_TRANSLATION = "让我在你的花园里盛放。"
_MATERIAL_PREVIEW_CACHE_VERSION = "3"


def _material_preview_cache_dir() -> Path:
    configured = os.environ.get("KARAOKE_FORGE_CACHE_DIR") or os.environ.get("GRADIO_TEMP_DIR")
    root = (
        Path(configured).expanduser()
        if configured
        else _default_output_root().parent / "KaraokeForgeCache"
    )
    target = (root / "material-previews").resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target


def _preview_file_signature(path: Path) -> str:
    resolved = path.resolve()
    stat = resolved.stat()
    return f"{resolved}|{stat.st_size}|{stat.st_mtime_ns}"


def _preview_cache_target(kind: str, *values: object) -> Path:
    payload = json.dumps(
        (_MATERIAL_PREVIEW_CACHE_VERSION, *values),
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return _material_preview_cache_dir() / f"{kind}-{digest}.jpg"


def _preview_image_data_url(path: Path) -> str:
    payload = path.read_bytes()
    if not payload:
        raise ValueError("生成的预览图片为空。")
    return "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii")


def _prune_material_preview_cache(cache_dir: Path, *, keep: int = 60) -> None:
    try:
        frames = sorted(
            (
                path
                for path in cache_dir.iterdir()
                if path.is_file() and path.suffix.lower() in {".jpg", ".png", ".webp"}
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for stale in frames[keep:]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass


def _cached_video_preview_frame(
    video: Path, timestamp: float | None, offset: float
) -> tuple[Path, float]:
    duration = probe_media_duration(video)
    frame_time = timestamp + offset if timestamp is not None else None
    if frame_time is None:
        frame_time = duration * 0.33 if duration else 5.0
    frame_time = max(0.0, frame_time)
    if duration is not None and duration > 0:
        frame_time = min(frame_time, max(0.0, duration - 0.1))
    target = _preview_cache_target(
        "mv",
        _preview_file_signature(video),
        round(frame_time, 3),
        960,
        540,
    )
    with _MATERIAL_PREVIEW_LOCK:
        if not target.is_file():
            extract_video_frame(
                video,
                target,
                timestamp=frame_time,
                resolution=(960, 540),
                overwrite=True,
            )
            _prune_material_preview_cache(target.parent)
    return target, frame_time


def _cached_cover_preview_frame(
    cover: Path,
    audio: Path | None,
    timestamp: float | None,
    background_theme: str,
    cover_style: str,
    show_waveform: bool,
) -> tuple[Path, float]:
    duration = probe_media_duration(audio) if audio is not None else None
    if audio is None:
        preview_time = 0.65
        audio_start = 0.0
        audio_signature = "synthetic-preview-audio-v1"
    else:
        preview_time = timestamp if timestamp is not None else None
        if preview_time is None:
            preview_time = duration * 0.33 if duration else 0.65
        preview_time = max(0.0, preview_time)
        audio_start = max(0.0, preview_time - 0.65)
        if duration is not None and duration > 0:
            audio_start = min(audio_start, max(0.0, duration - 1.25))
        audio_signature = _preview_file_signature(audio)
    target = _preview_cache_target(
        "cover",
        _preview_file_signature(cover),
        audio_signature,
        round(audio_start, 2),
        background_theme,
        cover_style,
        bool(show_waveform),
    )
    with _MATERIAL_PREVIEW_LOCK:
        if not target.is_file():
            cache_dir = target.parent
            with tempfile.TemporaryDirectory(
                prefix="cover-preview-",
                dir=cache_dir,
            ) as temp_name:
                scene = Path(temp_name) / "scene.mp4"
                preview_audio = audio or _material_preview_signal_audio()
                create_spinning_cover_video(
                    cover,
                    preview_audio,
                    scene,
                    resolution=(960, 540),
                    duration=1.25,
                    audio_start=audio_start,
                    style=cover_style,
                    background_theme=background_theme,
                    show_waveform=show_waveform,
                    overwrite=True,
                )
                extract_video_frame(
                    scene,
                    target,
                    timestamp=0.65,
                    resolution=(960, 540),
                    overwrite=True,
                )
            _prune_material_preview_cache(cache_dir)
    return target, audio_start + 0.65


def _material_preview_signal_audio() -> Path:
    """Create a tiny deterministic signal used only to display waveform layout."""

    target = _material_preview_cache_dir() / "synthetic-preview-audio-v1.wav"
    try:
        if target.is_file() and target.stat().st_size > 44:
            return target
    except OSError:
        pass
    sample_rate = 16_000
    frame_count = int(sample_rate * 1.5)
    samples = array("h")
    for index in range(frame_count):
        moment = index / sample_rate
        beat = moment % 0.375
        envelope = max(0.16, 1.0 - beat * 2.1)
        tone = math.sin(2.0 * math.pi * 180.0 * moment)
        overtone = 0.45 * math.sin(2.0 * math.pi * 360.0 * moment)
        samples.append(int(9_000 * envelope * (tone + overtone) / 1.45))
    if sys.byteorder == "big":  # pragma: no cover - supported for completeness
        samples.byteswap()
    temporary = target.with_suffix(".tmp")
    with wave.open(str(temporary), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples.tobytes())
    temporary.replace(target)
    return target


def _cached_static_cover_frame(cover: Path) -> Path:
    target = _preview_cache_target(
        "static-cover",
        _preview_file_signature(cover),
        960,
        540,
    )
    with _MATERIAL_PREVIEW_LOCK:
        if not target.is_file():
            extract_video_frame(
                cover,
                target,
                resolution=(960, 540),
                overwrite=True,
            )
            _prune_material_preview_cache(target.parent)
    return target


def _cached_online_preview_cover(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    cache_dir = _material_preview_cache_dir()
    stem = cache_dir / f"online-cover-{digest}"
    with _MATERIAL_PREVIEW_LOCK:
        for suffix in (".jpg", ".png", ".webp"):
            existing = stem.with_suffix(suffix)
            if existing.is_file():
                return existing
        return download_public_cover(url, stem, timeout=12.0)


@lru_cache(maxsize=32)
def _cached_public_netease_preview_info(link: str) -> Any:
    """Reuse public song metadata while the user experiments with preview styles."""

    return fetch_public_netease_info(link)


def _select_subtitle_preview_sample(
    document: LyricsDocument,
) -> SubtitlePreviewSample:
    lines = [line for line in document.visible_lines if line.text.strip()]
    if not lines:
        return SubtitlePreviewSample(
            _PREVIEW_DEFAULT_TEXT,
            _PREVIEW_DEFAULT_TRANSLATION,
            None,
            0.4,
            1,
            "示例歌词",
        )

    target_index = (len(lines) - 1) * 0.35
    timed_indices = [index for index, line in enumerate(lines) if line.start is not None]
    candidates = timed_indices or list(range(len(lines)))

    def sample_score(index: int) -> float:
        line = lines[index]
        position_score = 1.0 - abs(index - target_index) / max(1.0, len(lines) - 1)
        length_score = 0.16 if 4 <= len(line.text.strip()) <= 42 else 0.0
        return (
            position_score
            + (0.28 if line.tokens else 0.0)
            + (0.12 if line.translation else 0.0)
            + length_score
        )

    selected = max(candidates, key=sample_score)
    if selected == 0 and len(lines) > 1:
        selected = 1
    current = lines[selected]
    following = lines[selected + 1] if selected + 1 < len(lines) else None
    active_row = selected % 2
    if active_row == 0:
        upper_text = current.text
        lower_text = following.text if following else ""
    else:
        upper_text = following.text if following else ""
        lower_text = current.text
    sample_text = f"{upper_text}\n{lower_text}"
    highlight_progress = 0.4
    timestamp: float | None = None
    if current.start is not None:
        line_end = current.end
        if line_end is None and current.tokens:
            line_end = current.tokens[-1].end
        if line_end is None or line_end <= current.start:
            line_end = current.start + 2.0
        timestamp = current.start + (line_end - current.start) * highlight_progress
        if current.tokens and current.text:
            highlighted = 0.0
            token_characters = sum(max(1, len(token.text)) for token in current.tokens)
            for token in current.tokens:
                token_size = max(1, len(token.text))
                if timestamp >= token.end:
                    highlighted += token_size
                    continue
                if timestamp <= token.start:
                    break
                token_duration = max(0.01, token.end - token.start)
                highlighted += token_size * (timestamp - token.start) / token_duration
                break
            highlight_progress = max(0.0, min(1.0, highlighted / token_characters))
    if current.tokens:
        description = "真实逐字歌词"
    elif current.start is not None:
        description = "真实行级歌词 · 扫色为示意"
    else:
        description = "真实歌词 · 尚未校准，仅预览排版"
    return SubtitlePreviewSample(
        sample_text,
        current.translation or "",
        timestamp,
        highlight_progress,
        active_row,
        description,
    )


def prepare_subtitle_material_preview(
    audio_file: object,
    video_file: object,
    cover_file: object,
    lyrics_file: object,
    pasted_lyrics: str,
    offset: float,
    auto_sync: bool,
    background_theme: str,
    cover_style: str,
    show_waveform: bool,
    netease_link: str | None = "",
    qqmusic_link: str | None = "",
    utaten_link: str | None = "",
) -> tuple[str, str, str, str, bool, float, int, str]:
    """Build a fast subtitle preview from the song's actual lyrics and artwork."""

    notes: list[str] = []
    audio, video, cover_value, _fonts = _workspace_asset_fallbacks(
        audio_file,
        video_file,
        lyrics_file,
        cover_file,
    )
    if _is_empty_audio_placeholder(audio) or audio is None or not audio.is_file():
        audio = None
    if video is None or not video.is_file():
        video = None
    try:
        cover = _validated_cover(cover_value)
    except ValueError as exc:
        cover = None
        notes.append(str(exc))

    document: LyricsDocument | None = None
    lyrics_source = _file_path(lyrics_file)
    if lyrics_source is not None and lyrics_source.is_file():
        try:
            document = read_lyrics(lyrics_source)
        except Exception as exc:
            notes.append(f"歌词暂时无法用于预览：{exc}")
    elif pasted_lyrics and pasted_lyrics.strip():
        try:
            document = parse_plain(pasted_lyrics)
        except Exception as exc:
            notes.append(f"粘贴歌词暂时无法用于预览：{exc}")

    online_cover_url: str | None = None
    netease_link = (netease_link or "").strip()
    qqmusic_link = (qqmusic_link or "").strip()
    utaten_link = (utaten_link or "").strip()
    if (document is None or (video is None and cover is None)) and netease_link:
        try:
            info = _cached_public_netease_preview_info(netease_link)
            if document is None:
                if info.word_lyrics:
                    try:
                        document = parse_yrc(info.word_lyrics)
                    except Exception:
                        document = None
                if document is None and info.page_lyrics:
                    document = parse_lrc(info.page_lyrics)
                if document is not None and info.translated_lyrics:
                    attach_reference_translation(
                        document,
                        info.page_lyrics,
                        info.translated_lyrics,
                    )
            online_cover_url = info.cover_url
        except Exception as exc:
            notes.append(f"网易云预览信息读取失败：{exc}")
    if (document is None or (video is None and cover is None)) and qqmusic_link:
        try:
            info = fetch_public_qqmusic_info(qqmusic_link)
            if document is None:
                document = parse_lrc(info.page_lyrics)
                if info.translated_lyrics:
                    attach_reference_translation(
                        document,
                        info.page_lyrics,
                        info.translated_lyrics,
                    )
            online_cover_url = online_cover_url or info.cover_url
        except Exception as exc:
            notes.append(f"QQ 音乐预览信息读取失败：{exc}")
    if document is None and utaten_link:
        try:
            info = fetch_public_utaten_info(utaten_link)
            document = parse_plain("\n".join(info.lyrics))
        except Exception as exc:
            notes.append(f"UtaTen 预览歌词读取失败：{exc}")
    if cover is None and online_cover_url:
        try:
            cover = _cached_online_preview_cover(online_cover_url)
        except Exception as exc:
            notes.append(f"在线封面暂时无法用于预览：{exc}")

    sample = (
        _select_subtitle_preview_sample(document)
        if document is not None
        else SubtitlePreviewSample(
            _PREVIEW_DEFAULT_TEXT,
            _PREVIEW_DEFAULT_TRANSLATION,
            None,
            0.4,
            1,
            "示例歌词 · 加入歌词后自动替换",
        )
    )
    background_data = ""
    badge = sample.description
    preview_time: float | None = None
    if video is not None:
        try:
            frame, preview_time = _cached_video_preview_frame(
                video,
                sample.timestamp,
                float(offset or 0.0),
            )
            background_data = _preview_image_data_url(frame)
            badge = f"MV 实景 {preview_time // 60:02.0f}:{preview_time % 60:04.1f} · {sample.description}"
            if auto_sync and audio is not None and audio.resolve() != video.resolve():
                badge += " · 成片时自动校正"
        except Exception as exc:
            notes.append(f"MV 画面读取失败，已自动降级：{exc}")
    if not background_data and cover is not None:
        try:
            frame, preview_time = _cached_cover_preview_frame(
                cover,
                audio,
                sample.timestamp,
                background_theme or "adaptive",
                cover_style or "turntable",
                bool(show_waveform),
            )
            background_data = _preview_image_data_url(frame)
            waveform_detail = "真实音频波形" if audio is not None else "波形布局示意"
            badge = f"无 MV 成片样式 · {waveform_detail} · {sample.description}"
            if audio is None:
                notes.append(
                    "音频尚未下载，当前波形只用于展示布局；生成成片时会换成真实音乐波形。"
                )
        except Exception as exc:
            notes.append(f"动态封面预览失败，已改用静态封面：{exc}")
    if not background_data and cover is not None:
        try:
            frame = _cached_static_cover_frame(cover)
            background_data = _preview_image_data_url(frame)
            badge = f"专辑封面静态预览 · {sample.description}"
            if audio is None:
                notes.append("加入歌曲音频后，会自动显示所选唱片机与真实波形样式。")
        except Exception as exc:
            notes.append(f"封面画面读取失败，已使用内置背景：{exc}")

    if video is not None and background_data:
        if auto_sync and audio is not None and audio.resolve() != video.resolve():
            status = (
                "✅ 已使用当前歌词时间与手动偏移取出 MV 画面；"
                "生成成片时会自动定位 MV 片头并校正到准确时刻。"
            )
        else:
            status = "✅ 已自动使用 MV 中对应歌词时刻的画面。"
    elif cover is not None and audio is not None and background_data:
        status = "✅ 已按当前无 MV 主题、唱片布局和波形设置生成预览。"
    elif cover is not None and background_data:
        status = (
            "✅ 已用在线专辑封面生成当前无 MV 成片样式；"
            "音频下载完成后，波形会自动换成真实音乐响应。"
        )
    else:
        status = "ℹ️ 上传 MV，或上传音频和封面后，这里会自动换成这首歌的真实画面。"
    if document is None:
        status += " 当前先使用示例歌词，加入歌词后会自动替换。"
    if notes:
        status += "\n\n<small>" + html.escape("；".join(notes)) + "</small>"
    return (
        sample.text,
        sample.translation,
        background_data,
        badge,
        bool(background_data),
        sample.highlight_progress,
        sample.active_row,
        status,
    )


def _record_web_error(action: str, exc: BaseException) -> Path | None:
    """Persist callback failures that Gradio would otherwise only show as a popup."""

    try:
        root = _default_output_root()
        root.mkdir(parents=True, exist_ok=True)
        target = root / "karaoke-forge-errors.log"
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with _WEB_ERROR_LOG_LOCK, target.open("a", encoding="utf-8") as stream:
            stream.write(f"[{timestamp}] {action}\n{details}\n")
        return target
    except OSError:
        return None


def _prepare_lyrics(
    lyrics_file: object | None,
    pasted_lyrics: str | None,
    job_dir: Path,
) -> Path:
    source = _file_path(lyrics_file)
    if source is not None and source.is_file():
        return source
    if pasted_lyrics and pasted_lyrics.strip():
        target = job_dir / "lyrics.txt"
        target.write_text(pasted_lyrics.strip() + "\n", encoding="utf-8")
        return target
    raise ValueError("请上传歌词文件，或在歌词框中直接粘贴歌词。")


def inspect_make_lyrics(lyrics_file: object | None) -> str:
    """Describe a selected subtitle/project without starting an expensive render."""

    source = _file_path(lyrics_file)
    if source is None:
        return "尚未选择歌词文件。"
    if not source.is_file():
        return f"### ⚠️ 无法载入歌词\n找不到文件：{source}"
    try:
        document = read_lyrics(source)
    except Exception as exc:
        return f"### ⚠️ 无法载入歌词\n{exc}"
    visible = document.visible_lines
    timed_lines = sum(line.start is not None and line.end is not None for line in visible)
    word_timed_lines = sum(bool(line.tokens) for line in visible)
    if document.is_timed:
        timing = f"{timed_lines} 行有时间轴，其中 {word_timed_lines} 行含逐词时间"
        recommendation = (
            "；这是可直接制作的 Karaoke Forge JSON 工程"
            if source.suffix.lower() == ".json"
            else "；可直接制作，若有同批 JSON 建议优先选择 JSON 以保留最多编辑信息"
        )
        return f"### ✅ 已载入 {source.name}\n共 {len(visible)} 行，{timing}{recommendation}。"
    return (
        f"### ✅ 已载入 {source.name}\n共 {len(visible)} 行，但尚无完整时间轴；"
        "制作时会根据歌曲音频自动校准。"
    )


def exported_project_for_make(files: object) -> tuple[str | None, str]:
    """Select the recoverable JSON from one editor export for the make page."""

    values = (
        files if isinstance(files, Sequence) and not isinstance(files, (str, bytes)) else [files]
    )
    for value in values:
        path = _file_path(value)
        if path is not None and path.suffix.lower() == ".json" and path.is_file():
            return str(path), inspect_make_lyrics(path)
    return None, "### ⚠️ 导出完成，但没有找到可交给制作页的 JSON 工程。"


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
    auto_pronunciation: bool = True,
    auto_english_pronunciation: bool = True,
    translation_margin_v: float = 54,
    show_countdown: bool = True,
    countdown_gap_threshold: float = 8.0,
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
        translation_margin_v=int(translation_margin_v),
        show_countdown=bool(show_countdown),
        countdown_gap_threshold=float(countdown_gap_threshold),
        show_pronunciation=show_pronunciation,
        auto_pronunciation=auto_pronunciation,
        auto_english_pronunciation=auto_english_pronunciation,
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


def _lyrics_with_qqmusic_source(
    lyrics_path: Path,
    info: QQMusicSongInfo,
    job_dir: Path,
) -> Path:
    document = read_lyrics(lyrics_path)
    document.metadata.update(
        {
            "source": "QQ Music",
            "source_url": info.canonical_url,
            "source_id": info.song_mid,
            "ti": info.title,
            "ar": info.artist_text,
        }
    )
    target = job_dir / "lyrics-qqmusic.json"
    target.write_text(write_format(document, "json"), encoding="utf-8")
    return target


def _lyrics_with_utaten_source(
    lyrics_path: Path,
    info: UtaTenLyricsInfo,
    job_dir: Path,
    *,
    pronunciation_only: bool,
    lyrics_source_is_utaten: bool,
) -> tuple[Path, UtaTenPronunciationReport]:
    document = read_lyrics(lyrics_path)
    report = apply_utaten_pronunciation(
        document,
        info,
        replace_existing=pronunciation_only,
    )
    if lyrics_source_is_utaten:
        document.metadata.update(
            {
                "source": "UtaTen",
                "source_url": info.canonical_url,
                "source_id": info.lyric_id,
                "ti": info.title,
                "ar": info.artist,
            }
        )
        document.source_format = "utaten"
    else:
        document.metadata.update(
            {
                "pronunciation_source": "UtaTen",
                "pronunciation_source_url": info.canonical_url,
                "pronunciation_source_id": info.lyric_id,
            }
        )
    if pronunciation_only:
        document.metadata["pronunciation_policy"] = "utaten-only"
    target = job_dir / "lyrics-utaten.json"
    target.write_text(write_format(document, "json"), encoding="utf-8")
    return target, report


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
    auto_english_pronunciation: bool = True,
    background_data_url: str = "",
    preview_badge: str = "实时字幕预览 · KTV 双行布局",
    material_mode: bool = False,
    highlight_progress: float = 0.4,
    active_row: int = 1,
    translation_margin_v: float = 54,
) -> str:
    """Return a browser-native preview of the current ASS subtitle style."""

    safe_font = html.escape(font or "Microsoft YaHei", quote=True)
    active_row = 0 if int(active_row) == 0 else 1
    if material_mode:
        material_lines = (sample_text or "").split("\n")
        if len(material_lines) >= 2:
            upper_text = material_lines[0].strip()
            lower_text = material_lines[1].strip()
        elif active_row == 0:
            upper_text, lower_text = (sample_text or "").strip(), ""
        else:
            upper_text, lower_text = "", (sample_text or "").strip()
    else:
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
    active_text = upper_text if active_row == 0 else lower_text
    progress = max(0.0, min(1.0, float(highlight_progress)))
    split_at = max(0, min(len(active_text), round(len(active_text) * progress)))
    translation_value = (
        sample_translation if material_mode else sample_translation or "让歌声与画面在这里相遇。"
    )
    safe_translation = html.escape(translation_value)
    main_size = max(16, min(48, round(float(font_size) * 0.55)))
    translated_size = max(13, min(36, round(float(translation_font_size) * 0.55)))
    pronunciation_size = max(10, min(24, round(float(pronunciation_font_size) * 0.55)))
    bottom = max(12, min(92, round(float(margin_v) * 0.42)))
    translation_top = max(1.5, min(72.0, float(translation_margin_v) / 10.8))
    translation_html = ""
    if show_translation and safe_translation:
        translation_html = (
            '<div style="position:absolute;left:15%;right:15%;'
            f'top:{translation_top:.2f}%;'
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
        if not value:
            return ""
        pronunciation = (
            generate_pronunciation(
                value,
                include_english=auto_english_pronunciation,
            )
            if show_pronunciation
            else None
        )
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

    upper_line_html = preview_line(upper_text, active=active_row == 0)
    lower_line_html = preview_line(lower_text, active=active_row == 1)
    background_html = ""
    allowed_image_prefixes = (
        "data:image/jpeg;base64,",
        "data:image/png;base64,",
        "data:image/webp;base64,",
    )
    if background_data_url.startswith(allowed_image_prefixes):
        background_html = (
            '<img class="kf-preview-background" alt="" '
            f'src="{html.escape(background_data_url, quote=True)}">'
        )
    safe_badge = html.escape(preview_badge or "实时字幕预览 · KTV 双行布局")
    return f"""
    <div class="kf-subtitle-preview" data-kf-layout="ktv-split"
         data-kf-material="{str(bool(material_mode)).lower()}">
      {background_html}
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
      <div class="kf-preview-badge">{safe_badge}</div>
    </div>
    """


def _build_align_options(
    language: str,
    model: str,
    device: str,
    separate_vocals: bool,
    *,
    recover_low_coverage: bool = False,
) -> AlignOptions:
    return resolve_align_options(
        AlignOptions(
            model=model,
            language=None if language == "自动识别" else language,
            device=device,
            compute_type="int8" if device == "cpu" else "default",
            separate_vocals=separate_vocals,
            recover_low_coverage=recover_low_coverage,
        )
    )


def _web_timing_refinement(value: str | bool | None) -> str:
    if isinstance(value, bool):
        return "auto" if value else "off"
    return normalize_timing_refinement(value)


def _timing_drift_summary(report: object) -> str:
    anchor_lines = int(getattr(report, "timing_anchor_lines", 0) or 0)
    if not anchor_lines:
        return ""
    median_shift = float(getattr(report, "timing_median_shift", 0.0) or 0.0)
    maximum_shift = float(getattr(report, "timing_max_shift", 0.0) or 0.0)
    return (
        f"\n\n已根据 **{anchor_lines} 行**可靠演唱锚点自动校正渐进漂移；"
        f"中位偏移 **{median_shift:+.2f} 秒**，最大偏移 **{maximum_shift:.2f} 秒**。"
    )


def _low_coverage_summary(
    result: object,
    source: LyricsDocument,
    *,
    model: str,
    separate_vocals: bool,
) -> str:
    report = result.report  # type: ignore[attr-defined]
    transcription = result.transcription  # type: ignore[attr-defined]
    language = transcription.detected_language or "未识别"
    probability = transcription.language_probability
    language_detail = (
        f"{language}（置信度 {probability:.0%}）" if probability is not None else language
    )
    unmatched: list[str] = []
    for index in report.unmatched_line_indexes[:10]:
        if 0 <= index < len(source.lines):
            text = source.lines[index].text.strip() or "（空白行）"
            unmatched.append(f"- 第 {index + 1} 行：{text[:80]}")
    remaining = len(report.unmatched_line_indexes) - len(unmatched)
    if remaining > 0:
        unmatched.append(f"- 另有 {remaining} 行未匹配，请在歌词总览中检查。")
    unmatched_text = "\n".join(unmatched) if unmatched else "- 没有定位到具体未匹配行。"
    vocal_state = "已开启" if separate_vocals else "未开启"
    return (
        f"⚠️ 匹配覆盖率只有 **{report.coverage:.1%}**，低于安全阈值；"
        "已保留歌词并生成可编辑的保底时间轴，没有终止任务。\n\n"
        f"识别语言：**{language_detail}**；模型：**{model}**；人声分离：**{vocal_state}**。\n\n"
        f"未匹配歌词：\n{unmatched_text}\n\n"
        "建议先确认歌词与音频版本和语言；仍不理想时可开启人声分离，"
        "或改用 medium / large-v3 模型后重试。"
    )


def _materialize_auto_pronunciation(
    document: LyricsDocument,
    *,
    enabled: bool = True,
    include_english: bool = True,
) -> int:
    """Persist generated readings so the editor can adjust them before rendering."""

    document.metadata["auto_pronunciation"] = "true" if enabled else "false"
    document.metadata["auto_english_pronunciation"] = "true" if include_english else "false"
    if not enabled:
        return 0
    generated_count = 0
    for line in document.lines:
        if line.pronunciation or line.pronunciation_units:
            continue
        generated = generate_pronunciation(line.text, include_english=include_english)
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
    qqmusic_link: str = "",
    use_qqmusic_lyrics: bool = True,
    utaten_link: str = "",
    use_utaten_lyrics: bool = True,
    utaten_pronunciation_only: bool = False,
    auto_english_pronunciation: bool = True,
    cover_file: object | None = None,
    font_files: object | None = None,
    font: str = "Microsoft YaHei",
    cover_background: str = "adaptive",
    cover_style: str = "turntable",
    cover_waveform: bool = True,
    export_original: bool = True,
    export_instrumental: bool = False,
    cookie_browser: str = "",
    cookie_browser_profile: str = "",
    music_u: str = "",
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
        audio, video, cover_value, font_values = _workspace_asset_fallbacks(
            audio_file,
            video_file,
            lyrics_file,
            cover_file,
            font_files,
        )
        if _is_empty_audio_placeholder(audio):
            report("已忽略网页产生的空音频占位文件，将重新选择可用音源")
            audio = None
        if video is not None and not video.is_file():
            video = None
        cover = _validated_cover(cover_value)
        fonts = _validated_fonts(font_values)
        job_dir = _new_job_dir("rehearsal", output_root.strip() or None)
        netease_info = None
        qqmusic_info = None
        utaten_info = None
        link = (netease_link or "").strip()
        qq_link = (qqmusic_link or "").strip()
        uta_link = (utaten_link or "").strip()
        if utaten_pronunciation_only and not uta_link:
            raise ValueError("选择“仅使用 UtaTen 官方注音”时，请同时填写 UtaTen 歌词页链接。")
        if sum(bool(value) for value in (link, qq_link, uta_link)) > 1:
            raise ValueError("网易云、QQ 音乐和 UtaTen 链接一次只能填写一个。")

        if audio is not None and not audio.is_file():
            audio = None
        video_audio_state: bool | None = False if video is None else None
        using_mv_audio = False
        if audio is None and video is not None:
            video_audio_state = probe_media_has_audio(video)
            if video_audio_state is True:
                audio = video
                using_mv_audio = True

        if link:
            if not rights_confirmed:
                raise PermissionError("请勾选版权与使用权确认后再使用网易云链接。")
            if audio is None:
                track = download_netease_track(
                    link,
                    job_dir / ".source",
                    cookie_browser=cookie_browser,
                    cookie_browser_profile=cookie_browser_profile,
                    music_u=music_u,
                    progress=report,
                )
                netease_info = track
                if track.is_preview:
                    track.audio_path.unlink(missing_ok=True)
                    raise ValueError(
                        "网易云只返回了试听片段，不能用于完整校准；请在“账号权限”中选择"
                        "已登录且有本曲播放权的浏览器，或上传完整歌曲音频。"
                    )
                audio = track.audio_path
                if track.authenticated:
                    report("未上传本地音频，已使用网易云登录账号可播放的完整音频进行校准")
                else:
                    report("未上传本地音频，已使用网易云公开可播放的完整音频进行校准")
            else:
                netease_info = fetch_public_netease_info(link)
                if using_mv_audio:
                    report(
                        "已沿用制作页的 MV 内嵌完整音轨，无需重复上传；"
                        "仅从网易云读取公开歌曲信息、歌词和翻译，不下载音频"
                    )
                else:
                    report(
                        "已沿用制作页上传的本地音频和 MV，无需重复上传；"
                        "仅从网易云读取公开歌曲信息、歌词和翻译，不下载音频"
                    )
        elif qq_link:
            if not rights_confirmed:
                raise PermissionError("请勾选版权与使用权确认后再使用 QQ 音乐链接。")
            qqmusic_info = fetch_public_qqmusic_info(qq_link)
            report("仅从 QQ 音乐读取公开歌曲信息、行级 LRC 和翻译，不下载音频")
        elif uta_link:
            if not rights_confirmed:
                raise PermissionError("请勾选版权与使用权确认后再使用 UtaTen 歌词链接。")
            utaten_info = fetch_public_utaten_info(uta_link)
            report("已从 UtaTen 读取公开歌词和页面假名，不下载音频")

        if audio is None:
            if link:
                raise ValueError(
                    "网易云没有返回账号可完整播放的音频；请在“账号权限”选择已登录的浏览器，"
                    "或上传完整歌曲音频。"
                )
            if video_audio_state is False:
                raise ValueError(
                    "未上传独立歌曲音频，且 MV 不含可用音轨或没有上传 MV；请上传歌曲音频后再校准。"
                )
            raise ValueError(
                "无法检测这个 MV 是否含音轨；请确认 FFmpeg/FFprobe 可用，或上传独立歌曲音频。"
            )
        if audio.suffix.lower() == ".ncm":
            raise ValueError(
                "不支持转换或解密 NCM 文件；请上传官方允许导出的 MP3、FLAC、WAV 或 M4A。"
            )
        if using_mv_audio:
            report("未上传独立音频，已直接使用 MV 内嵌完整音轨进行校准")
        elif not link:
            report("已沿用制作页上传的音频和 MV，无需重复上传")

        online_cover_url = (
            netease_info.cover_url
            if netease_info is not None
            else qqmusic_info.cover_url
            if qqmusic_info is not None
            else None
        )
        cover = _online_cover_for_job(cover, online_cover_url, job_dir, report)
        if video is None and cover is None:
            raise ValueError(
                "没有上传 MV 时，请上传一张专辑图片；"
                "也可以填写带公开封面的网易云或 QQ 音乐单曲链接。"
            )

        translated_lyrics = (
            netease_info.translated_lyrics
            if netease_info is not None
            else qqmusic_info.translated_lyrics
            if qqmusic_info is not None
            else None
        )
        reference_lyrics = (
            netease_info.page_lyrics
            if netease_info is not None
            else qqmusic_info.page_lyrics
            if qqmusic_info is not None
            else None
        )

        provided_lyrics = lyrics_file is not None or bool(pasted_lyrics and pasted_lyrics.strip())
        if provided_lyrics:
            lyrics_path = _prepare_lyrics(lyrics_file, pasted_lyrics, job_dir)
            if netease_info is not None or qqmusic_info is not None:
                lyrics_path = _lyrics_with_translation(
                    lyrics_path,
                    translated_lyrics,
                    job_dir,
                    reference_lyrics,
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
        elif (
            qq_link and use_qqmusic_lyrics and qqmusic_info is not None and qqmusic_info.page_lyrics
        ):
            lyrics_path = job_dir / "qqmusic-lyrics.lrc"
            lyrics_path.write_text(qqmusic_info.page_lyrics, encoding="utf-8")
            report("已使用 QQ 音乐公开的行级 LRC 时间轴")
            lyrics_path = _lyrics_with_translation(
                lyrics_path,
                qqmusic_info.translated_lyrics,
                job_dir,
                qqmusic_info.page_lyrics,
            )
            if qqmusic_info.translated_lyrics and lyrics_path.suffix == ".json":
                report("已附加 QQ 音乐翻译")
        elif uta_link and use_utaten_lyrics and utaten_info is not None:
            lyrics_path = job_dir / "utaten-lyrics.txt"
            lyrics_path.write_text(utaten_info.plain_lyrics, encoding="utf-8")
            report(f"已导入 UtaTen 的 {len(utaten_info.lyrics)} 行公开歌词和假名")
        else:
            raise ValueError(
                "请上传/粘贴歌词，或填写网易云/QQ 音乐/UtaTen 链接并勾选使用公开歌词。"
            )

        if qqmusic_info is not None:
            lyrics_path = _lyrics_with_qqmusic_source(lyrics_path, qqmusic_info, job_dir)
        utaten_report = None
        if utaten_info is not None:
            lyrics_path, utaten_report = _lyrics_with_utaten_source(
                lyrics_path,
                utaten_info,
                job_dir,
                pronunciation_only=utaten_pronunciation_only,
                lyrics_source_is_utaten=not provided_lyrics,
            )
            if utaten_pronunciation_only and not utaten_report.annotated_lines:
                raise ValueError(
                    "UtaTen 官方歌词与当前歌词没有可安全转移的注音片段；"
                    "请确认链接对应同一首歌，正文和原时间轴均未被覆盖。"
                )
            mode = "仅采用官方注音" if utaten_pronunciation_only else "采用官方注音"
            report(
                f"UtaTen {mode}：匹配 {utaten_report.matched_lines}/"
                f"{utaten_report.local_lines} 行，写入 {utaten_report.annotated_lines} 行、"
                f"{utaten_report.mapped_units} 个 ruby 片段；未匹配文字保持原样"
            )
        source = read_lyrics(lyrics_path)
        if source.is_timed:
            timing_mode = _web_timing_refinement(timing_refinement)
            if should_refine_timing(source, timing_mode):
                refined = refine_audio_word_timing_with_fallback(
                    audio,
                    source,
                    timing_mode=timing_mode,
                    options=_build_align_options(language, model, device, separate_vocals),
                    work_dir=job_dir / ".work",
                    progress=report,
                )
                if refined is None:
                    document = source
                    timing_summary = "自动精修暂不可用，已保留原行级时间轴并继续生成工程。"
                else:
                    document = refined.document
                    drift_summary = _timing_drift_summary(refined.report)
                    if drift_summary:
                        report(drift_summary.strip())
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
                options=_build_align_options(
                    language,
                    model,
                    device,
                    separate_vocals,
                    recover_low_coverage=True,
                ),
                work_dir=job_dir / ".work",
                progress=report,
            )
            document = aligned.document
            if aligned.recovered:
                timing_summary = _low_coverage_summary(
                    aligned,
                    source,
                    model=model,
                    separate_vocals=separate_vocals,
                )
                report(f"匹配覆盖率 {aligned.report.coverage:.1%}，已生成可恢复的保底校准工程")
            else:
                timing_summary = (
                    f"已按上传音频生成时间轴，匹配覆盖率 **{aligned.report.coverage:.1%}**。"
                )

        auto_pronunciation = not (utaten_pronunciation_only and utaten_info is not None)
        generated_count = _materialize_auto_pronunciation(
            document,
            enabled=auto_pronunciation,
            include_english=auto_english_pronunciation,
        )
        if auto_pronunciation:
            english_detail = "包含英语片假名" if auto_english_pronunciation else "已跳过英语片假名"
            report(f"已为 {generated_count} 行歌词生成可编辑注音（{english_detail}）")
        else:
            report("已关闭自动补注音；未匹配到 UtaTen ruby 的文字保持无注音")
        document.require_timed()
        source_title = (
            netease_info.title
            if netease_info is not None
            else qqmusic_info.title
            if qqmusic_info is not None
            else utaten_info.title
            if utaten_info is not None
            else video.stem
            if video is not None
            else audio.stem
        )
        explicit_name = (output_name or "").strip()
        display_name = explicit_name or source_title
        fallback_stem = f"{source_title}-校准工程"
        stem = _safe_stem(explicit_name or fallback_stem, fallback=fallback_stem)
        manifest_path = job_dir / PROJECT_FILENAME
        document.metadata["workspace_manifest"] = str(manifest_path)
        exports = export_formats(
            document,
            job_dir,
            stem,
            ["lrc", "elrc", "srt", "vtt", "ass", "json"],
        )
        project = exports["json"]
        workspace = save_workspace_project(
            job_dir,
            name=display_name,
            lyrics_project=project,
            audio=audio,
            video=video,
            cover=cover,
            font_files=fonts,
            settings={
                "alignment_language": language,
                "alignment_model": model,
                "alignment_device": device,
                "alignment_separate_vocals": bool(separate_vocals),
                "timing_refinement": _web_timing_refinement(timing_refinement),
                "font": font,
                "cover_background": cover_background,
                "cover_style": cover_style,
                "cover_waveform": bool(cover_waveform),
                "export_original": bool(export_original),
                "export_instrumental": bool(export_instrumental),
                **_workspace_source_settings(netease_info, qqmusic_info, utaten_info),
            },
            recent_root=_default_output_root(),
        )
        files = [str(path) for path in exports.values()] + [str(workspace.manifest)]
        line_number = 1
        line = document.lines[0]
        status = (
            "### ✅ 可校准 KTV 工程已生成\n"
            f"{timing_summary}\n\n"
            f"共 {len(document.lines)} 行。"
            + (
                f"UtaTen 官方注音已写入 {utaten_report.annotated_lines} 行，"
                f"映射 {utaten_report.mapped_units} 个 ruby 片段；"
                "其余文字保持无注音。"
                if utaten_pronunciation_only and utaten_report is not None
                else f"自动注音 {generated_count} 行；"
                + (
                    "英语片假名自动注音已开启。"
                    if auto_english_pronunciation
                    else "英语片假名自动注音已关闭。"
                )
            )
            + "音频、MV/封面和字体已保存为可恢复工程；"
            "现在可逐句试听和微调，完成后再渲染视频。"
        )
        report("校准工程已生成并送入编辑器")
        return UiEditorPreparationResult(
            status=status,
            payload=document.to_dict(),
            rows=document_to_editor_rows(document),
            line_number=line_number,
            whole_pronunciation=line.pronunciation or "",
            pronunciation_rows=document_pronunciation_to_editor_rows(document, line),
            preview=editor_preview_html(document, line_number),
            project=str(project),
            audio=str(audio),
            project_name=display_name,
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
            project_name=None,
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
    music_u: str = "",
    auto_sync: bool = True,
    timing_refinement: str | bool = "auto",
    show_translation: bool = True,
    translation_font_size: float = 38,
    translation_color: str = "#EAF4FF",
    show_pronunciation: bool = True,
    pronunciation_font_size: float = 26,
    pronunciation_color: str = "#FFFFFF",
    output_root: str = "",
    qqmusic_link: str = "",
    use_qqmusic_lyrics: bool = True,
    utaten_link: str = "",
    use_utaten_lyrics: bool = True,
    utaten_pronunciation_only: bool = False,
    auto_english_pronunciation: bool = True,
    cover_file: object | None = None,
    font_files: object | None = None,
    cover_background: str = "adaptive",
    cover_style: str = "turntable",
    cover_waveform: bool = True,
    export_original: bool = True,
    export_instrumental: bool = False,
    translation_margin_v: float = 54,
    show_countdown: bool = True,
    countdown_gap_threshold: float = 8.0,
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
        if not export_original and not export_instrumental:
            raise ValueError("请至少选择导出原声版或无人声伴奏版中的一种。")
        audio, video, cover_value, font_values = _workspace_asset_fallbacks(
            audio_file,
            video_file,
            lyrics_file,
            cover_file,
            font_files,
        )
        if _is_empty_audio_placeholder(audio):
            report("已忽略网页产生的空音频占位文件，将重新选择可用音源")
            audio = None
        if video is not None and not video.is_file():
            video = None
        cover = _validated_cover(cover_value)
        fonts = _validated_fonts(font_values)
        if audio is not None and not audio.is_file():
            audio = None

        video_audio_state: bool | None = None
        if audio is None and video is not None:
            video_audio_state = probe_media_has_audio(video)
            if video_audio_state is True:
                audio = video
                report("未上传独立音频，已直接使用 MV 内嵌完整音轨")

        job_dir = _new_job_dir("mv", output_root.strip() or None)
        netease_info = None
        qqmusic_info = None
        utaten_info = None
        link = (netease_link or "").strip()
        qq_link = (qqmusic_link or "").strip()
        uta_link = (utaten_link or "").strip()
        if utaten_pronunciation_only and not uta_link:
            raise ValueError("选择“仅使用 UtaTen 官方注音”时，请同时填写 UtaTen 歌词页链接。")
        if sum(bool(value) for value in (link, qq_link, uta_link)) > 1:
            raise ValueError("网易云、QQ 音乐和 UtaTen 链接一次只能填写一个。")
        if link:
            if not rights_confirmed:
                raise PermissionError("请勾选版权与使用权确认后再使用网易云链接。")
            if audio is None:
                track = download_netease_track(
                    link,
                    job_dir / ".source",
                    cookie_browser=cookie_browser,
                    cookie_browser_profile=cookie_browser_profile,
                    music_u=music_u,
                    progress=report,
                )
                netease_info = track
                if track.is_preview:
                    track.audio_path.unlink(missing_ok=True)
                    if video_audio_state is True:
                        audio = video
                        report("网易云只返回试听片段，已自动改用 MV 内嵌的完整音轨")
                    elif video_audio_state is False:
                        raise ValueError(
                            "网易云只返回试听片段，而且该 MV 不含音轨；请上传完整歌曲音频。"
                        )
                    else:
                        raise ValueError(
                            "网易云只返回试听片段，且无法检测 MV 音轨；"
                            "请确认 FFmpeg/FFprobe 可用或上传完整歌曲音频。"
                        )
                else:
                    audio = track.audio_path
                    temporary_audio = track.audio_path
            else:
                netease_info = fetch_public_netease_info(link)
                if video is not None and audio.resolve() == video.resolve():
                    report("已使用 MV 内嵌完整音轨，仅从网易云读取公开歌曲信息和歌词")
                else:
                    report("已使用本地音频，仅从网易云读取公开歌曲信息和歌词")
        elif qq_link:
            if not rights_confirmed:
                raise PermissionError("请勾选版权与使用权确认后再使用 QQ 音乐链接。")
            qqmusic_info = fetch_public_qqmusic_info(qq_link)
            if video is not None and audio is not None and audio.resolve() == video.resolve():
                report("已使用 MV 内嵌完整音轨，仅从 QQ 音乐读取公开歌词")
            elif audio is not None:
                report("已使用本地音频，仅从 QQ 音乐读取公开歌词")
            else:
                report("QQ 音乐链接只提供公开歌词，不下载歌曲音频")
        elif uta_link:
            if not rights_confirmed:
                raise PermissionError("请勾选版权与使用权确认后再使用 UtaTen 歌词链接。")
            utaten_info = fetch_public_utaten_info(uta_link)
            if video is not None and audio is not None and audio.resolve() == video.resolve():
                report("已使用 MV 内嵌完整音轨，仅从 UtaTen 读取公开歌词和假名")
            elif audio is not None:
                report("已使用本地音频，仅从 UtaTen 读取公开歌词和假名")
            else:
                report("UtaTen 链接只提供公开歌词和假名，不下载歌曲音频")

        online_cover_url = (
            netease_info.cover_url
            if netease_info is not None
            else qqmusic_info.cover_url
            if qqmusic_info is not None
            else None
        )
        cover = _online_cover_for_job(cover, online_cover_url, job_dir, report)
        if video is None and cover is None:
            raise ValueError(
                "没有上传 MV 时，请上传一张专辑图片；"
                "也可以填写带公开封面的网易云或 QQ 音乐单曲链接。"
            )

        if audio is None or not audio.is_file():
            if video is None:
                raise ValueError(
                    "无 MV 制作需要完整歌曲音频；请上传 MP3、FLAC、WAV 或 M4A，"
                    "也可以提供可公开播放的网易云单曲链接。"
                )
            if video_audio_state is False:
                raise ValueError(
                    "未上传独立歌曲音频，而且该 MV 不含可用音轨；"
                    "请上传歌曲音频，或提供可公开播放的网易云单曲链接；"
                    "QQ 音乐和 UtaTen 链接只用于读取歌词。"
                )
            if video_audio_state is None:
                raise ValueError(
                    "无法检测该 MV 是否含音轨；请确认 FFmpeg/FFprobe 可用，或上传歌曲音频。"
                )
            raise ValueError(
                "请上传歌曲音频，或提供可公开播放的网易云单曲链接；"
                "QQ 音乐和 UtaTen 链接只用于读取歌词。"
            )
        if audio.suffix.lower() == ".ncm":
            raise ValueError(
                "不支持转换或解密 NCM 文件；请上传官方允许导出的 MP3、FLAC、WAV 或 M4A。"
            )

        translated_lyrics = (
            netease_info.translated_lyrics
            if netease_info is not None
            else qqmusic_info.translated_lyrics
            if qqmusic_info is not None
            else None
        )
        reference_lyrics = (
            netease_info.page_lyrics
            if netease_info is not None
            else qqmusic_info.page_lyrics
            if qqmusic_info is not None
            else None
        )

        provided_lyrics = lyrics_file is not None or bool(pasted_lyrics and pasted_lyrics.strip())
        if provided_lyrics:
            lyrics = _prepare_lyrics(lyrics_file, pasted_lyrics, job_dir)
            if netease_info is not None or qqmusic_info is not None:
                lyrics = _lyrics_with_translation(
                    lyrics,
                    translated_lyrics,
                    job_dir,
                    reference_lyrics,
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
        elif (
            qq_link and use_qqmusic_lyrics and qqmusic_info is not None and qqmusic_info.page_lyrics
        ):
            lyrics = job_dir / "qqmusic-lyrics.lrc"
            lyrics.write_text(qqmusic_info.page_lyrics, encoding="utf-8")
            lyrics = _lyrics_with_translation(
                lyrics,
                qqmusic_info.translated_lyrics,
                job_dir,
                qqmusic_info.page_lyrics,
            )
            report("已使用 QQ 音乐页面公开歌词和行级时间轴")
            if qqmusic_info.translated_lyrics and lyrics.suffix == ".json":
                report("已附加 QQ 音乐翻译，将固定显示在画面顶部")
        elif uta_link and use_utaten_lyrics and utaten_info is not None:
            lyrics = job_dir / "utaten-lyrics.txt"
            lyrics.write_text(utaten_info.plain_lyrics, encoding="utf-8")
            report(f"已导入 UtaTen 的 {len(utaten_info.lyrics)} 行公开歌词和假名")
        else:
            raise ValueError("请提供歌词，或勾选使用网易云/QQ 音乐/UtaTen 页面公开歌词。")

        if qqmusic_info is not None:
            lyrics = _lyrics_with_qqmusic_source(lyrics, qqmusic_info, job_dir)
        utaten_report = None
        if utaten_info is not None:
            lyrics, utaten_report = _lyrics_with_utaten_source(
                lyrics,
                utaten_info,
                job_dir,
                pronunciation_only=utaten_pronunciation_only,
                lyrics_source_is_utaten=not provided_lyrics,
            )
            if utaten_pronunciation_only and not utaten_report.annotated_lines:
                raise ValueError(
                    "UtaTen 官方歌词与当前歌词没有可安全转移的注音片段；"
                    "请确认链接对应同一首歌，正文和原时间轴均未被覆盖。"
                )
            mode = "仅采用官方注音" if utaten_pronunciation_only else "采用官方注音"
            report(
                f"UtaTen {mode}：匹配 {utaten_report.matched_lines}/"
                f"{utaten_report.local_lines} 行，写入 {utaten_report.annotated_lines} 行、"
                f"{utaten_report.mapped_units} 个 ruby 片段；未匹配文字保持原样"
            )

        source_title = (
            netease_info.title
            if netease_info is not None
            else qqmusic_info.title
            if qqmusic_info is not None
            else utaten_info.title
            if utaten_info is not None
            else video.stem
            if video is not None
            else audio.stem
        )
        display_name = (output_name or "").strip() or source_title
        fallback_stem = f"{source_title}-karaoke"
        stem = _safe_stem(display_name, fallback=fallback_stem)
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
                    not (utaten_pronunciation_only and utaten_info is not None),
                    auto_english_pronunciation,
                    translation_margin_v,
                    show_countdown,
                    countdown_gap_threshold,
                ),
                audio_offset=float(audio_offset),
                crf=crf,
                preset=preset,
                overwrite=False,
                auto_sync=auto_sync,
                timing_refinement=_web_timing_refinement(timing_refinement),
                cover_image=cover,
                font_files=fonts,
                cover_background=cover_background,
                cover_style=cover_style,
                cover_waveform=bool(cover_waveform),
                export_original=bool(export_original),
                export_instrumental=bool(export_instrumental),
            ),
            progress=report,
        )
        manifest_path = job_dir / PROJECT_FILENAME
        project_document = getattr(result, "document", None) or read_lyrics(lyrics)
        project_document.metadata["workspace_manifest"] = str(manifest_path)
        project_json = result.exports.get("json") or (job_dir / f"{stem}.json")
        project_json.write_text(write_format(project_document, "json"), encoding="utf-8")
        workspace = save_workspace_project(
            job_dir,
            name=display_name,
            lyrics_project=project_json,
            audio=audio,
            video=video,
            cover=cover,
            font_files=fonts,
            settings={
                "alignment_language": language,
                "alignment_model": model,
                "alignment_device": device,
                "alignment_separate_vocals": bool(separate_vocals),
                "timing_refinement": _web_timing_refinement(timing_refinement),
                "font": font,
                "font_size": int(font_size),
                "translation_margin_v": int(translation_margin_v),
                "show_countdown": bool(show_countdown),
                "countdown_gap_threshold": float(countdown_gap_threshold),
                "quality": quality,
                "auto_english_pronunciation": bool(auto_english_pronunciation),
                "cover_background": cover_background,
                "cover_style": cover_style,
                "cover_waveform": bool(cover_waveform),
                "export_original": bool(export_original),
                "export_instrumental": bool(export_instrumental),
                **_workspace_source_settings(netease_info, qqmusic_info, utaten_info),
            },
            recent_root=_default_output_root(),
        )
        if temporary_audio:
            temporary_audio.unlink(missing_ok=True)
            report("本次获取的临时音频已保存进工程，并清理下载缓存")
        result_videos = getattr(result, "videos", None) or {"original": result.video}
        files = [
            *(str(path) for path in result_videos.values()),
            *(str(path) for path in result.exports.values()),
            *([] if "json" in result.exports else [str(project_json)]),
            str(workspace.manifest),
        ]
        timing_warning = getattr(result, "timing_refinement_warning", None)
        if timing_warning:
            alignment = f"⚠️ {timing_warning}"
        elif result.alignment_report:
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
        elif video is not None and audio.resolve() == video.resolve():
            sync_text = "\n\n已直接使用 MV 内嵌完整音轨。"
        elif video is None:
            sync_text = "\n\n已使用虚化背景与旋转专辑封面生成无 MV 版本。"
        else:
            sync_text = ""
        versions = []
        if "original" in result_videos:
            versions.append("原声版")
        if "instrumental" in result_videos:
            versions.append("无人声伴奏版")
        status = (
            f"### ✅ 卡拉 OK MV 已生成：{' + '.join(versions)}\n{alignment}{sync_text}"
            "\n\n所选成品和所有歌词格式已保存，可以预览或下载。"
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
                refined = refine_audio_word_timing_with_fallback(
                    audio,
                    source,
                    timing_mode=timing_mode,
                    options=_build_align_options(language, model, device, separate_vocals),
                    work_dir=job_dir / ".work",
                    progress=report,
                )
                if refined is None:
                    document = source
                    alignment_text = "自动精修暂不可用，已保留输入时间轴并继续导出。"
                else:
                    document = refined.document
                    drift_summary = _timing_drift_summary(refined.report)
                    if drift_summary:
                        report(drift_summary.strip())
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
    music_u: str = "",
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
                music_u=music_u or None,
                timing_refinement=_web_timing_refinement(timing_refinement),
            ),
            progress=report,
        )
        files = [str(path) for path in result.exports.values()]
        if result.kept_audio:
            files.append(str(result.kept_audio))
        timing_warning = getattr(result, "timing_refinement_warning", None)
        if timing_warning:
            timing = f"⚠️ {timing_warning}"
        elif result.alignment_report:
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


def run_qqmusic_job(
    qqmusic_link: str,
    output_name: str,
    rights_confirmed: bool,
    output_root: str = "",
) -> UiJobResult:
    job_dir: Path | None = None
    logs: list[str] = []
    try:
        if not rights_confirmed:
            raise PermissionError("请先确认你有权使用和处理对应歌词。")
        link = (qqmusic_link or "").strip()
        if not link:
            raise ValueError("请粘贴 QQ 音乐单曲链接或完整分享文字。")
        job_dir = _new_job_dir("qqmusic", output_root.strip() or None)
        info = fetch_public_qqmusic_info(link)
        logs.append("已读取 QQ 音乐公开的歌曲信息和行级 LRC；没有请求歌曲音频")
        lyrics = job_dir / "qqmusic-lyrics.lrc"
        lyrics.write_text(info.page_lyrics, encoding="utf-8")
        lyrics = _lyrics_with_translation(
            lyrics,
            info.translated_lyrics,
            job_dir,
            info.page_lyrics,
        )
        lyrics = _lyrics_with_qqmusic_source(lyrics, info, job_dir)
        document = read_lyrics(lyrics)
        document.require_timed()
        stem = _safe_stem(output_name, fallback=info.title)
        exports = export_formats(
            document,
            job_dir,
            stem,
            ["lrc", "elrc", "srt", "vtt", "ass", "json"],
        )
        translation_detail = "，并附加公开翻译" if info.translated_lyrics else ""
        return UiJobResult(
            status=(
                "### ✅ QQ 音乐歌词已生成\n"
                f"**{info.title}** — {info.artist_text}{translation_detail}。"
                "可下载后直接载入歌词编辑器，或用于制作 MV。"
            ),
            video=None,
            files=[str(path) for path in exports.values()],
            log="\n".join(logs),
            output_dir=str(job_dir),
        )
    except Exception as exc:
        logs.append(f"失败：{exc}")
        return UiJobResult(
            status=f"### ⚠️ QQ 音乐歌词读取失败\n{exc}",
            video=None,
            files=[],
            log="\n".join(logs),
            output_dir=str(job_dir) if job_dir else None,
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
    workspace: WorkspaceProject | None = None
    if source.suffix.lower() == ".json":
        try:
            candidate = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            candidate = None
        if (
            isinstance(candidate, dict)
            and candidate.get("schema_version") == 1
            and candidate.get("lyrics_project")
        ):
            workspace = load_workspace_project(source)
            source = workspace.lyrics_project
    document = read_lyrics(source)
    if workspace is not None:
        document.metadata["workspace_manifest"] = str(workspace.manifest)
    document.require_timed()
    line_number = 1
    line = document.lines[0]
    return (
        document.to_dict(),
        document_to_editor_rows(document),
        (
            f"### ✅ 已载入工程 {workspace.name}\n"
            if workspace is not None
            else f"### ✅ 已载入 {source.name}\n"
        )
        + f"共 {len(document.lines)} 行，可编辑后导出。",
        line_number,
        line.pronunciation or "",
        document_pronunciation_to_editor_rows(document, line),
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
        document_pronunciation_to_editor_rows(document, line),
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
        document_pronunciation_to_editor_rows(document, line),
        editor_preview_html(document, selected),
        editor_token_timeline_html(document, selected),
        token_timing_to_json(line),
    )


def _editor_undo_snapshot(
    document: LyricsDocument,
    line_number: int,
) -> dict[str, Any]:
    """Store one reversible editor document together with its active line."""

    return {
        "document": document.to_dict(),
        "line_number": min(max(1, int(line_number)), len(document.lines)),
    }


def _editor_undo_document(
    undo_payload: dict[str, Any],
    fallback_line_number: int,
) -> tuple[LyricsDocument, int] | None:
    """Read current undo snapshots and legacy pre-0.4.0 snapshots."""

    if not undo_payload:
        return None
    snapshot = undo_payload.get("document")
    if isinstance(snapshot, dict) and snapshot.get("lines"):
        document = document_from_payload(snapshot)
        selected = int(undo_payload.get("line_number") or fallback_line_number)
        return document, min(max(1, selected), len(document.lines))
    if undo_payload.get("lines"):
        document = document_from_payload(undo_payload)
        selected = min(max(1, int(fallback_line_number)), len(document.lines))
        return document, selected
    return None


def _pronunciation_draft_signature(value: object) -> tuple[tuple[str, int, int], ...]:
    """Compare editor pronunciation rows by the fields that are actually saved."""

    if value is None:
        return ()
    if isinstance(value, dict) and isinstance(value.get("data"), list):
        value = value["data"]
    if hasattr(value, "values") and hasattr(value.values, "tolist"):
        value = value.values.tolist()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("逐词注音表格格式无效，请重新载入当前行。")
    signature: list[tuple[str, int, int]] = []
    for row_number, row in enumerate(value, 1):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            continue
        padded = [*row, None, None, None][:4]
        reading = str(padded[1] or "").strip()
        if not reading:
            continue
        try:
            start = int(float(padded[2]))
            end = int(float(padded[3]))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"逐词注音第 {row_number} 行缺少有效字符范围。") from exc
        signature.append((reading, start, end))
    return tuple(signature)


def _editor_row_source_ids(table: object, source_count: int) -> list[int | None]:
    """Return the original line identity for each row that survives a table edit."""

    source_ids: list[int | None] = []
    for position, row in enumerate(_table_rows(table), 1):
        padded = [*row, None, None][:2]
        if str(padded[1] or "显示").strip() == LINE_STATUS_DELETED:
            continue
        try:
            line_id = int(float(padded[0])) if padded[0] not in (None, "") else position
        except (TypeError, ValueError):
            line_id = position
        source_ids.append(line_id if 1 <= line_id <= source_count else None)
    return source_ids


def _mapped_editor_line_number(
    table: object,
    source_count: int,
    selected_line: int,
    result_count: int,
) -> int:
    """Keep the selected lyric identity stable across deletion and row reordering."""

    if result_count < 1:
        raise ValueError("编辑结果中没有歌词行。")
    selected = min(max(1, int(selected_line)), max(1, source_count))
    matches = [
        position
        for position, source_id in enumerate(
            _editor_row_source_ids(table, source_count),
            1,
        )
        if source_id == selected
    ]
    if len(matches) == 1:
        return matches[0]
    return min(max(1, selected), result_count)


def _editor_document_with_pending_changes(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    token_timing_json: str | None = None,
    whole_pronunciation: str | None = None,
    pronunciation_table: object | None = None,
    ripple_enabled: bool = False,
) -> tuple[LyricsDocument, dict[str, Any] | None]:
    """Persist current-line drafts before navigation and capture one undo snapshot."""

    original = document_from_payload(payload)
    selected = min(max(1, int(line_number)), len(original.lines))
    original_line = original.lines[selected - 1]
    source_ids = _editor_row_source_ids(line_table, len(original.lines))
    selected_matches = [
        position
        for position, source_id in enumerate(source_ids, 1)
        if source_id == selected
    ]
    selected_after = selected_matches[0] if len(selected_matches) == 1 else None
    pronunciation_changed = False
    if pronunciation_table is not None:
        pronunciation_changed = (whole_pronunciation or "").strip() != (
            original_line.pronunciation or ""
        ).strip() or _pronunciation_draft_signature(
            pronunciation_table
        ) != _pronunciation_draft_signature(
            document_pronunciation_to_editor_rows(original, original_line)
        )

    token_changed = False
    if token_timing_json:
        try:
            submitted_tokens = json.loads(token_timing_json)
            baseline_tokens = json.loads(token_timing_to_json(original_line))
        except (TypeError, ValueError, json.JSONDecodeError):
            submitted_tokens = None
            baseline_tokens = None
        token_changed = (
            isinstance(submitted_tokens, list)
            and bool(submitted_tokens)
            and submitted_tokens != baseline_tokens
        )

    if (pronunciation_changed or token_changed) and selected_after is None:
        raise ValueError(
            "当前句已被删除或重复，未保存的逐词/注音草稿无法安全对应；"
            "请先撤销结构修改，保存当前句后再删除或重排。"
        )

    # A changed pronunciation draft still describes the pre-edit text. Save it
    # there first, then let line/token text edits remap only the surviving spans.
    base = (
        apply_pronunciation_rows(
            original,
            selected,
            pronunciation_table,
            whole_pronunciation or "",
        )
        if pronunciation_changed
        else original
    )
    edited = apply_editor_rows(base, line_table)
    changed = edited.to_dict() != original.to_dict()

    if token_changed and selected_after is not None:
        edited = apply_token_timing(
            edited,
            document_to_editor_rows(edited),
            selected_after,
            token_timing_json or "[]",
        )
        changed = True

    if ripple_enabled:
        shifted = _ripple_global_editor_changes(
            original,
            edited,
            True,
            source_ids=source_ids,
        )
        changed = changed or bool(shifted)

    snapshot = _editor_undo_snapshot(original, selected) if changed else None
    return edited, snapshot


def _ripple_global_editor_changes(
    original: LyricsDocument,
    edited: LyricsDocument,
    enabled: bool,
    *,
    source_ids: Sequence[int | None] | None = None,
) -> tuple[int, ...]:
    """Ripple every directly extended row once, accounting for earlier propagated shifts."""

    if not enabled:
        return ()
    if source_ids is None:
        if len(original.lines) != len(edited.lines):
            return ()
        source_ids = list(range(1, len(edited.lines) + 1))
    if len(source_ids) != len(edited.lines):
        return ()
    direct_ends = [line.end for line in edited.lines]
    ripple_offsets = [0.0 for _line in edited.lines]
    shifted_lines: set[int] = set()
    for index, (source_id, direct_end) in enumerate(zip(source_ids, direct_ends), 1):
        if source_id is None or not 1 <= source_id <= len(original.lines):
            continue
        old_line = original.lines[source_id - 1]
        if (
            old_line.end is None
            or direct_end is None
            or direct_end <= old_line.end + 1e-9
        ):
            continue
        starts_before = [line.start for line in edited.lines]
        newly_shifted = ripple_following_line_timing(
            edited,
            index,
            previous_end=old_line.end + ripple_offsets[index - 1],
        )
        shifted_lines.update(newly_shifted)
        for shifted_line_number in newly_shifted:
            shifted_index = shifted_line_number - 1
            old_start = starts_before[shifted_index]
            new_start = edited.lines[shifted_index].start
            if old_start is not None and new_start is not None:
                ripple_offsets[shifted_index] += new_start - old_start
    return tuple(sorted(shifted_lines))


def _editor_rows_for_delete(
    payload: dict[str, Any],
    line_table: object,
    row_index: int,
) -> list[list[object]]:
    """Keep a cleared target row valid long enough for an explicit delete action."""

    rows = _table_rows(line_table)
    if row_index < 0 or row_index >= len(rows):
        return rows

    target = [*rows[row_index], None, None, None, None, None, None][:6]
    if str(target[4] or "").strip():
        return rows

    try:
        line_id = int(float(target[0])) if target[0] not in (None, "") else row_index + 1
    except (TypeError, ValueError):
        line_id = row_index + 1
    source = document_from_payload(payload)
    if line_id < 1 or line_id > len(source.lines):
        return rows
    source_text = source.lines[line_id - 1].text
    if not source_text.strip():
        return rows

    while len(rows[row_index]) <= 4:
        rows[row_index].append(None)
    rows[row_index][4] = source_text
    return rows


def apply_editor_line_action(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    action_request: str,
) -> tuple[object, ...]:
    try:
        request = json.loads(str(action_request or ""))
        row_index = int(request["row"])
        action = str(request["action"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("歌词行操作无效，请重新选择歌词行。") from exc
    if action == "delete":
        line_table = _editor_rows_for_delete(payload, line_table, row_index)
    document = apply_editor_rows(document_from_payload(payload), line_table)
    if row_index < 0 or row_index >= len(document.lines):
        raise ValueError("选择的歌词行已经变化，请重新选择。")

    # The action can target a row other than the currently playing line (for
    # example through the overview context menu). Restore focus to the row that
    # was actually changed so a deleted line is visibly restored after undo.
    undo_payload = _editor_undo_snapshot(document, row_index + 1)
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
    current = apply_editor_rows(document_from_payload(payload), line_table)
    snapshot = _editor_undo_document(undo_payload, int(line_number))
    if snapshot is None:
        workspace = _editor_selected_line_outputs(current, int(line_number))
        return (
            *workspace,
            "### ℹ️ 暂无可撤销修改\n继续编辑即可；这里不会再弹出错误。",
            {},
        )
    restored, restored_line_number = snapshot
    restored.require_timed()
    workspace = _editor_selected_line_outputs(restored, restored_line_number)
    status = "### ✅ 已撤销上次编辑\n已回到发生修改的歌词行；再次点击可以重做。"
    return (*workspace, status, _editor_undo_snapshot(current, int(line_number)))


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
        document_pronunciation_to_editor_rows(document, line),
        editor_preview_html(document, int(line_number)),
        f"### ✅ 已保存第 {int(line_number)} 行注音",
    )


def save_editor_pronunciation_workspace(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    token_timing_json: str,
    whole_line: str,
    pronunciation_table: object,
    undo_state: dict[str, Any],
    ripple_enabled: bool = False,
) -> tuple[object, ...]:
    """Save every current-line draft once, without replaying a stale pronunciation table."""

    before = document_from_payload(payload)
    selected = int(line_number)
    document, pending_snapshot = _editor_document_with_pending_changes(
        payload,
        line_table,
        selected,
        token_timing_json,
        whole_line,
        pronunciation_table,
        ripple_enabled,
    )
    selected = _mapped_editor_line_number(
        line_table,
        len(before.lines),
        selected,
        len(document.lines),
    )
    line = document.lines[selected - 1]
    result: tuple[object, ...] = (
        document.to_dict(),
        document_to_editor_rows(document),
        selected,
        line.pronunciation or "",
        document_pronunciation_to_editor_rows(document, line),
        editor_preview_html(document, selected),
        f"### ✅ 已保存第 {selected} 行注音",
    )
    next_undo = (
        pending_snapshot or _editor_undo_snapshot(before, selected)
        if document.to_dict() != before.to_dict()
        else undo_state
    )
    return (*result, next_undo)


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
    ripple_following: bool = True,
) -> tuple[dict[str, Any], list[list[object]], str, str]:
    before = apply_editor_rows(document_from_payload(payload), line_table)
    document = nudge_editor_line_timing(
        before,
        line_table,
        int(line_number),
        start_delta=start_delta,
        end_delta=end_delta,
        ripple_following=ripple_following,
    )
    line = document.lines[int(line_number) - 1]
    assert line.start is not None and line.end is not None
    shifted = [
        index + 1
        for index, (old_line, new_line) in enumerate(zip(before.lines, document.lines))
        if index != int(line_number) - 1
        and (old_line.start, old_line.end) != (new_line.start, new_line.end)
    ]
    ripple_note = (
        f"；已联动后移第 {shifted[0]}–{shifted[-1]} 行"
        if shifted
        else ""
    )
    status = (
        f"第 {int(line_number)} 行：**{line.start:.2f}s → {line.end:.2f}s**"
        f"（时长 {line.end - line.start:.2f}s）{ripple_note}"
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


@lru_cache(maxsize=32)
def _cached_media_duration(path: str, modified_ns: int, size: int) -> float | None:
    del modified_ns, size
    return probe_media_duration(path)


def editor_global_timeline_workspace(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    audio_file: object | None = None,
) -> str:
    """Render the full-song navigator using the real media duration when available."""

    try:
        document = apply_editor_rows(document_from_payload(payload), line_table)
        audio = _file_path(audio_file)
        media_duration = None
        if audio is not None and audio.is_file() and not _is_empty_audio_placeholder(audio):
            stat = audio.stat()
            media_duration = _cached_media_duration(
                str(audio.resolve()),
                stat.st_mtime_ns,
                stat.st_size,
            )
        return editor_global_timeline_html(
            document,
            int(line_number),
            media_duration=media_duration,
        )
    except Exception as exc:
        return f'<div class="kf-tip">全局时间轴暂不可用：{html.escape(str(exc))}</div>'


def save_editor_token_timing(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    token_timing_json: str,
) -> tuple[
    dict[str, Any],
    list[list[object]],
    str,
    list[list[object]],
    str,
    str,
    str,
    str,
]:
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
        line.pronunciation or "",
        document_pronunciation_to_editor_rows(document, line),
        editor_preview_html(document, int(line_number)),
        editor_token_timeline_html(document, int(line_number)),
        token_timing_to_json(line),
        (
            f"### ✅ 已保存第 {int(line_number)} 行逐词时间\n"
            f"整句范围：**{line.start:.2f}s → {line.end:.2f}s**，"
            f"共 {len(line.tokens)} 个词块。"
        ),
    )


def _editor_clip_lock(target: Path) -> threading.Lock:
    key = str(target)
    with _EDITOR_CLIP_LOCKS_GUARD:
        return _EDITOR_CLIP_LOCKS.setdefault(key, threading.Lock())


def _editor_clip_target(
    audio: Path,
    line_index: int,
    clip_start: float,
    clip_end: float,
) -> Path:
    cache_root = Path(
        os.environ.get("KARAOKE_FORGE_CACHE_DIR")
        or (_default_output_root().parent / "KaraokeForgeCache")
    ).expanduser()
    clip_dir = cache_root / "editor-clips"
    clip_dir.mkdir(parents=True, exist_ok=True)
    stat = audio.stat()
    identity = hashlib.sha256(
        f"{audio.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode()
    ).hexdigest()[:12]
    return clip_dir / (
        f"{_safe_stem(audio.stem)}-{identity}-line-{line_index + 1}-"
        f"clip-{clip_start:.3f}-{clip_end:.3f}.m4a"
    )


def preview_editor_audio_line(
    audio_file: object | None,
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    lead_in: float = 0.0,
    tail: float = 0.0,
) -> tuple[str, str]:
    audio = _file_path(audio_file)
    if audio is None or not audio.is_file():
        raise ValueError("请先上传用于校准的歌曲音频。")
    if _is_empty_audio_placeholder(audio):
        raise ValueError(
            "当前校准音频是网页产生的空占位文件，并不是真实歌曲；"
            "请重新上传音频，或在制作页选择已登录的网易云账号后重新生成校准工程。"
        )
    document = apply_editor_rows(document_from_payload(payload), line_table)
    index = int(line_number) - 1
    if index < 0 or index >= len(document.lines):
        raise ValueError(f"行号应在 1 到 {len(document.lines)} 之间。")
    line = document.lines[index]
    if line.start is None or line.end is None:
        raise ValueError("当前歌词行没有完整时间，无法试听。")
    clip_start = max(0.0, line.start - max(0.0, float(lead_in)))
    clip_end = line.end + max(0.0, float(tail))
    target = _editor_clip_target(audio, index, clip_start, clip_end)
    ffmpeg = find_runtime_executable("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("没有找到 FFmpeg，无法截取当前句试听片段。")
    with _editor_clip_lock(target):
        if not target.is_file() or target.stat().st_size <= 0:
            temporary = target.with_name(f"{target.stem}.{uuid4().hex}.part{target.suffix}")
            try:
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
                        str(temporary),
                    ],
                    check=False,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                )
                if completed.returncode != 0:
                    details = completed.stderr.strip()
                    if "Invalid data found" in details or "Error opening input" in details:
                        raise ValueError(
                            "当前校准音频不是可播放的音频文件；请重新上传，"
                            "或用已登录的网易云账号重新生成校准工程。"
                        )
                    raise RuntimeError(f"当前句试听片段生成失败：{details}")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
    status = f"当前歌词试听范围：**{line.start:.2f}s → {line.end:.2f}s**。"
    return str(target), status


def _next_playable_editor_line(
    document: LyricsDocument,
    line_number: int,
) -> int | None:
    """Find the next visible, non-blank line that has a complete timeline."""

    for candidate_number in range(max(1, int(line_number) + 1), len(document.lines) + 1):
        candidate = document.lines[candidate_number - 1]
        if (
            not candidate.hidden
            and candidate.text.strip()
            and candidate.start is not None
            and candidate.end is not None
        ):
            return candidate_number
    return None


def prefetch_editor_audio_line(
    audio_file: object | None,
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
) -> None:
    """Warm one editor clip in the background without delaying current playback."""

    try:
        document = apply_editor_rows(document_from_payload(payload), line_table)
        target_line = int(line_number)
        if target_line < 1 or target_line > len(document.lines):
            return
        audio = _file_path(audio_file)
        if audio is None or not audio.is_file() or _is_empty_audio_placeholder(audio):
            return
        line = document.lines[target_line - 1]
        if line.start is None or line.end is None:
            return
        target = _editor_clip_target(audio, target_line - 1, line.start, line.end)
        prefetch_key = str(target)
        if target.is_file() and target.stat().st_size > 0:
            return
        with _EDITOR_PREFETCH_GUARD:
            if prefetch_key in _EDITOR_PREFETCH_IN_FLIGHT:
                return
            _EDITOR_PREFETCH_IN_FLIGHT.add(prefetch_key)
        stable_payload = document.to_dict()
        stable_rows = document_to_editor_rows(document)
    except (OSError, TypeError, ValueError):
        return

    def warm() -> None:
        try:
            preview_editor_audio_line(
                audio,
                stable_payload,
                stable_rows,
                target_line,
            )
        except (OSError, RuntimeError, ValueError):
            return
        finally:
            with _EDITOR_PREFETCH_GUARD:
                _EDITOR_PREFETCH_IN_FLIGHT.discard(prefetch_key)

    try:
        _EDITOR_PREFETCH_EXECUTOR.submit(warm)
    except RuntimeError:
        with _EDITOR_PREFETCH_GUARD:
            _EDITOR_PREFETCH_IN_FLIGHT.discard(prefetch_key)


def export_editor_project(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    whole_line: str,
    pronunciation_table: object,
    output_name: str,
    token_timing_json: str | None = None,
    ripple_enabled: bool = True,
) -> tuple[dict[str, Any], list[list[object]], str, list[str], str]:
    document, _snapshot = _editor_document_with_pending_changes(
        payload,
        line_table,
        int(line_number),
        token_timing_json,
        whole_line,
        pronunciation_table,
        ripple_enabled,
    )
    workspace = None
    manifest_value = document.metadata.get("workspace_manifest")
    if manifest_value:
        try:
            workspace = load_workspace_project(manifest_value)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            workspace = None
    job_dir = workspace.manifest.parent if workspace is not None else _new_job_dir("editor")
    manifest_path = job_dir / PROJECT_FILENAME
    document.metadata["workspace_manifest"] = str(manifest_path)
    explicit_name = (output_name or "").strip()
    display_name = workspace.name if workspace is not None else "edited-lyrics"
    fallback_stem = _safe_stem(display_name, fallback="edited-lyrics")
    stem = _safe_stem(explicit_name, fallback=fallback_stem)
    formats = ["lrc", "elrc", "srt", "vtt", "ass", "json"] if document.visible_lines else ["json"]
    exports = export_formats(
        document,
        job_dir,
        stem,
        formats,
    )
    save_workspace_project(
        job_dir,
        name=explicit_name or display_name,
        lyrics_project=exports["json"],
        audio=workspace.audio if workspace is not None else None,
        video=workspace.video if workspace is not None else None,
        cover=workspace.cover if workspace is not None else None,
        font_files=workspace.font_files if workspace is not None else (),
        settings=workspace.settings if workspace is not None else {},
        recent_root=_default_output_root(),
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


def export_editor_project_for_web(
    payload: dict[str, Any],
    line_table: object,
    line_number: int,
    whole_line: str,
    pronunciation_table: object,
    output_name: str,
    token_timing_json: str | None = None,
    ripple_enabled: bool = True,
) -> tuple[dict[str, Any], object, str, list[str], str]:
    """Keep editor export errors visible in-page and persist their traceback."""

    try:
        return export_editor_project(
            payload,
            line_table,
            line_number,
            whole_line,
            pronunciation_table,
            output_name,
            token_timing_json,
            ripple_enabled,
        )
    except Exception as exc:
        log_path = _record_web_error("editor-export", exc)
        log_note = f"\n\n详细记录：`{log_path}`" if log_path else ""
        return (
            payload,
            line_table,
            f"### ⚠️ 导出失败\n{exc}{log_note}",
            [],
            str(_default_output_root()),
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
    audio, _video, _cover, _fonts = _workspace_asset_fallbacks(
        audio_file,
        None,
        project,
        None,
    )
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


def handoff_make_readiness(
    video_file: object | None,
    lyrics_file: object | None,
    cover_file: object | None = None,
) -> UiJobResult | None:
    """Return a user-facing stop result when an editor handoff cannot render yet."""

    lyrics = _file_path(lyrics_file)
    if lyrics is None or not lyrics.is_file():
        return UiJobResult(
            status=(
                "### ⚠️ 校准歌词没有成功载入制作页\n"
                "请重新点击“确认校准并开始制作 MV”；如果仍然失败，请查看页面上的错误详情。"
            ),
            video=None,
            files=[],
            log="编辑器交接完成后没有找到可供最终制作使用的歌词工程。",
            output_dir=None,
        )

    _audio, video, cover, _fonts = _workspace_asset_fallbacks(
        None,
        video_file,
        lyrics,
        cover_file,
    )
    if (video is None or not video.is_file()) and (cover is None or not cover.is_file()):
        return UiJobResult(
            status=(
                "### ✅ 校准歌词已载入制作页\n"
                f"已采用 `{lyrics.name}`，编辑结果不会丢失。\n\n"
                "### 下一步：请上传对应 MV 或一张专辑图片\n"
                "选择 MV，或用专辑图片生成旋转唱片画面；校准音频和歌词无需重复上传。"
            ),
            video=None,
            files=[],
            log="已保留校准歌词和音频；制作页尚未选择 MV 或专辑图片。",
            output_dir=None,
        )

    return None


def environment_markdown() -> str:
    demucs_runtime = inspect_demucs_runtime()
    ffmpeg = find_runtime_executable("ffmpeg")
    checks = [
        ("Python 3.10+", sys.version_info >= (3, 10), sys.version.split()[0]),
        ("FFmpeg", ffmpeg is not None, ffmpeg or "未找到"),
        (
            "faster-whisper",
            importlib.util.find_spec("faster_whisper") is not None,
            "已安装" if importlib.util.find_spec("faster_whisper") else "未安装",
        ),
        (
            "Demucs（可选）",
            demucs_runtime.ready,
            demucs_runtime.detail_zh,
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
            "网易云一键登录组件",
            importlib.util.find_spec("websocket") is not None,
            "已安装"
            if importlib.util.find_spec("websocket")
            else "未安装；重新双击启动网页版.bat 会自动补装",
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
    try:
        rows.extend(["", model_download_status_markdown()])
    except NetworkSettingsError as exc:
        rows.extend(
            [
                "",
                f"**模型下载设置需要修复**：{exc}  ",
                "请在下方恢复为国内直连或官方源，或双击项目根目录的 `模型下载设置.bat`。",
            ]
        )
    rows.extend(
        [
            "",
            (
                "> faster-whisper 用于从无时间轴歌词生成时间；Demucs 只在勾选"
                "“先分离人声”时需要。首次分离会下载模型；Windows 可双击独立的 Demucs "
                "安装脚本。yt-dlp 用于获取当前匿名或已登录账号有权播放的"
                "网易云音频；pykakasi 与 alkana 用于离线生成日语和英语注音。"
            ),
            "",
            "输出默认保存在项目的 `outputs` 目录。页面运行在本机，素材不会自动上传到公网。",
        ]
    )
    return "\n".join(rows)


def _model_network_form_defaults() -> tuple[str, str, bool, str]:
    try:
        settings = load_model_download_settings()
        return (
            settings.mode,
            settings.proxy_url or "http://127.0.0.1:7890",
            settings.mode == "mirror" and settings.mirror_confirmed,
            model_download_status_markdown(settings),
        )
    except NetworkSettingsError as exc:
        return (
            "modelscope",
            "http://127.0.0.1:7890",
            False,
            f"### ⚠️ 设置文件无效\n{exc}\n\n请选择一种方式并保存以修复。",
        )


def configure_model_network_for_web(
    mode: str,
    proxy_url: str,
    mirror_confirmed: bool,
) -> str:
    try:
        settings = configure_model_download_settings(
            mode,  # type: ignore[arg-type]
            proxy_url=proxy_url.strip() if mode == "proxy" else None,
            confirm_mirror=bool(mirror_confirmed),
        )
        status = model_download_status_markdown(settings)
        test = test_model_download_network(settings, timeout=6.0)
        icon = "✅" if test.ok else "⚠️"
        return f"{status}\n\n### {icon} {test.summary_zh}\n{test.detail_zh}"
    except (NetworkSettingsError, OSError, ValueError) as exc:
        return f"### ⚠️ 没有保存设置\n{exc}"


def auto_configure_model_network_for_web() -> tuple[str, str, str, bool]:
    domestic = ModelDownloadSettings(mode="modelscope")
    domestic_test = test_model_download_network(domestic, timeout=6.0)
    if domestic_test.ok:
        settings = configure_model_download_settings("modelscope")
        return (
            (
                f"{model_download_status_markdown(settings)}\n\n"
                f"### ✅ {domestic_test.summary_zh}\n{domestic_test.detail_zh}"
            ),
            "modelscope",
            "http://127.0.0.1:7890",
            False,
        )

    official = ModelDownloadSettings(mode="official")
    official_test = test_model_download_network(official, timeout=6.0)
    if official_test.ok:
        settings = configure_model_download_settings("official")
        return (
            (
                f"{model_download_status_markdown(settings)}\n\n"
                f"### ✅ {official_test.summary_zh}\n{official_test.detail_zh}"
            ),
            "official",
            "http://127.0.0.1:7890",
            False,
        )

    details = [
        f"国内直连：{domestic_test.detail_zh}",
        f"官方源：{official_test.detail_zh}",
    ]
    for candidate in auto_detect_local_proxies():
        settings = ModelDownloadSettings(mode="proxy", proxy_url=candidate.url)
        result = test_model_download_network(settings, timeout=6.0)
        details.append(f"{candidate.source_zh}：{result.detail_zh}")
        if result.ok:
            saved = configure_model_download_settings("proxy", proxy_url=candidate.url)
            return (
                (
                    f"{model_download_status_markdown(saved)}\n\n"
                    f"### ✅ 已自动找到可用本机代理\n{candidate.source_zh}：{candidate.url}"
                ),
                "proxy",
                candidate.url,
                False,
            )
    current_mode, current_proxy, current_confirmed, _ = _model_network_form_defaults()
    return (
        "### ⚠️ 自动探测没有找到可用路径\n"
        "国内直连和官方源都不可用时，程序也不会自动切换到未校验镜像。"
        "你可以先检查网络、打开代理软件再重试，"
        "手动填写其 HTTP 端口，或阅读提示后明确选择 hf-mirror。\n\n"
        + "  \n".join(details),
        current_mode,
        current_proxy,
        current_confirmed,
    )


def predownload_model_for_web(profile: str) -> str:
    command = [
        sys.executable,
        "-m",
        "karaoke_forge",
        "model-download",
        "--mode",
        "status",
        "--download-model",
        profile,
        "--timeout",
        "6",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=7200,
        )
    except subprocess.TimeoutExpired:
        return "### ⚠️ 模型下载超时\n两小时内没有完成；已有缓存会保留，下次可以继续。"
    log = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    if len(log) > 6000:
        log = log[-6000:]
    if completed.returncode == 0:
        return f"### ✅ 模型已准备完成\n```text\n{log}\n```"
    return f"### ⚠️ 模型下载未完成\n```text\n{log}\n```"


def _demucs_option_label(prefix: str = "先分离人声") -> str:
    return f"{prefix} · {inspect_demucs_runtime().short_label_zh}"


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


def _is_loopback_host(host: str) -> bool:
    normalized = (host or "").strip().strip("[]")
    if normalized.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _workspace_is_editable(workspace: WorkspaceProject) -> bool:
    try:
        document = read_lyrics(workspace.lyrics_project)
        document.require_timed()
        return bool(document.lines)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _valid_workspace_projects() -> list[WorkspaceProject]:
    return [
        workspace
        for workspace in list_workspace_projects(_default_output_root())
        if _workspace_is_editable(workspace)
    ]


def _recent_workspace_offer() -> tuple[str, str, bool]:
    workspace = load_recent_workspace(_default_output_root())
    if workspace is None or not _workspace_is_editable(workspace):
        projects = _valid_workspace_projects()
        workspace = projects[0] if projects else None
    if workspace is None:
        return "", "## 没有找到上次工程\n点击下方按钮即可进入空白制作页。", False
    message = (
        "## 要继续上次的工程吗？\n"
        f"找到了 **{html.escape(workspace.name)}**。继续后会恢复歌词、素材和"
        "无 MV / MV 预览；选择新建不会删除旧工程，之后仍可手动打开。"
    )
    return str(workspace.manifest), message, True


def _workspace_choice_label(workspace: WorkspaceProject) -> str:
    updated_at = getattr(workspace, "updated_at", None)
    timestamp = (
        updated_at.astimezone().strftime("%m-%d %H:%M")
        if updated_at
        else ""
    )
    suffix = f" · {timestamp}" if timestamp else ""
    return f"{workspace.name}{suffix} · {workspace.manifest.parent.name}"


def _workspace_dropdown_choices() -> list[tuple[str, str]]:
    return [
        (_workspace_choice_label(workspace), str(workspace.manifest))
        for workspace in _valid_workspace_projects()
    ]


def _link_source_ids(
    netease_link: str | None,
    qqmusic_link: str | None,
    utaten_link: str | None,
) -> dict[str, str]:
    values: dict[str, str] = {}
    netease_match = re.search(r"(?:[?&]id=|/song/)(\d+)", netease_link or "", re.IGNORECASE)
    if netease_match:
        values["netease"] = netease_match.group(1)
    qq_match = re.search(
        r"(?:songDetail/|[?&](?:songmid|song_mid|mid)=)([A-Za-z0-9]+)",
        qqmusic_link or "",
        re.IGNORECASE,
    )
    if qq_match:
        values["qqmusic"] = qq_match.group(1)
    utaten_match = re.search(r"/lyric/([A-Za-z0-9]+)", utaten_link or "", re.IGNORECASE)
    if utaten_match:
        values["utaten"] = utaten_match.group(1)
    return values


def _matching_workspace_manifest(
    netease_link: str | None,
    qqmusic_link: str | None,
    utaten_link: str | None,
) -> str | None:
    requested = _link_source_ids(netease_link, qqmusic_link, utaten_link)
    if len(requested) != 1:
        return None
    for workspace in _valid_workspace_projects():
        refs = (workspace.settings or {}).get("source_refs")
        if not isinstance(refs, dict):
            continue
        for provider, source_id in requested.items():
            reference = refs.get(provider)
            if isinstance(reference, dict) and str(reference.get("id") or "") == source_id:
                return str(workspace.manifest)
    return None


def create_web_app(
    *,
    managed_netease_login: bool = False,
    initial_netease_music_u: str = "",
) -> object:
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError(
            '网页依赖尚未安装。请运行 `pip install -e ".[web]"`，'
            '需要自动对齐和网易云链接时安装 `pip install -e ".[web,align,netease]"`。'
        ) from exc

    recent_manifest, recent_message, recent_available = _recent_workspace_offer()
    workspace_choices = _workspace_dropdown_choices()
    initial_music_u = initial_netease_music_u or ""
    netease_session_broker = _NeteaseSessionBroker(initial_music_u)
    if not managed_netease_login:
        initial_netease_status = (
            "远程监听模式已禁用本机账号登录；请在这台电脑上用 `启动网页版.bat` 打开。"
        )
    elif initial_music_u:
        initial_netease_status = (
            "### ✅ 已自动恢复网易云账号\n"
            "本机专用登录仍有效，本次无需再次扫码；过期后会在需要时自动重新登录。"
        )
    else:
        initial_netease_status = (
            "公开歌曲可以直接使用；已登录账号会在启动时自动恢复，"
            "过期后会在需要音频时自动打开官方登录窗口。"
        )

    with gr.Blocks(
        title=f"Karaoke Forge v{__version__}｜本地卡拉 OK 工作台",
        fill_width=True,
        delete_cache=(3600, 86400),
    ) as app:
        # Cancellation dependencies must be registered on an already queued
        # Blocks instance in Gradio 6. Launch repeats this idempotently.
        app.queue(default_concurrency_limit=1)
        make_output_directory = gr.State()
        align_output_directory = gr.State()
        netease_output_directory = gr.State()
        qqmusic_output_directory = gr.State()
        editor_payload = gr.State({})
        editor_line_undo_payload = gr.State({})
        editor_output_directory = gr.State()
        editor_handoff_ready = gr.State(False)
        netease_session_music_u = gr.State("")
        netease_login_generation = gr.State(0)
        make_preview_background = gr.State("")
        make_preview_badge = gr.State("实时字幕预览 · KTV 双行布局")
        make_preview_material_mode = gr.State(False)
        make_preview_progress = gr.State(0.4)
        make_preview_active_row = gr.State(1)
        recent_workspace_manifest = gr.State(recent_manifest)
        gr.HTML(
            f"""
            <div class="kf-shell">
              <section class="kf-hero">
                <div class="kf-kicker">
                  <span>Karaoke Forge · Local Studio</span>
                  <span class="kf-version" aria-label="当前版本 {__version__}">v{__version__}</span>
                </div>
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

        with gr.Tabs(selected="make") as main_tabs:
            with gr.Tab("制作卡拉 OK MV", id="make"), gr.Row(equal_height=False):
                with gr.Column(scale=7, min_width=340):
                    with gr.Group(
                        elem_classes=["kf-card", "kf-resume-card"],
                        visible=True,
                    ) as recent_workspace_prompt:
                        recent_workspace_message = gr.Markdown(recent_message)
                        saved_workspace_selector = gr.Dropdown(
                            label="已保存工程（自动选择最近有效工程）",
                            choices=workspace_choices,
                            value=recent_manifest or None,
                            interactive=recent_available,
                        )
                        with gr.Row():
                            continue_recent_workspace = gr.Button(
                                "载入所选工程",
                                variant="primary",
                                interactive=recent_available,
                            )
                            continue_recent_workspace_editor = gr.Button(
                                "载入并直接编辑",
                                interactive=recent_available,
                            )
                            start_blank_workspace = gr.Button(
                                "不载入，继续当前制作页",
                            )
                            refresh_saved_workspaces = gr.Button("刷新工程列表")
                    with gr.Group(elem_classes="kf-card"):
                        gr.HTML('<div class="kf-section-label">Step 01 · 素材</div>')
                        gr.Markdown("## 选择制作素材")
                        with gr.Row():
                            make_audio = gr.File(
                                label="① 歌曲音频（无 MV 时必填；有声 MV 可用其音轨）",
                                file_types=["audio"],
                                type="filepath",
                            )
                            make_video = gr.File(
                                label="② 对应 MV（可选）",
                                file_types=["video"],
                                type="filepath",
                            )
                            make_lyrics = gr.File(
                                label=(
                                    "③ 已生成歌词/字幕项目（推荐 JSON，也支持 ASS/LRC/SRT/VTT）"
                                ),
                                file_types=[".txt", ".lrc", ".srt", ".vtt", ".ass", ".json"],
                                type="filepath",
                            )
                        make_cover = gr.File(
                            label="没有 MV？上传专辑图片（可选；在线歌曲链接也会尝试读取封面）",
                            file_types=["image"],
                            type="filepath",
                        )
                        gr.Markdown(
                            "> 不上传 MV 时，可分别选择背景主题与唱片布局；5 种背景 × 5 种布局，"
                            "共 25 种组合。默认会从每张专辑封面继承颜色，并放入居中黑胶唱片机。"
                        )
                        with gr.Row():
                            make_cover_background = gr.Dropdown(
                                label="无 MV 背景主题",
                                choices=[
                                    ("专辑流光 · 自动继承每张封面颜色", "adaptive"),
                                    ("深空星环 · 蓝紫暗色舞台", "midnight"),
                                    ("日落玻璃 · 珊瑚暖色舞台", "sunset"),
                                    ("海盐极光 · 明亮青绿水光", "ocean"),
                                    ("纸艺花园 · 奶油色手工拼贴", "paper"),
                                ],
                                value="adaptive",
                            )
                            make_cover_style = gr.Dropdown(
                                label="唱片与频谱布局",
                                choices=[
                                    ("黑胶唱片机 · 大唱片、唱臂与柔焦封面", "turntable"),
                                    ("星环唱片 · 居中黑胶与双层声浪", "aurora"),
                                    ("偏置黑胶 · 悬浮唱片与脉冲窗", "vinyl"),
                                    ("环绕唱片 · 居中封面与横向声浪", "halo"),
                                    ("侧置频谱 · 左侧唱片与动态频谱", "spectrum"),
                                ],
                                value="turntable",
                            )
                            make_cover_waveform = gr.Checkbox(
                                label="显示随音乐实时变化的波形 / 频谱",
                                value=True,
                            )
                        make_lyrics_status = gr.Markdown("尚未选择歌词文件。")
                        with gr.Accordion("没有歌词文件？直接粘贴歌词", open=False):
                            make_pasted = gr.Textbox(
                                label="一行一句",
                                lines=8,
                                placeholder="第一句歌词\n第二句歌词\n第三句歌词",
                            )
                        with gr.Accordion(
                            "使用在线歌词来源（Vmoe / UtaTen / QQ 音乐 / 网易云）",
                            open=False,
                        ):
                            gr.HTML(
                                """
                                <div class="kf-tip">
                                  <b>Vmoe 卡拉 OK 字幕库</b>提供带逐字特效的 ASS。因官方接口要求
                                  reCAPTCHA，请在下方官方页面由本人完成搜索和验证，下载 ASS 后
                                  上传到上面的“③ 已生成歌词/字幕项目”；程序不会绕过验证码。
                                  <div style="margin-top:10px">
                                    <a href="https://karaoke.vmoe.info/" target="_blank"
                                       rel="noopener noreferrer">在新窗口打开 Vmoe 歌词搜索</a>
                                  </div>
                                  <details style="margin-top:10px">
                                    <summary style="cursor:pointer;font-weight:700">在这里展开 Vmoe 官方搜索页</summary>
                                    <iframe src="https://karaoke.vmoe.info/" title="Vmoe 卡拉 OK 搜索"
                                      style="width:100%;height:620px;border:1px solid #e3ded2;border-radius:12px;margin-top:8px">
                                    </iframe>
                                  </details>
                                </div>
                                """
                            )
                            make_utaten_link = gr.Textbox(
                                label="UtaTen 歌词页链接（读取歌词和页面假名）",
                                placeholder="https://utaten.com/lyric/yh15042710/",
                            )
                            make_use_utaten_lyrics = gr.Checkbox(
                                label="没有上传歌词时，直接导入 UtaTen 公开歌词和假名",
                                value=True,
                            )
                            make_utaten_pronunciation_only = gr.Checkbox(
                                label=(
                                    "有自己的歌词时，仅使用 UtaTen 官方注音（正文和时间轴保持不变）"
                                ),
                                value=False,
                            )
                            gr.Markdown(
                                "勾选后会先清除歌词文件中原有的注音，再按正文相似度和字符片段"
                                "转移 UtaTen ruby；对不上的文字保持无注音，不会自动猜读音。"
                            )
                            gr.Markdown("---\n**QQ 音乐（公开行级 LRC 与翻译）**")
                            make_qqmusic_link = gr.Textbox(
                                label="QQ 音乐单曲链接（只读取公开歌词，不下载音频）",
                                placeholder="https://y.qq.com/n/ryqq_v2/songDetail/...",
                            )
                            make_use_qqmusic_lyrics = gr.Checkbox(
                                label="没有上传歌词时，使用 QQ 音乐公开 LRC",
                                value=True,
                            )
                            gr.Markdown("---\n**网易云（可选音频与歌词）**")
                            make_netease_link = gr.Textbox(
                                label="网易云单曲链接",
                                placeholder="https://music.163.com/song?id=...",
                            )
                            make_netease_login_button = gr.Button(
                                "一键登录 / 连接网易云账号",
                                variant="primary",
                                interactive=managed_netease_login,
                            )
                            make_reset_netease_login = gr.Button(
                                "登录失效或要换账号？重新登录",
                                size="sm",
                                interactive=managed_netease_login,
                            )
                            make_netease_login_status = gr.Markdown(
                                initial_netease_status
                            )
                            gr.Markdown(
                                "会打开一个 **Karaoke Forge 专用 Edge 窗口**。请在网易云官网"
                                "正常扫码或登录，成功后窗口会自动关闭；不用退出平时的 Edge，"
                                "也不用安装 Firefox 或打开 F12。第一次登录后通常会记住账号。"
                            )
                            with gr.Accordion("高级 / 兼容登录方式（通常不用展开）", open=False):
                                with gr.Row():
                                    make_cookie_browser = gr.Dropdown(
                                        label="旧版：读取已完全退出的浏览器",
                                        choices=[
                                            ("匿名（仅公开音频）", ""),
                                            ("Chrome 已登录账号", "chrome"),
                                            ("Edge 已登录账号", "edge"),
                                            ("Firefox 已登录账号", "firefox"),
                                            ("Brave 已登录账号", "brave"),
                                        ],
                                        value="",
                                        interactive=managed_netease_login,
                                        info=("只用于兼容旧流程；一键登录成功后会自动忽略此项。"),
                                    )
                                    make_cookie_profile = gr.Textbox(
                                        label="浏览器配置（旧版可选）",
                                        interactive=managed_netease_login,
                                        placeholder=(
                                            "留空使用默认配置；也可填 Profile 1 或完整用户配置目录"
                                        ),
                                    )
                                make_music_u = gr.Textbox(
                                    label="手动 MUSIC_U（仅排障备用）",
                                    type="password",
                                    placeholder="粘贴 Value，或 MUSIC_U=...",
                                    info=(
                                        "填写后会优先使用此会话，不再读取浏览器数据库；"
                                        "仅在本机内存中按需使用。"
                                    ),
                                )
                                gr.Markdown(
                                    "1. 在 Edge 打开并登录 `music.163.com`；"
                                    "2. 按 **F12 → Application/应用 → Cookies → "
                                    "https://music.163.com**；"
                                    "3. 找到 `MUSIC_U`，复制 **Value** 粘贴到上方。  \n"
                                    "`MUSIC_U` 等同登录凭据，请勿截图或分享；"
                                    "程序不会把它写入工程、输出文件或日志。"
                                )
                                make_clear_netease_login = gr.Button(
                                    "退出账号并清除专用登录数据",
                                    size="sm",
                                    interactive=managed_netease_login,
                                )
                            make_use_netease_lyrics = gr.Checkbox(
                                label="没有上传歌词时，使用网易云页面公开歌词",
                                value=True,
                            )
                            make_rights = gr.Checkbox(
                                label=(
                                    "我确认歌曲和歌词归我合法使用，且不会绕过地区、版权、"
                                    "验证码或 DRM 限制"
                                ),
                                value=False,
                            )
                            gr.Markdown(
                                "> 一键登录只打开网易云官网，程序不接收密码。专用 Edge 会在本机"
                                "保留登录状态，方便下次连接；程序只使用账号实际有权播放的音质，"
                                "不会把凭据写入工程、成片或日志，也不转换 NCM。"
                            )
                        gr.HTML(
                            '<div class="kf-tip">歌曲和 MV 应是同一版本。'
                            "未上传独立音频时会自动检测并使用 MV 内嵌音轨。"
                            "普通 LRC/SRT 在“自动”模式下会用 Whisper 精修逐字时间；"
                            "选择“关闭”可完全保留原时间轴。</div>"
                        )

                    with gr.Group(elem_classes="kf-card"):
                        gr.HTML('<div class="kf-section-label">Step 02 · 效果</div>')
                        gr.Markdown("## 选择字幕外观")
                        with gr.Row():
                            make_name = gr.Textbox(
                                label="成品名称",
                                value="",
                                placeholder="留空时自动采用在线歌名或素材文件名",
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
                        make_font_files = gr.File(
                            label="导入自定义字体（TTF / OTF / TTC，可多选；会随工程保存）",
                            file_types=[".ttf", ".otf", ".ttc"],
                            file_count="multiple",
                            type="filepath",
                        )
                        gr.Markdown(
                            "> 字体名称一般填写字体文件显示的家族名；上传后会先用首个文件名自动填入，"
                            "若预览不一致可手动修改。请只使用有授权的字体。"
                        )
                        with gr.Row():
                            make_text_color = gr.ColorPicker(label="未唱颜色", value="#FFFFFF")
                            make_highlight_color = gr.ColorPicker(
                                label="唱到的颜色", value="#FFD54A"
                            )
                        with gr.Row():
                            make_show_translation = gr.Checkbox(
                                label="有中文翻译时显示翻译",
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
                            make_translation_margin = gr.Slider(
                                16,
                                760,
                                value=54,
                                step=2,
                                label="翻译距顶部",
                                info="按最终 1080p 字幕坐标计算；数值越大，翻译越靠下。",
                            )
                        with gr.Row():
                            make_show_pronunciation = gr.Checkbox(
                                label="显示日语振假名和英语片假名读音",
                                value=True,
                            )
                            make_auto_english_pronunciation = gr.Checkbox(
                                label="显示英语片假名（关闭后也会过滤旧工程和已导入的英文注音）",
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
                        with gr.Row():
                            make_show_countdown = gr.Checkbox(
                                label="长间奏结束前显示三点开唱提示",
                                value=True,
                            )
                            make_countdown_gap = gr.Slider(
                                5,
                                20,
                                value=8,
                                step=1,
                                label="视为长间奏的空档（秒）",
                                info="超过此长度会清空字幕，并在下一句前 3 秒给出提示。",
                            )

                        with gr.Accordion("歌曲实景字幕预览", open=True):
                            make_preview_status = gr.Markdown(
                                "上传 MV，或上传音频和封面后，会自动换成这首歌的真实画面；"
                                "加入歌词后也会自动挑选对应时刻的一句。"
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
                            gr.Markdown(
                                "<small>画面、歌词和时间点来自当前素材；颜色、位置会即时更新。"
                                "浏览器未安装的自定义字体在这里可能近似显示，最终成片会嵌入上传字体。</small>"
                            )
                            with gr.Accordion("手动更换预览歌词（可选）", open=False):
                                with gr.Row():
                                    make_preview_text = gr.Textbox(
                                        label="原文双行预览（画面上排 + 下排）",
                                        value=_PREVIEW_DEFAULT_TEXT,
                                        lines=2,
                                    )
                                    make_preview_translation = gr.Textbox(
                                        label="翻译预览",
                                        value=_PREVIEW_DEFAULT_TRANSLATION,
                                    )

                        with gr.Accordion("高级设置", open=False):
                            with gr.Row():
                                make_model = gr.Dropdown(
                                    label="识别档位 / 模型",
                                    choices=_ALIGNMENT_MODEL_CHOICES,
                                    value="profile:balanced",
                                    info=_ALIGNMENT_MODEL_INFO,
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
                                    label=_demucs_option_label("先分离人声（复杂伴奏可尝试）"),
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
                                        ("强制检查：仅采纳可靠修正", "force"),
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
                    gr.Markdown("**最终导出版本（可单选，也可同时选择）**")
                    with gr.Row():
                        make_export_original = gr.Checkbox(
                            label="导出原声版",
                            value=True,
                        )
                        make_export_instrumental = gr.Checkbox(
                            label=_demucs_option_label("导出无人声伴奏版"),
                            value=False,
                        )
                    with gr.Row():
                        make_prepare_button = gr.Button(
                            "① 生成可校准 KTV 工程",
                            variant="primary",
                            elem_classes="kf-primary",
                        )
                        make_button = gr.Button(
                            "② 生成所选卡拉 OK MV",
                            variant="secondary",
                        )
                    make_open_editor_button = gr.Button(
                        "打开当前歌词工程继续微调",
                        size="sm",
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
                            label="识别档位 / 模型",
                            choices=_ALIGNMENT_MODEL_CHOICES,
                            value="profile:balanced",
                            info=_ALIGNMENT_MODEL_INFO,
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
                            label=_demucs_option_label(),
                            value=False,
                        )
                        align_timing_refinement = gr.Dropdown(
                            label="已有时间轴时的逐字精修",
                            choices=[
                                ("关闭：完全保留输入时间", "off"),
                                ("自动：只精修行级/合成时间", "auto"),
                                ("强制检查：仅采纳可靠修正", "force"),
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
                            label="识别档位 / 模型",
                            choices=_ALIGNMENT_MODEL_CHOICES,
                            value="profile:balanced",
                            info=_ALIGNMENT_MODEL_INFO,
                        )
                    netease_use_page_lyrics = gr.Checkbox(
                        label="没有提供自己的歌词时，使用网易云页面公开 LRC",
                        value=True,
                    )
                    netease_login_button = gr.Button(
                        "一键登录 / 连接网易云账号",
                        variant="primary",
                        interactive=managed_netease_login,
                    )
                    netease_reset_login = gr.Button(
                        "登录失效或要换账号？重新登录",
                        size="sm",
                        interactive=managed_netease_login,
                    )
                    netease_login_status = gr.Markdown(
                        initial_netease_status
                    )
                    gr.Markdown(
                        "会打开一个 **Karaoke Forge 专用 Edge 窗口**。请在网易云官网"
                        "正常扫码或登录，成功后窗口会自动关闭；不用退出平时的 Edge，"
                        "也不用安装 Firefox 或打开 F12。第一次登录后通常会记住账号。"
                    )
                    with gr.Accordion("高级 / 兼容登录方式（通常不用展开）", open=False):
                        with gr.Row():
                            netease_cookie_browser = gr.Dropdown(
                                label="旧版：读取已完全退出的浏览器",
                                choices=[
                                    ("匿名（仅公开音频）", ""),
                                    ("Chrome 已登录账号", "chrome"),
                                    ("Edge 已登录账号", "edge"),
                                    ("Firefox 已登录账号", "firefox"),
                                    ("Brave 已登录账号", "brave"),
                                ],
                                value="",
                                interactive=managed_netease_login,
                                info="只用于兼容旧流程；一键登录成功后会自动忽略此项。",
                            )
                            netease_cookie_profile = gr.Textbox(
                                label="浏览器配置（旧版可选）",
                                interactive=managed_netease_login,
                                placeholder=(
                                    "留空使用默认配置；也可填 Profile 1 或完整用户配置目录"
                                ),
                            )
                        netease_music_u = gr.Textbox(
                            label="手动 MUSIC_U（仅排障备用）",
                            type="password",
                            placeholder="粘贴 Value，或 MUSIC_U=...",
                            info=(
                                "填写后会优先使用此会话，不再读取浏览器数据库；"
                                "仅在本机内存中按需使用。"
                            ),
                        )
                        gr.Markdown(
                            "1. 在 Edge 打开并登录 `music.163.com`；"
                            "2. 按 **F12 → Application/应用 → Cookies → "
                            "https://music.163.com**；"
                            "3. 找到 `MUSIC_U`，复制 **Value** 粘贴到上方。  \n"
                            "`MUSIC_U` 等同登录凭据，请勿截图或分享；"
                            "程序不会把它写入工程、输出文件或日志。"
                        )
                        netease_clear_login = gr.Button(
                            "退出账号并清除专用登录数据",
                            size="sm",
                            interactive=managed_netease_login,
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
                        netease_separate = gr.Checkbox(
                            label=_demucs_option_label(),
                            value=False,
                        )
                        netease_keep_audio = gr.Checkbox(
                            label="保留本次获取的音频文件",
                            value=False,
                        )
                        netease_timing_refinement = gr.Dropdown(
                            label="逐字时间精修",
                            choices=[
                                ("关闭：完全保留网易云时间", "off"),
                                ("自动：保留 YRC，精修普通 LRC", "auto"),
                                ("强制检查：仅采纳可靠修正", "force"),
                            ],
                            value="auto",
                        )
                    gr.Markdown(
                        "> 一键登录只打开网易云官网，程序不接收密码。专用 Edge 会在本机"
                        "保留登录状态，方便下次连接；程序只使用账号有权播放的最高音质，"
                        "不会把凭据写入工程、输出或日志，也不会转换 NCM。"
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

            with gr.Tab("QQ 音乐生成歌词", id="qqmusic"), gr.Row(equal_height=False):
                with gr.Column(scale=7), gr.Group(elem_classes="kf-card"):
                    gr.HTML('<div class="kf-section-label">QQ Music Lyrics</div>')
                    gr.Markdown("## 从 QQ 音乐单曲链接生成时间轴歌词")
                    qqmusic_link = gr.Textbox(
                        label="QQ 音乐单曲链接",
                        placeholder=(
                            "可粘贴完整分享文字，或 https://y.qq.com/n/ryqq_v2/songDetail/..."
                        ),
                    )
                    qqmusic_name = gr.Textbox(label="输出名称", value="QQ音乐歌词时间轴")
                    qqmusic_rights = gr.Checkbox(
                        label="我确认有权使用和处理对应歌曲与歌词",
                        value=False,
                    )
                    with gr.Accordion("高级设置", open=False):
                        qqmusic_output_root = gr.Textbox(
                            label="输出目录",
                            value=str(_default_output_root()),
                        )
                    gr.Markdown(
                        "> 此入口只读取 QQ 音乐网页公开的歌曲信息、行级 LRC 和可用翻译，"
                        "不请求音频、不读取账号或 Cookie。普通 LRC 可在制作页结合本地音频"
                        "进一步精修为逐字时间。"
                    )
                    qqmusic_button = gr.Button(
                        "读取公开歌词并导出",
                        variant="primary",
                        elem_classes="kf-primary",
                    )
                with gr.Column(scale=5), gr.Group(elem_classes="kf-card"):
                    qqmusic_status = gr.Markdown("### 等待 QQ 音乐单曲链接")
                    qqmusic_downloads = gr.File(
                        label="下载时间轴歌词",
                        file_count="multiple",
                    )
                    qqmusic_log = gr.Textbox(
                        label="处理记录",
                        lines=10,
                        interactive=False,
                    )
                    open_qqmusic_dir = gr.Button("在电脑中打开输出文件夹")
                    open_qqmusic_message = gr.Markdown()

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
                    with gr.Row(elem_id="editor-mode-bar"):
                        editor_timing_mode = gr.Radio(
                            choices=[
                                ("逐句逐词精修", "line"),
                                ("全局连续调整", "global"),
                            ],
                            value="line",
                            label="编辑模式",
                            interactive=True,
                            scale=2,
                            elem_id="editor-timing-mode",
                        )
                        editor_ripple_following = gr.Checkbox(
                            label="本句句尾越过下一句时，联动后续歌词（合唱可关闭）",
                            value=True,
                            interactive=True,
                            scale=3,
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
                                        label="带时间轴歌词或 Karaoke Forge 歌词 JSON",
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
                                    value="",
                                    placeholder="留空时沿用工程歌名",
                                )
                                editor_export = gr.Button(
                                    "保存并导出全部格式（同时载入制作页）",
                                    variant="primary",
                                    elem_classes="kf-primary",
                                )
                                editor_handoff = gr.Button(
                                    "确认校准并开始制作 MV",
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
                            with gr.Column(elem_id="editor-audio-panel") as editor_audio_panel:
                                editor_line_audio = gr.Audio(
                                    label="当前句试听",
                                    interactive=False,
                                    autoplay=True,
                                    loop=False,
                                    elem_id="editor-line-audio",
                                )
                                editor_audio_refresh_trigger = gr.Textbox(
                                    value="",
                                    visible=False,
                                )
                            editor_timing_status = gr.Markdown(
                                "从歌词总览选择一句即可自动播放。",
                                elem_id="editor-timing-status",
                            )
                            with gr.Column(
                                visible=False,
                                elem_id="editor-global-mode-panel",
                            ) as editor_global_mode_panel:
                                gr.Markdown(
                                    "### 全局连续时间轴\n"
                                    "整首音频只加载一次；点击句块或拖动红色播放头即可连续定位，"
                                    "不再为每句重新生成试听片段。点击任一句后，可直接在下方继续"
                                    "精修这句的每个字。"
                                )
                                editor_global_audio = gr.Audio(
                                    label="全曲连续试听",
                                    interactive=False,
                                    autoplay=False,
                                    loop=False,
                                    elem_id="editor-global-audio",
                                )
                                editor_global_timeline = gr.HTML(
                                    '<div class="kf-tip">载入工程后，这里会显示全曲时间轴。</div>',
                                    elem_id="editor-global-timeline",
                                )
                                editor_global_line_request = gr.Number(
                                    value=1,
                                    precision=0,
                                    elem_id="kf-global-line-request",
                                )
                                editor_global_select_line = gr.Button(
                                    "选择全局时间轴句子",
                                    elem_id="kf-global-select-line",
                                )
                                editor_global_edge_request = gr.Textbox(
                                    value="",
                                    elem_id="kf-global-edge-request",
                                )
                                editor_global_edge_apply = gr.Button(
                                    "应用全局时间轴句界",
                                    elem_id="kf-global-edge-apply",
                                )
                                editor_apply_global_rows = gr.Button(
                                    "保存总览中的全局时间修改",
                                    variant="secondary",
                                )
                                with gr.Row():
                                    editor_global_scope = gr.Radio(
                                        choices=[
                                            ("整首歌", "all"),
                                            ("当前句及之后", "suffix"),
                                        ],
                                        value="all",
                                        label="平移范围",
                                        interactive=True,
                                    )
                                    editor_global_offset = gr.Number(
                                        value=0.1,
                                        precision=3,
                                        label="平移秒数（可为负）",
                                    )
                                    editor_apply_global_shift = gr.Button(
                                        "应用全局平移",
                                        variant="primary",
                                        elem_classes="kf-primary",
                                    )
                            with gr.Column(elem_id="editor-token-tuning-panel"):
                                gr.Markdown(
                                    "### 当前句逐字微调\n"
                                    "全局模式中点击上方任一句即可载入；可改字、删字、拖动黄色"
                                    "边界，并用红线定位到整曲中的真实时间。"
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
                                with gr.Row(
                                    elem_id="editor-timing-actions"
                                ) as editor_timing_actions:
                                    editor_save_tokens = gr.Button(
                                        "保存逐词时间",
                                        variant="primary",
                                        elem_classes="kf-primary",
                                        elem_id="editor-save-tokens",
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
                                        label="当前行",
                                        value=1,
                                        precision=0,
                                        minimum=1,
                                        interactive=False,
                                        elem_id="editor-current-line",
                                    )
                                    editor_load_line = gr.Button("重新载入")
                                    editor_listen_line = gr.Button("试听")
                            with gr.Row():
                                editor_toggle_line_hidden = gr.Button("👁 隐藏 / 显示")
                                editor_delete_line = gr.Button("🗑 删除")
                                editor_undo_line_action = gr.Button("↶ 撤销 / 重做（全工程）")
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
                                editor_loop_line = gr.Checkbox(
                                    label="循环当前句",
                                    value=False,
                                    interactive=True,
                                    elem_id="editor-loop-line",
                                )
                                gr.Slider(
                                    minimum=0.5,
                                    maximum=2.0,
                                    value=1.0,
                                    step=0.5,
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
                    (
                        initial_model_mode,
                        initial_proxy_url,
                        initial_mirror_confirmed,
                        initial_model_network_status,
                    ) = _model_network_form_defaults()
                    gr.Markdown("### AI 模型下载向导")
                    gr.Markdown(
                        "不懂代理时先点自动探测。程序会先试国内 ModelScope 直连，"
                        "下载文件通过内置 SHA-256 校验后才加载；再尝试官方源和本机代理。"
                    )
                    model_network_mode = gr.Radio(
                        label="下载方式",
                        choices=[
                            ("国内直连 · ModelScope 魔搭（推荐）", "modelscope"),
                            ("Hugging Face 官方源", "official"),
                            ("本机 HTTP 代理", "proxy"),
                            ("hf-mirror 第三方镜像", "mirror"),
                            ("完全离线，只读缓存", "offline"),
                        ],
                        value=initial_model_mode,
                    )
                    model_proxy_url = gr.Textbox(
                        label="本机代理地址（仅代理模式使用）",
                        value=initial_proxy_url,
                        placeholder="http://127.0.0.1:7890",
                        info="不保存账号或密码；Clash 常见端口为 7890 / 7897。",
                    )
                    model_mirror_confirmed = gr.Checkbox(
                        label=(
                            "我明白 hf-mirror 是第三方服务，并明确同意仅用它下载公开模型"
                        ),
                        value=initial_mirror_confirmed,
                    )
                    with gr.Row():
                        auto_model_network = gr.Button("自动探测（推荐）", variant="primary")
                        save_model_network = gr.Button("保存并测试所选方式")
                    model_network_status = gr.Markdown(initial_model_network_status)
                    with gr.Row():
                        model_prefetch_profile = gr.Dropdown(
                            label="提前下载模型（可选）",
                            choices=[
                                ("快速 · small", "fast"),
                                ("均衡 · large-v3-turbo", "balanced"),
                                ("KTV 精准 · large-v3", "precise"),
                            ],
                            value="fast",
                        )
                        predownload_model = gr.Button("下载所选模型")
                with gr.Column(scale=5), gr.Group(elem_classes="kf-card"):
                    gr.Markdown(
                        """
                                ### 第一次使用

                                1. Windows 用户先双击项目根目录的 `首次安装.bat`。
                                2. 安装完成后双击 `启动网页版.bat`。
                                3. 安装向导会自动准备私有 FFmpeg，并让你选择模型下载方式；
                                   也可稍后在左侧重新测试或提前下载模型。

                                ### 常见情况

                                - **只有格式转换需求**：不需要 Whisper。
                                - **歌词已有时间轴**：YRC 等真实逐字时间在“自动”模式下会直接
                                  使用；普通 LRC/SRT 会运行 Whisper 精修，选择“关闭”则完全
                                  保留原时间轴且无需模型。
                                - **网易云会员歌曲**：点击“一键登录 / 连接网易云账号”，在弹出的
                                  Karaoke Forge 专用 Edge 窗口登录即可；平时的 Edge 不用关闭。
                                  也可上传标准音频；不支持 NCM。
                                - **匹配率低**：确认歌词与歌曲是同一版本，或尝试分离人声。
                                - **模型无法下载**：先点左侧“自动探测”；它会依次尝试国内
                                  ModelScope、官方源和本机代理，不会自行改用 hf-mirror。
                                - **字幕没有中文字体**：在样式里换成本机已安装字体。
                                """
                    )

        gr.HTML(
            f'<div class="kf-footer">Karaoke Forge v{__version__} · 本地处理 · '
            "请确保你拥有歌曲、歌词和视频的使用权</div>"
        )
        app.load(fn=None, js=TOKEN_TIMELINE_JS, queue=False)

        def refresh_recent_workspace_offer() -> tuple[object, ...]:
            manifest, message, available = _recent_workspace_offer()
            choices = _workspace_dropdown_choices()
            return (
                manifest,
                gr.update(
                    choices=choices,
                    value=manifest or None,
                    interactive=available,
                ),
                message,
                gr.update(visible=True),
                gr.update(interactive=available),
                gr.update(interactive=available),
            )

        app.load(
            refresh_recent_workspace_offer,
            outputs=[
                recent_workspace_manifest,
                saved_workspace_selector,
                recent_workspace_message,
                recent_workspace_prompt,
                continue_recent_workspace,
                continue_recent_workspace_editor,
            ],
            queue=False,
        )
        refresh_saved_workspaces.click(
            refresh_recent_workspace_offer,
            outputs=[
                recent_workspace_manifest,
                saved_workspace_selector,
                recent_workspace_message,
                recent_workspace_prompt,
                continue_recent_workspace,
                continue_recent_workspace_editor,
            ],
            queue=False,
        )

        def select_saved_workspace(manifest: str | None) -> tuple[object, ...]:
            if not manifest:
                return (
                    "",
                    "## 没有选择工程\n请选择一个已保存工程，或新建空白工程。",
                    gr.update(interactive=False),
                    gr.update(interactive=False),
                )
            try:
                workspace = load_workspace_project(manifest)
                if not _workspace_is_editable(workspace):
                    raise ValueError("工程歌词已损坏、为空，或缺少完整时间轴。")
                message = (
                    f"## 已选择 **{html.escape(workspace.name)}**\n"
                    "可以载入制作页，或直接进入歌词编辑器；切换不会删除任何旧工程。"
                )
                available = True
            except Exception as exc:
                message = f"## 工程暂时不可用\n{html.escape(str(exc))}"
                available = False
            return (
                manifest,
                message,
                gr.update(interactive=available),
                gr.update(interactive=available),
            )

        saved_workspace_selector.change(
            select_saved_workspace,
            inputs=saved_workspace_selector,
            outputs=[
                recent_workspace_manifest,
                recent_workspace_message,
                continue_recent_workspace,
                continue_recent_workspace_editor,
            ],
            queue=False,
        )

        def netease_login_button_updates(*, interactive: bool) -> tuple[object, ...]:
            return tuple(gr.update(interactive=interactive) for _index in range(4))

        def remote_netease_login_result() -> tuple[object, ...]:
            status = (
                "### ⚠️ 远程监听模式不能使用本机网易云账号\n"
                "请在这台电脑上用 `启动网页版.bat` 打开本地页面；"
                "远程页面仍可使用公开歌曲或自己上传的音频。"
            )
            return (
                "",
                "",
                "",
                status,
                status,
                *netease_login_button_updates(interactive=False),
            )

        def local_netease_login_request(request: object | None) -> bool:
            if not managed_netease_login:
                return False
            if request is None:
                return True
            client = getattr(request, "client", None)
            return _is_loopback_host(str(getattr(client, "host", "")))

        def ensure_netease_session_for_download(
            link: str,
            audio_file: object,
            video_file: object,
            rights_confirmed: bool,
            current_music_u: str,
            request: object | None = None,
        ) -> tuple[object, ...]:
            if not local_netease_login_request(request):
                supplied_music_u = (current_music_u or "").strip()
                safe_music_u = (
                    ""
                    if netease_session_broker.recognizes_managed(supplied_music_u)
                    else supplied_music_u
                )
                return safe_music_u, gr.skip(), gr.skip()
            if not (link or "").strip() or not rights_confirmed:
                return current_music_u or "", gr.skip(), gr.skip()
            audio = _file_path(audio_file)
            if audio is not None and audio.is_file() and not _is_empty_audio_placeholder(audio):
                return current_music_u or "", gr.skip(), gr.skip()
            video = _file_path(video_file)
            if video is not None and video.is_file() and probe_media_has_audio(video) is True:
                return current_music_u or "", gr.skip(), gr.skip()
            supplied_music_u = (current_music_u or "").strip()
            if supplied_music_u and not netease_session_broker.recognizes_managed(
                supplied_music_u
            ):
                # The advanced MUSIC_U field is intentionally session-scoped and is not
                # copied into the managed, cross-session login broker.
                return supplied_music_u, gr.skip(), gr.skip()
            try:
                reuse_generation = netease_session_broker.begin_profile_reuse()
                if reuse_generation is None:
                    return "", gr.skip(), gr.skip()
                if not managed_netease_profile_exists():
                    netease_session_broker.clear_managed()
                    return "", gr.skip(), gr.skip()
                token = acquire_netease_music_u()
            except NeteaseLoginError as exc:
                status = (
                    "### ⚠️ 网易云登录没有完成\n"
                    f"{html.escape(str(exc))}\n\n将继续尝试公开音频；也可以稍后再点一键登录。"
                )
                return "", status, status
            except Exception as exc:
                _record_web_error("netease-session-refresh", exc)
                status = (
                    "### ⚠️ 网易云登录没有完成\n"
                    "将继续尝试公开音频；如果歌曲需要账号权限，请稍后再点一键登录。"
                )
                return "", status, status
            if not netease_session_broker.commit_profile_reuse(
                token,
                reuse_generation,
            ):
                status = (
                    "### ℹ️ 已按退出操作断开网易云账号\n"
                    "登录检查完成前账号已被退出，本次结果已安全丢弃。"
                )
                return "", status, status
            status = (
                "### ✅ 网易云账号已自动确认 / 重新连接\n"
                "仍有效的登录会直接续用；若旧登录过期，官方登录窗口会完成更新。"
            )
            return token, status, status

        ensure_netease_session_for_download.__annotations__["request"] = gr.Request

        def ensure_netease_session_without_video(
            link: str,
            audio_file: object,
            rights_confirmed: bool,
            current_music_u: str,
            request: object | None = None,
        ) -> tuple[object, ...]:
            return ensure_netease_session_for_download(
                link,
                audio_file,
                None,
                rights_confirmed,
                current_music_u,
                request,
            )

        ensure_netease_session_without_video.__annotations__["request"] = gr.Request

        def begin_netease_login(request: object | None = None) -> tuple[object, ...]:
            if not local_netease_login_request(request):
                result = remote_netease_login_result()
                return gr.skip(), *result[3:]
            login_generation = netease_session_broker.begin_explicit_login()
            status = (
                "### ⏳ 正在打开网易云登录窗口\n"
                "请在弹出的 Karaoke Forge 专用 Edge 窗口中正常扫码或登录。"
                "检测成功后窗口会自动关闭。"
            )
            return (
                login_generation,
                status,
                status,
                *netease_login_button_updates(interactive=False),
            )

        def begin_netease_relogin(request: object | None = None) -> tuple[object, ...]:
            if not local_netease_login_request(request):
                return gr.skip(), *remote_netease_login_result()
            login_generation = netease_session_broker.begin_explicit_login(
                disable_existing=True
            )
            status = (
                "### ⏳ 正在重新连接网易云账号\n"
                "旧会话将被清理，请在弹出的专用 Edge 窗口中完成官方登录。"
            )
            return (
                login_generation,
                "",
                "",
                "",
                status,
                status,
                *netease_login_button_updates(interactive=False),
            )

        def capture_netease_login_wrapper(
            login_generation: int,
            request: object | None = None,
        ) -> tuple[object, ...]:
            if not local_netease_login_request(request):
                return remote_netease_login_result()
            try:
                token = capture_netease_music_u()
            except NeteaseLoginError as exc:
                detail = html.escape(str(exc))
                status = (
                    "### ⚠️ 网易云账号尚未连接\n"
                    f"{detail}\n\n可以再点一次重试；受管电脑若禁止 Edge 调试，"
                    "仍可展开“高级 / 兼容登录方式”使用旧方法。"
                )
                return (
                    gr.skip(),
                    gr.skip(),
                    gr.skip(),
                    status,
                    status,
                    *netease_login_button_updates(interactive=True),
                )
            except Exception as exc:
                _record_web_error("netease-edge-login", exc)
                status = (
                    "### ⚠️ 网易云登录窗口没有完成连接\n"
                    "程序已记录错误详情。请重试，或使用高级兼容登录方式。"
                )
                return (
                    gr.skip(),
                    gr.skip(),
                    gr.skip(),
                    status,
                    status,
                    *netease_login_button_updates(interactive=True),
                )

            if not netease_session_broker.commit_explicit_login(
                token,
                int(login_generation),
            ):
                status = (
                    "### ℹ️ 已按退出操作断开网易云账号\n"
                    "登录完成前账号已被退出，本次登录结果已安全丢弃。"
                )
                return (
                    "",
                    "",
                    "",
                    status,
                    status,
                    *netease_login_button_updates(interactive=True),
                )
            status = (
                "### ✅ 网易云账号已连接\n"
                "登录会话只保存在本机服务端，已接入制作页和网易云歌词页。"
                "如果之后提示登录失效，请点击“重新登录”。"
            )
            return (
                token,
                "",
                "",
                status,
                status,
                *netease_login_button_updates(interactive=True),
            )

        def clear_netease_login_wrapper(request: object | None = None) -> tuple[object, ...]:
            if not local_netease_login_request(request):
                return remote_netease_login_result()
            netease_session_broker.clear_managed()
            try:
                detail = clear_netease_login_profile()
            except NeteaseLoginError as exc:
                status = (
                    "### ⚠️ 已断开当前账号，但专用登录资料暂时无法完全清理\n"
                    f"{html.escape(str(exc))}"
                )
                return (
                    "",
                    "",
                    "",
                    status,
                    status,
                    *netease_login_button_updates(interactive=True),
                )

            status = f"### ✅ 已退出网易云账号\n{html.escape(detail)}"
            return (
                "",
                "",
                "",
                status,
                status,
                *netease_login_button_updates(interactive=True),
            )

        def relogin_netease_wrapper(
            login_generation: int,
            request: object | None = None,
        ) -> tuple[object, ...]:
            if not local_netease_login_request(request):
                return remote_netease_login_result()
            try:
                clear_netease_login_profile()
            except NeteaseLoginError as exc:
                status = f"### ⚠️ 暂时无法重新登录\n{html.escape(str(exc))}"
                return (
                    "",
                    "",
                    "",
                    status,
                    status,
                    *netease_login_button_updates(interactive=True),
                )
            return capture_netease_login_wrapper(login_generation, request)

        def store_manual_music_u(value: str) -> str:
            return value or ""

        for request_callback in (
            begin_netease_login,
            begin_netease_relogin,
            capture_netease_login_wrapper,
            clear_netease_login_wrapper,
            relogin_netease_wrapper,
        ):
            request_callback.__annotations__["request"] = gr.Request

        netease_login_outputs = [
            netease_session_music_u,
            make_music_u,
            netease_music_u,
            make_netease_login_status,
            netease_login_status,
            make_netease_login_button,
            netease_login_button,
            make_reset_netease_login,
            netease_reset_login,
        ]
        for manual_music_u in (make_music_u, netease_music_u):
            manual_music_u.input(
                store_manual_music_u,
                inputs=manual_music_u,
                outputs=netease_session_music_u,
                queue=False,
                show_progress="hidden",
            )

        for login_button in (make_netease_login_button, netease_login_button):
            login_button.click(
                begin_netease_login,
                outputs=[
                    netease_login_generation,
                    make_netease_login_status,
                    netease_login_status,
                    make_netease_login_button,
                    netease_login_button,
                    make_reset_netease_login,
                    netease_reset_login,
                ],
                queue=False,
            ).then(
                capture_netease_login_wrapper,
                inputs=netease_login_generation,
                outputs=netease_login_outputs,
                show_progress="hidden",
                concurrency_limit=1,
                concurrency_id="netease-managed-login",
            )

        for reset_login_button in (make_reset_netease_login, netease_reset_login):
            reset_login_button.click(
                begin_netease_relogin,
                outputs=[netease_login_generation, *netease_login_outputs],
                queue=False,
            ).then(
                relogin_netease_wrapper,
                inputs=netease_login_generation,
                outputs=netease_login_outputs,
                show_progress="hidden",
                concurrency_limit=1,
                concurrency_id="netease-managed-login",
            )

        for clear_login_button in (make_clear_netease_login, netease_clear_login):
            clear_login_button.click(
                clear_netease_login_wrapper,
                outputs=netease_login_outputs,
                show_progress="hidden",
                concurrency_limit=1,
                concurrency_id="netease-managed-login",
            )

        def local_browser_cookie_inputs(browser: str, profile: str) -> tuple[str, str]:
            if managed_netease_login:
                return browser, profile
            return "", ""

        def netease_credentials_for_request(
            browser: str,
            profile: str,
            music_u: str,
            request: object | None,
        ) -> tuple[str, str, str]:
            safe_music_u = (
                music_u if netease_session_broker.managed_token_allowed(music_u) else ""
            )
            if not local_netease_login_request(request):
                if netease_session_broker.recognizes_managed(safe_music_u):
                    safe_music_u = ""
                return "", "", safe_music_u
            browser, profile = local_browser_cookie_inputs(browser, profile)
            return browser, profile, safe_music_u

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
            music_u: str,
            auto_sync: bool,
            timing_refinement: str,
            show_translation: bool,
            translation_font_size: float,
            translation_color: str,
            show_pronunciation: bool,
            pronunciation_font_size: float,
            pronunciation_color: str,
            output_root: str,
            qqmusic_link: str,
            use_qqmusic_lyrics: bool,
            utaten_link: str,
            use_utaten_lyrics: bool,
            utaten_pronunciation_only: bool,
            auto_english_pronunciation: bool,
            cover: object,
            font_files: object,
            cover_background: str,
            cover_style: str,
            cover_waveform: bool,
            export_original: bool,
            export_instrumental: bool,
            translation_margin_v: float,
            show_countdown: bool,
            countdown_gap_threshold: float,
            request: object | None = None,
            progress: object = gr.Progress(),
        ) -> tuple[str, str | None, list[str], str, str | None]:
            def update(message: str) -> None:
                progress((0, None), desc=message)

            cookie_browser, cookie_browser_profile, music_u = netease_credentials_for_request(
                cookie_browser,
                cookie_browser_profile,
                music_u,
                request,
            )
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
                music_u,
                auto_sync,
                timing_refinement,
                show_translation,
                translation_font_size,
                translation_color,
                show_pronunciation,
                pronunciation_font_size,
                pronunciation_color,
                output_root,
                qqmusic_link,
                use_qqmusic_lyrics,
                utaten_link,
                use_utaten_lyrics,
                utaten_pronunciation_only,
                auto_english_pronunciation,
                cover,
                font_files,
                cover_background,
                cover_style,
                cover_waveform,
                export_original,
                export_instrumental,
                translation_margin_v,
                show_countdown,
                countdown_gap_threshold,
                progress_callback=update,
            )
            _allow_gradio_file_paths(app, result.video, result.files)
            _allow_gradio_result_workspaces(app, result.files)
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
            cookie_browser: str,
            cookie_browser_profile: str,
            music_u: str,
            timing_refinement: str,
            output_root: str,
            qqmusic_link: str,
            use_qqmusic_lyrics: bool,
            utaten_link: str,
            use_utaten_lyrics: bool,
            utaten_pronunciation_only: bool,
            auto_english_pronunciation: bool,
            cover: object,
            font_files: object,
            font: str,
            cover_background: str,
            cover_style: str,
            cover_waveform: bool,
            export_original: bool,
            export_instrumental: bool,
            request: object | None = None,
            progress: object = gr.Progress(),
        ) -> tuple[object, ...]:
            def update(message: str) -> None:
                progress((0, None), desc=message)

            cookie_browser, cookie_browser_profile, music_u = netease_credentials_for_request(
                cookie_browser,
                cookie_browser_profile,
                music_u,
                request,
            )
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
                qqmusic_link,
                use_qqmusic_lyrics,
                utaten_link,
                use_utaten_lyrics,
                utaten_pronunciation_only,
                auto_english_pronunciation,
                cover,
                font_files,
                font,
                cover_background,
                cover_style,
                cover_waveform,
                export_original,
                export_instrumental,
                cookie_browser,
                cookie_browser_profile,
                music_u,
                progress_callback=update,
            )
            _allow_gradio_file_paths(app, result.project, result.audio, result.files)
            _allow_gradio_result_workspaces(app, result.files)
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
                result.project_name if result.project_name else gr.skip(),
                result.files,
                result.output_dir,
                result.status,
                result.log,
                gr.update(selected="editor" if result.project else "make"),
            )

        make_wrapper.__annotations__["request"] = gr.Request
        prepare_make_editor_wrapper.__annotations__["request"] = gr.Request

        prepare_editor_event = make_prepare_button.click(
            ensure_netease_session_for_download,
            inputs=[
                make_netease_link,
                make_audio,
                make_video,
                make_rights,
                netease_session_music_u,
            ],
            outputs=[
                netease_session_music_u,
                make_netease_login_status,
                netease_login_status,
            ],
            show_progress="full",
        ).then(
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
                make_cookie_browser,
                make_cookie_profile,
                netease_session_music_u,
                make_timing_refinement,
                make_output_root,
                make_qqmusic_link,
                make_use_qqmusic_lyrics,
                make_utaten_link,
                make_use_utaten_lyrics,
                make_utaten_pronunciation_only,
                make_auto_english_pronunciation,
                make_cover,
                make_font_files,
                make_font,
                make_cover_background,
                make_cover_style,
                make_cover_waveform,
                make_export_original,
                make_export_instrumental,
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
                editor_name,
                editor_downloads,
                editor_output_directory,
                make_status,
                make_log,
                main_tabs,
            ],
            show_progress="full",
        )
        prepare_editor_event.then(
            refresh_recent_workspace_offer,
            outputs=[
                recent_workspace_manifest,
                saved_workspace_selector,
                recent_workspace_message,
                recent_workspace_prompt,
                continue_recent_workspace,
                continue_recent_workspace_editor,
            ],
            queue=False,
        )

        make_event = make_button.click(
            ensure_netease_session_for_download,
            inputs=[
                make_netease_link,
                make_audio,
                make_video,
                make_rights,
                netease_session_music_u,
            ],
            outputs=[
                netease_session_music_u,
                make_netease_login_status,
                netease_login_status,
            ],
            show_progress="full",
        ).then(
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
                netease_session_music_u,
                make_auto_sync,
                make_timing_refinement,
                make_show_translation,
                make_translation_size,
                make_translation_color,
                make_show_pronunciation,
                make_pronunciation_size,
                make_pronunciation_color,
                make_output_root,
                make_qqmusic_link,
                make_use_qqmusic_lyrics,
                make_utaten_link,
                make_use_utaten_lyrics,
                make_utaten_pronunciation_only,
                make_auto_english_pronunciation,
                make_cover,
                make_font_files,
                make_cover_background,
                make_cover_style,
                make_cover_waveform,
                make_export_original,
                make_export_instrumental,
                make_translation_margin,
                make_show_countdown,
                make_countdown_gap,
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

        material_preview_inputs = [
            make_audio,
            make_video,
            make_cover,
            make_lyrics,
            make_pasted,
            make_offset,
            make_auto_sync,
            make_cover_background,
            make_cover_style,
            make_cover_waveform,
            make_netease_link,
            make_qqmusic_link,
            make_utaten_link,
        ]
        material_preview_outputs = [
            make_preview_text,
            make_preview_translation,
            make_preview_background,
            make_preview_badge,
            make_preview_material_mode,
            make_preview_progress,
            make_preview_active_row,
            make_preview_status,
        ]
        material_preview_event = gr.on(
            triggers=[
                make_audio.change,
                make_video.change,
                make_cover.change,
                make_lyrics.change,
                make_pasted.change,
                make_offset.change,
                make_auto_sync.change,
                make_cover_background.change,
                make_cover_style.change,
                make_cover_waveform.change,
                make_netease_link.change,
                make_qqmusic_link.change,
                make_utaten_link.change,
            ],
            fn=prepare_subtitle_material_preview,
            inputs=material_preview_inputs,
            outputs=material_preview_outputs,
            queue=True,
            trigger_mode="always_last",
            concurrency_limit=1,
            concurrency_id="make-material-preview",
            show_progress="hidden",
        )

        def auto_select_workspace_for_links(
            netease_link: str,
            qqmusic_link: str,
            utaten_link: str,
        ) -> tuple[object, ...]:
            manifest = _matching_workspace_manifest(
                netease_link,
                qqmusic_link,
                utaten_link,
            )
            if not manifest:
                return (
                    gr.update(value=None),
                    "",
                    gr.update(visible=True),
                    "## 当前链接没有匹配到已保存工程\n可从工程列表手动选择，或继续创建新工程。",
                    gr.update(interactive=False),
                    gr.update(interactive=False),
                )
            workspace = load_workspace_project(manifest)
            message = (
                f"## 已自动匹配工程 **{html.escape(workspace.name)}**\n"
                "链接来源与已保存工程一致；点击“载入所选工程”或“载入并直接编辑”即可。"
            )
            return (
                gr.update(value=manifest),
                manifest,
                gr.update(visible=True),
                message,
                gr.update(interactive=True),
                gr.update(interactive=True),
            )

        gr.on(
            triggers=[
                make_netease_link.change,
                make_qqmusic_link.change,
                make_utaten_link.change,
            ],
            fn=auto_select_workspace_for_links,
            inputs=[make_netease_link, make_qqmusic_link, make_utaten_link],
            outputs=[
                saved_workspace_selector,
                recent_workspace_manifest,
                recent_workspace_prompt,
                recent_workspace_message,
                continue_recent_workspace,
                continue_recent_workspace_editor,
            ],
            queue=False,
            trigger_mode="always_last",
        )

        instant_preview_controls = [
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
            make_auto_english_pronunciation,
        ]
        preview_inputs = [
            *instant_preview_controls,
            make_preview_background,
            make_preview_badge,
            make_preview_material_mode,
            make_preview_progress,
            make_preview_active_row,
            make_translation_margin,
        ]
        material_preview_event.then(
            subtitle_preview_html,
            inputs=preview_inputs,
            outputs=make_style_preview,
            queue=False,
        )
        for preview_input in [
            *instant_preview_controls[:11],
            make_auto_english_pronunciation,
            make_translation_margin,
        ]:
            preview_input.change(
                subtitle_preview_html,
                inputs=preview_inputs,
                outputs=make_style_preview,
                queue=False,
            )
        for preview_input in [make_preview_text, make_preview_translation]:
            preview_input.input(
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
                progress((0, None), desc=message)

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
            music_u: str,
            timing_refinement: str,
            request: object | None = None,
            progress: object = gr.Progress(),
        ) -> tuple[str, list[str], str, str | None]:
            def update(message: str) -> None:
                progress((0, None), desc=message)

            cookie_browser, cookie_browser_profile, music_u = netease_credentials_for_request(
                cookie_browser,
                cookie_browser_profile,
                music_u,
                request,
            )
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
                music_u,
                timing_refinement,
                progress_callback=update,
            )
            progress(1.0, desc="完成" if result.files else "未完成")
            return result.status, result.files, result.log, result.output_dir

        netease_wrapper.__annotations__["request"] = gr.Request

        netease_button.click(
            ensure_netease_session_without_video,
            inputs=[
                netease_link,
                netease_local_audio,
                netease_rights,
                netease_session_music_u,
            ],
            outputs=[
                netease_session_music_u,
                make_netease_login_status,
                netease_login_status,
            ],
            show_progress="full",
        ).then(
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
                netease_session_music_u,
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

        def qqmusic_wrapper(
            link: str,
            name: str,
            rights_confirmed: bool,
            output_root: str,
            progress: object = gr.Progress(),
        ) -> tuple[str, list[str], str, str | None]:
            progress((0, None), desc="正在读取 QQ 音乐公开歌词")
            result = run_qqmusic_job(link, name, rights_confirmed, output_root)
            _allow_gradio_file_paths(app, result.files)
            progress(1.0, desc="完成" if result.files else "未完成")
            return result.status, result.files, result.log, result.output_dir

        qqmusic_button.click(
            qqmusic_wrapper,
            inputs=[qqmusic_link, qqmusic_name, qqmusic_rights, qqmusic_output_root],
            outputs=[
                qqmusic_status,
                qqmusic_downloads,
                qqmusic_log,
                qqmusic_output_directory,
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

        workspace_restore_outputs = [
            make_audio,
            make_video,
            make_lyrics,
            make_cover,
            make_font_files,
            make_name,
            make_language,
            make_model,
            make_device,
            make_separate,
            make_timing_refinement,
            make_quality,
            make_font,
            make_font_size,
            make_auto_english_pronunciation,
            make_cover_background,
            make_cover_style,
            make_cover_waveform,
            make_export_original,
            make_export_instrumental,
            make_translation_margin,
            make_show_countdown,
            make_countdown_gap,
            recent_workspace_prompt,
            make_status,
        ]

        def restore_recent_workspace(manifest: str) -> tuple[object, ...]:
            try:
                workspace = load_workspace_project(manifest)
                read_lyrics(workspace.lyrics_project)
            except Exception as exc:
                _record_web_error("restore-recent-workspace", exc)
                result = [gr.skip() for _ in workspace_restore_outputs]
                result[-2] = gr.update(visible=True)
                result[-1] = (
                    "### ⚠️ 所选工程暂时无法恢复\n"
                    "当前制作页内容没有被覆盖；可刷新工程列表或选择其他工程。"
                )
                return tuple(result)

            _allow_gradio_workspace_paths(app, workspace)

            video = str(workspace.video) if workspace.video and workspace.video.is_file() else None
            if workspace.audio and workspace.audio.is_file():
                audio = str(workspace.audio)
            elif (
                workspace.video
                and workspace.video.is_file()
                and probe_media_has_audio(workspace.video) is True
            ):
                audio = str(workspace.video)
            else:
                audio = None
            cover = str(workspace.cover) if workspace.cover and workspace.cover.is_file() else None
            missing_assets = [
                label
                for label, path in (
                    ("音频", workspace.audio),
                    ("MV", workspace.video),
                    ("封面", workspace.cover),
                )
                if path is not None and not path.is_file()
            ]
            missing_note = (
                f"；未找到：{'、'.join(missing_assets)}，其余内容仍已恢复"
                if missing_assets
                else ""
            )
            safe_workspace_name = html.escape(workspace.name)
            font_files = [str(path) for path in workspace.font_files]
            settings = workspace.settings or {}
            font_name = str(settings.get("font") or "Microsoft YaHei")
            try:
                font_size = int(settings.get("font_size") or 58)
            except (TypeError, ValueError):
                font_size = 58
            font_size = max(32, min(88, font_size))
            quality = str(settings.get("quality") or "推荐质量")
            if quality not in {"快速预览", "推荐质量", "高质量"}:
                quality = "推荐质量"
            auto_english_pronunciation = bool(
                settings.get("auto_english_pronunciation", True)
            )
            cover_background = str(settings.get("cover_background") or "adaptive")
            if cover_background not in {"adaptive", "midnight", "sunset", "ocean", "paper"}:
                cover_background = "adaptive"
            cover_style = str(settings.get("cover_style") or "turntable")
            if cover_style == "cdplayer":
                cover_style = "turntable"
            if cover_style not in {"aurora", "vinyl", "halo", "spectrum", "turntable"}:
                cover_style = "turntable"
            cover_waveform = bool(settings.get("cover_waveform", True))
            export_original = bool(settings.get("export_original", True))
            export_instrumental = bool(settings.get("export_instrumental", False))
            try:
                translation_margin_v = int(settings.get("translation_margin_v") or 54)
            except (TypeError, ValueError):
                translation_margin_v = 54
            translation_margin_v = max(16, min(760, translation_margin_v))
            show_countdown = bool(settings.get("show_countdown", True))
            try:
                countdown_gap_threshold = float(
                    settings.get("countdown_gap_threshold") or 8.0
                )
            except (TypeError, ValueError):
                countdown_gap_threshold = 8.0
            countdown_gap_threshold = max(5.0, min(20.0, countdown_gap_threshold))
            alignment_language = str(settings.get("alignment_language") or "自动识别")
            alignment_model = str(settings.get("alignment_model") or "profile:fast")
            alignment_device = str(settings.get("alignment_device") or "auto")
            alignment_separate_vocals = bool(settings.get("alignment_separate_vocals", False))
            timing_refinement = _web_timing_refinement(settings.get("timing_refinement", "auto"))
            make_status = (
                f"### ✅ 已恢复工程 `{safe_workspace_name}`\n"
                f"可以继续编辑或直接制作{missing_note}。"
            )
            return (
                audio,
                video,
                str(workspace.lyrics_project),
                cover,
                font_files,
                workspace.name,
                alignment_language,
                alignment_model,
                alignment_device,
                alignment_separate_vocals,
                timing_refinement,
                quality,
                font_name,
                font_size,
                auto_english_pronunciation,
                cover_background,
                cover_style,
                cover_waveform,
                export_original,
                export_instrumental,
                translation_margin_v,
                show_countdown,
                countdown_gap_threshold,
                gr.update(visible=True),
                make_status,
            )

        restore_workspace_event = continue_recent_workspace.click(
            restore_recent_workspace,
            inputs=recent_workspace_manifest,
            outputs=workspace_restore_outputs,
            show_progress="full",
        )
        restore_workspace_event = restore_workspace_event.then(
            prepare_subtitle_material_preview,
            inputs=material_preview_inputs,
            outputs=material_preview_outputs,
            show_progress="hidden",
            concurrency_limit=1,
            concurrency_id="make-material-preview",
        )
        restore_workspace_event = restore_workspace_event.then(
            subtitle_preview_html,
            inputs=preview_inputs,
            outputs=make_style_preview,
            queue=False,
        )

        def open_current_make_project_editor(
            source: object,
            audio: object,
        ) -> tuple[object, ...]:
            try:
                path = _file_path(source)
                if path is None or not path.is_file():
                    raise ValueError("请先恢复工程，或选择一个项目 JSON / 时间轴歌词文件。")
                loaded = load_editor_project_workspace(path)
                workspace: WorkspaceProject | None = None
                if path.name == PROJECT_FILENAME:
                    workspace = load_workspace_project(path)
                else:
                    workspace = _workspace_for_lyrics_project(path)
                loaded_document = document_from_payload(loaded[0])
                project_name = (
                    workspace.name
                    if workspace is not None
                    else str(
                        loaded_document.metadata.get("ti")
                        or loaded_document.metadata.get("title")
                        or path.stem
                    )
                )
            except Exception as exc:
                result = [gr.skip() for _ in range(15)]
                result[-2] = gr.update(selected="make")
                result[-1] = f"### ⚠️ 暂时无法打开歌词编辑器\n{html.escape(str(exc))}"
                return tuple(result)
            return (
                *loaded,
                str(path),
                audio,
                project_name,
                gr.update(selected="editor"),
                "### ✅ 歌词工程已载入编辑器\n可逐句试听和微调；制作页素材会继续保留。",
            )

        make_open_editor_button.click(
            open_current_make_project_editor,
            inputs=[make_lyrics, make_audio],
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
                editor_name,
                main_tabs,
                make_status,
            ],
            show_progress="full",
        )

        def open_selected_workspace_editor(manifest: str) -> tuple[object, ...]:
            try:
                workspace = load_workspace_project(manifest)
                if not _workspace_is_editable(workspace):
                    raise ValueError("工程歌词已损坏、为空，或缺少完整时间轴。")
                if workspace.audio is not None and workspace.audio.is_file():
                    audio: object = str(workspace.audio)
                elif (
                    workspace.video is not None
                    and workspace.video.is_file()
                    and probe_media_has_audio(workspace.video) is True
                ):
                    audio = str(workspace.video)
                else:
                    audio = None
                return open_current_make_project_editor(workspace.manifest, audio)
            except Exception as exc:
                result = [gr.skip() for _ in range(15)]
                result[-2] = gr.update(selected="make")
                result[-1] = f"### ⚠️ 暂时无法打开所选工程\n{html.escape(str(exc))}"
                return tuple(result)

        continue_recent_workspace_editor.click(
            restore_recent_workspace,
            inputs=recent_workspace_manifest,
            outputs=workspace_restore_outputs,
            show_progress="full",
        ).then(
            open_selected_workspace_editor,
            inputs=recent_workspace_manifest,
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
                editor_name,
                main_tabs,
                make_status,
            ],
            show_progress="full",
        )
        make_event.then(
            refresh_recent_workspace_offer,
            outputs=[
                recent_workspace_manifest,
                saved_workspace_selector,
                recent_workspace_message,
                recent_workspace_prompt,
                continue_recent_workspace,
                continue_recent_workspace_editor,
            ],
            queue=False,
        )

        def start_blank_workspace_choice() -> tuple[object, str]:
            return (
                gr.update(visible=True),
                "### ✅ 已保留当前制作页\n已保存工程没有被删除，可随时从上方载入。",
            )

        start_blank_workspace.click(
            start_blank_workspace_choice,
            outputs=[
                recent_workspace_prompt,
                make_status,
            ],
            queue=False,
        )

        def load_editor_line_workspace(
            payload: dict[str, Any],
            table: object,
            line_number: int,
            token_json: str,
            whole_pronunciation: str,
            pronunciation_table: object,
            undo_state: dict[str, Any],
            ripple_enabled: bool,
        ) -> tuple[object, ...]:
            before = document_from_payload(payload)
            document, snapshot = _editor_document_with_pending_changes(
                payload,
                table,
                int(line_number),
                token_json,
                whole_pronunciation,
                pronunciation_table,
                ripple_enabled,
            )
            selected = _mapped_editor_line_number(
                table,
                len(before.lines),
                int(line_number),
                len(document.lines),
            )
            rows = document_to_editor_rows(document)
            result = load_editor_line(document.to_dict(), rows, selected)
            token_timeline, token_json = editor_token_workspace(
                result[0],
                result[1],
                selected,
            )
            return (
                result[0],
                result[1],
                selected,
                *result[2:],
                token_timeline,
                token_json,
                snapshot or undo_state,
            )

        def editor_audio_with_prefetch(
            audio: object,
            payload: dict[str, Any],
            table: object,
            line_number: int,
        ) -> tuple[str | None, str]:
            try:
                clip, timing_status = preview_editor_audio_line(
                    audio,
                    payload,
                    table,
                    line_number,
                )
            except (ValueError, RuntimeError) as exc:
                return None, f"已选择第 {line_number} 行；暂时无法自动播放：{exc}"
            try:
                document = apply_editor_rows(document_from_payload(payload), table)
                next_line = _next_playable_editor_line(document, int(line_number))
                if next_line is not None:
                    prefetch_editor_audio_line(
                        audio,
                        document.to_dict(),
                        document_to_editor_rows(document),
                        next_line,
                    )
            except (TypeError, ValueError):
                pass
            return clip, timing_status

        editor_preview_inputs = [
            editor_payload,
            editor_lines,
            editor_line_number,
            editor_whole_pronunciation,
            editor_pronunciation_units,
        ]
        editor_preview_event = gr.on(
            triggers=[
                editor_whole_pronunciation.input,
                editor_pronunciation_units.input,
            ],
            fn=preview_editor_changes,
            inputs=editor_preview_inputs,
            outputs=editor_preview,
            queue=True,
            trigger_mode="always_last",
            concurrency_limit=1,
            concurrency_id="editor-preview",
            show_progress="hidden",
        )

        def preview_editor_line_draft(
            payload: dict[str, Any],
            table: object,
            line_number: int,
            token_json: str,
            whole_pronunciation: str,
            pronunciation_table: object,
        ) -> tuple[object, ...]:
            original = document_from_payload(payload)
            original_selected = min(max(1, int(line_number)), len(original.lines))
            document, _snapshot = _editor_document_with_pending_changes(
                payload,
                table,
                original_selected,
                token_json,
                whole_pronunciation,
                pronunciation_table,
            )
            selected = _mapped_editor_line_number(
                table,
                len(original.lines),
                original_selected,
                len(document.lines),
            )
            rows = document_to_editor_rows(document)
            timeline, _ = editor_token_workspace(
                document.to_dict(),
                rows,
                selected,
            )
            old_line = original.lines[original_selected - 1]
            new_line = document.lines[selected - 1]
            timing_changed = (old_line.start, old_line.end) != (
                new_line.start,
                new_line.end,
            )
            return (
                editor_preview_html(document, selected),
                timeline,
                gr.skip(),
                uuid4().hex if timing_changed else gr.skip(),
            )

        editor_line_draft_event = editor_lines.input(
            preview_editor_line_draft,
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_token_json,
                editor_whole_pronunciation,
                editor_pronunciation_units,
            ],
            outputs=[
                editor_preview,
                editor_token_timeline,
                editor_token_json,
                editor_audio_refresh_trigger,
            ],
            queue=True,
            trigger_mode="always_last",
            concurrency_limit=1,
            concurrency_id="editor-line-draft",
            cancels=[editor_preview_event],
            show_progress="hidden",
        )

        def select_editor_row(
            audio: object,
            payload: dict[str, Any],
            table: object,
            current_line_number: int,
            token_json: str,
            whole_pronunciation: str,
            pronunciation_table: object,
            undo_state: dict[str, Any],
            timing_mode: str,
            ripple_enabled: bool,
            event: gr.SelectData,
        ) -> tuple[object, ...]:
            skipped = tuple(gr.skip() for _ in range(11))
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
                before = document_from_payload(payload)
                table_rows = _table_rows(table)
                if selected_row >= len(table_rows):
                    return skipped
                selected_source_ids = _editor_row_source_ids(table, len(before.lines))
                raw_row = [*table_rows[selected_row], None, None][:2]
                if str(raw_row[1] or "显示").strip() == LINE_STATUS_DELETED:
                    return skipped
                try:
                    requested_source_id = (
                        int(float(raw_row[0]))
                        if raw_row[0] not in (None, "")
                        else selected_row + 1
                    )
                except (TypeError, ValueError):
                    requested_source_id = selected_row + 1
                document, snapshot = _editor_document_with_pending_changes(
                    payload,
                    table,
                    int(current_line_number),
                    token_json,
                    whole_pronunciation,
                    pronunciation_table,
                    ripple_enabled,
                )
                matches = [
                    position
                    for position, source_id in enumerate(selected_source_ids, 1)
                    if source_id == requested_source_id
                ]
                line_number = (
                    matches[0]
                    if len(matches) == 1
                    else min(max(1, line_number), len(document.lines))
                )
                rows = document_to_editor_rows(document)
                loaded = load_editor_line(document.to_dict(), rows, line_number)
                token_timeline, token_json = editor_token_workspace(
                    loaded[0],
                    loaded[1],
                    line_number,
                )
            except (TypeError, ValueError, IndexError):
                return skipped
            if str(timing_mode) == "global":
                clip = gr.skip()
                timing_status = (
                    f"已在全局时间轴选择第 {line_number} 行；"
                    "整曲播放器保持连续，不会重新切片。"
                )
            else:
                clip, timing_status = editor_audio_with_prefetch(
                    audio,
                    loaded[0],
                    loaded[1],
                    line_number,
                )
            return (
                loaded[0] if snapshot else gr.skip(),
                loaded[1] if snapshot else gr.skip(),
                line_number,
                loaded[2],
                loaded[3],
                loaded[4],
                token_timeline,
                token_json,
                clip,
                timing_status,
                snapshot or undo_state,
            )

        select_editor_row.__annotations__["event"] = gr.SelectData

        def step_editor_line_workspace(
            audio: object,
            payload: dict[str, Any],
            table: object,
            line_number: int,
            token_json: str,
            whole_pronunciation: str,
            pronunciation_table: object,
            undo_state: dict[str, Any],
            ripple_enabled: bool,
            delta: int,
            *,
            load_audio: bool = True,
            continuous_mode: bool = False,
            absolute_target: int | None = None,
        ) -> tuple[object, ...]:
            before = document_from_payload(payload)
            document, snapshot = _editor_document_with_pending_changes(
                payload,
                table,
                int(line_number),
                token_json,
                whole_pronunciation,
                pronunciation_table,
                ripple_enabled,
            )
            selected = _mapped_editor_line_number(
                table,
                len(before.lines),
                int(line_number),
                len(document.lines),
            )
            target = min(
                max(
                    1,
                    int(absolute_target)
                    if absolute_target is not None
                    else selected + int(delta),
                ),
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
            if load_audio:
                clip, timing_status = editor_audio_with_prefetch(
                    audio,
                    loaded[0],
                    loaded[1],
                    target,
                )
            elif continuous_mode:
                clip = gr.skip()
                timing_status = (
                    f"已在全局时间轴选中第 {target} 行；整曲播放保持连续，"
                    "下方逐字微调区已载入这句。"
                )
            else:
                clip = gr.skip()
                timing_status = f"已自动切换到第 {target} 行，正在准备试听音频……"
            return (
                loaded[0] if snapshot else gr.skip(),
                loaded[1] if snapshot else gr.skip(),
                target,
                loaded[2],
                loaded[3],
                loaded[4],
                token_timeline,
                token_json,
                clip,
                timing_status,
                snapshot or undo_state,
            )

        editor_exit_workspace.click(
            lambda: gr.update(selected="make"),
            outputs=main_tabs,
            queue=False,
        )
        editor_load_event = editor_load.click(
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
            cancels=[editor_preview_event, editor_line_draft_event],
        )

        def load_editor_workspace_assets(source: object) -> tuple[object, object]:
            path = _file_path(source)
            if path is None or not path.is_file():
                return gr.skip(), gr.skip()
            workspace: WorkspaceProject | None = None
            if path.suffix.lower() == ".json":
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if (
                        isinstance(data, dict)
                        and data.get("schema_version") == 1
                        and data.get("lyrics_project")
                    ):
                        workspace = load_workspace_project(path)
                    elif isinstance(data, dict):
                        workspace = _workspace_for_lyrics_project(path)
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    workspace = None
            if workspace is None:
                try:
                    document = read_lyrics(path)
                    project_name = str(
                        document.metadata.get("ti")
                        or document.metadata.get("title")
                        or path.stem
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    project_name = path.stem
                return gr.skip(), project_name
            if workspace.audio is not None and workspace.audio.is_file():
                audio: object = str(workspace.audio)
            elif (
                workspace.video is not None
                and workspace.video.is_file()
                and probe_media_has_audio(workspace.video) is True
            ):
                audio = str(workspace.video)
            else:
                audio = None
            return audio, workspace.name

        editor_load_event.then(
            load_editor_workspace_assets,
            inputs=editor_source,
            outputs=[editor_audio, editor_name],
            queue=False,
        )
        editor_load_line_event = editor_load_line.click(
            load_editor_line_workspace,
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_token_json,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_line_undo_payload,
                editor_ripple_following,
            ],
            outputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_preview,
                editor_token_timeline,
                editor_token_json,
                editor_line_undo_payload,
            ],
            queue=False,
            cancels=[editor_preview_event, editor_line_draft_event],
        )
        editor_lines.select(
            select_editor_row,
            inputs=[
                editor_audio,
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_token_json,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_line_undo_payload,
                editor_timing_mode,
                editor_ripple_following,
            ],
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
                editor_line_undo_payload,
            ],
            queue=False,
            cancels=[editor_preview_event, editor_line_draft_event],
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
            editor_line_undo_payload,
        ]

        def switch_editor_timing_mode(
            mode: str,
            audio: object,
            payload: dict[str, Any],
            table: object,
            line_number: int,
            token_json: str,
            whole_pronunciation: str,
            pronunciation_table: object,
            undo_state: dict[str, Any],
            ripple_enabled: bool,
        ) -> tuple[object, ...]:
            global_mode = str(mode) == "global"
            before = document_from_payload(payload)
            document, snapshot = _editor_document_with_pending_changes(
                payload,
                table,
                int(line_number),
                token_json,
                whole_pronunciation,
                pronunciation_table,
                ripple_enabled,
            )
            selected = _mapped_editor_line_number(
                table,
                len(before.lines),
                int(line_number),
                len(document.lines),
            )
            rows = document_to_editor_rows(document)
            line = document.lines[selected - 1]
            timeline, refreshed_token_json = editor_token_workspace(
                document.to_dict(),
                rows,
                selected,
            )
            if global_mode:
                line_audio: object = None
                timing_status = (
                    "### 🎵 全局连续模式\n"
                    "整曲音频保持加载；点句块或拖红线直接定位，空格暂停 / 继续。"
                )
            else:
                line_audio, preview_status = editor_audio_with_prefetch(
                    audio,
                    document.to_dict(),
                    rows,
                    selected,
                )
                timing_status = f"### 🎤 逐句精修模式\n{preview_status}"
            if snapshot:
                timing_status += "\n切换模式前的当前句草稿已自动保存。"
            return (
                gr.update(visible=not global_mode),
                gr.update(value=timeline, visible=True),
                gr.update(visible=True),
                gr.update(visible=global_mode),
                timing_status,
                document.to_dict(),
                rows,
                selected,
                line.pronunciation or "",
                document_pronunciation_to_editor_rows(document, line),
                editor_preview_html(document, selected),
                refreshed_token_json,
                snapshot or undo_state,
                line_audio,
            )

        editor_timing_mode.change(
            switch_editor_timing_mode,
            inputs=[
                editor_timing_mode,
                editor_audio,
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_token_json,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_line_undo_payload,
                editor_ripple_following,
            ],
            outputs=[
                editor_audio_panel,
                editor_token_timeline,
                editor_timing_actions,
                editor_global_mode_panel,
                editor_timing_status,
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_preview,
                editor_token_json,
                editor_line_undo_payload,
                editor_line_audio,
            ],
            queue=False,
        )
        editor_audio.change(
            lambda audio: audio,
            inputs=editor_audio,
            outputs=editor_global_audio,
            queue=False,
        )
        gr.on(
            triggers=[
                editor_payload.change,
                editor_lines.change,
                editor_audio.change,
            ],
            fn=editor_global_timeline_workspace,
            inputs=[editor_payload, editor_lines, editor_line_number, editor_audio],
            outputs=editor_global_timeline,
            queue=False,
            trigger_mode="always_last",
        )

        editor_global_select_line.click(
            lambda audio, payload, table, line_number, token_json, whole, units, undo_state, ripple, requested: (
                step_editor_line_workspace(
                    audio,
                    payload,
                    table,
                    line_number,
                    token_json,
                    whole,
                    units,
                    undo_state,
                    ripple,
                    0,
                    load_audio=False,
                    continuous_mode=True,
                    absolute_target=int(requested),
                )
            ),
            inputs=[
                editor_audio,
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_token_json,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_line_undo_payload,
                editor_ripple_following,
                editor_global_line_request,
            ],
            outputs=editor_line_workspace_outputs,
            queue=False,
            trigger_mode="always_last",
            cancels=[editor_preview_event, editor_line_draft_event],
        )

        def global_workspace_result(
            document: LyricsDocument,
            line_number: int,
            audio: object,
            status: str,
            undo_state: dict[str, Any],
        ) -> tuple[object, ...]:
            rows = document_to_editor_rows(document)
            selected = min(max(1, int(line_number)), len(document.lines))
            line = document.lines[selected - 1]
            token_timeline, token_json = editor_token_workspace(
                document.to_dict(),
                rows,
                selected,
            )
            return (
                document.to_dict(),
                rows,
                selected,
                line.pronunciation or "",
                document_pronunciation_to_editor_rows(document, line),
                editor_preview_html(document, selected),
                token_timeline,
                token_json,
                editor_global_timeline_workspace(
                    document.to_dict(), rows, selected, audio
                ),
                status,
                undo_state,
            )

        def apply_global_rows_workspace(
            payload: dict[str, Any],
            table: object,
            line_number: int,
            ripple_enabled: bool,
            audio: object,
            undo_state: dict[str, Any],
            token_json: str,
            whole_pronunciation: str,
            pronunciation_table: object,
        ) -> tuple[object, ...]:
            before = document_from_payload(payload)
            source_ids = _editor_row_source_ids(table, len(before.lines))
            document, _pending_snapshot = _editor_document_with_pending_changes(
                payload,
                table,
                int(line_number),
                token_json,
                whole_pronunciation,
                pronunciation_table,
                False,
            )
            shifted_lines = _ripple_global_editor_changes(
                before,
                document,
                ripple_enabled,
                source_ids=source_ids,
            )
            selected = _mapped_editor_line_number(
                table,
                len(before.lines),
                int(line_number),
                len(document.lines),
            )
            changed = document.to_dict() != before.to_dict()
            snapshot = (
                _editor_undo_snapshot(before, int(line_number))
                if changed
                else undo_state
            )
            ripple_note = (
                f"；已联动后移第 {shifted_lines[0]}–{shifted_lines[-1]} 行"
                if shifted_lines
                else ""
            )
            status = (
                f"### ✅ 已保存全局总览修改{ripple_note}"
                if changed
                else "### ℹ️ 全局总览没有待保存的修改"
            )
            return global_workspace_result(
                document,
                selected,
                audio,
                status,
                snapshot,
            )

        global_workspace_outputs = [
            editor_payload,
            editor_lines,
            editor_line_number,
            editor_whole_pronunciation,
            editor_pronunciation_units,
            editor_preview,
            editor_token_timeline,
            editor_token_json,
            editor_global_timeline,
            editor_timing_status,
            editor_line_undo_payload,
        ]

        def apply_global_line_edge_workspace(
            payload: dict[str, Any],
            table: object,
            line_number: int,
            token_json: str,
            whole_pronunciation: str,
            pronunciation_table: object,
            undo_state: dict[str, Any],
            ripple_enabled: bool,
            audio: object,
            request_json: str,
        ) -> tuple[object, ...]:
            try:
                request_payload = json.loads(request_json or "")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("全局句界拖动请求无效，请重新拖动一次。") from exc
            if not isinstance(request_payload, dict):
                raise TypeError("全局句界拖动请求格式无效。")

            edge = str(request_payload.get("edge") or "").strip().lower()
            if edge not in {"start", "end"}:
                raise ValueError("全局句界只能调整句首或句尾。")
            raw_line = request_payload.get("line")
            if isinstance(raw_line, bool):
                raise TypeError("全局句界拖动请求缺少有效的行号。")
            try:
                requested_line = float(raw_line)
                seconds = float(request_payload.get("seconds"))
                base_start = float(request_payload.get("base_start"))
                base_end = float(request_payload.get("base_end"))
            except (TypeError, ValueError) as exc:
                raise ValueError("全局句界拖动请求缺少有效的行号或时间。") from exc
            if (
                not math.isfinite(requested_line)
                or not requested_line.is_integer()
                or requested_line < 1
                or not all(math.isfinite(value) for value in (seconds, base_start, base_end))
            ):
                raise ValueError("全局句界拖动请求包含无效时间。")
            if seconds < 0:
                raise ValueError("歌词时间不能早于 0 秒。")
            target = int(requested_line)

            before = document_from_payload(payload)
            source_ids = _editor_row_source_ids(table, len(before.lines))
            timeline_document = apply_editor_rows(before, table)
            if target > len(timeline_document.lines):
                raise ValueError("要调整的歌词行已经不存在，请刷新全局时间轴后重试。")
            timeline_line = timeline_document.lines[target - 1]
            if (
                timeline_line.hidden
                or not timeline_line.text.strip()
                or timeline_line.start is None
                or timeline_line.end is None
            ):
                raise ValueError("要调整的歌词行当前不可见或没有完整时间。")
            request_text = str(request_payload.get("text") or "")
            stale = (
                request_text != timeline_line.text
                or abs(base_start - timeline_line.start) > 0.002
                or abs(base_end - timeline_line.end) > 0.002
            )
            if stale:
                raise ValueError("全局时间轴已经变化，请在刷新后的句块上重新拖动。")
            base_edge = base_start if edge == "start" else base_end
            if abs(seconds - base_edge) < 0.0005:
                selected = min(max(1, int(line_number)), len(before.lines))
                edge_label = "句首" if edge == "start" else "句尾"
                return global_workspace_result(
                    before,
                    selected,
                    audio,
                    f"### ℹ️ 第 {target} 行{edge_label}时间没有变化",
                    undo_state,
                )

            document, _pending_snapshot = _editor_document_with_pending_changes(
                payload,
                table,
                int(line_number),
                token_json,
                whole_pronunciation,
                pronunciation_table,
                False,
            )
            if target > len(document.lines):
                raise ValueError("要调整的歌词行已经不存在，请刷新全局时间轴后重试。")
            current = document.lines[target - 1]
            if (
                current.hidden
                or not current.text.strip()
                or current.start is None
                or current.end is None
            ):
                raise ValueError("要调整的歌词行当前不可见或没有完整时间。")

            original_edge = current.start if edge == "start" else current.end
            if abs(seconds - original_edge) < 0.0005:
                after = document
            else:
                after = nudge_editor_line_timing(
                    document,
                    document_to_editor_rows(document),
                    target,
                    start_delta=seconds - current.start if edge == "start" else 0.0,
                    end_delta=seconds - current.end if edge == "end" else 0.0,
                    ripple_following=False,
                )
            shifted_lines = _ripple_global_editor_changes(
                before,
                after,
                ripple_enabled,
                source_ids=source_ids,
            )
            changed = after.to_dict() != before.to_dict()
            source_focus = source_ids[target - 1] if target <= len(source_ids) else None
            focus = (
                source_focus
                if source_focus is not None and 1 <= source_focus <= len(before.lines)
                else min(max(1, int(line_number)), len(before.lines))
            )
            next_undo = _editor_undo_snapshot(before, focus) if changed else undo_state
            line = after.lines[target - 1]
            assert line.start is not None and line.end is not None
            ripple_note = (
                f"；已联动后移第 {shifted_lines[0]}–{shifted_lines[-1]} 行"
                if shifted_lines
                else ""
            )
            edge_label = "句首" if edge == "start" else "句尾"
            status = (
                f"### ✅ 已拖动第 {target} 行{edge_label}\n"
                f"当前范围：**{line.start:.2f}s → {line.end:.2f}s**{ripple_note}。"
                if changed
                else f"### ℹ️ 第 {target} 行{edge_label}时间没有变化"
            )
            return global_workspace_result(after, target, audio, status, next_undo)

        editor_global_edge_apply.click(
            apply_global_line_edge_workspace,
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_token_json,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_line_undo_payload,
                editor_ripple_following,
                editor_audio,
                editor_global_edge_request,
            ],
            outputs=global_workspace_outputs,
            queue=False,
            trigger_mode="always_last",
            cancels=[editor_preview_event, editor_line_draft_event],
        )

        editor_apply_global_rows.click(
            apply_global_rows_workspace,
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_ripple_following,
                editor_audio,
                editor_line_undo_payload,
                editor_token_json,
                editor_whole_pronunciation,
                editor_pronunciation_units,
            ],
            outputs=global_workspace_outputs,
            queue=False,
            cancels=[editor_preview_event, editor_line_draft_event],
        )

        def apply_global_shift_workspace(
            payload: dict[str, Any],
            table: object,
            line_number: int,
            offset: float,
            scope: str,
            audio: object,
            undo_state: dict[str, Any],
            token_json: str,
            whole_pronunciation: str,
            pronunciation_table: object,
            ripple_enabled: bool,
        ) -> tuple[object, ...]:
            before = document_from_payload(payload)
            document, _pending_snapshot = _editor_document_with_pending_changes(
                payload,
                table,
                int(line_number),
                token_json,
                whole_pronunciation,
                pronunciation_table,
                ripple_enabled,
            )
            selected = _mapped_editor_line_number(
                table,
                len(before.lines),
                int(line_number),
                len(document.lines),
            )
            start_line = selected if str(scope) == "suffix" else 1
            document, applied = shift_editor_timeline(
                document,
                document_to_editor_rows(document),
                float(offset),
                start_line=start_line,
            )
            changed = document.to_dict() != before.to_dict()
            snapshot = (
                _editor_undo_snapshot(before, int(line_number))
                if changed
                else undo_state
            )
            range_label = "当前句及之后" if start_line > 1 else "整首歌"
            status = (
                f"### ✅ 已将{range_label}整体平移 {applied:+.3f}s\n"
                "所有逐词时长和句内相对位置均保持不变。"
            )
            return global_workspace_result(
                document,
                selected,
                audio,
                status,
                snapshot,
            )

        editor_apply_global_shift.click(
            apply_global_shift_workspace,
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_global_offset,
                editor_global_scope,
                editor_audio,
                editor_line_undo_payload,
                editor_token_json,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_ripple_following,
            ],
            outputs=global_workspace_outputs,
            queue=False,
            cancels=[editor_preview_event, editor_line_draft_event],
        )
        editor_previous_line.click(
            lambda audio, payload, table, line_number, token_json, whole, units, undo_state, ripple, mode: (
                step_editor_line_workspace(
                    audio,
                    payload,
                    table,
                    line_number,
                    token_json,
                    whole,
                    units,
                    undo_state,
                    ripple,
                    -1,
                    load_audio=str(mode) != "global",
                    continuous_mode=str(mode) == "global",
                )
            ),
            inputs=[
                editor_audio,
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_token_json,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_line_undo_payload,
                editor_ripple_following,
                editor_timing_mode,
            ],
            outputs=editor_line_workspace_outputs,
            queue=False,
            cancels=[editor_preview_event, editor_line_draft_event],
        )
        editor_next_line.click(
            lambda audio, payload, table, line_number, token_json, whole, units, undo_state, ripple, mode: (
                step_editor_line_workspace(
                    audio,
                    payload,
                    table,
                    line_number,
                    token_json,
                    whole,
                    units,
                    undo_state,
                    ripple,
                    1,
                    load_audio=str(mode) != "global",
                    continuous_mode=str(mode) == "global",
                )
            ),
            inputs=[
                editor_audio,
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_token_json,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_line_undo_payload,
                editor_ripple_following,
                editor_timing_mode,
            ],
            outputs=editor_line_workspace_outputs,
            queue=False,
            cancels=[editor_preview_event, editor_line_draft_event],
        )

        editor_loop_line.change(
            lambda enabled: gr.update(loop=bool(enabled)),
            inputs=editor_loop_line,
            outputs=editor_line_audio,
            queue=False,
        )

        def advance_editor_line_after_playback(
            audio: object,
            payload: dict[str, Any],
            table: object,
            line_number: int,
            token_json: str,
            whole_pronunciation: str,
            pronunciation_table: object,
            undo_state: dict[str, Any],
            ripple_enabled: bool,
            timing_mode: str,
            loop_enabled: bool,
        ) -> tuple[object, ...]:
            before = document_from_payload(payload)
            document, _snapshot = _editor_document_with_pending_changes(
                payload,
                table,
                int(line_number),
                token_json,
                whole_pronunciation,
                pronunciation_table,
                ripple_enabled,
            )
            selected = _mapped_editor_line_number(
                table,
                len(before.lines),
                int(line_number),
                len(document.lines),
            )
            target = _next_playable_editor_line(document, selected)
            if str(timing_mode) == "global" or bool(loop_enabled) or target is None:
                return tuple(
                    gr.skip()
                    for _ in [*editor_line_workspace_outputs, editor_audio_refresh_trigger]
                )
            stepped = step_editor_line_workspace(
                audio,
                payload,
                table,
                int(line_number),
                token_json,
                whole_pronunciation,
                pronunciation_table,
                undo_state,
                ripple_enabled,
                0,
                load_audio=False,
                absolute_target=target,
            )
            return (*stepped, uuid4().hex)

        editor_auto_advance_event = editor_line_audio.stop(
            advance_editor_line_after_playback,
            inputs=[
                editor_audio,
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_token_json,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_line_undo_payload,
                editor_ripple_following,
                editor_timing_mode,
                editor_loop_line,
            ],
            outputs=[*editor_line_workspace_outputs, editor_audio_refresh_trigger],
            queue=True,
            trigger_mode="always_last",
            concurrency_limit=1,
            concurrency_id="editor-auto-advance",
            cancels=[editor_preview_event, editor_line_draft_event],
            js=EDITOR_STOP_GATE_JS,
        )
        gr.on(
            triggers=[
                editor_load.click,
                editor_load_line.click,
                editor_lines.select,
                editor_lines.input,
                editor_previous_line.click,
                editor_next_line.click,
                editor_listen_line.click,
                editor_loop_line.change,
                editor_token_json.input,
                editor_whole_pronunciation.input,
                editor_pronunciation_units.input,
            ],
            fn=None,
            cancels=[editor_auto_advance_event],
            queue=False,
            show_progress="hidden",
        )

        def refresh_editor_audio_for_mode(
            audio: object,
            payload: dict[str, Any],
            table: object,
            line_number: int,
            mode: str,
        ) -> tuple[object, object]:
            if str(mode) == "global":
                return gr.skip(), gr.skip()
            return editor_audio_with_prefetch(audio, payload, table, line_number)

        editor_audio_refresh_trigger.change(
            refresh_editor_audio_for_mode,
            inputs=[
                editor_audio,
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_timing_mode,
            ],
            outputs=[editor_line_audio, editor_timing_status],
            queue=True,
            trigger_mode="always_last",
            concurrency_limit=1,
            concurrency_id="editor-audio-refresh",
            cancels=[editor_auto_advance_event],
            show_progress="hidden",
        )

        def refresh_audio_after_editor_event(dependency: Any) -> None:
            dependency.then(
                lambda: uuid4().hex,
                outputs=editor_audio_refresh_trigger,
                queue=False,
            )

        refresh_audio_after_editor_event(editor_load_event)
        refresh_audio_after_editor_event(editor_load_line_event)

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

        def apply_editor_line_action_workspace(
            payload: dict[str, Any],
            table: object,
            line_number: int,
            token_json: str,
            whole_pronunciation: str,
            pronunciation_table: object,
            action_request: str,
            ripple_enabled: bool,
        ) -> tuple[object, ...]:
            before = document_from_payload(payload)
            undo_focus = min(max(1, int(line_number)), len(before.lines))
            request_data: dict[str, Any] | None = None
            raw_action_row: int | None = None
            try:
                parsed_request = json.loads(str(action_request or ""))
                if not isinstance(parsed_request, dict):
                    raise TypeError
                request_data = parsed_request
                raw_action_row = int(parsed_request["row"])
                raw_rows_before_action = _table_rows(table)
                if (
                    not bool(parsed_request.get("current"))
                    and 0 <= raw_action_row < len(raw_rows_before_action)
                ):
                    raw_target = [*raw_rows_before_action[raw_action_row], None][:1]
                    try:
                        source_id = int(float(raw_target[0]))
                    except (TypeError, ValueError):
                        source_id = 0
                    if 1 <= source_id <= len(before.lines):
                        undo_focus = source_id
                if str(parsed_request.get("action")) == "delete":
                    table = _editor_rows_for_delete(
                        payload,
                        table,
                        raw_action_row,
                    )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                # The line-action handler below owns the user-facing validation.
                pass
            if request_data is not None and bool(request_data.get("current")):
                original_selected = min(max(1, int(line_number)), len(before.lines))
                if _editor_row_source_ids(table, len(before.lines)).count(
                    original_selected
                ) != 1:
                    raise ValueError(
                        "当前句已被删除或重复；请先应用全局表格修改，再选择歌词行。"
                    )
            document, _snapshot = _editor_document_with_pending_changes(
                payload,
                table,
                int(line_number),
                token_json,
                whole_pronunciation,
                pronunciation_table,
                False,
            )
            selected = _mapped_editor_line_number(
                table,
                len(before.lines),
                int(line_number),
                len(document.lines),
            )
            rows = document_to_editor_rows(document)
            if request_data is not None:
                if bool(request_data.get("current")):
                    mapped_action_row = selected - 1
                elif raw_action_row is not None:
                    raw_rows = _table_rows(table)
                    if raw_action_row < 0 or raw_action_row >= len(raw_rows):
                        raise ValueError("选择的歌词行已经变化，请重新选择。")
                    mapped_action_row = -1
                    for raw_index, raw_row in enumerate(raw_rows):
                        padded = [*raw_row, None, None][:2]
                        if str(padded[1] or "显示").strip() != LINE_STATUS_DELETED:
                            mapped_action_row += 1
                        if raw_index == raw_action_row:
                            if str(padded[1] or "显示").strip() == LINE_STATUS_DELETED:
                                raise ValueError("选择的歌词行已经删除，请重新选择。")
                            break
                else:
                    raise ValueError("歌词行操作缺少目标行。")
                request_data["row"] = mapped_action_row
                action_request = json.dumps(request_data, ensure_ascii=False)
            action_result = apply_editor_line_action(
                document.to_dict(),
                rows,
                selected,
                action_request,
            )
            after = document_from_payload(action_result[0])
            final_source_ids = _editor_row_source_ids(table, len(before.lines))
            action = str(request_data.get("action")) if request_data is not None else ""
            if action == "delete":
                final_source_ids.pop(mapped_action_row)
            elif action == "insert-before":
                final_source_ids.insert(mapped_action_row, None)
            elif action == "insert-after":
                final_source_ids.insert(mapped_action_row + 1, None)
            shifted_lines = _ripple_global_editor_changes(
                before,
                after,
                ripple_enabled,
                source_ids=final_source_ids,
            )
            ripple_note = (
                f"\n已联动后移第 {shifted_lines[0]}–{shifted_lines[-1]} 行。"
                if shifted_lines
                else ""
            )
            final_selected = min(max(1, int(action_result[2])), len(after.lines))
            return (
                *_editor_selected_line_outputs(after, final_selected),
                f"{action_result[8]}{ripple_note}",
                _editor_undo_snapshot(before, undo_focus),
            )

        def apply_editor_current_line_action_workspace(
            payload: dict[str, Any],
            table: object,
            line_number: int,
            token_json: str,
            whole_pronunciation: str,
            pronunciation_table: object,
            action: str,
            ripple_enabled: bool,
        ) -> tuple[object, ...]:
            request = json.dumps(
                {"row": int(line_number) - 1, "action": action, "current": True},
                ensure_ascii=False,
            )
            return apply_editor_line_action_workspace(
                payload,
                table,
                line_number,
                token_json,
                whole_pronunciation,
                pronunciation_table,
                request,
                ripple_enabled,
            )

        editor_context_action_event = editor_apply_context_action.click(
            apply_editor_line_action_workspace,
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_token_json,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_line_context_action,
                editor_ripple_following,
            ],
            outputs=editor_line_action_outputs,
            queue=False,
            cancels=[
                editor_preview_event,
                editor_line_draft_event,
                editor_auto_advance_event,
            ],
        )
        editor_toggle_line_event = editor_toggle_line_hidden.click(
            lambda payload, table, line_number, token_json, whole, units, ripple: (
                apply_editor_current_line_action_workspace(
                    payload,
                    table,
                    line_number,
                    token_json,
                    whole,
                    units,
                    "toggle-hidden",
                    ripple,
                )
            ),
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_token_json,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_ripple_following,
            ],
            outputs=editor_line_action_outputs,
            queue=False,
            cancels=[
                editor_preview_event,
                editor_line_draft_event,
                editor_auto_advance_event,
            ],
        )
        editor_delete_line_event = editor_delete_line.click(
            lambda payload, table, line_number, token_json, whole, units, ripple: (
                apply_editor_current_line_action_workspace(
                    payload,
                    table,
                    line_number,
                    token_json,
                    whole,
                    units,
                    "delete",
                    ripple,
                )
            ),
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_token_json,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_ripple_following,
            ],
            outputs=editor_line_action_outputs,
            queue=False,
            cancels=[
                editor_preview_event,
                editor_line_draft_event,
                editor_auto_advance_event,
            ],
        )
        undo_editor_event = editor_undo_line_action.click(
            undo_editor_line_action,
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_line_undo_payload,
            ],
            outputs=editor_line_action_outputs,
            queue=False,
            cancels=[
                editor_preview_event,
                editor_line_draft_event,
                editor_auto_advance_event,
            ],
        )
        for editor_event in [
            editor_context_action_event,
            editor_toggle_line_event,
            editor_delete_line_event,
            undo_editor_event,
        ]:
            refresh_audio_after_editor_event(editor_event)
        def listen_editor_line_for_mode(
            audio: object,
            payload: dict[str, Any],
            table: object,
            line_number: int,
            mode: str,
        ) -> tuple[object, str]:
            if str(mode) == "global":
                return (
                    gr.skip(),
                    "全局模式中请直接在整曲播放器上试听，不会重新生成逐句片段。",
                )
            return preview_editor_audio_line(audio, payload, table, line_number)

        editor_listen_line.click(
            listen_editor_line_for_mode,
            inputs=[
                editor_audio,
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_timing_mode,
            ],
            outputs=[editor_line_audio, editor_timing_status],
            queue=False,
            trigger_mode="always_last",
            cancels=[editor_auto_advance_event],
        )

        def nudge_editor_timing_workspace(
            payload: dict[str, Any],
            table: object,
            line_number: int,
            token_json: str,
            whole_pronunciation: str,
            pronunciation_table: object,
            undo_state: dict[str, Any],
            ripple_enabled: bool,
            *,
            start_delta: float = 0.0,
            end_delta: float = 0.0,
        ) -> tuple[object, ...]:
            before = document_from_payload(payload)
            source_ids = _editor_row_source_ids(table, len(before.lines))
            original_selected = min(max(1, int(line_number)), len(before.lines))
            if source_ids.count(original_selected) != 1:
                raise ValueError(
                    "当前句已被删除或重复；请先应用全局表格修改，再选择要微调的歌词。"
                )
            document, pending_snapshot = _editor_document_with_pending_changes(
                payload,
                table,
                int(line_number),
                token_json,
                whole_pronunciation,
                pronunciation_table,
            )
            selected = _mapped_editor_line_number(
                table,
                len(before.lines),
                int(line_number),
                len(document.lines),
            )
            rows = document_to_editor_rows(document)
            result = nudge_editor_timing(
                document.to_dict(),
                rows,
                selected,
                start_delta=start_delta,
                end_delta=end_delta,
                ripple_following=False,
            )
            after = document_from_payload(result[0])
            shifted_lines = _ripple_global_editor_changes(
                before,
                after,
                ripple_enabled,
                source_ids=source_ids,
            )
            line = after.lines[selected - 1]
            assert line.start is not None and line.end is not None
            ripple_note = (
                f"；已联动后移第 {shifted_lines[0]}–{shifted_lines[-1]} 行"
                if shifted_lines
                else ""
            )
            result = (
                after.to_dict(),
                document_to_editor_rows(after),
                selected,
                editor_preview_html(after, selected),
                (
                    f"第 {selected} 行：**{line.start:.2f}s → {line.end:.2f}s**"
                    f"（时长 {line.end - line.start:.2f}s）{ripple_note}"
                ),
            )
            token_timeline, token_json = editor_token_workspace(
                result[0],
                result[1],
                selected,
            )
            after = document_from_payload(result[0])
            next_undo = (
                pending_snapshot or _editor_undo_snapshot(before, int(line_number))
                if after.to_dict() != before.to_dict()
                else undo_state
            )
            return (*result, token_timeline, token_json, next_undo)

        for timing_button, timing_function in [
            (
                editor_start_earlier,
                lambda payload, table, line_number, token_json, whole, units, undo_state, ripple: (
                    nudge_editor_timing_workspace(
                        payload,
                        table,
                        line_number,
                        token_json,
                        whole,
                        units,
                        undo_state,
                        ripple,
                        start_delta=-0.1,
                    )
                ),
            ),
            (
                editor_start_later,
                lambda payload, table, line_number, token_json, whole, units, undo_state, ripple: (
                    nudge_editor_timing_workspace(
                        payload,
                        table,
                        line_number,
                        token_json,
                        whole,
                        units,
                        undo_state,
                        ripple,
                        start_delta=0.1,
                    )
                ),
            ),
            (
                editor_end_earlier,
                lambda payload, table, line_number, token_json, whole, units, undo_state, ripple: (
                    nudge_editor_timing_workspace(
                        payload,
                        table,
                        line_number,
                        token_json,
                        whole,
                        units,
                        undo_state,
                        ripple,
                        end_delta=-0.1,
                    )
                ),
            ),
            (
                editor_end_later,
                lambda payload, table, line_number, token_json, whole, units, undo_state, ripple: (
                    nudge_editor_timing_workspace(
                        payload,
                        table,
                        line_number,
                        token_json,
                        whole,
                        units,
                        undo_state,
                        ripple,
                        end_delta=0.1,
                    )
                ),
            ),
        ]:
            timing_event = timing_button.click(
                timing_function,
                inputs=[
                    editor_payload,
                    editor_lines,
                    editor_line_number,
                    editor_token_json,
                    editor_whole_pronunciation,
                    editor_pronunciation_units,
                    editor_line_undo_payload,
                    editor_ripple_following,
                ],
                outputs=[
                    editor_payload,
                    editor_lines,
                    editor_line_number,
                    editor_preview,
                    editor_timing_status,
                    editor_token_timeline,
                    editor_token_json,
                    editor_line_undo_payload,
                ],
                queue=False,
                cancels=[
                    editor_preview_event,
                    editor_line_draft_event,
                    editor_auto_advance_event,
                ],
            )
            refresh_audio_after_editor_event(timing_event)

        def save_editor_token_timing_workspace(
            payload: dict[str, Any],
            table: object,
            line_number: int,
            token_json: str,
            whole_pronunciation: str,
            pronunciation_table: object,
            undo_state: dict[str, Any],
            ripple_enabled: bool,
        ) -> tuple[object, ...]:
            before = document_from_payload(payload)
            source_ids = _editor_row_source_ids(table, len(before.lines))
            document, pending_snapshot = _editor_document_with_pending_changes(
                payload,
                table,
                int(line_number),
                token_json,
                whole_pronunciation,
                pronunciation_table,
            )
            selected = _mapped_editor_line_number(
                table,
                len(before.lines),
                int(line_number),
                len(document.lines),
            )
            # The pending-change helper has already validated and applied the
            # token draft to its stable source line. Applying the same JSON a
            # second time after deletion/reordering could overwrite a neighbour.
            after = document
            shifted_lines = _ripple_global_editor_changes(
                before,
                after,
                ripple_enabled,
                source_ids=source_ids,
            )
            line = after.lines[selected - 1]
            assert line.start is not None and line.end is not None
            ripple_note = (
                f"；已联动后移第 {shifted_lines[0]}–{shifted_lines[-1]} 行"
                if shifted_lines
                else ""
            )
            result = (
                after.to_dict(),
                document_to_editor_rows(after),
                selected,
                line.pronunciation or "",
                document_pronunciation_to_editor_rows(after, line),
                editor_preview_html(after, selected),
                editor_token_timeline_html(after, selected),
                token_timing_to_json(line),
                (
                    f"### ✅ 已保存第 {selected} 行逐词时间\n"
                    f"整句范围：**{line.start:.2f}s → {line.end:.2f}s**{ripple_note}。"
                ),
            )
            next_undo = (
                pending_snapshot or _editor_undo_snapshot(before, int(line_number))
                if after.to_dict() != before.to_dict()
                else undo_state
            )
            return (*result, next_undo)

        editor_save_tokens_event = editor_save_tokens.click(
            save_editor_token_timing_workspace,
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_token_json,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_line_undo_payload,
                editor_ripple_following,
            ],
            outputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_preview,
                editor_token_timeline,
                editor_token_json,
                editor_timing_status,
                editor_line_undo_payload,
            ],
            queue=False,
            cancels=[
                editor_preview_event,
                editor_line_draft_event,
                editor_auto_advance_event,
            ],
        )
        refresh_audio_after_editor_event(editor_save_tokens_event)

        editor_save_pronunciation.click(
            save_editor_pronunciation_workspace,
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_token_json,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_line_undo_payload,
                editor_ripple_following,
            ],
            outputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_preview,
                editor_status,
                editor_line_undo_payload,
            ],
            queue=False,
            cancels=[
                editor_preview_event,
                editor_line_draft_event,
                editor_auto_advance_event,
            ],
        )

        def export_editor_project_workspace(
            payload: dict[str, Any],
            line_table: object,
            line_number: int,
            whole_line: str,
            pronunciation_table: object,
            output_name: str,
            token_timing_json: str | None,
            ripple_enabled: bool,
        ) -> tuple[object, ...]:
            before = document_from_payload(payload)
            result = export_editor_project_for_web(
                payload,
                line_table,
                line_number,
                whole_line,
                pronunciation_table,
                output_name,
                token_timing_json,
                ripple_enabled,
            )
            try:
                result_count = len(document_from_payload(result[0]).lines)
                selected = _mapped_editor_line_number(
                    line_table,
                    len(before.lines),
                    int(line_number),
                    result_count,
                )
            except (TypeError, ValueError):
                selected = min(max(1, int(line_number)), len(before.lines))
            _allow_gradio_file_paths(app, result[3])
            _allow_gradio_result_workspaces(app, result[3])
            if isinstance(result[0], dict):
                manifest_value = result[0].get("metadata", {}).get("workspace_manifest")
                if manifest_value:
                    _allow_gradio_workspace_paths(app, manifest_value)
            return result[0], result[1], selected, *result[2:]

        editor_export_event = editor_export.click(
            export_editor_project_workspace,
            inputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_whole_pronunciation,
                editor_pronunciation_units,
                editor_name,
                editor_token_json,
                editor_ripple_following,
            ],
            outputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_status,
                editor_downloads,
                editor_output_directory,
            ],
            api_name="export_editor_project",
        )
        export_to_make_event = editor_export_event.then(
            exported_project_for_make,
            inputs=editor_downloads,
            outputs=[make_lyrics, make_lyrics_status],
            queue=False,
        )
        export_preview_event = export_to_make_event.then(
            prepare_subtitle_material_preview,
            inputs=material_preview_inputs,
            outputs=material_preview_outputs,
            show_progress="hidden",
            concurrency_limit=1,
            concurrency_id="make-material-preview",
        )
        export_preview_event.then(
            subtitle_preview_html,
            inputs=preview_inputs,
            outputs=make_style_preview,
            queue=False,
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
            ripple_enabled: bool,
        ) -> tuple[object, ...]:
            try:
                before = document_from_payload(payload)
                timed_document, _snapshot = _editor_document_with_pending_changes(
                    payload,
                    line_table,
                    int(line_number),
                    token_timing_json,
                    whole_line,
                    pronunciation_table,
                    ripple_enabled,
                )
                timed_payload = timed_document.to_dict()
                timed_rows = document_to_editor_rows(timed_document)
                selected = _mapped_editor_line_number(
                    line_table,
                    len(before.lines),
                    int(line_number),
                    len(timed_document.lines),
                )
                timed_line = timed_document.lines[selected - 1]
                result = handoff_editor_to_make(
                    timed_payload,
                    timed_rows,
                    selected,
                    timed_line.pronunciation or "",
                    document_pronunciation_to_editor_rows(timed_document, timed_line),
                    output_name,
                    audio_file,
                )
                _allow_gradio_file_paths(app, result[3], result[5], result[6])
                _allow_gradio_result_workspaces(app, result[3])
                metadata = result[0].get("metadata") if isinstance(result[0], dict) else None
                if isinstance(metadata, dict) and metadata.get("workspace_manifest"):
                    _allow_gradio_workspace_paths(app, metadata["workspace_manifest"])
                return (
                    result[0],
                    result[1],
                    selected,
                    *result[2:],
                    gr.update(selected="make"),
                    True,
                )
            except Exception as exc:
                log_path = _record_web_error("editor-handoff", exc)
                log_note = f"\n\n详细记录：`{log_path}`" if log_path else ""
                return (
                    payload,
                    line_table,
                    min(max(1, int(line_number)), len(document_from_payload(payload).lines)),
                    f"### ⚠️ 无法交给制作页\n{exc}{log_note}",
                    [],
                    str(_default_output_root()),
                    gr.skip(),
                    gr.skip(),
                    gr.skip(),
                    False,
                )

        editor_handoff_event = editor_handoff.click(
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
                editor_ripple_following,
            ],
            outputs=[
                editor_payload,
                editor_lines,
                editor_line_number,
                editor_status,
                editor_downloads,
                editor_output_directory,
                make_lyrics,
                make_audio,
                main_tabs,
                editor_handoff_ready,
            ],
        )

        def make_after_editor_handoff(
            handoff_ready: bool,
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
            music_u: str,
            auto_sync: bool,
            timing_refinement: str,
            show_translation: bool,
            translation_font_size: float,
            translation_color: str,
            show_pronunciation: bool,
            pronunciation_font_size: float,
            pronunciation_color: str,
            output_root: str,
            qqmusic_link: str,
            use_qqmusic_lyrics: bool,
            utaten_link: str,
            use_utaten_lyrics: bool,
            utaten_pronunciation_only: bool,
            auto_english_pronunciation: bool,
            cover: object,
            font_files: object,
            cover_background: str,
            cover_style: str,
            cover_waveform: bool,
            export_original: bool,
            export_instrumental: bool,
            translation_margin_v: float,
            show_countdown: bool,
            countdown_gap_threshold: float,
            request: object | None = None,
            progress: object = gr.Progress(),
        ) -> tuple[object, ...]:
            if not handoff_ready:
                return tuple(gr.skip() for _ in range(5))

            stop_result = handoff_make_readiness(video, lyrics, cover)
            if stop_result is not None:
                progress(1.0, desc="等待上传 MV 或专辑图片")
                return (
                    stop_result.status,
                    stop_result.video,
                    stop_result.files,
                    stop_result.log,
                    stop_result.output_dir,
                )

            return make_wrapper(
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
                music_u,
                auto_sync,
                timing_refinement,
                show_translation,
                translation_font_size,
                translation_color,
                show_pronunciation,
                pronunciation_font_size,
                pronunciation_color,
                output_root,
                qqmusic_link,
                use_qqmusic_lyrics,
                utaten_link,
                use_utaten_lyrics,
                utaten_pronunciation_only,
                auto_english_pronunciation,
                cover,
                font_files,
                cover_background,
                cover_style,
                cover_waveform,
                export_original,
                export_instrumental,
                translation_margin_v,
                show_countdown,
                countdown_gap_threshold,
                request=request,
                progress=progress,
            )

        make_after_editor_handoff.__annotations__["request"] = gr.Request

        editor_handoff_event.then(
            make_after_editor_handoff,
            inputs=[
                editor_handoff_ready,
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
                netease_session_music_u,
                make_auto_sync,
                make_timing_refinement,
                make_show_translation,
                make_translation_size,
                make_translation_color,
                make_show_pronunciation,
                make_pronunciation_size,
                make_pronunciation_color,
                make_output_root,
                make_qqmusic_link,
                make_use_qqmusic_lyrics,
                make_utaten_link,
                make_use_utaten_lyrics,
                make_utaten_pronunciation_only,
                make_auto_english_pronunciation,
                make_cover,
                make_font_files,
                make_cover_background,
                make_cover_style,
                make_cover_waveform,
                make_export_original,
                make_export_instrumental,
                make_translation_margin,
                make_show_countdown,
                make_countdown_gap,
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
        make_lyrics.change(
            inspect_make_lyrics,
            inputs=make_lyrics,
            outputs=make_lyrics_status,
            queue=False,
        )
        font_name_event = make_font_files.change(
            lambda files: _file_paths(files)[0].stem if _file_paths(files) else gr.skip(),
            inputs=make_font_files,
            outputs=make_font,
            queue=False,
        )
        font_name_event.then(
            subtitle_preview_html,
            inputs=preview_inputs,
            outputs=make_style_preview,
            queue=False,
        )
        auto_model_network.click(
            auto_configure_model_network_for_web,
            outputs=[
                model_network_status,
                model_network_mode,
                model_proxy_url,
                model_mirror_confirmed,
            ],
        )
        save_model_network.click(
            configure_model_network_for_web,
            inputs=[model_network_mode, model_proxy_url, model_mirror_confirmed],
            outputs=model_network_status,
        )
        predownload_model.click(
            predownload_model_for_web,
            inputs=model_prefetch_profile,
            outputs=model_network_status,
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
        open_qqmusic_dir.click(
            _open_output_directory,
            inputs=qqmusic_output_directory,
            outputs=open_qqmusic_message,
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

    managed_netease_login = _is_loopback_host(host)
    initial_music_u = ""
    if managed_netease_login:
        try:
            initial_music_u = try_reuse_netease_music_u(timeout_seconds=12.0) or ""
        except NeteaseLoginError:
            initial_music_u = ""
        except Exception as exc:
            _record_web_error("netease-session-restore", exc)
    app = create_web_app(
        managed_netease_login=managed_netease_login,
        initial_netease_music_u=initial_music_u,
    )
    theme = gr.themes.Base(
        primary_hue="orange",
        secondary_hue="teal",
        neutral_hue="slate",
        radius_size="lg",
    )
    allowed_paths = {str(output_root)}
    for workspace in _valid_workspace_projects():
        for path in (
            workspace.manifest,
            workspace.lyrics_project,
            workspace.audio,
            workspace.video,
            workspace.cover,
            *workspace.font_files,
        ):
            if path is not None and path.is_file():
                allowed_paths.add(str(path.resolve()))
    app.queue(default_concurrency_limit=1).launch(
        server_name=host,
        server_port=port,
        inbrowser=open_browser,
        share=False,
        show_error=True,
        theme=theme,
        css=WEB_CSS,
        allowed_paths=sorted(allowed_paths),
    )
