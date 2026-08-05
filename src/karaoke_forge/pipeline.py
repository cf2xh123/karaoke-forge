from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from .align import (
    AlignmentError,
    AlignmentReport,
    align_document,
    apply_forced_line_alignments,
    refine_timed_document,
)
from .formats import read_lyrics
from .media import MediaError, separate_vocals
from .models import LyricsDocument
from .runtime import inspect_demucs_runtime
from .text import split_display_units
from .transcribe import (
    TranscriptionError,
    TranscriptionResult,
    force_align_lyrics_with_faster_whisper,
    load_faster_whisper_model,
    transcribe_with_faster_whisper,
)

TimingRefinement = Literal["off", "auto", "force"]
TIMING_REFINEMENT_MODES = ("off", "auto", "force")


@dataclass(frozen=True)
class AlignmentProfile:
    key: str
    model: str
    beam_size: int
    forced_alignment: bool
    prefer_vocal_separation: bool


ALIGNMENT_PROFILES = {
    "fast": AlignmentProfile("fast", "small", 3, False, False),
    "balanced": AlignmentProfile("balanced", "large-v3-turbo", 5, False, False),
    "precise": AlignmentProfile("precise", "large-v3", 5, True, True),
}
ALIGNMENT_PROFILE_ALIASES = {
    "profile:fast": "fast",
    "profile:balanced": "balanced",
    "profile:precise": "precise",
    "ktv-precise": "precise",
}


def resolve_alignment_profile(value: str | None) -> AlignmentProfile:
    normalized = (value or "small").strip()
    key = ALIGNMENT_PROFILE_ALIASES.get(normalized, normalized)
    if key in ALIGNMENT_PROFILES:
        return ALIGNMENT_PROFILES[key]
    return AlignmentProfile("custom", normalized, 5, False, False)


def normalize_timing_refinement(
    value: str | None,
    *,
    legacy_refine_word_timing: bool | None = None,
) -> TimingRefinement:
    if legacy_refine_word_timing is not None:
        return "auto" if legacy_refine_word_timing else "off"
    normalized = (value or "auto").strip().lower()
    if normalized not in TIMING_REFINEMENT_MODES:
        raise ValueError(f"逐字时间精修策略必须是 off、auto 或 force，当前值为 {value!r}。")
    return normalized  # type: ignore[return-value]


def should_refine_timing(document: LyricsDocument, mode: str) -> bool:
    normalized = normalize_timing_refinement(mode)
    if not document.is_timed or normalized == "off":
        return False
    if normalized == "force":
        return True
    return document.metadata.get("word_timing") == "synthetic"


@dataclass(frozen=True)
class AlignOptions:
    model: str = "small"
    language: str | None = None
    device: str = "auto"
    compute_type: str = "default"
    beam_size: int = 5
    minimum_coverage: float = 0.2
    separate_vocals: bool = False
    demucs_model: str = "htdemucs"
    recover_low_coverage: bool = False
    profile: str = "custom"
    forced_alignment: bool = False
    prefer_vocal_separation: bool = False


@dataclass(frozen=True)
class AlignResult:
    document: LyricsDocument
    report: AlignmentReport
    transcription: TranscriptionResult
    alignment_audio: Path
    recovered: bool = False


def resolve_align_options(options: AlignOptions) -> AlignOptions:
    selection = options.profile if options.profile in ALIGNMENT_PROFILES else options.model
    profile = resolve_alignment_profile(selection)
    if profile.key == "custom":
        return options
    return replace(
        options,
        model=profile.model,
        beam_size=profile.beam_size,
        profile=profile.key,
        forced_alignment=profile.forced_alignment,
        prefer_vocal_separation=profile.prefer_vocal_separation,
    )


