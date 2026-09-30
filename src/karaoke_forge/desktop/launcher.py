"""PyInstaller entry point; importing this module never changes source/CLI behavior."""

from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import traceback
import wave
from pathlib import Path
from typing import Any

SELF_TEST_IMPORTS = (
    "karaoke_forge.desktop",  # initialize the same Windows ICU selection as normal startup
    "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "faster_whisper", "faster_whisper.vad", "ctranslate2", "onnxruntime",
    "av", "numpy", "tokenizers", "huggingface_hub", "certifi",
    "yt_dlp", "yt_dlp.extractor.neteasemusic", "websocket", "pykakasi", "alkana",
    "torch", "torchaudio", "soundfile", "demucs.separate", "demucs.htdemucs",
    "sphn", "julius", "lameenc", "safetensors",
    "karaoke_forge.model_worker", "karaoke_forge.desktop.app",
)
_stream_handles: list[io.IOBase] = []


def configure_frozen_environment() -> dict[str, Path]:
    """Keep package assets immutable and write application data under the user profile."""
    if not getattr(sys, "frozen", False):
        return {}
    root = Path(sys.executable).resolve().parent
    contents = Path(getattr(sys, "_MEIPASS", root / "_internal")).resolve()
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    user_root = local / "KaraokeForge"
    os.environ["KARAOKE_FORGE_ROOT"] = str(root)
    os.environ["KARAOKE_FORGE_FFMPEG_DIR"] = str(contents / "ffmpeg" / "bin")
    # Demucs invokes ffmpeg/ffprobe by name. This affects only this process and
    # its children; it never changes the user's or system's PATH.
    private_media = str(contents / "ffmpeg" / "bin")
    os.environ["PATH"] = private_media + os.pathsep + os.environ.get("PATH", "")
    defaults = {
        "KARAOKE_FORGE_SETTINGS_DIR": user_root,
        "KARAOKE_FORGE_CACHE_DIR": user_root / "cache",
        "KARAOKE_FORGE_OUTPUT_DIR": user_root / "outputs",
    }
    for name, path in defaults.items():
        os.environ.setdefault(name, str(path))
        Path(os.environ[name]).mkdir(parents=True, exist_ok=True)
    logs = user_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return {"root": root, "contents": contents, "data": user_root, "logs": logs}


def _inherited_windows_stream(standard_handle: int) -> io.TextIOWrapper | None:
    """Reopen a redirected OS pipe which a windowed Python bootloader leaves as None."""
    if sys.platform != "win32":
        return None
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    kernel.DuplicateHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    original = kernel.GetStdHandle(standard_handle & 0xFFFFFFFF)
    if not original or original == ctypes.c_void_p(-1).value:
        return None
    process = kernel.GetCurrentProcess()
    duplicate = wintypes.HANDLE()
    if not kernel.DuplicateHandle(process, original, process, ctypes.byref(duplicate), 0, False, 2):
        return None
    try:
        descriptor = msvcrt.open_osfhandle(duplicate.value, os.O_WRONLY | os.O_BINARY)
    except OSError:
        kernel.CloseHandle(duplicate)
        return None
    return io.TextIOWrapper(io.FileIO(descriptor, "w", closefd=True), encoding="utf-8",
                            errors="backslashreplace", write_through=True)


def ensure_standard_streams(log_directory: Path | None = None) -> None:
    for name, handle in (("stdout", -11), ("stderr", -12)):
        existing = getattr(sys, name)
        if existing is not None:
            # Redirected streams may already exist with the Windows ANSI code
            # page. PYTHONUTF8 is ignored by PyInstaller's embedded interpreter;
            # normalize the actual streams before any worker emits a path.
            if getattr(sys, "frozen", False) and hasattr(existing, "reconfigure"):
                existing.reconfigure(encoding="utf-8", errors="backslashreplace", write_through=True)
            continue
        stream = _inherited_windows_stream(handle)
        if stream is None:
            destination = log_directory / "desktop.log" if log_directory else Path(os.devnull)
            stream = destination.open("a", encoding="utf-8", buffering=1)
        _stream_handles.append(stream)
        setattr(sys, name, stream)
    if sys.stdin is None:
        # This process-owned stream must remain open after this function returns.
        stream = Path(os.devnull).open(encoding="utf-8")  # noqa: SIM115
        _stream_handles.append(stream)
        sys.stdin = stream


