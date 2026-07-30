from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .align import AlignmentReport, align_document
from .formats import read_lyrics
from .media import separate_vocals
from .models import LyricsDocument
from .transcribe import TranscriptionResult, transcribe_with_faster_whisper


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
