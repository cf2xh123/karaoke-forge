from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
TORCH_VERSION = "2.13.0"
CUDA_INDEX = "https://download.pytorch.org/whl/cu130"


def _run(command: list[str]) -> bool:
    print("\n> " + " ".join(command))
    return subprocess.run(command, cwd=PROJECT_ROOT, check=False).returncode == 0


def _torch_status(python: Path) -> tuple[bool, str]:
    code = (
        "import torch; "
        "ok=torch.cuda.is_available(); "
        "print(torch.__version__); "
        "print(torch.cuda.get_device_name(0) if ok else 'CPU'); "
        "raise SystemExit(0 if ok else 2)"
    )
    result = subprocess.run(
        [str(python), "-c", code],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    return result.returncode == 0, result.stdout.strip().replace("\n", " · ")


def main() -> int:
    parser = argparse.ArgumentParser(description="安装并检查 Karaoke Forge 的 Demucs 人声分离")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    args = parser.parse_args()

    if not VENV_PYTHON.is_file():
        print("[错误] 尚未找到项目运行环境。请先双击“首次安装.bat”。")
        return 1

    print("正在安装 Demucs 4.x。包本身约几 MB，CPU 版 Torch 约 120 MB。")
    if not _run([str(VENV_PYTHON), "-m", "pip", "install", "-e", ".[separate]"]):
        print("[错误] Demucs 基础安装失败，请检查网络后重试。")
        return 1

    wants_cuda = args.device == "cuda" or (
        args.device == "auto" and shutil.which("nvidia-smi") is not None
    )
    if wants_cuda:
        print(
            "\n检测到 NVIDIA 显卡，正在切换到官方 CUDA 13.0 版 Torch。"
            "下载约 1.9 GB；失败时已安装的 CPU 版 Demucs 仍可使用。"
        )
        cuda_ok = _run(
            [
                str(VENV_PYTHON),
                "-m",
                "pip",
                "install",
                "--force-reinstall",
                "--no-deps",
                f"torch=={TORCH_VERSION}",
                "--index-url",
                CUDA_INDEX,
            ]
        )
        gpu_ready, detail = _torch_status(VENV_PYTHON)
        if cuda_ok and gpu_ready:
            print(f"\n[完成] Demucs 已启用 NVIDIA 显卡：{detail}")
            return 0
        print("\n[提醒] 显卡版下载或安装未完成，将继续使用 CPU。可稍后重新运行本脚本。")
    else:
        _gpu_ready, detail = _torch_status(VENV_PYTHON)
        print(f"\n[完成] Demucs 已安装：{detail or 'CPU'}")

    check = subprocess.run(
        [str(VENV_PYTHON), "-m", "demucs", "--help"],
        cwd=PROJECT_ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0 if check.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