def _hide_child_consoles() -> None:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    original = subprocess.Popen
    if getattr(original, "_karaoke_windowless", False):
        return

    class WindowlessPopen(original):
        _karaoke_windowless = True

        def __init__(self, *args, **kwargs):
            # Leave an explicitly requested child-process policy intact.
            if len(args) < 14:
                kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
            super().__init__(*args, **kwargs)

    subprocess.Popen = WindowlessPopen


def dispatch_worker(arguments: list[str]) -> int | None:
    """Preserve the known `sys.executable -m ...` download/separation protocols."""
    if not arguments or arguments[0] != "-m":
        return None
    if len(arguments) < 2:
        raise ValueError("Missing internal worker module")
    module, rest = arguments[1], arguments[2:]
    if module == "karaoke_forge.model_worker":
        from karaoke_forge.model_worker import main

        return main(rest)
    if module == "karaoke_forge" and rest and rest[0] == "model-download":
        from karaoke_forge.cli import main

        return main(rest)
    if module == "demucs":
        from demucs.separate import main

        return int(main(rest) or 0)
    raise ValueError(f"Unsupported internal worker module: {module}")


def _worker_command(*arguments: str) -> list[str]:
    entry = [sys.executable]
    if not getattr(sys, "frozen", False):
        entry.extend(("-m", "karaoke_forge.desktop.launcher"))
    return [*entry, *arguments]


