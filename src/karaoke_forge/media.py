from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import json
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from math import log1p, sqrt
from pathlib import Path
from statistics import median


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioSyncResult:
    offset: float
    confidence: float
    matched_windows: int
    total_windows: int
    reference_duration: float
    video_duration: float
    correlation: float

    @property
    def reliable(self) -> bool:
        return self.matched_windows >= 3 and self.confidence >= 0.48


def find_ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise MediaError(
            "FFmpeg was not found on PATH. Install FFmpeg and make sure `ffmpeg -version` works."
        )
    return executable


def find_ffprobe() -> str | None:
    return shutil.which("ffprobe")


def probe_media_duration(media_path: str | Path) -> float | None:
    executable = find_ffprobe()
    media = Path(media_path)
    if not executable or not media.is_file():
        return None
    completed = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(media),
        ],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        return None
    try:
        value = json.loads(completed.stdout)["format"]["duration"]
        return float(value)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _audio_envelope(
    media_path: str | Path,
    *,
    sample_rate: int = 2000,
    frame_seconds: float = 0.05,
) -> list[float]:
    media = Path(media_path).resolve()
    if not media.is_file():
        raise FileNotFoundError(f"Media file not found: {media}")
    command = [
        find_ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(media),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ]
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise MediaError(f"无法提取音轨用于自动同步：{message}")

    samples = array("h")
    samples.frombytes(completed.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    frame_size = max(1, round(sample_rate * frame_seconds))
    envelope: list[float] = []
    for start in range(0, len(samples) - frame_size + 1, frame_size):
        frame = samples[start : start + frame_size]
        square_sum = 0.0
        difference_sum = 0.0
        previous = int(frame[0])
        for sample in frame:
            value = int(sample)
            square_sum += value * value
            difference_sum += abs(value - previous)
            previous = value
        rms = sqrt(square_sum / frame_size)
        transient = difference_sum / frame_size
        envelope.append(log1p(rms) + 0.18 * log1p(transient))
    if len(envelope) < round(15 / frame_seconds):
        raise MediaError("音轨太短，无法可靠地自动定位歌曲开始位置。")
    return envelope


def _window_correlation(pattern: list[float], target: list[float], start: int) -> float:
    window = target[start : start + len(pattern)]
    pattern_mean = sum(pattern) / len(pattern)
    window_mean = sum(window) / len(window)
    numerator = 0.0
    pattern_energy = 0.0
    window_energy = 0.0
    for left, right in zip(pattern, window, strict=True):
        left_delta = left - pattern_mean
        right_delta = right - window_mean
        numerator += left_delta * right_delta
        pattern_energy += left_delta * left_delta
        window_energy += right_delta * right_delta
    denominator = sqrt(pattern_energy * window_energy)
    return numerator / denominator if denominator > 1e-12 else 0.0


def _best_window_matches(
    pattern: list[float],
    target: list[float],
    *,
    limit: int = 8,
    separation: int = 10,
) -> list[tuple[float, int]]:
    if len(pattern) > len(target):
        return []
    scored = [
        (_window_correlation(pattern, target, start), start)
        for start in range(len(target) - len(pattern) + 1)
    ]
    scored.sort(reverse=True)
    selected: list[tuple[float, int]] = []
    for score, start in scored:
        if any(abs(start - existing) < separation for _, existing in selected):
            continue
        selected.append((score, start))
        if len(selected) >= limit:
            break
    return selected


def match_audio_envelopes(
    reference: list[float],
    video: list[float],
    *,
    frame_seconds: float = 0.05,
    window_seconds: float = 12.0,
) -> AudioSyncResult:
    """Find where a reference song starts inside a video's audio envelope."""

    window_size = max(20, round(window_seconds / frame_seconds))
    if len(reference) < window_size or len(video) < window_size:
        raise MediaError("音轨太短，无法可靠地自动定位歌曲开始位置。")

    available = len(reference) - window_size
    fractions = (0.06, 0.24, 0.42, 0.60, 0.78, 0.92)
    reference_starts = sorted({round(available * fraction) for fraction in fractions})
    candidate_groups: list[list[tuple[float, int]]] = []
    for reference_start in reference_starts:
        pattern = reference[reference_start : reference_start + window_size]
        matches = _best_window_matches(
            pattern,
            video,
            separation=max(1, round(1.5 / frame_seconds)),
        )
        candidate_groups.append(
            [
                (score, video_start - reference_start)
                for score, video_start in matches
            ]
        )

    tolerance = max(1, round(0.8 / frame_seconds))
    all_offsets = [
        offset
        for group in candidate_groups
        for score, offset in group
        if score >= 0.18
    ]
    best_cluster: list[tuple[float, int]] = []
    best_key = (-1, -1.0)
    for center in all_offsets:
        cluster: list[tuple[float, int]] = []
        for group_index, group in enumerate(candidate_groups):
            nearby = [
                (score, offset)
                for score, offset in group
                if abs(offset - center) <= tolerance
            ]
            if nearby:
                score, offset = max(nearby)
                cluster.append((score, offset))
        strong = sum(score >= 0.42 for score, _ in cluster)
        average = sum(score for score, _ in cluster) / len(cluster) if cluster else 0.0
        key = (strong, average)
        if key > best_key:
            best_key = key
            best_cluster = cluster

    if not best_cluster:
        return AudioSyncResult(
            offset=0.0,
            confidence=0.0,
            matched_windows=0,
            total_windows=len(reference_starts),
            reference_duration=len(reference) * frame_seconds,
            video_duration=len(video) * frame_seconds,
            correlation=0.0,
        )

    offset_frames = round(median(offset for _, offset in best_cluster))
    correlations: list[float] = []
    offsets: list[int] = []
    for reference_start in reference_starts:
        video_start = reference_start + offset_frames
        if video_start < 0 or video_start + window_size > len(video):
            continue
        score = _window_correlation(
            reference[reference_start : reference_start + window_size],
            video,
            video_start,
        )
        if score >= 0.30:
            correlations.append(score)
            offsets.append(offset_frames)

    matched = sum(score >= 0.42 for score in correlations)
    correlation = median(correlations) if correlations else 0.0
    coverage = matched / len(reference_starts)
    confidence = max(0.0, min(1.0, correlation * (0.55 + 0.45 * coverage)))
    return AudioSyncResult(
        offset=offset_frames * frame_seconds,
        confidence=confidence,
        matched_windows=matched,
        total_windows=len(reference_starts),
        reference_duration=len(reference) * frame_seconds,
        video_duration=len(video) * frame_seconds,
        correlation=correlation,
    )


def detect_audio_sync(
    reference_audio: str | Path,
    video_path: str | Path,
    *,
    progress: Callable[[str], None] | None = None,
) -> AudioSyncResult:
    """Detect a song's start offset inside an MV, tolerating narrative intros/outros."""

    if progress:
        progress("正在提取歌曲与 MV 音轨指纹")
    reference = _audio_envelope(reference_audio)
    video = _audio_envelope(video_path)
    if progress:
        progress("正在定位 MV 中歌曲真正开始的位置")
    return match_audio_envelopes(reference, video)


def render_karaoke_video(
    video_path: str | Path,
    ass_path: str | Path,
    output_path: str | Path,
    *,
    audio_path: str | Path | None = None,
    audio_offset: float = 0.0,
    crf: int = 18,
    preset: str = "medium",
    audio_bitrate: str = "320k",
    overwrite: bool = False,
    progress: Callable[[str], None] | None = None,
) -> Path:
    video = Path(video_path).resolve()
    subtitles = Path(ass_path).resolve()
    output = Path(output_path).resolve()
    external_audio = Path(audio_path).resolve() if audio_path else None
    use_external_audio = external_audio is not None and external_audio != video

    if not video.is_file():
        raise FileNotFoundError(f"Video file not found: {video}")
    if not subtitles.is_file():
        raise FileNotFoundError(f"ASS subtitle file not found: {subtitles}")
    if external_audio and not external_audio.is_file():
        raise FileNotFoundError(f"Audio file not found: {external_audio}")
    if output.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {output}. Pass --overwrite to replace it.")
    output.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = find_ffmpeg()
    with tempfile.TemporaryDirectory(prefix="karaoke-forge-") as temp_name:
        temp_dir = Path(temp_name)
        local_ass = temp_dir / "karaoke.ass"
        shutil.copy2(subtitles, local_ass)

        command = [ffmpeg, "-hide_banner", "-y" if overwrite else "-n", "-i", str(video)]
        if use_external_audio:
            if audio_offset:
                command.extend(["-itsoffset", f"{audio_offset:.3f}"])
            command.extend(["-i", str(external_audio)])

        command.extend(["-vf", "ass=filename=karaoke.ass", "-map", "0:v:0"])
        if use_external_audio:
            command.extend(["-map", "1:a:0"])
        else:
            command.extend(["-map", "0:a?"])
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                preset,
                "-crf",
                str(crf),
                "-c:a",
                "aac",
                "-b:a",
                audio_bitrate,
                "-movflags",
                "+faststart",
                "-shortest",
                str(output),
            ]
        )
        if progress:
            progress(f"Rendering karaoke video: {output}")
        completed = subprocess.run(
            command,
            cwd=temp_dir,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if completed.returncode != 0:
            tail = "\n".join(completed.stdout.splitlines()[-30:])
            raise MediaError(f"FFmpeg failed with exit code {completed.returncode}:\n{tail}")
    return output


def separate_vocals(
    audio_path: str | Path,
    output_dir: str | Path,
    *,
    model: str = "htdemucs",
    progress: Callable[[str], None] | None = None,
) -> Path:
    audio = Path(audio_path).resolve()
    directory = Path(output_dir).resolve()
    if not audio.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio}")
    try:
        import demucs  # noqa: F401
    except ImportError as exc:
        raise MediaError(
            "Vocal separation requires Demucs. Run "
            '`pip install -e ".[separate]"` (or `pip install karaoke-forge[separate]`).'
        ) from exc

    directory.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "demucs",
        "--two-stems",
        "vocals",
        "-n",
        model,
        "-o",
        str(directory),
        str(audio),
    ]
    if progress:
        progress(f"Separating vocals with Demucs model: {model}")
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        tail = "\n".join(completed.stdout.splitlines()[-30:])
        raise MediaError(f"Demucs failed with exit code {completed.returncode}:\n{tail}")

    candidates = sorted(directory.rglob("vocals.wav"), key=lambda path: path.stat().st_mtime)
    if not candidates:
        raise MediaError("Demucs completed, but vocals.wav could not be found.")
    return candidates[-1]
