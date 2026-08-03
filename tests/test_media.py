import math
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from karaoke_forge.media import (
    MediaError,
    match_audio_envelopes,
    probe_media_has_audio,
    render_karaoke_video,
    separate_vocals,
)


def _distinctive_envelope(length: int, seed: int) -> list[float]:
    randomizer = random.Random(seed)
    return [
        math.sin(index * 0.071) + 0.55 * math.sin(index * 0.193) + randomizer.uniform(-0.08, 0.08)
        for index in range(length)
    ]


def test_audio_sync_finds_song_after_narrative_intro() -> None:
    reference = _distinctive_envelope(2400, seed=1)
    intro = _distinctive_envelope(150, seed=2)
    outro = _distinctive_envelope(90, seed=3)
    video = [*intro, *reference, *outro]

    result = match_audio_envelopes(reference, video, frame_seconds=0.05)

    assert result.reliable
    assert abs(result.offset - 7.5) <= 0.1
    assert result.confidence > 0.9
    assert result.matched_windows == result.total_windows


def test_audio_sync_rejects_different_song() -> None:
    reference = _distinctive_envelope(2400, seed=4)
    randomizer = random.Random(5)
    video = [randomizer.uniform(-1.0, 1.0) for _ in range(2200)]

    result = match_audio_envelopes(reference, video, frame_seconds=0.05)

    assert not result.reliable
    assert result.matched_windows < 3


def test_probe_media_has_audio_distinguishes_silent_video(tmp_path, monkeypatch) -> None:
    media = tmp_path / "silent.webm"
    media.write_bytes(b"video")
    monkeypatch.setattr("karaoke_forge.media.find_ffprobe", lambda: "ffprobe")
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout='{"streams": []}'),
            SimpleNamespace(returncode=0, stdout='{"streams": [{"index": 1}]}'),
        ]
    )
    monkeypatch.setattr(
        "karaoke_forge.media.subprocess.run",
        lambda *_args, **_kwargs: next(responses),
    )

    assert probe_media_has_audio(media) is False
    assert probe_media_has_audio(media) is True


def test_render_reports_disk_full_and_removes_new_partial_output(tmp_path, monkeypatch) -> None:
    video = tmp_path / "video.webm"
    subtitles = tmp_path / "lyrics.ass"
    output = tmp_path / "karaoke.mp4"
    video.write_bytes(b"video")
    subtitles.write_text("[Script Info]\n", encoding="utf-8")
    monkeypatch.setattr("karaoke_forge.media.find_ffmpeg", lambda: "ffmpeg")

    def fail_without_space(command, **_kwargs):
        output_path = Path(command[-1])
        output_path.write_bytes(b"incomplete video")
        return SimpleNamespace(
            returncode=-28,
            stdout="Error writing trailer: No space left on device",
        )

    monkeypatch.setattr("karaoke_forge.media.subprocess.run", fail_without_space)

    with pytest.raises(MediaError, match="输出磁盘空间不足"):
        render_karaoke_video(video, subtitles, output)

    assert not output.exists()


def test_separate_vocals_rejects_cuda_when_torch_is_cpu_only(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.m4a"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(
        "karaoke_forge.media.inspect_demucs_runtime",
        lambda: SimpleNamespace(
            installed=True,
            error=None,
            device="cpu",
            device_name=None,
            nvidia_detected=True,
        ),
    )

    with pytest.raises(MediaError, match="Torch 是 CPU 版"):
        separate_vocals(audio, tmp_path / "separated", device="cuda")


def test_separate_vocals_streams_progress_and_uses_requested_device(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "song.m4a"
    audio.write_bytes(b"audio")
    output_dir = tmp_path / "separated"
    vocals = output_dir / "htdemucs" / "song" / "vocals.wav"
    captured: dict[str, object] = {}

    class FakeProcess:
        stdout = iter(["Downloading model\n", "100% separated\n"])

        def wait(self) -> int:
            vocals.parent.mkdir(parents=True)
            vocals.write_bytes(b"vocals")
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(
        "karaoke_forge.media.inspect_demucs_runtime",
        lambda: SimpleNamespace(
            installed=True,
            error=None,
            device="cpu",
            device_name=None,
            nvidia_detected=False,
        ),
    )
    monkeypatch.setattr("karaoke_forge.media.subprocess.Popen", fake_popen)
    monkeypatch.setattr("karaoke_forge.media._ensure_demucs_legacy_model", lambda *_args: None)
    certificate_bundle = tmp_path / "cacert.pem"
    monkeypatch.setitem(
        sys.modules,
        "certifi",
        SimpleNamespace(where=lambda: str(certificate_bundle)),
    )
    messages: list[str] = []

    result = separate_vocals(audio, output_dir, device="cpu", progress=messages.append)

    assert result == vocals
    command = captured["command"]
    assert command[3:5] == ["-d", "cpu"]
    assert captured["environment"]["HF_HUB_OFFLINE"] == "1"
    assert captured["environment"]["SSL_CERT_FILE"] == str(certificate_bundle)
    assert captured["environment"]["REQUESTS_CA_BUNDLE"] == str(certificate_bundle)
    assert any("Downloading model" in message for message in messages)
    assert messages[-1].startswith("Demucs 人声分离完成")
