from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import log1p, sqrt
from pathlib import Path
from statistics import median

from .runtime import inspect_demucs_runtime


class MediaError(RuntimeError):
    pass


def _demucs_legacy_model_files(model: str) -> list[tuple[str, str, str]]:
    """Resolve an official Demucs model name to its legacy mirror files."""

    try:
        import demucs
        import yaml
    except ImportError:
        return []
    remote_root = Path(demucs.__file__).resolve().parent / "remote"
    file_list = remote_root / "files.txt"
    if not file_list.is_file():
        return []
    root = ""
    models: dict[str, tuple[str, str, str]] = {}
    for raw_line in file_list.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("root:"):
            root = line.split(":", 1)[1].strip()
            continue
        signature = line.split("-", 1)[0]
        checksum = Path(line).stem.rsplit("-", 1)[-1]
        models[signature] = (
            f"https://dl.fbaipublicfiles.com/demucs/{root}{line}",
            line,
            checksum,
        )
    bag_file = remote_root / f"{model}.yaml"
    if bag_file.is_file():
        bag = yaml.safe_load(bag_file.read_text(encoding="utf-8")) or {}
        signatures = [str(value) for value in bag.get("models", [])]
    else:
        signatures = [model]
    return [models[signature] for signature in signatures if signature in models]


