from karaoke_forge.align import RecognizedWord
from karaoke_forge.formats import parse_lrc, parse_yrc, read_lyrics
from karaoke_forge.pipeline import (
    _build_initial_prompt,
    normalize_timing_refinement,
    refine_audio_word_timing,
    should_refine_timing,
)
from karaoke_forge.transcribe import TranscriptionResult


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