def _check_unicode_worker_protocol(root: Path) -> dict[str, str]:
    """Exercise a successful worker's real path protocol without downloading weights."""
    from karaoke_forge.network import (
        ModelDownloadSettings,
        model_cache_directory,
        save_model_download_settings,
    )
    from karaoke_forge.transcribe import PINNED_MODEL_REVISIONS

    data = root / "中文缓存_🎵"
    settings = ModelDownloadSettings(mode="offline")
    save_model_download_settings(settings, settings_dir=data)
    cache = model_cache_directory(settings, settings_dir=data) / "hub"
    snapshot = (cache / "models--Systran--faster-whisper-small" / "snapshots"
                / PINNED_MODEL_REVISIONS["small"])
    # The Hub's offline path lookup accepts an existing snapshot directory
    # without a cached tree. This is a protocol fixture, never a usable model.
    snapshot.mkdir(parents=True)
    environment = os.environ.copy()
    environment.update({
        "KARAOKE_FORGE_SETTINGS_DIR": str(data),
        "KARAOKE_FORGE_CACHE_DIR": str(root / "cache"),
        "KARAOKE_FORGE_OUTPUT_DIR": str(root / "outputs"),
        "LOCALAPPDATA": str(root / "profile"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "HF_HUB_OFFLINE": "1",
    })
    result = subprocess.run(_worker_command("-m", "karaoke_forge.model_worker", "small"),
                            capture_output=True, text=True, encoding="utf-8", errors="strict",
                            env=environment, timeout=45, check=True)
    returned = Path(result.stdout.strip().splitlines()[-1]).resolve()
    if returned != snapshot.resolve() or not returned.is_dir():
        raise RuntimeError("The isolated worker corrupted a non-ASCII cache path")
    return {"round_trip": "中文缓存_🎵", "worker": "successful offline snapshot path"}


def _check_demucs_separation(root: Path) -> dict[str, object]:
    """Run Demucs' tiny random test network through its real child-process CLI."""
    import math
    from array import array

    directory = root / "伴奏测试_🎵"
    directory.mkdir()
    source = directory / "歌曲.wav"
    frames = array("h", (int(4000 * math.sin(2 * math.pi * 220 * index / 44100))
                         for index in range(44100)))
    with wave.open(str(source), "wb") as output:
        output.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
        output.writeframes(frames.tobytes())
    environment = os.environ.copy()
    environment.update({"HF_HUB_OFFLINE": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                        "TORCH_HOME": str(root / "torch"), "OMP_NUM_THREADS": "2"})
    from karaoke_forge.runtime import find_runtime_executable

    ffmpeg = find_runtime_executable("ffmpeg")
    if ffmpeg:
        environment["PATH"] = str(Path(ffmpeg).parent) + os.pathsep + environment.get("PATH", "")
    result = subprocess.run(
        _worker_command("-m", "demucs", "-n", "demucs_unittest", "-d", "cpu", "--shifts", "0",
                        "--segment", "1", "--overlap", "0", "--two-stems", "vocals",
                        "-o", str(directory / "out"), str(source)),
        capture_output=True, text=True, encoding="utf-8", errors="strict", env=environment,
        timeout=90, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Demucs child failed: {(result.stderr or result.stdout)[-2000:]}")
    stems = directory / "out" / "demucs_unittest" / source.stem
    for name in ("vocals.wav", "no_vocals.wav"):
        with wave.open(str(stems / name), "rb") as audio:
            if audio.getnframes() != 44100 or audio.getnchannels() != 2:
                raise RuntimeError(f"Demucs produced an invalid test stem: {name}")
    return {"device": "cpu", "samples": 44100, "stems": 2, "pretrained_weights": False}


def run_self_test(report_path: Path) -> bool:
    """Exercise bundled resources locally without a model download or user login."""
    from karaoke_forge import __version__

    report: dict[str, Any] = {
        "version": __version__, "frozen": bool(getattr(sys, "frozen", False)), "checks": {},
    }
    checks = report["checks"]

    def check(name, operation):
        try:
            detail = operation()
            checks[name] = {"ok": True, "detail": detail}
        except Exception as exc:  # noqa: BLE001 - report every independent diagnostic
            checks[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    for module in SELF_TEST_IMPORTS:
        check(f"import:{module}", lambda module=module: importlib.import_module(module).__name__)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QLibraryInfo
    from PySide6.QtGui import QImage
    from PySide6.QtMultimedia import QMediaFormat
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    def resources():
        from karaoke_forge.desktop.timeline import LyricPreviewWidget
        from karaoke_forge.models import LyricLine, LyricsDocument

        package = Path(importlib.import_module("karaoke_forge").__file__).parent
        required = [package / "desktop/theme.qss", package / "assets/workspace.css",
                    package / "assets/editor.js", package / "assets/visuals/turntable-chassis.png"]
        if not all(path.is_file() and path.stat().st_size for path in required):
            raise FileNotFoundError("A bundled style or background asset is missing")
        if sys.platform == "win32":
            plugin = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
            if not (plugin / "platforms/qwindows.dll").is_file():
                raise FileNotFoundError("Qt Windows platform plugin is missing")
        preview = LyricPreviewWidget()
        preview.set_document(LyricsDocument([LyricLine("Karaoke Forge", 0, 2)]))
        preview.resize(480, 270)
        preview.show()
        app.processEvents()
        image = preview.grab().toImage()
        preview.close()
        if image.isNull() or QImage(str(required[-1])).isNull():
            raise RuntimeError("Qt paint or PNG image plugin failed")
        return {"paint_size": [image.width(), image.height()], "assets": len(required)}

    check("qt:resources-and-paint", resources)
    def multimedia():
        formats = QMediaFormat().supportedFileFormats(QMediaFormat.ConversionMode.Decode)
        if not formats:
            raise RuntimeError("Qt multimedia decoding backend is unavailable")
        return [item.name for item in formats]

    check("qt:multimedia", multimedia)

    def dictionaries():
        import alkana
        import certifi
        import pykakasi

        result = pykakasi.kakasi().convert("日本語")
        if not result or not result[0].get("hira") or not alkana.get_kana("music"):
            raise RuntimeError("Japanese/English pronunciation dictionaries are missing")
        if not Path(certifi.where()).is_file():
            raise FileNotFoundError("TLS certificate bundle is missing")
        return {"japanese": result[0]["hira"], "english": alkana.get_kana("music")}

    check("dictionaries-and-certificates", dictionaries)

    with tempfile.TemporaryDirectory(prefix="karaoke-forge-self-test-") as temporary:
        root = Path(temporary)
        audio = root / "audio.wav"
        with wave.open(str(audio), "wb") as output:
            output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            output.writeframes(b"\0\0" * 3200)

        def media_tools():
            from karaoke_forge.runtime import find_runtime_executable

            paths = {}
            for tool in ("ffmpeg", "ffprobe"):
                path = find_runtime_executable(tool)
                if not path:
                    raise FileNotFoundError(tool)
                if getattr(sys, "frozen", False):
                    Path(path).resolve().relative_to(
                        Path(os.environ["KARAOKE_FORGE_FFMPEG_DIR"]).resolve())
                result = subprocess.run([path, "-version"], capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=20, check=True)
                paths[tool] = {"path": path, "version": result.stdout.splitlines()[0]}
            ffmpeg = paths["ffmpeg"]["path"]
            filters = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True,
                                     text=True, encoding="utf-8", errors="replace",
                                     timeout=20, check=True)
            if " ass " not in filters.stdout or " subtitles " not in filters.stdout:
                raise RuntimeError("Bundled FFmpeg lacks libass subtitle filters")
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "color=black:s=64x64:d=0.2", "-r", "10", "-an",
                            "-c:v", "libx264", "-y", str(root / "test.mp4")],
                           capture_output=True, timeout=30, check=True)
            subprocess.run([paths["ffprobe"]["path"], "-v", "error", str(root / "test.mp4")],
                           capture_output=True, timeout=20, check=True)
            return paths

        def audio_backend():
            from faster_whisper.audio import decode_audio
            from faster_whisper.vad import get_vad_model

            samples = decode_audio(str(audio))
            if len(samples) != 3200:
                raise RuntimeError("PyAV audio decoder returned the wrong duration")
            model = get_vad_model()
            return {"samples": len(samples), "vad": type(model).__name__}

        check("private-media-tools", media_tools)
        check("whisper-audio-and-vad", audio_backend)
        def cpu_audio():
            import soundfile
            import torch
            import torchaudio

            samples, rate = soundfile.read(str(audio))
            tensor = torch.from_numpy(samples).to(dtype=torch.float32)
            resampled = torchaudio.functional.resample(tensor, rate, 8000)
            if resampled.numel() != 1600 or not torch.isfinite(resampled).all():
                raise RuntimeError("CPU Torch/Torchaudio processing failed")
            return {"torch": torch.__version__, "torchaudio": torchaudio.__version__,
                    "device": str(resampled.device), "samples": resampled.numel()}

        check("cpu-torch-audio", cpu_audio)
        check("isolated-worker-unicode-protocol", lambda: _check_unicode_worker_protocol(root))
        check("demucs-cpu-separation", lambda: _check_demucs_separation(root))
    report["ok"] = all(item["ok"] for item in checks.values())
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "report": str(report_path)}, ensure_ascii=False))
    return report["ok"]


