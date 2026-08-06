import math
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from karaoke_forge.media import (
    MediaError,
    create_spinning_cover_video,
    extract_video_frame,
    find_ffmpeg,
    find_ffprobe,
    match_audio_envelopes,
    probe_media_has_audio,
    render_karaoke_video,
    separate_vocals,
)


def test_media_tools_use_the_shared_runtime_resolver(monkeypatch) -> None:
    resolved = {"ffmpeg": "private-ffmpeg", "ffprobe": "private-ffprobe"}
    monkeypatch.setattr(
        "karaoke_forge.media.find_runtime_executable",
        lambda name: resolved.get(name),
    )

    assert find_ffmpeg() == "private-ffmpeg"
    assert find_ffprobe() == "private-ffprobe"


def test_missing_ffmpeg_points_windows_users_to_one_click_repair(monkeypatch) -> None:
    monkeypatch.setattr("karaoke_forge.media.find_runtime_executable", lambda _name: None)

    with pytest.raises(MediaError, match="首次安装\\.bat"):
        find_ffmpeg()


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


def test_spinning_cover_builds_audio_reactive_aurora_graph(tmp_path, monkeypatch) -> None:
    cover = tmp_path / "cover.jpg"
    audio = tmp_path / "song.wav"
    output = tmp_path / "background.mp4"
    cover.write_bytes(b"image")
    audio.write_bytes(b"audio")
    captured: dict[str, object] = {}
    monkeypatch.setattr("karaoke_forge.media.find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr("karaoke_forge.media.probe_media_duration", lambda _path: 42.5)

    def fake_run(command, **_kwargs):
        captured["command"] = command
        output.write_bytes(b"video")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("karaoke_forge.media.subprocess.run", fake_run)

    result = create_spinning_cover_video(cover, audio, output)

    command = captured["command"]
    graph = command[command.index("-filter_complex") + 1]
    assert result == output
    assert "midnight-stage.png" not in " ".join(str(value) for value in command)
    assert "gblur=sigma=42" in graph
    assert "saturation=1.25" in graph
    assert "geq=r='11+8*sin" in graph
    assert "gblur=sigma=14" in graph
    assert "rotate=2*PI*t/12.000" in graph
    assert "showwaves=" in graph
    assert command[command.index("-t") + 1] == "42.500"


def test_spinning_cover_supports_fixed_background_theme(tmp_path, monkeypatch) -> None:
    cover = tmp_path / "cover.jpg"
    audio = tmp_path / "song.wav"
    output = tmp_path / "sunset.mp4"
    cover.write_bytes(b"image")
    audio.write_bytes(b"audio")
    captured: dict[str, object] = {}
    monkeypatch.setattr("karaoke_forge.media.find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr("karaoke_forge.media.probe_media_duration", lambda _path: 8.0)

    def fake_run(command, **_kwargs):
        captured["command"] = command
        output.write_bytes(b"video")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("karaoke_forge.media.subprocess.run", fake_run)

    create_spinning_cover_video(cover, audio, output, background_theme="sunset")

    command = captured["command"]
    graph = command[command.index("-filter_complex") + 1]
    assert "sunset-glass.png" in " ".join(str(value) for value in command)
    assert "sin(t/11)" in graph
    assert "saturation=1.00" in graph


def test_spinning_cover_rejects_unknown_background_theme(tmp_path) -> None:
    cover = tmp_path / "cover.jpg"
    audio = tmp_path / "song.wav"
    cover.write_bytes(b"image")
    audio.write_bytes(b"audio")

    with pytest.raises(ValueError, match="Unsupported cover background theme"):
        create_spinning_cover_video(
            cover,
            audio,
            tmp_path / "bad.mp4",
            duration=1.0,
            background_theme="neon-city",
        )


def test_spinning_cover_supports_audio_frequency_stage(tmp_path, monkeypatch) -> None:
    cover = tmp_path / "cover.jpg"
    audio = tmp_path / "song.wav"
    output = tmp_path / "spectrum.mp4"
    cover.write_bytes(b"image")
    audio.write_bytes(b"audio")
    captured: dict[str, object] = {}
    monkeypatch.setattr("karaoke_forge.media.find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr("karaoke_forge.media.probe_media_duration", lambda _path: 5.0)

    def fake_run(command, **_kwargs):
        captured["command"] = command
        output.write_bytes(b"video")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("karaoke_forge.media.subprocess.run", fake_run)

    create_spinning_cover_video(cover, audio, output, style="spectrum")

    command = captured["command"]
    graph = command[command.index("-filter_complex") + 1]
    assert "showfreqs=" in graph
    assert command[command.index("-i") + 1] == str(cover)
    assert str(audio) in command


def test_spinning_cover_can_seek_audio_for_a_short_preview(tmp_path, monkeypatch) -> None:
    cover = tmp_path / "cover.jpg"
    audio = tmp_path / "song.wav"
    output = tmp_path / "preview.mp4"
    cover.write_bytes(b"image")
    audio.write_bytes(b"audio")
    captured: dict[str, object] = {}
    monkeypatch.setattr("karaoke_forge.media.find_ffmpeg", lambda: "ffmpeg")

    def fake_run(command, **_kwargs):
        captured["command"] = command
        output.write_bytes(b"video")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("karaoke_forge.media.subprocess.run", fake_run)

    create_spinning_cover_video(
        cover,
        audio,
        output,
        duration=1.25,
        audio_start=37.5,
    )

    command = captured["command"]
    audio_index = command.index(str(audio))
    assert command[audio_index - 3 : audio_index] == ["-ss", "37.500", "-i"]
    assert command[command.index("-t") + 1] == "1.250"


def test_extract_video_frame_builds_fast_single_frame_command(tmp_path, monkeypatch) -> None:
    video = tmp_path / "mv.mp4"
    output = tmp_path / "frame.jpg"
    video.write_bytes(b"video")
    captured: dict[str, object] = {}
    monkeypatch.setattr("karaoke_forge.media.find_ffmpeg", lambda: "ffmpeg")

    def fake_run(command, **_kwargs):
        captured["command"] = command
        output.write_bytes(b"jpeg")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("karaoke_forge.media.subprocess.run", fake_run)

    result = extract_video_frame(
        video,
        output,
        timestamp=42.25,
        resolution=(960, 540),
    )

    command = captured["command"]
    assert result == output
    assert command[command.index("-ss") + 1] == "42.250"
    assert command[command.index("-map") + 1] == "0:V:0"
    assert command[command.index("-frames:v") + 1] == "1"
    graph = command[command.index("-vf") + 1]
    assert "scale=960:540" in graph
    assert "pad=960:540" in graph


@pytest.mark.parametrize("style", ["turntable", "cdplayer"])
def test_spinning_cover_builds_centered_turntable_graph(tmp_path, monkeypatch, style) -> None:
    cover = tmp_path / "cover.jpg"
    audio = tmp_path / "song.wav"
    output = tmp_path / "turntable.mp4"
    cover.write_bytes(b"image")
    audio.write_bytes(b"audio")
    captured: dict[str, object] = {}
    monkeypatch.setattr("karaoke_forge.media.find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr("karaoke_forge.media.probe_media_duration", lambda _path: 6.0)

    def fake_run(command, **_kwargs):
        captured["command"] = command
        output.write_bytes(b"video")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("karaoke_forge.media.subprocess.run", fake_run)

    create_spinning_cover_video(
        cover,
        audio,
        output,
        style=style,
        background_theme="sunset",
    )

    command = captured["command"]
    graph = command[command.index("-filter_complex") + 1]
    assert "turntable-chassis.png" in " ".join(str(value) for value in command)
    assert "sunset-glass.png" in " ".join(str(value) for value in command)
    assert "[3:v]scale=" in graph
    assert "[recordwavebg][turntableshadowsoft]overlay=(W-w)/2:120" in graph
    assert "[recordshadowstage][turntable]overlay=(W-w)/2:108" in graph
    assert "rotate=2*PI*t/12.000" in graph
    assert "[recordlabel][hub]overlay=" in graph


def test_render_passes_uploaded_fonts_to_libass(tmp_path, monkeypatch) -> None:
    video = tmp_path / "video.mp4"
    subtitles = tmp_path / "lyrics.ass"
    font = tmp_path / "Pretty Font.otf"
    output = tmp_path / "karaoke.mp4"
    video.write_bytes(b"video")
    subtitles.write_text("[Script Info]\n", encoding="utf-8")
    font.write_bytes(b"font")
    captured: dict[str, object] = {}
    monkeypatch.setattr("karaoke_forge.media.find_ffmpeg", lambda: "ffmpeg")

    def fake_run(command, **_kwargs):
        captured["command"] = command
        output.write_bytes(b"video")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("karaoke_forge.media.subprocess.run", fake_run)

    render_karaoke_video(video, subtitles, output, font_files=[font])

    command = captured["command"]
    assert command[command.index("-vf") + 1] == "ass=filename=karaoke.ass:fontsdir=fonts"


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
    instrumental = output_dir / "htdemucs" / "song" / "no_vocals.wav"
    captured: dict[str, object] = {}

    class FakeProcess:
        stdout = iter(["Downloading model\n", "100% separated\n"])

        def wait(self) -> int:
            vocals.parent.mkdir(parents=True)
            vocals.write_bytes(b"vocals")
            instrumental.write_bytes(b"instrumental")
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
