from __future__ import annotations

import importlib.metadata
import importlib.util
import shutil
from dataclasses import dataclass
from functools import lru_cache


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
