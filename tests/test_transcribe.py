from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from karaoke_forge.models import LyricLine, LyricsDocument
from karaoke_forge.transcribe import (
    TranscriptionError,
    force_align_lyrics_with_faster_whisper,
    transcribe_with_faster_whisper,
)


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
    assert received["condition_on_previous_text"] is False
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


def test_older_faster_whisper_without_hotwords_uses_the_short_hint_as_prompt(
    tmp_path,
    monkeypatch,
) -> None:
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio")
    attempts: list[dict[str, object]] = []

    class FakeWhisperModel:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def transcribe(self, path, **kwargs):
            attempts.append(dict(kwargs))
            if "hotwords" in kwargs:
                raise TypeError("unexpected keyword argument 'hotwords'")
            word = SimpleNamespace(word="alpha", start=1.0, end=1.5, probability=0.9)
            segment = SimpleNamespace(start=1.0, end=1.5, words=[word])
            info = SimpleNamespace(language="en", language_probability=0.99)
            return iter([segment]), info

    faster_whisper = ModuleType("faster_whisper")
    faster_whisper.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", faster_whisper)

    result = transcribe_with_faster_whisper(audio, hotwords="alpha beta")

    assert len(result.words) == 1
    assert len(attempts) == 2
    assert attempts[0]["hotwords"] == "alpha beta"
    assert "hotwords" not in attempts[1]
    assert attempts[1]["initial_prompt"] == "alpha beta"
    assert attempts[1]["condition_on_previous_text"] is False


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


def test_forced_alignment_uses_native_model_and_preserves_absolute_line_index(
    tmp_path,
    monkeypatch,
) -> None:
    """Short line windows must not be padded before their real frame count is recorded."""

    np = pytest.importorskip("numpy")
    audio_path = tmp_path / "song.wav"
    audio_path.write_bytes(b"audio")
    extractor_calls: list[dict[str, object]] = []
    pad_calls: list[tuple[object, int]] = []
    align_calls: list[tuple[object, tuple[int, ...], list[list[int]], int, int]] = []

    audio_module = ModuleType("faster_whisper.audio")
    audio_module.decode_audio = lambda _path, *, sampling_rate: np.zeros(
        sampling_rate * 30,
        dtype=np.float32,
    )

    def fake_pad_or_trim(features, frame_limit):
        pad_calls.append((features, frame_limit))
        return features

    audio_module.pad_or_trim = fake_pad_or_trim

    class FakeTokenizer:
        eot = 99
        sot_sequence = (1, 2)

        def __init__(self, _tokenizer, _multilingual, *, task, language) -> None:
            assert task == "transcribe"
            assert language == "en"

        def encode(self, text):
            assert text == "alpha beta"
            return [10, 11]

        def split_to_word_tokens(self, tokens):
            assert tokens == [10, 11, self.eot]
            return ["alpha", " beta", ""], [[10], [11], [self.eot]]

    tokenizer_module = ModuleType("faster_whisper.tokenizer")
    tokenizer_module.Tokenizer = FakeTokenizer
    faster_whisper = ModuleType("faster_whisper")
    monkeypatch.setitem(sys.modules, "faster_whisper", faster_whisper)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", audio_module)
    monkeypatch.setitem(sys.modules, "faster_whisper.tokenizer", tokenizer_module)

    class FakeFeatureExtractor:
        sampling_rate = 100
        nb_max_frames = 3000

        def __call__(self, audio, *, padding):
            extractor_calls.append({"samples": len(audio), "padding": padding})
            return np.zeros((80, 120), dtype=np.float32)

    class FakeNativeModel:
        is_multilingual = True

        def align(
            self,
            encoded,
            sot_sequence,
            text_tokens,
            num_frames,
            *,
            median_filter_width,
        ):
            align_calls.append(
                (
                    encoded,
                    tuple(sot_sequence),
                    text_tokens,
                    num_frames,
                    median_filter_width,
                )
            )
            # Three timestamp jumps delimit two real lyric words plus EOT.
            result = SimpleNamespace(
                alignments=[(0, 0), (0, 4), (1, 10), (1, 14), (2, 24)],
                text_token_probs=[0.96, 0.92],
            )
            return [result]

    class FakeWhisperModel:
        feature_extractor = FakeFeatureExtractor()
        hf_tokenizer = object()
        model = FakeNativeModel()
        tokens_per_second = 10.0
        max_length = 448

        def encode(self, features):
            return ("encoded", features.shape)

    lyrics = LyricsDocument(
        lines=[
            LyricLine(text="metadata", hidden=True),
            LyricLine(text="alpha beta", start=10.0, end=12.0),
        ],
        metadata={"language": "en"},
    )

    result = force_align_lyrics_with_faster_whisper(
        audio_path,
        lyrics,
        whisper_model=FakeWhisperModel(),
        language="en",
    )

    assert result.attempted_lines == 1
    assert result.aligned_lines == 1
    assert result.lines[0].line_index == 1
    assert [word.text for word in result.lines[0].words] == ["alpha", " beta"]
    # The native timestamps are relative to the 9.4-second line window.
    assert result.lines[0].words[0].start == pytest.approx(9.4)
    assert result.lines[0].words[1].start == pytest.approx(10.4)
    assert result.lines[0].words[1].end == pytest.approx(11.8)
    assert extractor_calls == [{"samples": 320, "padding": False}]
    assert len(pad_calls) == 1
    assert pad_calls[0][0].shape == (80, 120)
    assert pad_calls[0][1] == 3000
    assert len(align_calls) == 1
    assert align_calls[0][1:] == ((1, 2), [[10, 11]], 120, 7)
