from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from karaoke_forge.transcribe import TranscriptionError, transcribe_with_faster_whisper


def test_song_transcription_scans_the_complete_track_by_default(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio")
    received: dict[str, object] = {}
    model_options: list[dict[str, object]] = []
    messages: list[str] = []

    class FakeWhisperModel:
        def __init__(self, *args, **kwargs) -> None:
            model_options.append(kwargs)

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

    result = transcribe_with_faster_whisper(audio, progress=messages.append)

    assert received["vad_filter"] is False
    assert result.detected_language == "ja"
    assert len(result.words) == 1
    assert len(model_options) == 1
    assert model_options[0]["local_files_only"] is True
    assert any("正在检查 Whisper 模型 small 的本地缓存" in item for item in messages)
    assert any("Whisper 模型 small 已就绪" in item for item in messages)
    assert all("50.0%" not in item for item in messages)


def test_missing_model_reports_actionable_network_error(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio")
    attempts: list[dict[str, object]] = []
    messages: list[str] = []

    class LocalEntryNotFoundError(RuntimeError):
        pass

    class ConnectTimeout(RuntimeError):
        pass

    class FakeWhisperModel:
        def __init__(self, *args, **kwargs) -> None:
            attempts.append(kwargs)
            if kwargs.get("local_files_only"):
                raise LocalEntryNotFoundError("no cached snapshot folder")
            raise ConnectTimeout("connection attempt failed")

    faster_whisper = ModuleType("faster_whisper")
    faster_whisper.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", faster_whisper)

    with pytest.raises(TranscriptionError, match="本机没有完整缓存.*无法连接"):
        transcribe_with_faster_whisper(audio, model="small", progress=messages.append)

    assert len(attempts) == 2
    assert attempts[0]["local_files_only"] is True
    assert "local_files_only" not in attempts[1]
    assert any("无法提供可靠百分比" in item for item in messages)


def test_online_model_configuration_error_is_not_misreported_as_network_failure(
    tmp_path,
    monkeypatch,
) -> None:
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio")

    class LocalEntryNotFoundError(RuntimeError):
        pass

    class FakeWhisperModel:
        def __init__(self, *args, **kwargs) -> None:
            if kwargs.get("local_files_only"):
                raise LocalEntryNotFoundError("no cached snapshot folder")
            raise ValueError("unsupported compute type float16 on this device")

    faster_whisper = ModuleType("faster_whisper")
    faster_whisper.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", faster_whisper)

    with pytest.raises(TranscriptionError, match="unsupported compute type") as caught:
        transcribe_with_faster_whisper(audio, model="small")

    assert "无法连接 Hugging Face" not in str(caught.value)


def test_older_faster_whisper_without_local_only_option_still_works(
    tmp_path,
    monkeypatch,
) -> None:
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio")
    attempts: list[dict[str, object]] = []

    class FakeWhisperModel:
        def __init__(self, *args, **kwargs) -> None:
            attempts.append(kwargs)
            if "local_files_only" in kwargs:
                raise TypeError("unexpected keyword argument 'local_files_only'")

        def transcribe(self, path, **kwargs):
            word = SimpleNamespace(word="Hello", start=1.0, end=1.5, probability=0.9)
            segment = SimpleNamespace(start=1.0, end=1.5, words=[word])
            info = SimpleNamespace(language="en", language_probability=0.99)
            return iter([segment]), info

    faster_whisper = ModuleType("faster_whisper")
    faster_whisper.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", faster_whisper)

    result = transcribe_with_faster_whisper(audio, model="small")

    assert len(result.words) == 1
    assert len(attempts) == 2
    assert attempts[0]["local_files_only"] is True
    assert "local_files_only" not in attempts[1]


def test_incomplete_cached_model_retries_online(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio")
    attempts: list[dict[str, object]] = []

    class FakeWhisperModel:
        def __init__(self, *args, **kwargs) -> None:
            attempts.append(kwargs)
            if kwargs.get("local_files_only"):
                raise RuntimeError("Unable to open file 'model.bin'")
            raise RuntimeError("ConnectTimeout: connection attempt failed")

    faster_whisper = ModuleType("faster_whisper")
    faster_whisper.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", faster_whisper)

    with pytest.raises(TranscriptionError, match="本机没有完整缓存.*无法连接"):
        transcribe_with_faster_whisper(audio, model="large-v3")

    assert len(attempts) == 2


def test_incomplete_snapshot_error_retries_online(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio")
    attempts: list[dict[str, object]] = []

    class IncompleteSnapshotError(RuntimeError):
        pass

    class FakeWhisperModel:
        def __init__(self, *args, **kwargs) -> None:
            attempts.append(kwargs)
            if kwargs.get("local_files_only"):
                raise IncompleteSnapshotError("tokenizer file is missing")
            raise RuntimeError("ConnectTimeout: connection attempt failed")

    faster_whisper = ModuleType("faster_whisper")
    faster_whisper.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", faster_whisper)

    with pytest.raises(TranscriptionError, match="本机没有完整缓存.*无法连接"):
        transcribe_with_faster_whisper(audio, model="small")

    assert len(attempts) == 2


def test_invalid_local_model_does_not_report_hub_network_failure(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.mp3"
    model = tmp_path / "local-model"
    audio.write_bytes(b"audio")
    model.mkdir()
    received_model: list[str] = []

    class FakeWhisperModel:
        def __init__(self, model_path, **kwargs) -> None:
            received_model.append(model_path)
            raise RuntimeError("Unable to open file 'model.bin'")

    faster_whisper = ModuleType("faster_whisper")
    faster_whisper.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", faster_whisper)

    with pytest.raises(TranscriptionError, match="model.bin") as caught:
        transcribe_with_faster_whisper(audio, model=str(model))

    assert "无法连接 Hugging Face" not in str(caught.value)
    assert received_model == [str(model.resolve())]