def main(argv: list[str] | None = None) -> int:
    paths = configure_frozen_environment()
    ensure_standard_streams(paths.get("logs"))
    _hide_child_consoles()
    import multiprocessing

    multiprocessing.freeze_support()
    arguments = list(sys.argv[1:] if argv is None else argv)
    worker_result = dispatch_worker(arguments)
    if worker_result is not None:
        return worker_result
    parser = argparse.ArgumentParser(description="Karaoke Forge portable desktop")
    parser.add_argument("project", nargs="?")
    parser.add_argument("--self-test", nargs="?", const="", metavar="REPORT.json")
    args = parser.parse_args(arguments)
    if args.self_test is not None:
        report = (Path(args.self_test) if args.self_test
                  else paths.get("data", Path.cwd()) / "self-test.json")
        try:
            return 0 if run_self_test(report) else 1
        except Exception as exc:  # noqa: BLE001 - even missing Qt must leave a machine-readable report
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps({
                "ok": False, "bootstrap_error": f"{type(exc).__name__}: {exc}",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            traceback.print_exc()
            return 1
    from karaoke_forge.desktop import launch_desktop

    return launch_desktop(args.project)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001 - preserve a diagnostic when no console is available
        traceback.print_exc()
        if "--self-test" not in sys.argv and "-m" not in sys.argv and sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0, "Karaoke Forge 启动失败。请查看用户目录 KaraokeForge/logs/desktop.log。",
                "Karaoke Forge", 0x10,
            )
        raise SystemExit(1)
