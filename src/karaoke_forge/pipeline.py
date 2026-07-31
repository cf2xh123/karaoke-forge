from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .align import AlignmentReport, align_document, refine_timed_document
from .formats import read_lyrics
from .media import separate_vocals
from .models import LyricsDocument
from .transcribe import TranscriptionError, TranscriptionResult, transcribe_with_faster_whisper

TimingRefinement = Literal["off", "auto", "force"]
TIMING_REFINEMENT_MODES = ("off", "auto", "force")


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


@dataclass(frozen=True)
class AlignResult:
    document: LyricsDocument
    report: AlignmentReport
    transcription: TranscriptionResult
    alignment_audio: Path


def _build_initial_prompt(lyrics_path: Path, lyrics: LyricsDocument) -> str:
    if lyrics.source_format == "txt":
        raw_text = lyrics_path.read_text(encoding="utf-8-sig")
        if raw_text.strip():
            # Stanza breaks can help Whisper keep repeated song sections in order.
            return raw_text
    return "\n".join(line.text for line in lyrics.lines)


def align_audio_and_lyrics(
    audio_path: str | Path,
    lyrics_path: str | Path,
    *,
    options: AlignOptions | None = None,
    work_dir: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> AlignResult:
    options = options or AlignOptions()
    audio = Path(audio_path)
    lyrics = read_lyrics(lyrics_path)
    alignment_audio = audio

    if options.separate_vocals:
        if work_dir is None:
            raise ValueError("A work directory is required when vocal separation is enabled.")
        alignment_audio = separate_vocals(
            audio,
            Path(work_dir) / "separated",
            model=options.demucs_model,
            progress=progress,
        )

    prompt = _build_initial_prompt(Path(lyrics_path), lyrics)
    transcription = transcribe_with_faster_whisper(
        alignment_audio,
        model=options.model,
        language=options.language,
        device=options.device,
        compute_type=options.compute_type,
        beam_size=options.beam_size,
        initial_prompt=prompt,
        progress=progress,
    )
    document, report = align_document(
        lyrics,
        transcription.words,
        minimum_coverage=options.minimum_coverage,
    )
    if transcription.detected_language:
        document.metadata.setdefault("language", transcription.detected_language)
    document.metadata["generator"] = "Karaoke Forge"
    document.metadata["alignment_model"] = options.model
    return AlignResult(
        document=document,
        report=report,
        transcription=transcription,
        alignment_audio=Path(alignment_audio),
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

    options = options or AlignOptions()
    lyrics.require_timed()
    visible_lyrics = LyricsDocument(
        lines=copy.deepcopy(lyrics.visible_lines),
        metadata=dict(lyrics.metadata),
        source_format=lyrics.source_format,
    )
    audio = Path(audio_path)
    alignment_audio = audio
    if options.separate_vocals:
        if work_dir is None:
            raise ValueError("A work directory is required when vocal separation is enabled.")
        alignment_audio = separate_vocals(
            audio,
            Path(work_dir) / "separated",
            model=options.demucs_model,
            progress=progress,
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
        initial_prompt="\n".join(line.text for line in visible_lyrics.lines),
        progress=progress,
    )
    document, report = refine_timed_document(
        visible_lyrics,
        transcription.words,
        minimum_coverage=options.minimum_coverage,
        protect_existing_word_timing=protect_existing_word_timing,
    )
    if progress:
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
    except TranscriptionError as exc:
        if normalized_mode != "auto":
            raise
        if progress:
            progress(f"自动逐字时间精修暂不可用，已保留原时间轴并继续：{exc}")
        return None
