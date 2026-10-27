"""A new project can prepare timing from audio without requiring video artwork."""

from pathlib import Path

import pytest

from karaoke_forge.align import RecognizedWord
from karaoke_forge.formats import read_lyrics, write_json
from karaoke_forge.models import KaraokeToken, LyricLine, LyricsDocument
from karaoke_forge.projects import load_workspace_project, read_workspace_lyrics
from karaoke_forge.transcribe import TranscriptionResult
from karaoke_forge.web import prepare_make_editor_job


def prepare(tmp_path, monkeypatch, *, lyrics=None, pasted=""):
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr("karaoke_forge.web._materialize_auto_pronunciation", lambda *a, **k: 0)
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    return prepare_make_editor_job(
        str(audio), None, str(lyrics) if lyrics else None, pasted, "New song",
        "en", "small", "cpu", False, timing_refinement="off",
        output_root=str(tmp_path / "outputs"),
        recognize_audio=not bool(lyrics or pasted),
    )


def test_audio_and_timed_lyrics_load_without_video_cover_or_recognition(tmp_path, monkeypatch):
    source = LyricsDocument([LyricLine("Hello", 1, 2, [KaraokeToken("Hello", 1, 2)])])
    lyrics = tmp_path / "lyrics.json"
    lyrics.write_text(write_json(source), encoding="utf-8")

    def unexpected_recognition(*args, **kwargs):
        pytest.fail("Pre-timed lyrics must not invoke speech recognition")

    monkeypatch.setattr("karaoke_forge.pipeline.transcribe_with_faster_whisper", unexpected_recognition)
    result = prepare(tmp_path, monkeypatch, lyrics=lyrics)
    assert result.project, result.log
    assert read_lyrics(result.project).lines == source.lines
    workspace = load_workspace_project(Path(result.output_dir) / "karaoke-forge-project.json")
    assert workspace.audio and workspace.video is None and workspace.cover is None


def test_audio_recognition_creates_editable_words_and_preserves_silent_gap(tmp_path, monkeypatch):
    words = [
        RecognizedWord(" Hello", 1, 1.5, .9),
        RecognizedWord(" world.", 1.5, 2, .8),
        RecognizedWord(" Again", 4, 5, .95),
    ]
    calls = []

    def recognize(audio, **kwargs):
        calls.append((audio, kwargs))
        return TranscriptionResult(words, "en", .99)

    monkeypatch.setattr("karaoke_forge.pipeline.transcribe_with_faster_whisper", recognize)
    result = prepare(tmp_path, monkeypatch)
    assert result.project, result.log
    assert len(calls) == 1
    assert calls[0][1]["model"] == "small"
    document = read_lyrics(result.project)
    assert [line.text for line in document.lines] == ["Hello world.", "Again"]
    assert [(line.start, line.end) for line in document.lines] == [(1, 2), (4, 5)]
    tokens = [token for line in document.lines for token in line.tokens]
    assert [(token.start, token.end, token.confidence) for token in tokens] == [
        (word.start, word.end, word.confidence) for word in words
    ]
    assert document.metadata["lyrics_source"] == "transcription"
    assert "复核识别文字" in result.status
    workspace = load_workspace_project(Path(result.output_dir) / "karaoke-forge-project.json")
    assert read_workspace_lyrics(workspace).lines == document.lines


def test_empty_recognition_reports_failure_without_empty_success_project(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "karaoke_forge.pipeline.transcribe_with_faster_whisper",
        lambda *a, **k: TranscriptionResult([], "en", .99),
    )
    result = prepare(tmp_path, monkeypatch)
    assert result.project is None
    assert "未识别到可用" in result.log
    assert not (Path(result.output_dir) / "karaoke-forge-project.json").exists()