def _prepare_alignment_audio(
    audio: Path,
    options: AlignOptions,
    *,
    work_dir: str | Path | None,
    progress: Callable[[str], None] | None,
) -> Path:
    required = options.separate_vocals
    preferred = options.prefer_vocal_separation and not required
    if not required and not preferred:
        return audio
    if work_dir is None:
        if required:
            raise ValueError("A work directory is required when vocal separation is enabled.")
        if progress:
            progress("KTV 精准模式未提供工作目录，已跳过人声分离并继续使用原音频")
        return audio
    if preferred and not inspect_demucs_runtime().ready:
        if progress:
            progress("KTV 精准模式未检测到可用 Demucs，已使用原音频继续精准对齐")
        return audio
    try:
        return separate_vocals(
            audio,
            Path(work_dir) / "separated",
            model=options.demucs_model,
            device=options.device,
            progress=progress,
        )
    except (MediaError, OSError) as exc:
        if required:
            raise
        if progress:
            progress(f"KTV 精准模式人声分离暂不可用，已改用原音频继续：{exc}")
        return audio


def _apply_precise_forced_alignment(
    alignment_audio: Path,
    document: LyricsDocument,
    report: AlignmentReport,
    transcription: TranscriptionResult,
    options: AlignOptions,
    whisper_model: object | None,
    *,
    protect_existing_word_timing: bool,
    progress: Callable[[str], None] | None,
) -> tuple[LyricsDocument, AlignmentReport]:
    if not options.forced_alignment or whisper_model is None:
        return document, report
    if progress:
        progress("KTV 精准模式：开始用正式歌词逐行执行 CTranslate2 强制对齐")
    try:
        forced = force_align_lyrics_with_faster_whisper(
            alignment_audio,
            document,
            whisper_model=whisper_model,
            language=options.language or transcription.detected_language,
            progress=progress,
        )
        refined, accepted_lines = apply_forced_line_alignments(
            document,
            {item.line_index: item.words for item in forced.lines},
            minimum_coverage=options.minimum_coverage,
            protect_existing_word_timing=protect_existing_word_timing,
        )
        if accepted_lines:
            document = refined
            document.metadata["forced_alignment"] = "ctranslate2-line-bounded"
        else:
            document.metadata["forced_alignment"] = "fallback-coarse"
            document.metadata["forced_alignment_warning"] = (
                "逐行结果未通过置信度或边界安全检查，已保留粗对齐时间。"
            )
        document.metadata["forced_alignment_attempted_lines"] = str(forced.attempted_lines)
        document.metadata["forced_alignment_aligned_lines"] = str(forced.aligned_lines)
        document.metadata["forced_alignment_accepted_lines"] = str(accepted_lines)
        report = replace(
            report,
            forced_alignment_attempted_lines=forced.attempted_lines,
            forced_alignment_aligned_lines=forced.aligned_lines,
            forced_alignment_accepted_lines=accepted_lines,
        )
        if progress and accepted_lines:
            progress(
                f"KTV 精准对齐完成：尝试 {forced.attempted_lines} 行，"
                f"底层返回 {forced.aligned_lines} 行，采纳 {accepted_lines} 行"
            )
        elif progress:
            progress(
                "KTV 精准对齐结果未通过安全质量门，已完整保留 0.12 粗对齐时间"
            )
    except (AlignmentError, TranscriptionError) as exc:
        document.metadata["forced_alignment"] = "fallback-coarse"
        document.metadata["forced_alignment_warning"] = str(exc)
        if progress:
            progress(f"KTV 精准对齐未达到安全质量门，已保留 0.12 粗对齐结果：{exc}")
    return document, report


def _build_initial_prompt(lyrics_path: Path, lyrics: LyricsDocument) -> str:
    if lyrics.source_format == "txt":
        raw_text = lyrics_path.read_text(encoding="utf-8-sig")
        if raw_text.strip():
            # Stanza breaks can help Whisper keep repeated song sections in order.
            return raw_text
    return "\n".join(line.text for line in lyrics.lines)


def _is_cjk_unit(value: str) -> bool:
    return len(value) == 1 and any(
        "\u3400" <= char <= "\u9fff"
        or "\u3040" <= char <= "\u30ff"
        or "\uac00" <= char <= "\ud7af"
        for char in value
    )


