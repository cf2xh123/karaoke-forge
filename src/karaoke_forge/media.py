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
    style: str = "vinyl",
    show_waveform: bool = True,
    overwrite: bool = False,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Create a silent blurred-background video with a rotating circular cover."""

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
    if style not in {"vinyl", "halo", "spectrum"}:
        raise ValueError(f"Unsupported cover video style: {style}")
    disc_size = max(240, min(width, height) * 2 // 3)
    radius_expression = (
        "if(lte((X-W/2)*(X-W/2)+(Y-H/2)*(Y-H/2),"
        "(min(W,H)/2)*(min(W,H)/2)),255,0)"
    )
    duration_text = f"{effective_duration:.3f}"
    background = (
        "[0:v]split=2[background][cover];"
        f"[background]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma=42,eq=brightness=-0.28:saturation=0.9[bg];"
    )
    if style == "vinyl":
        vinyl_size = max(360, min(width, height) * 3 // 4)
        label_size = vinyl_size * 58 // 100
        arm_width = max(12, height // 50)
        arm_height = max(180, height * 42 // 100)
        arm_x = width // 2 + vinyl_size * 25 // 100
        arm_y = height // 2 - vinyl_size * 55 // 100
        pivot_size = max(46, height * 8 // 100)
        filter_graph = (
            background
            + f"color=c=0x111214:s={vinyl_size}x{vinyl_size}:d={duration_text}:r=30,"
            "format=rgba,"
            "geq=r='18+7*sin(hypot(X-W/2,Y-H/2)*0.22)':"
            "g='19+7*sin(hypot(X-W/2,Y-H/2)*0.22)':"
            "b='21+7*sin(hypot(X-W/2,Y-H/2)*0.22)':"
            f"a='{radius_expression}'[vinyl];"
            f"[cover]scale={label_size}:{label_size}:force_original_aspect_ratio=increase,"
            f"crop={label_size}:{label_size},format=rgba,"
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{radius_expression}',"
            f"rotate=2*PI*t/{max(1.0, rotation_seconds):.3f}:"
            "ow=rotw(iw):oh=roth(ih):c=none[label];"
            "[vinyl][label]overlay=(W-w)/2:(H-h)/2:shortest=1[record];"
            "[bg][record]overlay=(W-w)/2:(H-h)/2+30:shortest=1[recordscene];"
            f"color=c=0x303238:s={pivot_size}x{pivot_size}:d={duration_text}:r=30,"
            "format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
            f"a='{radius_expression}'[pivot];"
            f"[recordscene][pivot]overlay="
            f"{arm_x + arm_width + arm_height * 52 // 100 - pivot_size // 2}:"
            f"{max(8, arm_y - pivot_size // 3)}:"
            "shortest=1[turntable];"
            f"color=c=white@0.88:s={arm_width}x{arm_height}:d={duration_text}:r=30,"
            "format=rgba,"
            "rotate=0.30:ow=rotw(iw):oh=roth(ih):c=none[arm];"
            f"[turntable][arm]overlay={arm_x}:{arm_y}:shortest=1[scene]"
        )
        if show_waveform:
            filter_graph += (
                f";[1:a]showwaves=s={width * 2 // 3}x110:mode=cline:rate=30:"
                "colors=0xFFD166,format=rgba,colorchannelmixer=aa=0.78[wave];"
                "[scene][wave]overlay=(W-w)/2:H*0.58:shortest=1[visual]"
            )
        else:
            filter_graph += ";[scene]null[visual]"
    elif style == "spectrum":
        cover_size = max(300, min(width, height) * 56 // 100)
        filter_graph = (
            background
            + f"[cover]scale={cover_size}:{cover_size}:force_original_aspect_ratio=increase,"
            f"crop={cover_size}:{cover_size},format=rgba,"
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{radius_expression}',"
            f"rotate=2*PI*t/{max(1.0, rotation_seconds):.3f}:"
            "ow=rotw(iw):oh=roth(ih):c=none[disc];"
            "[bg][disc]overlay=W*0.08:(H-h)/2:shortest=1[scene]"
        )
        if show_waveform:
            filter_graph += (
                f";[1:a]showfreqs=s={width * 42 // 100}x{height * 48 // 100}:"
                "mode=bar:ascale=log:fscale=log:colors=0x69E6D2,format=rgba,"
                "colorkey=0x000000:0.08:0.0[frequency];"
                "[scene][frequency]overlay=W-w-100:(H-h)/2:shortest=1[visual]"
            )
        else:
            filter_graph += ";[scene]null[visual]"
    else:
        filter_graph = (
            background
            + f"[cover]scale={disc_size}:{disc_size}:force_original_aspect_ratio=increase,"
            f"crop={disc_size}:{disc_size},format=rgba,"
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{radius_expression}',"
            f"rotate=2*PI*t/{max(1.0, rotation_seconds):.3f}:"
            "ow=rotw(iw):oh=roth(ih):c=none[disc];"
            "[bg][disc]overlay=(W-w)/2:(H-h)/2:shortest=1[scene]"
        )
        if show_waveform:
            filter_graph += (
                f";[1:a]showwaves=s={width * 3 // 4}x150:mode=p2p:rate=30:"
                "colors=0x8BE9FD,format=rgba,colorchannelmixer=aa=0.72[wave];"
                "[scene][wave]overlay=(W-w)/2:H*0.58:shortest=1[visual]"
            )
        else:
            filter_graph += ";[scene]null[visual]"
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
