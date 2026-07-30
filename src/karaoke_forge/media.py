from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path


class MediaError(RuntimeError):
    pass


def find_ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise MediaError(
            "FFmpeg was not found on PATH. Install FFmpeg and make sure `ffmpeg -version` works."
        )
    return executable


def find_ffprobe() -> str | None:
    return shutil.which("ffprobe")


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
        if external_audio:
            if audio_offset:
                command.extend(["-itsoffset", f"{audio_offset:.3f}"])
            command.extend(["-i", str(external_audio)])

        command.extend(["-vf", "ass=filename=karaoke.ass", "-map", "0:v:0"])
        if external_audio:
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
