import pytest

from karaoke_forge.align import RecognizedWord
from karaoke_forge.formats import parse_lrc, parse_yrc, read_lyrics
from karaoke_forge.pipeline import (
    _build_initial_prompt,
    align_audio_and_lyrics,
    normalize_timing_refinement,
    refine_audio_word_timing,
    refine_audio_word_timing_with_fallback,
    should_refine_timing,
)
from karaoke_forge.transcribe import TranscriptionError, TranscriptionResult


def test_plain_lyrics_prompt_preserves_stanza_breaks(tmp_path) -> None:
    lyrics_path = tmp_path / "lyrics.txt"
    raw_text = "第一段\n\n第二段\n"
    lyrics_path.write_text(raw_text, encoding="utf-8")

    prompt = _build_initial_prompt(lyrics_path, read_lyrics(lyrics_path))

    assert prompt == raw_text


def test_timing_refinement_policy_distinguishes_source_and_synthetic() -> None:
    synthetic = parse_lrc("[00:01.00]Hello\n")
    source = parse_yrc("[1000,500](1000,500,0)Hello\n")

    assert not should_refine_timing(synthetic, "off")
    assert should_refine_timing(synthetic, "auto")
    assert not should_refine_timing(source, "auto")
    assert should_refine_timing(source, "force")
    assert normalize_timing_refinement("force") == "force"
    assert normalize_timing_refinement("force", legacy_refine_word_timing=False) == "off"


def test_refinement_preserves_hidden_project_lines(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    document = parse_lrc("[00:01.00]Hidden\n[00:03.00]Visible\n")
    document.lines[0].hidden = True
    monkeypatch.setattr(
        "karaoke_forge.pipeline.transcribe_with_faster_whisper",
        lambda *_args, **_kwargs: TranscriptionResult(
            words=[RecognizedWord("Visible", 3.0, 4.0, 0.9)],
            detected_language="en",
            language_probability=0.99,
        ),
    )

    result = refine_audio_word_timing(audio, document)

    assert result.document.lines[0].hidden
    assert result.document.lines[0].text == "Hidden"
    assert result.document.lines[1].text == "Visible"


def test_auto_refinement_keeps_timed_lyrics_when_whisper_is_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    document = parse_lrc("[00:01.00]Hello\n")
    messages: list[str] = []

    def unavailable(*_args, **_kwargs):
        raise TranscriptionError("model unavailable")

    monkeypatch.setattr(
        "karaoke_forge.pipeline.transcribe_with_faster_whisper",
        unavailable,
    )

    result = refine_audio_word_timing_with_fallback(
        audio,
        document,
        timing_mode="auto",
        progress=messages.append,
    )

    assert result is None
    assert any("已保留原时间轴并继续" in message for message in messages)


def test_force_refinement_still_raises_when_whisper_is_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    document = parse_lrc("[00:01.00]Hello\n")

    def unavailable(*_args, **_kwargs):
        raise TranscriptionError("model unavailable")

    monkeypatch.setattr(
        "karaoke_forge.pipeline.transcribe_with_faster_whisper",
        unavailable,
    )

    with pytest.raises(TranscriptionError, match="model unavailable"):
        refine_audio_word_timing_with_fallback(
            audio,
            document,
            timing_mode="force",
        )


def test_plain_lyrics_still_require_whisper(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.wav"
    lyrics = tmp_path / "lyrics.txt"
    audio.write_bytes(b"audio")
    lyrics.write_text("Hello\n", encoding="utf-8")

    def unavailable(*_args, **_kwargs):
        raise TranscriptionError("model unavailable")

    monkeypatch.setattr(
        "karaoke_forge.pipeline.transcribe_with_faster_whisper",
        unavailable,
    )

    with pytest.raises(TranscriptionError, match="model unavailable"):
        align_audio_and_lyrics(audio, lyrics)
