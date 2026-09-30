"""Native desktop entry point. Qt is optional and imported only on launch."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_system_icu = None
if sys.platform == "win32":
    # Qt 6.11 uses Windows' ICU API. Conda's PATH can otherwise supply an
    # incompatible icuuc.dll with version-suffixed exports. Load the system
    # library by its absolute path before Qt; never edit PATH or replace DLLs.
    import ctypes

    _icu_path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icuuc.dll"
    if _icu_path.is_file():
        _system_icu = ctypes.WinDLL(str(_icu_path))


def launch_desktop(project: str | None = None) -> int:
    try:
        from .app import run
    except ImportError as exc:
        if exc.name and exc.name.startswith("PySide6"):
            raise RuntimeError(
                '桌面依赖尚未安装。请运行 pip install -e ".[desktop]"，或双击“启动桌面版.bat”。'
            ) from exc
        raise
    return run(project)


def main() -> int:
    from ..cli import main as cli_main

    return cli_main(["desktop", *sys.argv[1:]])
