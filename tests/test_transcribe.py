from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from karaoke_forge.transcribe import transcribe_with_faster_whisper


def test_song_transcription_scans_the_complete_track_by_default(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio")
    received: dict[str, object] = {}

    class FakeWhisperModel:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def transcribe(self, path, **kwargs):
            received.update(kwargs)
            word = SimpleNamespace(
                word="歌",
                start=1.0,
                end=1.5,
                probability=0.95,
            )
            segment = SimpleNamespace(start=1.0, end=1.5, words=[word])
            info = SimpleNamespace(language="ja", language_probability=0.99)
            return iter([segment]), info

    faster_whisper = ModuleType("faster_whisper")
    faster_whisper.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", faster_whisper)

    result = transcribe_with_faster_whisper(audio)

    assert received["vad_filter"] is False
    assert result.detected_language == "ja"
    assert len(result.words) == 1