def _build_lyric_hotwords(lyrics: LyricsDocument, *, limit: int = 320) -> str:
    """Build a compact, de-duplicated hint list spanning the complete song."""

    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = value.strip()
        if not value or value in seen:
            return
        seen.add(value)
        candidates.append(value)

    for line in lyrics.lines:
        lowered = line.text.strip().casefold()
        if (":" in lowered or "：" in lowered) and lowered.startswith(
            (
                "作词",
                "作詞",
                "作曲",
                "编曲",
                "編曲",
                "lyrics",
                "music",
                "composer",
                "arranger",
            )
        ):
            continue
        cjk_run = ""
        for unit in split_display_units(line.text):
            if _is_cjk_unit(unit.key):
                cjk_run += unit.key
                continue
            if cjk_run:
                for offset in range(0, len(cjk_run), 4):
                    add(cjk_run[offset : offset + 4])
                cjk_run = ""
            if len(unit.key) >= 2:
                add(unit.key)
        if cjk_run:
            for offset in range(0, len(cjk_run), 4):
                add(cjk_run[offset : offset + 4])

    if not candidates:
        return ""
    # Apply the final character budget while sampling the whole song. Sampling
    # first and then truncating would quietly turn a long-song hint back into an
    # opening-verse-only prompt.
    candidates = [value for value in candidates if len(value) <= limit]
    for count in range(min(80, len(candidates)), 0, -1):
        if count == 1:
            selected = [candidates[len(candidates) // 2]]
        else:
            indexes = [
                round(index * (len(candidates) - 1) / (count - 1))
                for index in range(count)
            ]
            selected = [candidates[index] for index in indexes]
        hint = " ".join(selected)
        if len(hint) <= limit:
            return hint
    return ""


def align_audio_and_lyrics(
    audio_path: str | Path,
    lyrics_path: str | Path,
    *,
    options: AlignOptions | None = None,
    work_dir: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> AlignResult:
    options = resolve_align_options(options or AlignOptions())
    audio = Path(audio_path)
    lyrics = read_lyrics(lyrics_path)
    alignment_audio = _prepare_alignment_audio(
        audio,
        options,
        work_dir=work_dir,
        progress=progress,
    )
    whisper_model = (
        load_faster_whisper_model(
            model=options.model,
            device=options.device,
            compute_type=options.compute_type,
            progress=progress,
        )
        if options.forced_alignment
        else None
    )

    hotwords = _build_lyric_hotwords(lyrics)
    transcription = transcribe_with_faster_whisper(
        alignment_audio,
        model=options.model,
        language=options.language,
        device=options.device,
        compute_type=options.compute_type,
        beam_size=options.beam_size,
        initial_prompt=None,
        hotwords=hotwords,
        progress=progress,
        whisper_model=whisper_model,
    )
    document, report = align_document(
        lyrics,
        transcription.words,
        minimum_coverage=options.minimum_coverage,
        allow_low_coverage=options.recover_low_coverage,
    )
    recovered = report.coverage < options.minimum_coverage
    if transcription.detected_language:
        document.metadata.setdefault("language", transcription.detected_language)
    document, report = _apply_precise_forced_alignment(
        Path(alignment_audio),
        document,
        report,
        transcription,
        options,
        whisper_model,
        protect_existing_word_timing=False,
        progress=progress,
    )
    document.metadata["generator"] = "Karaoke Forge"
    document.metadata["alignment_model"] = options.model
    document.metadata["alignment_profile"] = options.profile
    document.metadata["alignment_coverage"] = f"{report.coverage:.6f}"
    if recovered:
        document.metadata["alignment_status"] = "low_coverage_recovery"
        document.metadata["unmatched_lyric_lines"] = ",".join(
            str(index + 1) for index in report.unmatched_line_indexes
        )
    return AlignResult(
        document=document,
        report=report,
        transcription=transcription,
        alignment_audio=Path(alignment_audio),
        recovered=recovered,
    )


def refine_audio_word_timing(
    audio_path: str | Path,
    lyrics: LyricsDocument,
    *,
    options: AlignOptions | None = None,
    work_dir: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
    protect_existing_word_timing: bool = False,
) -> AlignResult:
    """Use timestamped ASR words to refine timing inside already-timed lines."""

    options = resolve_align_options(options or AlignOptions())
    lyrics.require_timed()
    visible_lyrics = LyricsDocument(
        lines=copy.deepcopy(lyrics.visible_lines),
        metadata=dict(lyrics.metadata),
        source_format=lyrics.source_format,
    )
    audio = Path(audio_path)
    alignment_audio = _prepare_alignment_audio(
        audio,
        options,
        work_dir=work_dir,
        progress=progress,
    )
    whisper_model = (
        load_faster_whisper_model(
            model=options.model,
            device=options.device,
            compute_type=options.compute_type,
            progress=progress,
        )
        if options.forced_alignment
        else None
    )
    if progress:
        progress("正在根据演唱音频精修句内逐字时间")
    transcription = transcribe_with_faster_whisper(
        alignment_audio,
        model=options.model,
        language=options.language,
        device=options.device,
        compute_type=options.compute_type,
        beam_size=options.beam_size,
        initial_prompt=None,
        hotwords=_build_lyric_hotwords(visible_lyrics),
        progress=progress,
        whisper_model=whisper_model,
    )
    document, report = refine_timed_document(
        visible_lyrics,
        transcription.words,
        minimum_coverage=options.minimum_coverage,
        protect_existing_word_timing=protect_existing_word_timing,
    )
    document, report = _apply_precise_forced_alignment(
        Path(alignment_audio),
        document,
        report,
        transcription,
        options,
        whisper_model,
        protect_existing_word_timing=protect_existing_word_timing,
        progress=progress,
    )
    if progress:
        if report.timing_anchor_lines:
            progress(
                f"已用 {report.timing_anchor_lines} 行可靠演唱锚点校正时轴漂移："
                f"中位偏移 {report.timing_median_shift:+.2f} 秒，"
                f"最大偏移 {report.timing_max_shift:.2f} 秒"
            )
        refined_lines = int(document.metadata.get("audio_refined_lines", "0"))
        preserved_lines = int(document.metadata.get("audio_preserved_lines", "0"))
        if protect_existing_word_timing:
            progress(
                f"强制检查完成：采纳 {refined_lines} 行可靠修正，"
                f"保留 {preserved_lines} 行原逐字时间"
            )
        else:
            progress(f"逐字时间精修完成：已处理 {refined_lines} 行")
    if any(line.hidden for line in lyrics.lines):
        refined_visible = iter(document.lines)
        merged_lines = [
            copy.deepcopy(line) if line.hidden else next(refined_visible) for line in lyrics.lines
        ]
        document = LyricsDocument(
            lines=merged_lines,
            metadata=dict(document.metadata),
            source_format=lyrics.source_format,
        )
    if transcription.detected_language:
        document.metadata.setdefault("language", transcription.detected_language)
    document.metadata["generator"] = "Karaoke Forge"
    document.metadata["alignment_model"] = options.model
    document.metadata["alignment_profile"] = options.profile
    return AlignResult(
        document=document,
        report=report,
        transcription=transcription,
        alignment_audio=Path(alignment_audio),
    )


def refine_audio_word_timing_with_fallback(
    audio_path: str | Path,
    lyrics: LyricsDocument,
    *,
    timing_mode: str,
    options: AlignOptions | None = None,
    work_dir: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> AlignResult | None:
    """Refine timed lyrics, preserving them when optional recognition is unavailable."""

    normalized_mode = normalize_timing_refinement(timing_mode)
    try:
        return refine_audio_word_timing(
            audio_path,
            lyrics,
            options=options,
            work_dir=work_dir,
            progress=progress,
            protect_existing_word_timing=(
                normalized_mode == "force" and lyrics.metadata.get("word_timing") != "synthetic"
            ),
        )
    except (AlignmentError, TranscriptionError) as exc:
        if normalized_mode != "auto":
            raise
        if progress:
            progress(f"自动逐字时间精修覆盖率不足或暂不可用，已保留原时间轴并继续：{exc}")
        return None
