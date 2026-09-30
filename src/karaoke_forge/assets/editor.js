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

  const editorPreferences = (() => {
    try {
      return JSON.parse(document.querySelector(
        "#kf-editor-preferences textarea, #kf-editor-preferences input"
      )?.value || "{}");
    } catch { return {}; }
  })();
  let appliedPreferenceJson = null;
  const saveViewPreference = (key, value) => {
    editorPreferences[key] = value;
    window.clearTimeout(window.__kfPreferenceSaveTimer);
    window.__kfPreferencePatch = { ...window.__kfPreferencePatch, [key]: value };
    window.__kfPreferenceSaveTimer = window.setTimeout(() => {
      setHiddenInput("#kf-editor-preferences-update", JSON.stringify(window.__kfPreferencePatch));
      window.__kfPreferencePatch = {};
    }, 120);
  };
  let globalView = null;
  const rememberGlobalView = (timeline) => {
    const scroll = timeline?.querySelector(".kf-global-scroll");
    const canvas = timeline?.querySelector(".kf-global-canvas");
    if (!scroll || !canvas || !canvas.scrollWidth) return;
    globalView = {
      key: timeline.dataset.projectKey,
      seconds: scroll.scrollLeft / canvas.scrollWidth * Number(timeline.dataset.duration),
    };
  };
  const syncGlobalTools = (timeline) => {
    for (const [selector, key, label] of [
      [".kf-global-snap", "snap_enabled", "吸附"],
      [".kf-global-follow", "follow_playback", "跟随播放"],
    ]) {
      const button = timeline?.querySelector(selector);
      const enabled = editorPreferences[key] !== false;
      button?.setAttribute("aria-pressed", String(enabled));
      if (button) button.textContent = `${label}：${enabled ? "开" : "关"}`;
    }
  };
  const stopGlobalFollow = (timeline) => {
    saveViewPreference("follow_playback", false);
    syncGlobalTools(timeline);
  };
  const applyGlobalZoom = (timeline, mode) => {
    const scroll = timeline?.querySelector(".kf-global-scroll");
    const canvas = timeline?.querySelector(".kf-global-canvas");
    if (!scroll || !canvas) return;
    const oldWidth = Math.max(1, canvas.scrollWidth);
    const base = Math.max(1, Number(canvas.dataset.baseWidth));
    const current = Number(canvas.dataset.zoom || 1);
    const next = mode === "fit" ? 0 : Math.max(0.1, Math.min(8,
      (current || scroll.clientWidth / base) * (mode === "in" ? 1.35 : 1 / 1.35)));
    const head = timeline.querySelector(".kf-global-playhead");
    const headX = Number.parseFloat(head?.style.left || 0) / 100 * oldWidth;
    const inView = headX >= scroll.scrollLeft && headX <= scroll.scrollLeft + scroll.clientWidth;
    const anchorX = inView ? headX : scroll.scrollLeft + scroll.clientWidth / 2;
    const screenX = anchorX - scroll.scrollLeft;
    canvas.dataset.zoom = String(next);
    canvas.style.minWidth = next === 0 ? "100%" : `${Math.max(scroll.clientWidth, base * next)}px`;
    scroll.scrollLeft = Math.max(0, anchorX / oldWidth * canvas.scrollWidth - screenX);
    saveViewPreference("global_zoom", next);
    rememberGlobalView(timeline);
    stopGlobalFollow(timeline);
  };
  const escapeEditorText = (value) => String(value ?? "").replace(/[&<>"']/g,
    character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  const editorMutationIds = [
    "kf-global-edge-apply", "editor-save-global-rows", "editor-shift-global",
    "kf-line-context-apply", "editor-toggle-line", "editor-delete-line",
    "editor-undo", "editor-redo", "editor-start-earlier", "editor-start-later",
    "editor-end-earlier", "editor-end-later", "editor-save-tokens",
    "editor-save-pronunciation", "editor-export", "editor-handoff",
    "editor-reload-line", "editor-previous-line", "editor-next-line",
  ];
  const setEditorBusy = (busy) => {
    for (const selector of [
      "#editor-token-tuning-panel", "#editor-pronunciation-panel", "#editor-lines",
      "#editor-timing-mode", "#editor-line-controls", "#editor-overview-panel",
      "#editor-project-actions", "#editor-project-loader",
    ]) {
      const panel = document.querySelector(selector);
      const closedDrawer = selector === "#editor-overview-panel" &&
        !panel?.classList.contains("is-open");
      if (busy || closedDrawer) panel?.setAttribute("inert", "");
      else panel?.removeAttribute("inert");
    }
  };
  const beginEditorMutation = () => {
    window.__kfEditorMutationPending = {
      acknowledgment: document.querySelector(
        "#kf-editor-mutation-ack input, #kf-editor-mutation-ack textarea"
      )?.value || "",
    };
    pauseForEditorMutation();
    setEditorBusy(true);
  };
  document.addEventListener("change", (event) => {
    if (!event.target.matches?.("#editor-timing-mode input[type='radio']")) return;
    if (window.__kfEditorMutationPending || window.__kfGlobalNavigation?.pending) return;
    beginEditorMutation();
  }, true);
  document.addEventListener("click", (event) => {
    const cell = event.target.closest?.("#editor-lines tbody td, #editor-lines [role='gridcell']");
    const column = cell?.cellIndex ?? (Number(cell?.getAttribute("aria-colindex")) - 1);
    const rowSelection = cell && column === 0;
    const action = event.target.closest?.(editorMutationIds.map(id => `#${id}`).join(",")) ||
      (rowSelection ? { id: "editor-row-select" } : null);
    if (!action || (!rowSelection && !event.target.closest?.("button"))) return;
    if (["editor-previous-line", "editor-next-line"].includes(action.id) && globalEditorModeActive()) return;
    if (window.__kfEditorMutationPending || window.__kfGlobalNavigation?.pending) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (["editor-undo", "editor-redo"].includes(action.id)) {
        (window.__kfEditorHistoryQueue ||= []).push(action.id);
      }
      return;
    }
    if (rowSelection) window.__kfOverviewSeek = globalEditorModeActive();
    beginEditorMutation();
  }, true);
  const showGlobalPreview = (activeBlock) => {
    if (!activeBlock) return;
    const timeline = activeBlock.closest(".kf-global-timeline");
    const host = document.querySelector("#editor-preview");
    if (!timeline || !host) return;
    if (!timeline.__kfPreviewLines) {
      try { timeline.__kfPreviewLines = JSON.parse(timeline.dataset.previewLines || "[]"); }
      catch { timeline.__kfPreviewLines = []; }
    }
    const lines = timeline.__kfPreviewLines;
    const index = lines.findIndex(line => line.number === Number(activeBlock.dataset.lineNumber));
    if (index < 0) return;
    const line = lines[index];
    let stage = host.querySelector(".kf-editor-preview-stage");
    if (stage && Number(stage.dataset.lineNumber) === line.number &&
        Math.abs(Number(stage.dataset.lineStart) - line.start) < .0005 &&
        Math.abs(Number(stage.dataset.lineEnd) - line.end) < .0005) {
      // A matching server preview may contain the newest unsaved ruby draft.
      stage.__kfGlobalSource = timeline;
      return;
    }
    if (!stage) {
      stage = document.createElement("div");
      stage.className = "kf-editor-preview-stage";
      host.replaceChildren(stage);
    }
    const measure = line.tokens.map((token, index) => {
      const text = String(token.text);
      const leading = text.match(/^\s*/)[0];
      const trailing = text.match(/\s*$/)[0];
      const core = text.slice(leading.length, text.length - trailing.length || undefined);
      return `<span class="kf-karaoke-token-space">${escapeEditorText(leading)}</span>` +
        `<span class="kf-karaoke-token-core" data-token-index="${index}">${escapeEditorText(core)}</span>` +
        `<span class="kf-karaoke-token-space">${escapeEditorText(trailing)}</span>`;
    }).join("");
    const active = `<div class="kf-live-karaoke-current" data-line-start="${line.start}" ` +
      `data-line-end="${line.end}" style="position:relative;display:inline-block;color:white">` +
      `<div class="kf-live-karaoke-base">${line.ruby}</div>` +
      `<div class="kf-live-karaoke-fill" style="position:absolute;inset:0;color:#ffd54a;clip-path:inset(0 100% 0 0)">${line.ruby}</div>` +
      `<span class="kf-live-karaoke-measure" aria-hidden="true" style="position:absolute;left:0;top:0;visibility:hidden;pointer-events:none;white-space:inherit">${measure}</span></div>`;
    const upcoming = lines[index + 1] ? `<div style="color:white">${lines[index + 1].ruby}</div>` : "";
    stage.innerHTML = `<div class="kf-editor-preview-info">第 ${line.number} 行 · 当前句黄色 / 下一句白色</div>` +
      `<div class="kf-editor-preview-translation">${escapeEditorText(line.translation)}</div>` +
      `<div class="kf-editor-preview-row kf-editor-preview-upper">${index % 2 ? upcoming : active}</div>` +
      `<div class="kf-editor-preview-row kf-editor-preview-lower">${index % 2 ? active : upcoming}</div>`;
    Object.assign(stage.dataset, { lineNumber: String(line.number), lineStart: String(line.start),
      lineEnd: String(line.end), lineCount: timeline.dataset.lineCount });
    stage.__kfGlobalSource = timeline;
  };

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
    saveViewPreference("token_zoom", mode === "fit" ? 0 : nextZoom);
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
    stage.style.setProperty("--kf-preview-font-size", `${next}px`);
    saveViewPreference("preview_font_size", next);
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
    saveViewPreference("overview_font_size", next);
  };

  document.addEventListener("wheel", (event) => {
    if (!(event.ctrlKey || event.metaKey)) return;
    const timeline = event.target.closest?.(".kf-token-editor");
    const globalTimeline = event.target.closest?.(".kf-global-timeline");
    const previewStage = event.target.closest?.(".kf-editor-preview-stage");
    const overview = event.target.closest?.("#editor-lines");
    if (!timeline && !globalTimeline && !previewStage && !overview) return;
    event.preventDefault();
    const direction = event.deltaY < 0 ? "in" : "out";
    if (timeline) applyTimelineZoom(timeline, direction);
    if (globalTimeline) applyGlobalZoom(globalTimeline, direction);
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
    const navigation = window.__kfGlobalNavigation ||= { pending: null, latest: null };
    navigation.latest = Number(lineNumber);
    if (navigation.pending || window.__kfEditorMutationPending) return;
    const requested = navigation.latest;
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    if (!setHiddenInput("#kf-global-line-request", String(requested))) return;
    if (!setHiddenInput("#kf-global-navigation-id", id)) return;
    navigation.pending = { id, line: requested };
    setEditorBusy(true);
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
    showGlobalPreview(activeBlock);
    const preview = visibleElement(".kf-editor-preview-stage");
    const activeLine = Number(activeBlock?.dataset.lineNumber);
    const previewLine = Number(preview?.dataset.lineNumber);
    const matches = Boolean(
      activeBlock && preview && Number.isFinite(activeLine) && activeLine === previewLine
    );
    preview?.classList.toggle("is-global-gap", !activeBlock);
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
    window.__kfRestoreEditorViews?.();
    window.__kfSyncOverviewState?.();
    const preferenceJson = document.querySelector(
      "#kf-editor-preferences textarea, #kf-editor-preferences input"
    )?.value;
    if (preferenceJson && preferenceJson !== appliedPreferenceJson) {
      appliedPreferenceJson = preferenceJson;
      try { Object.assign(editorPreferences, JSON.parse(preferenceJson)); } catch {}
      document.querySelectorAll(".kf-global-timeline, .kf-token-editor").forEach(timeline => {
        timeline.__kfViewRestored = false;
      });
    }
    const mutation = window.__kfEditorMutationPending;
    const mutationAck = document.querySelector(
      "#kf-editor-mutation-ack input, #kf-editor-mutation-ack textarea"
    )?.value || "";
    if (mutation && mutationAck && mutationAck !== mutation.acknowledgment) {
      window.__kfEditorMutationPending = null;
      window.__kfGlobalEdgePending?.();
      window.__kfGlobalEdgePending = null;
      setEditorBusy(false);
      if (window.__kfOverviewSeek) {
        window.__kfOverviewSeek = false;
        const timeline = visibleElement(".kf-global-timeline");
        const block = timeline?.querySelector(`.kf-global-line-block[data-line-number="${displayedEditorLine()}"]`);
        if (block) {
          window.__karaokeForgeGlobalFollowLine = displayedEditorLine();
          markGlobalLineSelected(timeline, displayedEditorLine());
          showGlobalPreview(block);
          queueGlobalSeek(timeline, waveSurferPartsFor("#editor-global-audio"), Number(block.dataset.start), true);
        }
      }
    }
    const navigation = window.__kfGlobalNavigation;
    const acknowledgment = document.querySelector(
      "#kf-global-navigation-ack input, #kf-global-navigation-ack textarea"
    )?.value;
    if (navigation?.pending && acknowledgment === navigation.pending.id) {
      const completed = navigation.pending.line;
      navigation.pending = null;
      if (navigation.latest !== completed) selectGlobalLine(navigation.latest);
      else {
        navigation.latest = null;
        setEditorBusy(false);
      }
    }
    if (!window.__kfEditorMutationPending && !navigation?.pending) {
      const action = window.__kfEditorHistoryQueue?.shift();
      if (action) document.querySelector(`#${action} button, button#${action}`)?.click();
      else if (window.__kfDeferredGlobalClick) {
        const line = window.__kfDeferredGlobalClick;
        window.__kfDeferredGlobalClick = null;
        // Resolve the target after saving, when ripple and the current draft
        // have updated its actual bounds. A blur-triggered save must not eat a click.
        visibleElement(".kf-global-timeline")?.querySelector(
          `.kf-global-line-block[data-line-number="${line}"]`
        )?.click();
      }
      else if (navigation?.latest && navigation.latest !== displayedEditorLine()) {
        selectGlobalLine(navigation.latest);
      }
    }
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
        if (!followsPlayback && !navigation?.pending) {
          window.__karaokeForgeGlobalFollowLine = displayedLine;
          markGlobalLineSelected(globalTimeline, displayedLine);
          window.__karaokeForgeGlobalManualSelectionUntil = performance.now() + 320;
        } else if (!playbackActive && !navigation?.pending) {
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
          editorPreferences.follow_playback !== false &&
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
  const restoreEditorViews = () => {
    const timeline = document.querySelector(".kf-global-timeline");
    if (timeline && elementIsVisible(timeline) && !timeline.__kfViewRestored) {
      timeline.__kfViewRestored = true;
      const canvas = timeline.querySelector(".kf-global-canvas");
      const scroll = timeline.querySelector(".kf-global-scroll");
      const zoom = Number(editorPreferences.global_zoom ?? 1);
      canvas.dataset.zoom = String(zoom);
      canvas.style.minWidth = zoom === 0 ? "100%" : `${Number(canvas.dataset.baseWidth) * zoom}px`;
      if (globalView?.key === timeline.dataset.projectKey) {
        scroll.scrollLeft = globalView.seconds / Number(timeline.dataset.duration) * canvas.scrollWidth;
      }
      syncGlobalTools(timeline);
    }
    const token = document.querySelector(".kf-token-editor");
    if (token && elementIsVisible(token) && !token.__kfViewRestored) {
      token.__kfViewRestored = true;
      const canvas = token.querySelector(".kf-token-canvas");
      const scroll = token.querySelector(".kf-token-scroll");
      const zoom = editorPreferences.token_zoom;
      if (canvas && scroll && typeof zoom === "number") {
        const width = zoom === 0 ? scroll.clientWidth : Math.max(scroll.clientWidth, Number(canvas.dataset.baseWidth) * zoom);
        canvas.dataset.zoom = String(zoom || width / Number(canvas.dataset.baseWidth));
        canvas.style.width = canvas.style.minWidth = `${width}px`;
      }
      const prior = window.__kfTokenViews?.[token.dataset.lineNumber];
      if (prior && scroll && canvas) scroll.scrollLeft = prior * canvas.scrollWidth;
    }
    const preview = document.querySelector(".kf-editor-preview-stage");
    if (preview && editorPreferences.preview_font_size) {
      const host = preview.closest("#editor-preview") || preview;
      host.dataset.fontSize = String(editorPreferences.preview_font_size);
      host.style.setProperty("--kf-preview-font-size", `${editorPreferences.preview_font_size}px`);
      preview.style.setProperty("--kf-preview-font-size", `${editorPreferences.preview_font_size}px`);
    }
    const overview = document.querySelector("#editor-lines");
    if (overview && editorPreferences.overview_font_size) {
      overview.dataset.fontSize = String(editorPreferences.overview_font_size);
      overview.style.setProperty("--kf-overview-font-size", `${editorPreferences.overview_font_size}px`);
    }
  };
  const clearAuditionAfterLineChange = () => {
    const timeline = visibleElement(".kf-token-editor");
    const line = timeline?.dataset.lineNumber || null;
    if (observedTimelineLine !== null && line !== observedTimelineLine) {
      clearTokenStopTimer();
    }
    observedTimelineLine = line;
    restoreEditorViews();
  };
  new MutationObserver(clearAuditionAfterLineChange).observe(document.body, {
    childList: true,
    subtree: true,
  });
  clearAuditionAfterLineChange();
  window.__kfRestoreEditorViews = restoreEditorViews;
  document.addEventListener("scroll", (event) => {
    if (event.target.matches?.(".kf-global-scroll")) {
      rememberGlobalView(event.target.closest(".kf-global-timeline"));
    } else if (event.target.matches?.(".kf-token-scroll")) {
      const token = event.target.closest(".kf-token-editor");
      const canvas = token.querySelector(".kf-token-canvas");
      (window.__kfTokenViews ||= {})[token.dataset.lineNumber] =
        event.target.scrollLeft / Math.max(1, canvas.scrollWidth);
    }
  }, true);
  document.addEventListener("wheel", (event) => {
    const timeline = event.target.closest?.(".kf-global-timeline");
    if (timeline && !(event.ctrlKey || event.metaKey)) stopGlobalFollow(timeline);
  }, { passive: true });
  document.addEventListener("pointerdown", (event) => {
    if (event.target.matches?.(".kf-global-scroll")) {
      stopGlobalFollow(event.target.closest(".kf-global-timeline"));
    }
  });

  document.addEventListener("pointerdown", (event) => {
    const handle = event.target.closest?.(".kf-token-boundary");
    if (!handle) return;
    pauseForEditorMutation();
    handle.__kfBeforeDrag = handle.value;
  });

  document.addEventListener("change", (event) => {
    const edited = event.target.closest?.(".kf-token-boundary, .kf-token-text");
    if (!edited) return;
    if (edited.classList.contains("kf-token-boundary") && edited.__kfBeforeDrag === edited.value) return;
    document.querySelector("#editor-save-tokens button, button#editor-save-tokens")?.click();
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
      const id = undo ? "editor-undo" : "editor-redo";
      document.querySelector(`#${id} button, button#${id}`)?.click();
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
  drawerBackdrop.setAttribute("aria-hidden", "true");
  document.body.appendChild(drawerBackdrop);

  let overviewOpen = false;
  let overviewDrawer = null;
  let overviewReturnFocus = null;
  let overviewPreviousOverflow = "";
  let overviewPreviousOverflowPriority = "";
  const overviewFocusable = (drawer) => Array.from(drawer.querySelectorAll(
    "button, a[href], input, select, textarea, [tabindex], [contenteditable='true']"
  )).filter(element => (
    !element.disabled && element.tabIndex >= 0 && !element.closest("[inert]") &&
    element.getClientRects().length > 0
  ));

  const setOverviewOpen = (open, restoreFocus = true) => {
    const drawer = document.querySelector("#editor-overview-panel");
    if (!drawer && open) return;
    overviewDrawer = drawer;
    const changed = overviewOpen !== open;
    if (open && changed) {
      overviewReturnFocus = document.activeElement;
      overviewPreviousOverflow = document.body.style.getPropertyValue("overflow");
      overviewPreviousOverflowPriority = document.body.style.getPropertyPriority("overflow");
    }
    overviewOpen = open;
    drawer?.classList.toggle("is-open", open);
    drawer?.setAttribute("role", "dialog");
    drawer?.setAttribute("aria-label", "歌词总览");
    drawer?.setAttribute("aria-hidden", String(!open));
    drawer?.setAttribute("tabindex", "-1");
    if (open) {
      drawer.setAttribute("aria-modal", "true");
      if (!window.__kfEditorMutationPending && !window.__kfGlobalNavigation?.pending) {
        drawer.removeAttribute("inert");
      }
    } else {
      drawer?.removeAttribute("aria-modal");
      drawer?.setAttribute("inert", "");
    }
    const toggle = document.querySelector(
      "button#editor-overview-toggle, #editor-overview-toggle button"
    );
    toggle?.setAttribute("aria-expanded", String(open));
    toggle?.setAttribute("aria-controls", "editor-overview-panel");
    drawerBackdrop.classList.toggle("is-open", open);
    if (open) {
      document.body.style.setProperty("overflow", "hidden");
      if (changed) (overviewFocusable(drawer)[0] || drawer).focus({ preventScroll: true });
    } else if (changed) {
      if (overviewPreviousOverflow) {
        document.body.style.setProperty(
          "overflow", overviewPreviousOverflow, overviewPreviousOverflowPriority
        );
      } else document.body.style.removeProperty("overflow");
      if (restoreFocus && overviewReturnFocus?.isConnected &&
          overviewReturnFocus.getClientRects().length > 0) {
        overviewReturnFocus.focus({ preventScroll: true });
      }
      overviewReturnFocus = null;
    }
  };
  setOverviewOpen(false, false);

  // The backdrop lives outside Gradio's tab panel. A server-driven tab change
  // must release it too, including handoff to rendering or a restored project.
  window.__kfSyncOverviewState = () => {
    if (overviewOpen && !visibleElement("#editor-workspace")) setOverviewOpen(false, false);
    // Gradio mounts inactive tabs lazily. Initialize each new drawer once,
    // without rewriting attributes on every animation frame.
    const drawer = document.querySelector("#editor-overview-panel");
    if (drawer && drawer !== overviewDrawer) setOverviewOpen(overviewOpen, false);
  };

  document.addEventListener("keydown", (event) => {
    if (!overviewOpen || event.key !== "Tab") return;
    const drawer = document.querySelector("#editor-overview-panel");
    if (!drawer) return;
    const focusable = overviewFocusable(drawer);
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!first) {
      event.preventDefault();
      drawer.focus({ preventScroll: true });
    } else if (!drawer.contains(document.activeElement) ||
               (event.shiftKey && (document.activeElement === first || document.activeElement === drawer))) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus({ preventScroll: true });
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus({ preventScroll: true });
    }
  }, true);

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
    "#editor-project-actions button, #editor-project-loader button, " +
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
      window.__karaokeForgeDraggingGlobalLineEdge ||
      window.__kfEditorMutationPending || window.__kfGlobalNavigation?.pending
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
    const grabOffset = pointerStartX - track.getBoundingClientRect().left -
      previewSeconds / duration * track.getBoundingClientRect().width;
    const snapGuide = timeline.querySelector(".kf-global-snap-guide");
    const feedback = timeline.querySelector(".kf-global-feedback");
    const originalTokenStyles = tokenBlocks.map(token => ({
      left: token.style.left, width: token.style.width,
      start: token.dataset.start, end: token.dataset.end,
    }));
    let snapTarget = null;
    let pointerMoved = false;
    let finished = false;

    const secondsFromPointer = (clientX, bypassSnap = false) => {
      const bounds = track.getBoundingClientRect();
      if (bounds.width <= 0) return previewSeconds;
      const ratio = Math.min(1, Math.max(0, (clientX - bounds.left - grabOffset) / bounds.width));
      let seconds = Math.round(ratio * duration * 100) / 100;
      const threshold = Math.min(.15, 8 / bounds.width * duration);
      snapTarget = null;
      if (editorPreferences.snap_enabled !== false && !bypassSnap) {
        for (const candidate of timeline.querySelectorAll(".kf-global-line-edge")) {
          if (Number(candidate.dataset.lineNumber) === lineNumber) continue;
          const value = Number(candidate.dataset[candidate.dataset.edge]);
          const distance = Math.abs(value - seconds);
          if (distance <= threshold && (!snapTarget || distance < snapTarget.distance)) {
            snapTarget = { line: Number(candidate.dataset.lineNumber),
              edge: candidate.dataset.edge, seconds: value, distance };
          }
        }
        if (snapTarget) seconds = snapTarget.seconds;
      }
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
      const result = Math.max(0, Math.min(duration, seconds));
      if (snapTarget && Math.abs(result - snapTarget.seconds) > .000001) snapTarget = null;
      return snapTarget ? result : Math.round(result * 100) / 100;
    };

    const previewAt = (seconds) => {
      previewSeconds = seconds;
      const nextStart = edgeName === "start" ? seconds : baseStart;
      const nextEnd = edgeName === "end" ? seconds : baseEnd;
      block.style.left = `${nextStart / duration * 100}%`;
      block.style.width = `${Math.max(0.22, (nextEnd - nextStart) / duration * 100)}%`;
      const span = Math.max(.01, nextEnd - nextStart);
      tokenBlocks.forEach((token, index) => {
        const start = edgeName === "start" && index === 0 ? nextStart : Number(originalTokenStyles[index].start);
        const end = edgeName === "end" && index === tokenBlocks.length - 1 ? nextEnd : Number(originalTokenStyles[index].end);
        token.style.left = `${(start - nextStart) / span * 100}%`;
        token.style.width = `${Math.max(0, end - start) / span * 100}%`;
      });
      edge.style.left = `${seconds / duration * 100}%`;
      edge.classList.toggle(
        "is-flipped",
        edgeName === "start" ? seconds <= 0.000001 : seconds >= duration - 0.000001
      );
      edge.setAttribute("aria-valuenow", seconds.toFixed(2));
      edge.title = `拖动${edgeName === "start" ? "句首" : "句尾"}：${seconds.toFixed(2)}s`;
      if (snapGuide) {
        snapGuide.hidden = !snapTarget;
        snapGuide.style.left = `${seconds / duration * 100}%`;
        snapGuide.querySelector("span").textContent = snapTarget
          ? `已对齐第 ${snapTarget.line} 行${snapTarget.edge === "start" ? "句首" : "句尾"} · ${seconds.toFixed(2)}s`
          : "";
      }
      if (feedback) feedback.textContent = snapTarget
        ? `已吸附 ${seconds.toFixed(2)}s · 按住 Alt 可自由拖动`
        : `第 ${lineNumber} 行${edgeName === "start" ? "句首" : "句尾"} ${seconds.toFixed(2)}s`;
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
      tokenBlocks.forEach((token, index) => {
        token.style.left = originalTokenStyles[index].left;
        token.style.width = originalTokenStyles[index].width;
      });
      if (snapGuide) snapGuide.hidden = true;
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
      window.__karaokeForgeDraggingGlobalLineEdge = false;
      window.__karaokeForgeGlobalManualSelectionUntil = performance.now() + 420;
      const original = edgeName === "start" ? baseStart : baseEnd;
      const changed = pointerMoved && Math.abs(previewSeconds - original) >= 0.005;
      if (!commit || !changed) {
        restorePreview();
        if (feedback) feedback.textContent = "";
        if (resumeAfterDrag) playPlayback(parts);
        return;
      }
      edge.classList.remove("is-dragging");
      if (snapGuide) snapGuide.hidden = true;
      rememberGlobalView(timeline);
      if (feedback) feedback.textContent = "保存中…";
      timeline.dataset.edgeSaving = "true";
      window.__kfGlobalEdgePending = () => {
        if (document.body.contains(timeline)) {
          restorePreview();
          delete timeline.dataset.edgeSaving;
          if (feedback) feedback.textContent = "未能确认修改，请查看操作提示后重试。";
        }
      };
      const submitted = applyGlobalLineEdge({
        line: lineNumber,
        edge: edgeName,
        seconds: previewSeconds,
        base_start: baseStart,
        base_end: baseEnd,
        text: block.dataset.text || "",
        snap: snapTarget ? { line: snapTarget.line, edge: snapTarget.edge, seconds: snapTarget.seconds } : null,
      });
      if (!submitted) {
        window.__kfGlobalEdgePending?.();
        window.__kfGlobalEdgePending = null;
      }
    };

    const move = (moveEvent) => {
      if (moveEvent.pointerId !== pointerId || finished) return;
      moveEvent.preventDefault();
      pointerMoved ||= Math.abs(moveEvent.clientX - pointerStartX) >= 1;
      previewAt(secondsFromPointer(moveEvent.clientX, moveEvent.altKey));
    };
    const pointerUp = (finishEvent) => {
      if (finishEvent.pointerId !== pointerId) return;
      finishEvent.preventDefault();
      pointerMoved ||= Math.abs(finishEvent.clientX - pointerStartX) >= 1;
      if (pointerMoved) previewAt(secondsFromPointer(finishEvent.clientX, finishEvent.altKey));
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
    const step = event.target.closest?.("#editor-previous-line, #editor-next-line");
    if (step && globalEditorModeActive()) {
      event.preventDefault();
      event.stopImmediatePropagation();
      const timeline = visibleElement(".kf-global-timeline");
      const blocks = Array.from(timeline?.querySelectorAll(".kf-global-line-block") || []);
      const current = Number(window.__kfDeferredGlobalClick ?? window.__kfGlobalNavigation?.latest ??
        window.__karaokeForgeGlobalFollowLine ?? displayedEditorLine());
      const index = blocks.findIndex(block => Number(block.dataset.lineNumber) === current);
      const target = Math.max(0, Math.min(blocks.length - 1,
        index + (step.id === "editor-next-line" ? 1 : -1)));
      blocks[target]?.click();
      return;
    }
    const tool = event.target.closest?.(".kf-global-snap, .kf-global-follow, .kf-global-return");
    if (tool) {
      const timeline = tool.closest(".kf-global-timeline");
      if (tool.classList.contains("kf-global-snap")) {
        saveViewPreference("snap_enabled", editorPreferences.snap_enabled === false);
      } else if (tool.classList.contains("kf-global-follow")) {
        saveViewPreference("follow_playback", editorPreferences.follow_playback === false);
      } else {
        saveViewPreference("follow_playback", true);
        const scroll = timeline.querySelector(".kf-global-scroll");
        const canvas = timeline.querySelector(".kf-global-canvas");
        const head = timeline.querySelector(".kf-global-playhead");
        scroll.scrollLeft = Math.max(0, Number.parseFloat(head.style.left) / 100 * canvas.scrollWidth - scroll.clientWidth / 2);
        rememberGlobalView(timeline);
      }
      syncGlobalTools(timeline);
      return;
    }
    const zoomButton = event.target.closest?.(
      ".kf-global-zoom-in, .kf-global-zoom-out, .kf-global-zoom-fit"
    );
    if (zoomButton) {
      const timeline = zoomButton.closest(".kf-global-timeline");
      applyGlobalZoom(timeline, zoomButton.classList.contains("kf-global-zoom-fit")
        ? "fit" : zoomButton.classList.contains("kf-global-zoom-in") ? "in" : "out");
      return;
    }
    const block = event.target.closest?.(".kf-global-line-block");
    if (!block) return;
    event.preventDefault();
    event.stopPropagation();
    if (window.__kfEditorMutationPending) {
      window.__kfDeferredGlobalClick = Number(block.dataset.lineNumber);
      return;
    }
    clearTokenStopTimer();
    clearTokenAuditionGuard();
    clearEditorMutationGuard();
    const { timeline, parts } = globalTimelineParts();
    const lineNumber = Number(block.dataset.lineNumber);
    window.__karaokeForgeLastDisplayedEditorLine = displayedEditorLine();
    window.__karaokeForgeGlobalFollowLine = lineNumber;
    window.__karaokeForgeGlobalManualSelectionUntil = performance.now() + 320;
    markGlobalLineSelected(timeline, lineNumber);
    showGlobalPreview(block);
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
      isTextEntry(event.target) ||
      event.target.closest?.(
        "button, a[href], input, select, [role='button'], [role='checkbox'], " +
        "[role='radio'], [role='slider'], [role='tab']"
      )
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
    if (!(event.ctrlKey || event.metaKey) || !["z", "y"].includes(event.key.toLowerCase())) return;
    if (isTextEntry(event.target) || !visibleElement("#editor-workspace")) return;
    event.preventDefault();
    const id = event.shiftKey || event.key.toLowerCase() === "y" ? "editor-redo" : "editor-undo";
    document.querySelector(`#${id} button, button#${id}`)?.click();
  });
  return [];
}
