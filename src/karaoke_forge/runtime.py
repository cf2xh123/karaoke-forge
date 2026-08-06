from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import shutil
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _candidate_project_roots() -> tuple[Path, ...]:
    """Return trusted portable-install roots without consulting the working directory."""

    candidates: list[Path] = []
    configured_root = os.environ.get("KARAOKE_FORGE_ROOT", "").strip()
    if configured_root:
        candidates.append(Path(configured_root).expanduser())

    # A global installation can be started from an arbitrary, potentially
    # untrusted directory.  Never discover executables from cwd or a generic
    # drive ancestor.  Implicit roots must be an actual Karaoke Forge source
    # checkout; the Windows launchers use the explicit directory variable.
    executable = Path(sys.executable).resolve()
    implicit_candidates = [*executable.parents[:4], Path(__file__).resolve().parents[2]]
    for candidate in implicit_candidates:
        if (
            (candidate / "pyproject.toml").is_file()
            and (candidate / "src" / "karaoke_forge" / "runtime.py").is_file()
        ):
            candidates.append(candidate)

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return tuple(unique)


def bundled_ffmpeg_directory() -> Path | None:
    """Locate the project-private FFmpeg directory used by Windows launchers."""

    configured = os.environ.get("KARAOKE_FORGE_FFMPEG_DIR", "").strip()
    if configured:
        directory = Path(configured).expanduser()
        if directory.is_dir():
            return directory.resolve()

    for root in _candidate_project_roots():
        directory = root / ".runtime" / "ffmpeg" / "bin"
        if directory.is_dir():
            return directory
    return None


def find_runtime_executable(name: str) -> str | None:
    """Resolve a bundled media tool first, then fall back to the current PATH."""

    if not name or Path(name).name != name:
        raise ValueError("Runtime executable names must not contain a path.")
    directory = bundled_ffmpeg_directory()
    if directory is not None:
        filename = f"{name}.exe" if os.name == "nt" else name
        bundled = directory / filename
        if bundled.is_file():
            return str(bundled.resolve())
    return shutil.which(name)


@dataclass(frozen=True)
class DemucsRuntime:
    installed: bool
    demucs_version: str | None = None
    torch_version: str | None = None
    device: str = "unavailable"
    device_name: str | None = None
    nvidia_detected: bool = False
    error: str | None = None

    @property
    def ready(self) -> bool:
        return self.installed and not self.error and self.device in {"cpu", "cuda"}

    @property
    def short_label_zh(self) -> str:
        if not self.installed:
            return "未安装"
        if self.error:
            return "安装异常"
        if self.device == "cuda":
            return f"已就绪：{self.device_name or 'NVIDIA 显卡'}"
        if self.nvidia_detected:
            return "可用：CPU（可切换 NVIDIA）"
        return "已就绪：CPU"

    @property
    def detail_zh(self) -> str:
        if not self.installed:
            return "未安装；Windows 可双击“安装人声分离（Demucs）.bat”"
        versions = [f"Demucs {self.demucs_version or '未知版本'}"]
        if self.torch_version:
            versions.append(f"Torch {self.torch_version}")
        if self.error:
            versions.append(f"加载异常：{self.error}")
        elif self.device == "cuda":
            versions.append(f"显卡：{self.device_name or 'NVIDIA CUDA'}")
        elif self.nvidia_detected:
            versions.append("当前使用 CPU；检测到 NVIDIA，可运行独立安装脚本启用显卡")
        else:
            versions.append("使用 CPU")
        return " · ".join(versions)


def _version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


@lru_cache(maxsize=1)
def inspect_demucs_runtime() -> DemucsRuntime:
    """Inspect the optional separator without making it a mandatory dependency."""

    nvidia_detected = shutil.which("nvidia-smi") is not None
    if importlib.util.find_spec("demucs") is None:
        return DemucsRuntime(installed=False, nvidia_detected=nvidia_detected)
    try:
        import torch

        cuda_available = bool(torch.cuda.is_available())
        device_name = torch.cuda.get_device_name(0) if cuda_available else None
        return DemucsRuntime(
            installed=True,
            demucs_version=_version("demucs"),
            torch_version=str(torch.__version__),
            device="cuda" if cuda_available else "cpu",
            device_name=device_name,
            nvidia_detected=nvidia_detected,
        )
    except Exception as exc:  # noqa: BLE001 - native Torch failures must stay diagnosable
        return DemucsRuntime(
            installed=True,
            demucs_version=_version("demucs"),
            nvidia_detected=nvidia_detected,
            error=str(exc),
        )