def _file_checksum_prefix(path: Path, length: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()[:length]


def _ensure_demucs_legacy_model(
    model: str,
    environment: dict[str, str],
    progress: Callable[[str], None] | None,
) -> None:
    """Download model weights with httpx to avoid broken Windows cert stores."""

    model_files = _demucs_legacy_model_files(model)
    if not model_files:
        return
    torch_home = environment.get("TORCH_HOME")
    if torch_home:
        checkpoint_dir = Path(torch_home) / "hub" / "checkpoints"
    else:
        import torch

        checkpoint_dir = Path(torch.hub.get_dir()) / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    try:
        import certifi
        import httpx
    except ImportError:
        return
    for url, filename, checksum in model_files:
        target = checkpoint_dir / filename
        if target.is_file() and target.stat().st_size > 0:
            continue
        if progress:
            progress(f"正在下载 Demucs 模型 {filename}（首次仅需一次）")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f"{target.stem}-",
                suffix=".partial",
                dir=checkpoint_dir,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                with httpx.stream(
                    "GET",
                    url,
                    follow_redirects=True,
                    timeout=httpx.Timeout(60.0, connect=15.0),
                    verify=certifi.where(),
                ) as response:
                    response.raise_for_status()
                    total = int(response.headers.get("content-length") or 0)
                    downloaded = 0
                    last_percent = -10
                    for chunk in response.iter_bytes(1024 * 1024):
                        temporary.write(chunk)
                        downloaded += len(chunk)
                        if progress and total > 0:
                            percent = int(downloaded * 100 / total)
                            if percent >= last_percent + 10:
                                progress(f"Demucs 模型下载：{min(100, percent)}%")
                                last_percent = percent
            if temporary_path is None or temporary_path.stat().st_size <= 0:
                raise MediaError("Demucs 模型下载结果为空。")
            actual_checksum = _file_checksum_prefix(temporary_path, len(checksum))
            if actual_checksum != checksum:
                raise MediaError(f"Demucs 模型校验失败：期望 {checksum}，实际 {actual_checksum}。")
            temporary_path.replace(target)
            temporary_path = None
            if progress:
                progress("Demucs 模型下载：100%（校验通过）")
        except Exception as exc:
            raise MediaError(f"Demucs 模型下载失败：{exc}。请检查网络后重试。") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


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
        capture_output=True,
    )
    if completed.returncode != 0:
        return None
    try:
        value = json.loads(completed.stdout)["format"]["duration"]
        return float(value)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def probe_media_has_audio(media_path: str | Path) -> bool | None:
    """Return whether a media file has an audio stream, or None when probing fails."""

    executable = find_ffprobe()
    media = Path(media_path)
    if not executable or not media.is_file():
        return None
    completed = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "json",
            str(media),
        ],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if completed.returncode != 0:
        return None
    try:
        streams = json.loads(completed.stdout)["streams"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    return isinstance(streams, list) and bool(streams)


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
        capture_output=True,
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
            [(score, video_start - reference_start) for score, video_start in matches]
        )

    tolerance = max(1, round(0.8 / frame_seconds))
    all_offsets = [offset for group in candidate_groups for score, offset in group if score >= 0.18]
    best_cluster: list[tuple[float, int]] = []
    best_key = (-1, -1.0)
    for center in all_offsets:
        cluster: list[tuple[float, int]] = []
        for group_index, group in enumerate(candidate_groups):
            nearby = [
                (score, offset) for score, offset in group if abs(offset - center) <= tolerance
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


def create_spinning_cover_video(
    image_path: str | Path,
    audio_path: str | Path,
    output_path: str | Path,
    *,
    resolution: tuple[int, int] = (1920, 1080),
    duration: float | None = None,
    rotation_seconds: float = 12.0,
    style: str = "aurora",
    background_theme: str = "adaptive",
    show_waveform: bool = True,
    overwrite: bool = False,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Create a polished audio-reactive background from cover art and a song."""

    image = Path(image_path).resolve()
    audio = Path(audio_path).resolve()
    output = Path(output_path).resolve()
    if not image.is_file():
        raise FileNotFoundError(f"Cover image not found: {image}")
    if not audio.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio}")
    if output.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {output}. Pass --overwrite to replace it.")
    effective_duration = duration or probe_media_duration(audio)
    if effective_duration is None or effective_duration <= 0:
        raise MediaError("无法读取歌曲时长，不能生成旋转封面背景。请确认 FFprobe 可用。")
    width, height = resolution
    style = style.strip().lower()
    if style not in {"aurora", "vinyl", "halo", "spectrum", "cdplayer"}:
        raise ValueError(f"Unsupported cover video style: {style}")
    background_theme = background_theme.strip().lower()
    theme_specs = {
        "adaptive": (None, "FFFFFF", (255, 255, 255), -0.10, 1.25),
        "midnight": ("midnight-stage.png", "6DE7FF", (94, 218, 255), -0.07, 0.94),
        "sunset": ("sunset-glass.png", "FFE0A3", (255, 192, 126), -0.04, 1.00),
        "ocean": ("sea-salt.png", "E9FFFF", (255, 255, 255), -0.08, 0.96),
        "paper": ("paper-garden.png", "163B7A", (232, 112, 92), -0.06, 0.98),
    }
    if background_theme not in theme_specs:
        raise ValueError(f"Unsupported cover background theme: {background_theme}")
    radius_expression = (
        "if(lte((X-W/2)*(X-W/2)+(Y-H/2)*(Y-H/2),"
        "(min(W,H)/2)*(min(W,H)/2)),255,0)"
    )
    duration_text = f"{effective_duration:.3f}"
    rotation = max(1.0, rotation_seconds)
    asset_name, wave_color, ring_color, theme_brightness, theme_saturation = theme_specs[
        background_theme
    ]
    overscan_width = width * 108 // 100
    overscan_height = height * 108 // 100
    drift_crop = (
        f"crop={width}:{height}:"
        "x='(in_w-out_w)/2*(1+sin(t/11))':"
        "y='(in_h-out_h)/2*(1+cos(t/13))'"
    )
    stage_args: list[str] = []
    if asset_name is not None:
        stage_asset = Path(__file__).resolve().parent / "assets" / "visuals" / asset_name
        if not stage_asset.is_file():
            raise MediaError(f"Background theme asset not found: {stage_asset}")
        stage_args = ["-loop", "1", "-i", str(stage_asset)]
        background = (
            "[0:v]null[cover];"
            f"[2:v]scale={overscan_width}:{overscan_height}:"
            "force_original_aspect_ratio=increase,"
            f"crop={overscan_width}:{overscan_height},{drift_crop},format=rgba,"
            f"eq=brightness={theme_brightness:.2f}:saturation={theme_saturation:.2f}[bg];"
        )
    else:
        background = (
            "[0:v]split=2[coverbgsrc][cover];"
            f"[coverbgsrc]scale={overscan_width}:{overscan_height}:"
            "force_original_aspect_ratio=increase,"
            f"crop={overscan_width}:{overscan_height},{drift_crop},gblur=sigma=42,"
            f"eq=brightness={theme_brightness:.2f}:contrast=1.04:"
            f"saturation={theme_saturation:.2f},format=rgba[bg];"
        )

    player_args: list[str] = []
    player_input: str | None = None
    if style == "cdplayer":
        player_asset = (
            Path(__file__).resolve().parent / "assets" / "visuals" / "cd-player-chassis.png"
        )
        if not player_asset.is_file():
            raise MediaError(f"CD player asset not found: {player_asset}")
        player_input = f"[{2 + (1 if asset_name is not None else 0)}:v]"
        player_args = ["-loop", "1", "-i", str(player_asset)]

    if style == "aurora":
        platter_size = max(280, min(width, height) * 49 // 100)
        label_size = platter_size * 80 // 100
        ring_size = platter_size + max(40, height * 5 // 100)
        ring_expression = (
            "if(between(hypot(X-W/2,Y-H/2),min(W,H)*0.472,min(W,H)*0.488),120,0)"
        )
        disc_graph = (
            f"color=c=0x090B17:s={platter_size}x{platter_size}:d={duration_text}:r=30,"
            "format=rgba,geq="
            "r='11+8*sin(hypot(X-W/2,Y-H/2)*0.25)':"
            "g='13+7*sin(hypot(X-W/2,Y-H/2)*0.25)':"
            "b='24+11*sin(hypot(X-W/2,Y-H/2)*0.25)':"
            f"a='{radius_expression}'[platter];"
            f"[cover]scale={label_size}:{label_size}:force_original_aspect_ratio=increase,"
            f"crop={label_size}:{label_size},format=rgba,"
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{radius_expression}',"
            f"rotate=2*PI*t/{rotation:.3f}:ow=iw:oh=ih:c=none[label];"
            "[platter][label]overlay=(W-w)/2:(H-h)/2:shortest=1[discbase];"
            f"color=c=0x00000000:s={ring_size}x{ring_size}:d={duration_text}:r=30,"
            f"format=rgba,geq=r='{ring_color[0]}':g='{ring_color[1]}':"
            f"b='{ring_color[2]}':"
            f"a='{ring_expression}',gblur=sigma=3[ring];"
            "[ring][discbase]overlay=(W-w)/2:(H-h)/2:shortest=1[disc]"
        )
        filter_graph = background + disc_graph
        if show_waveform:
            wave_width = width * 76 // 100
            filter_graph += (
                f";[1:a]showwaves=s={wave_width}x170:mode=p2p:rate=30:"
                f"colors=0x{wave_color},format=rgba,"
                "colorkey=0x000000:0.20:0.08[wavebase];"
                "[wavebase]split[wave][wavesoft];"
                "[wavesoft]gblur=sigma=14,colorchannelmixer=aa=0.70[waveglow];"
                f"[bg][waveglow]overlay=(W-w)/2:{height * 39 // 100}:shortest=1[wavebg];"
                f"[wavebg][wave]overlay=(W-w)/2:{height * 39 // 100}:shortest=1[scene];"
                f"[scene][disc]overlay=(W-w)/2:{height * 13 // 100}:shortest=1[visual]"
            )
        else:
            filter_graph += (
                f";[bg][disc]overlay=(W-w)/2:{height * 13 // 100}:shortest=1[visual]"
            )
    elif style == "vinyl":
        vinyl_size = max(340, min(width, height) * 58 // 100)
        label_size = vinyl_size * 58 // 100
        disc_x = width * 8 // 100
        disc_y = height * 5 // 100
        filter_graph = (
            background
            + f"color=c=0x0A0B10:s={vinyl_size}x{vinyl_size}:d={duration_text}:r=30,"
            "format=rgba,geq="
            "r='12+9*sin(hypot(X-W/2,Y-H/2)*0.27)':"
            "g='13+8*sin(hypot(X-W/2,Y-H/2)*0.27)':"
            "b='17+8*sin(hypot(X-W/2,Y-H/2)*0.27)':"
            f"a='{radius_expression}'[vinyl];"
            f"[cover]scale={label_size}:{label_size}:force_original_aspect_ratio=increase,"
            f"crop={label_size}:{label_size},format=rgba,"
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{radius_expression}',"
            f"rotate=2*PI*t/{rotation:.3f}:ow=iw:oh=ih:c=none[label];"
            "[vinyl][label]overlay=(W-w)/2:(H-h)/2:shortest=1[record]"
        )
        if show_waveform:
            filter_graph += (
                f";[bg]drawbox=x={width * 45 // 100}:y={height * 14 // 100}:"
                f"w={width * 48 // 100}:h={height * 33 // 100}:"
                "color=0x070A18@0.30:t=fill,"
                f"drawbox=x={width * 45 // 100}:y={height * 14 // 100}:"
                f"w={width * 48 // 100}:h={height * 33 // 100}:"
                "color=0x8EDFFF@0.24:t=2[glass];"
                f"[1:a]showwaves=s={width * 42 // 100}x180:mode=cline:rate=30:"
                f"colors=0x{wave_color},format=rgba,"
                "colorkey=0x000000:0.20:0.08[wave];"
                f"[glass][wave]overlay={width * 48 // 100}:{height * 22 // 100}:"
                "shortest=1[wavebg];"
                f"[wavebg][record]overlay={disc_x}:{disc_y}:shortest=1[scene]"
            )
        else:
            filter_graph += f";[bg][record]overlay={disc_x}:{disc_y}:shortest=1[scene]"
        filter_graph += ";[scene]null[visual]"
    elif style == "spectrum":
        cover_size = max(260, min(width, height) * 43 // 100)
        filter_graph = (
            background
            + f"[cover]scale={cover_size}:{cover_size}:force_original_aspect_ratio=increase,"
            f"crop={cover_size}:{cover_size},format=rgba,"
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{radius_expression}',"
            f"rotate=2*PI*t/{rotation:.3f}:ow=iw:oh=ih:c=none[disc];"
            f"[bg]drawbox=x={width * 38 // 100}:y={height * 9 // 100}:"
            f"w={width * 55 // 100}:h={height * 43 // 100}:"
            "color=0x050817@0.34:t=fill,"
            f"drawbox=x={width * 38 // 100}:y={height * 9 // 100}:"
            f"w={width * 55 // 100}:h={height * 43 // 100}:"
            "color=0xA78BFA@0.22:t=2[glass]"
        )
        if show_waveform:
            filter_graph += (
                f";[1:a]showfreqs=s={width * 49 // 100}x{height * 35 // 100}:"
                f"mode=bar:ascale=log:fscale=log:colors=0x{wave_color},"
                f"format=rgba,colorchannelmixer=rr={ring_color[0] / 255:.2f}:"
                f"gg={ring_color[1] / 255:.2f}:bb={ring_color[2] / 255:.2f},"
                "colorkey=0x000000:0.20:0.08[frequency];"
                f"[glass][frequency]overlay={width * 41 // 100}:{height * 13 // 100}:"
                "shortest=1[freqscene];"
                f"[freqscene][disc]overlay={width * 9 // 100}:{height * 8 // 100}:"
                "shortest=1[visual]"
            )
        else:
            filter_graph += (
                f";[glass][disc]overlay={width * 9 // 100}:{height * 8 // 100}:"
                "shortest=1[visual]"
            )
    elif style == "cdplayer":
        assert player_input is not None
        player_size = max(380, min(width, height) * 88 // 100)
        disc_size = player_size * 41 // 100
        platter_size = disc_size + max(14, height * 2 // 100)
        hub_size = max(16, disc_size * 7 // 100)
        disc_y = player_size * 20 // 100
        hub_expression = (
            "if(lte((X-W/2)*(X-W/2)+(Y-H/2)*(Y-H/2),"
            "(min(W,H)/2)*(min(W,H)/2)),235,0)"
        )
        filter_graph = (
            background
            + f"{player_input}scale={player_size}:{player_size},format=rgba[player];"
            f"color=c=0x171A20:s={platter_size}x{platter_size}:"
            f"d={duration_text}:r=30,format=rgba,geq="
            "r='23+6*sin(hypot(X-W/2,Y-H/2)*0.30)':"
            "g='26+6*sin(hypot(X-W/2,Y-H/2)*0.30)':"
            "b='32+6*sin(hypot(X-W/2,Y-H/2)*0.30)':"
            f"a='{radius_expression}'[cdwell];"
            f"[cover]scale={disc_size}:{disc_size}:force_original_aspect_ratio=increase,"
            f"crop={disc_size}:{disc_size},format=rgba,"
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{radius_expression}',"
            f"rotate=2*PI*t/{rotation:.3f}:ow=iw:oh=ih:c=none[cdart];"
            "[cdwell][cdart]overlay=(W-w)/2:(H-h)/2:shortest=1[cdbase];"
            f"color=c=0x00000000:s={hub_size}x{hub_size}:d={duration_text}:r=30,"
            "format=rgba,geq=r='232':g='221':b='196':"
            f"a='{hub_expression}',gblur=sigma=0.6[hub];"
            "[cdbase][hub]overlay=(W-w)/2:(H-h)/2:shortest=1[disc]"
        )
        if show_waveform:
            wave_width = width * 76 // 100
            filter_graph += (
                f";[1:a]showwaves=s={wave_width}x130:mode=p2p:rate=30:"
                f"colors=0x{wave_color},format=rgba,"
                "colorkey=0x000000:0.20:0.08[cdwavebase];"
                "[cdwavebase]split[cdwave][cdwavesoft];"
                "[cdwavesoft]gblur=sigma=12,colorchannelmixer=aa=0.62[cdwaveglow];"
                f"[bg][cdwaveglow]overlay=(W-w)/2:{height * 31 // 100}:"
                "shortest=1[cdglowbg];"
                f"[cdglowbg][cdwave]overlay=(W-w)/2:{height * 31 // 100}:"
                "shortest=1[cdwavebg];"
                "[cdwavebg][player]overlay=(W-w)/2:0:shortest=1[cdstage];"
                f"[cdstage][disc]overlay=(W-w)/2:{disc_y}:shortest=1[visual]"
            )
        else:
            filter_graph += (
                ";[bg][player]overlay=(W-w)/2:0:shortest=1[cdstage];"
                f"[cdstage][disc]overlay=(W-w)/2:{disc_y}:shortest=1[visual]"
            )
    else:
        disc_size = max(300, min(width, height) * 51 // 100)
        ring_size = disc_size + max(34, height * 4 // 100)
        ring_expression = (
            "if(between(hypot(X-W/2,Y-H/2),min(W,H)*0.470,min(W,H)*0.490),120,0)"
        )
        filter_graph = (
            background
            + f"[cover]scale={disc_size}:{disc_size}:force_original_aspect_ratio=increase,"
            f"crop={disc_size}:{disc_size},format=rgba,"
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{radius_expression}',"
            f"rotate=2*PI*t/{rotation:.3f}:ow=iw:oh=ih:c=none[discbase];"
            f"color=c=0x00000000:s={ring_size}x{ring_size}:d={duration_text}:r=30,"
            f"format=rgba,geq=r='{ring_color[0]}':g='{ring_color[1]}':"
            f"b='{ring_color[2]}':"
            f"a='{ring_expression}',gblur=sigma=3[haloring];"
            "[haloring][discbase]overlay=(W-w)/2:(H-h)/2:shortest=1[disc]"
        )
        if show_waveform:
            filter_graph += (
                f";[1:a]showwaves=s={width * 82 // 100}x190:mode=p2p:rate=30:"
                f"colors=0x{wave_color},format=rgba,"
                "colorkey=0x000000:0.20:0.08[wave];"
                f"[bg][wave]overlay=(W-w)/2:{height * 37 // 100}:shortest=1[wavebg];"
                f"[wavebg][disc]overlay=(W-w)/2:{height * 13 // 100}:shortest=1[visual]"
            )
        else:
            filter_graph += (
                f";[bg][disc]overlay=(W-w)/2:{height * 13 // 100}:shortest=1[visual]"
            )
    filter_graph += ";[visual]format=yuv420p[outv]"
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        find_ffmpeg(),
        "-hide_banner",
        "-y" if overwrite else "-n",
        "-loop",
        "1",
        "-i",
        str(image),
        "-i",
        str(audio),
        *stage_args,
        *player_args,
        "-filter_complex",
        filter_graph,
        "-map",
        "[outv]",
        "-t",
        duration_text,
        "-r",
        "30",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-movflags",
        "+faststart",
        str(output),
    ]
    if progress:
        progress("正在生成虚化背景和旋转唱片封面")
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
        output.unlink(missing_ok=True)
        raise MediaError(f"旋转封面视频生成失败。FFmpeg 输出：\n{tail}")
    return output


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
    font_files: Sequence[str | Path] | None = None,
    overwrite: bool = False,
    progress: Callable[[str], None] | None = None,
) -> Path:
    video = Path(video_path).resolve()
    subtitles = Path(ass_path).resolve()
    output = Path(output_path).resolve()
    external_audio = Path(audio_path).resolve() if audio_path else None
    use_external_audio = external_audio is not None and external_audio != video
    output_was_new = not output.exists()

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
        local_fonts: Path | None = None
        for font_file in font_files or ():
            source_font = Path(font_file).resolve()
            if source_font.suffix.lower() not in {".ttf", ".otf", ".ttc"}:
                raise ValueError(f"Unsupported font file: {source_font.name}")
            if not source_font.is_file():
                raise FileNotFoundError(f"Font file not found: {source_font}")
            local_fonts = local_fonts or (temp_dir / "fonts")
            local_fonts.mkdir(exist_ok=True)
            shutil.copy2(source_font, local_fonts / source_font.name)

        command = [ffmpeg, "-hide_banner", "-y" if overwrite else "-n", "-i", str(video)]
        if use_external_audio:
            if audio_offset:
                command.extend(["-itsoffset", f"{audio_offset:.3f}"])
            command.extend(["-i", str(external_audio)])

        ass_filter = "ass=filename=karaoke.ass"
        if local_fonts is not None:
            ass_filter += ":fontsdir=fonts"
        command.extend(["-vf", ass_filter, "-map", "0:v:0"])
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
            disk_full = (
                "no space left on device" in completed.stdout.lower()
                or "error code: -28" in completed.stdout.lower()
            )
            removed_partial = False
            if output_was_new and output.is_file():
                try:
                    output.unlink()
                    removed_partial = True
                except OSError:
                    pass
            if disk_full:
                cleanup = (
                    "本次未完成的视频已自动清理。"
                    if removed_partial
                    else "本次未完成的视频无法自动清理，请手动删除后再试。"
                )
                raise MediaError(
                    "输出磁盘空间不足，FFmpeg 无法继续写入视频。\n"
                    f"输出目录：{output.parent}\n"
                    f"{cleanup} 请更换到空间更充足的输出目录，建议至少预留 2 GB。"
                )
            raise MediaError(f"FFmpeg failed with exit code {completed.returncode}:\n{tail}")
    return output


def separate_vocals(
    audio_path: str | Path,
    output_dir: str | Path,
    *,
    model: str = "htdemucs",
    device: str = "auto",
    progress: Callable[[str], None] | None = None,
) -> Path:
    audio = Path(audio_path).resolve()
    directory = Path(output_dir).resolve()
    if not audio.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio}")
    runtime = inspect_demucs_runtime()
    if not runtime.installed:
        raise MediaError(
            "尚未安装 Demucs 人声分离。Windows 请双击“安装人声分离（Demucs）.bat”，"
            '或运行 `pip install -e ".[separate]"`。'
        )
    if runtime.error:
        raise MediaError(f"Demucs/Torch 安装异常：{runtime.error}。请重新运行 Demucs 安装脚本。")
    if device == "cuda" and runtime.device != "cuda":
        detail = (
            "检测到了 NVIDIA 显卡，但当前 Torch 是 CPU 版。"
            if runtime.nvidia_detected
            else "没有检测到可用的 NVIDIA CUDA 环境。"
        )
        raise MediaError(
            f"已选择 NVIDIA 显卡，但 Demucs 无法使用 CUDA：{detail}"
            "请双击“安装人声分离（Demucs）.bat”，或把运行设备改为“自动选择/只用 CPU”。"
        )

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
    if device in {"cpu", "cuda"}:
        command[3:3] = ["-d", device]
    if progress:
        target = runtime.device_name if runtime.device == "cuda" else "CPU"
        progress(f"正在用 Demucs {model} 分离人声（{target}）")
        progress("首次使用该模型会联网下载；下载完成后会保存在本机，后续无需重复下载")
    process_environment = os.environ.copy()
    cache_root = process_environment.get("KARAOKE_FORGE_CACHE_DIR") or process_environment.get(
        "GRADIO_TEMP_DIR"
    )
    if cache_root:
        process_environment.setdefault("TORCH_HOME", str(Path(cache_root) / "demucs-torch"))
    try:
        import certifi

        certificate_bundle = certifi.where()
        process_environment.setdefault("SSL_CERT_FILE", certificate_bundle)
        process_environment.setdefault("REQUESTS_CA_BUNDLE", certificate_bundle)
    except ImportError:
        pass
    # Demucs 4.1 tries Hugging Face first and then its official model mirror.
    # An offline lookup still reuses a local HF cache, but avoids minutes of
    # retries on networks where huggingface.co is blocked.
    process_environment.setdefault("HF_HUB_OFFLINE", "1")
    _ensure_demucs_legacy_model(model, process_environment, progress)
    process = subprocess.Popen(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        env=process_environment,
    )
    output_lines: list[str] = []
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.strip()
        if not line:
            continue
        output_lines.append(line)
        if progress:
            progress(f"Demucs：{line[-240:]}")
    return_code = process.wait()
    if return_code != 0:
        tail = "\n".join(output_lines[-30:])
        lowered = tail.lower()
        if "out of memory" in lowered or "cuda out of memory" in lowered:
            hint = "\n显存不足；可把运行设备改为 CPU 后重试。"
        elif any(word in lowered for word in ("download", "connection", "timeout")):
            hint = "\n模型下载失败；请检查网络，稍后重试会继续使用已下载的缓存。"
        else:
            hint = ""
        raise MediaError(f"Demucs 运行失败（退出码 {return_code}）：\n{tail}{hint}")

    candidates = sorted(directory.rglob("vocals.wav"), key=lambda path: path.stat().st_mtime)
    if not candidates:
        raise MediaError("Demucs 已结束，但没有找到 vocals.wav 人声文件。")
    if progress:
        progress("Demucs 人声分离完成，接下来使用人声轨进行歌词识别")
    return candidates[-1]
