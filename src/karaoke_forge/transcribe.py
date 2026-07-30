from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .align import RecognizedWord


class TranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class TranscriptionResult:
    words: list[RecognizedWord]
    detected_language: str | None
    language_probability: float | None


def transcribe_with_faster_whisper(
    audio_path: str | Path,
    *,
    model: str = "small",
    language: str | None = None,
    device: str = "auto",
    compute_type: str = "default",
    beam_size: int = 5,
    initial_prompt: str | None = None,
    vad_filter: bool = False,
    progress: Callable[[str], None] | None = None,
) -> TranscriptionResult:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscriptionError(
            "The alignment engine is not installed. Run "
            '`pip install -e ".[align]"` (or `pip install karaoke-forge[align]`).'
        ) from exc

    audio = Path(audio_path)
    if not audio.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio}")
    if progress:
        progress(f"Loading Whisper model: {model}")

    try:
        whisper_model = WhisperModel(model, device=device, compute_type=compute_type)
        segments, info = whisper_model.transcribe(
            str(audio),
            language=language,
            beam_size=beam_size,
            word_timestamps=True,
            # Speech-oriented VAD can reject long stretches of vocals mixed with
            # dense music. Scan the complete track by default for song alignment.
            vad_filter=vad_filter,
            condition_on_previous_text=True,
            initial_prompt=initial_prompt[:4000] if initial_prompt else None,
        )
        words: list[RecognizedWord] = []
        for segment in segments:
            if progress:
                progress(f"Transcribing {segment.start:7.2f}s - {segment.end:7.2f}s")
            for word in segment.words or []:
                if word.start is None or word.end is None:
                    continue
                words.append(
                    RecognizedWord(
                        text=word.word,
                        start=float(word.start),
                        end=float(word.end),
                        confidence=(
                            float(word.probability) if word.probability is not None else None
                        ),
                    )
                )
    except Exception as exc:
        raise TranscriptionError(f"Whisper transcription failed: {exc}") from exc

    if not words:
        raise TranscriptionError(
            "Whisper returned no word timestamps. Check the audio or try vocal separation."
        )
    return TranscriptionResult(
        words=words,
        detected_language=getattr(info, "language", None),
        language_probability=getattr(info, "language_probability", None),
    )
