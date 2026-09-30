"""Build the Windows onedir distribution using the current, clean Python environment.

Nothing is installed by this script. Use --dry-run to inspect the PyInstaller
command without running a build or downloading supplemental license texts.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import importlib.util
import json
import platform
import re
import shutil
import ssl
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

APP_NAME = "KaraokeForge"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLLECT_ALL = (
    "faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers",
    "huggingface_hub", "yt_dlp", "pykakasi", "alkana", "websocket", "certifi",
    "demucs", "julius", "sphn", "lameenc", "einops", "safetensors",
)
HIDDEN_IMPORTS = (
    "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "karaoke_forge.model_worker", "karaoke_forge.cli", "numpy",
    "torch", "torchaudio", "soundfile", "demucs.separate", "demucs.htdemucs",
)
REQUIRED_DISTRIBUTIONS = (
    "PyInstaller", "pyinstaller-hooks-contrib", "PySide6", "faster-whisper",
    "ctranslate2", "onnxruntime", "av", "tokenizers", "huggingface-hub",
    "yt-dlp", "pykakasi", "alkana", "websocket-client", "certifi",
    "torch", "torchaudio", "demucs", "sphn", "soundfile",
)
PINNED_CPU_RUNTIME = {"torch": "2.8.0+cpu", "torchaudio": "2.8.0+cpu", "demucs": "4.1.0"}
EXCLUDED_MODULES = (
    "gradio", "gradio_client", "torchvision",
    "PyQt5", "PyQt6", "PySide2", "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "tkinter",
    "pytest", "IPython", "jupyter", "notebook", "matplotlib", "tensorboard",
)
LICENSE_SOURCES = {
    "LGPL-3.0-only.txt": (
        "https://raw.githubusercontent.com/qt/qtbase/v6.8.3/LICENSES/LGPL-3.0-only.txt"
    ),
    "GPL-3.0-only.txt": (
        "https://raw.githubusercontent.com/qt/qtbase/v6.8.3/LICENSES/GPL-3.0-only.txt"
    ),
    "GPL-2.0-only.txt": (
        "https://raw.githubusercontent.com/spdx/license-list-data/v3.25.0/text/GPL-2.0-only.txt"
    ),
}


def project_version(root: Path = PROJECT_ROOT) -> str:
    module = ast.parse((root / "src/karaoke_forge/__init__.py").read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            version = ast.literal_eval(node.value)
            if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version):
                return version
    raise ValueError("Cannot determine the semantic version from karaoke_forge/__init__.py")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_environment(ffmpeg_root: Path) -> None:
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("Build this Windows x64 package on Windows x64")
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Use a clean CPython 3.12 x64 build environment")
    if (Path(sys.base_prefix) / "conda-meta").exists():
        raise RuntimeError("Use the project's private CPython runtime, not an Anaconda environment")
    missing = []
    for distribution in REQUIRED_DISTRIBUTIONS:
        try:
            importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            missing.append(distribution)
    if missing:
        raise RuntimeError("Missing build dependencies: " + ", ".join(missing))
    for name, expected in PINNED_CPU_RUNTIME.items():
        actual = importlib.metadata.version(name)
        if actual != expected:
            raise RuntimeError(f"Portable CPU build requires {name}=={expected}, found {actual}")
    for name in ("bin/ffmpeg.exe", "bin/ffprobe.exe", "LICENSE", "README.txt"):
        if not (ffmpeg_root / name).is_file():
            raise FileNotFoundError(f"Private FFmpeg distribution is incomplete: {ffmpeg_root / name}")


def pyinstaller_command(root: Path, dist: Path, work: Path, ffmpeg_root: Path) -> list[str]:
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
        "--noupx", "--name", APP_NAME, "--contents-directory", "_internal",
        "--distpath", str(dist), "--workpath", str(work / "pyinstaller"),
        "--specpath", str(work), "--paths", str(root / "src"),
        "--collect-data", "karaoke_forge", "--copy-metadata", "karaoke-forge",
        "--recursive-copy-metadata", "faster-whisper",
        "--recursive-copy-metadata", "PySide6",
        "--recursive-copy-metadata", "demucs",
        "--recursive-copy-metadata", "torchaudio",
        "--copy-metadata", "soundfile",
        "--add-data", f"{root / 'src/karaoke_forge/desktop/theme.qss'}:karaoke_forge/desktop",
        "--add-data", f"{root / 'src/karaoke_forge/assets'}:karaoke_forge/assets",
    ]
    for module in COLLECT_ALL:
        command.extend(("--collect-all", module))
    for module in HIDDEN_IMPORTS:
        command.extend(("--hidden-import", module))
    for module in EXCLUDED_MODULES:
        command.extend(("--exclude-module", module))
    for executable in ("ffmpeg.exe", "ffprobe.exe"):
        command.extend(("--add-binary", f"{ffmpeg_root / 'bin' / executable}:ffmpeg/bin"))
    for filename in ("LICENSE", "README.txt"):
        command.extend(("--add-data", f"{ffmpeg_root / filename}:ffmpeg"))
    command.append(str(root / "src/karaoke_forge/desktop/launcher.py"))
    return command


def _license_file(relative: Path) -> bool:
    lowered = relative.name.lower()
    return relative.suffix.lower() not in {".py", ".pyc", ".pyd", ".dll"} and (
        lowered.startswith(("license", "licence", "copying", "copyright", "notice"))
        or any(part.lower() in {"licenses", "licences"} for part in relative.parts)
    )


def _download_license(url: str, destination: Path) -> None:
    """Cache human-readable upstream licenses; never disable TLS verification."""
    if destination.is_file() and destination.stat().st_size > 100:
        return
    import certifi

    request = Request(url, headers={"User-Agent": "Karaoke-Forge-release-builder"})
    context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(request, timeout=45, context=context) as response:
        content = response.read(2 * 1024 * 1024 + 1)
    if not 100 < len(content) <= 2 * 1024 * 1024 or b"<html" in content[:1000].lower():
        raise ValueError(f"Unexpected license response: {url}")
    content.decode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)


def collect_licenses(destination: Path, cache: Path) -> list[dict]:
    destination.mkdir(parents=True, exist_ok=True)
    excluded = {"gradio", "gradio-client", "torchvision"}
    inventory = []
    for distribution in sorted(importlib.metadata.distributions(),
                               key=lambda item: item.metadata.get("Name", "").lower()):
        name = distribution.metadata.get("Name", "unknown")
        if name.lower().replace("_", "-") in excluded:
            continue
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
        copied = []
        for relative in distribution.files or ():
            if not _license_file(Path(str(relative))):
                continue
            source = Path(distribution.locate_file(relative))
            if not source.is_file():
                continue
            # Keep package-relative structure to avoid duplicate LICENSE basenames.
            clean_parts = [part for part in relative.parts if part not in {"..", "."}]
            target = destination / safe_name / Path(*clean_parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(target.relative_to(destination).as_posix())
        inventory.append({
            "name": name, "version": distribution.version,
            "license": distribution.metadata.get("License-Expression")
            or distribution.metadata.get("License", "See upstream project"),
            "project_urls": distribution.metadata.get_all("Project-URL") or [],
            "files": copied,
        })
    sources = dict(LICENSE_SOURCES)
    ctranslate_version = importlib.metadata.version("ctranslate2")
    sources["CTranslate2-LICENSE.txt"] = (
        f"https://raw.githubusercontent.com/OpenNMT/CTranslate2/v{ctranslate_version}/LICENSE"
    )
    for filename, url in sources.items():
        cached = cache / filename
        _download_license(url, cached)
        shutil.copy2(cached, destination / filename)
    for candidate in (Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE"):
        if candidate.is_file():
            shutil.copy2(candidate, destination / "Python-LICENSE.txt")
            break
    (destination / "inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (destination / "UPSTREAM-SOURCES.json").write_text(
        json.dumps(sources, indent=2) + "\n", encoding="utf-8")
    return inventory


def stage_distribution(root: Path, bundle: Path, work: Path, ffmpeg_root: Path) -> None:
    version = project_version(root)
    inventory = collect_licenses(bundle / "licenses", work / "license-cache")
    shutil.copy2(root / "LICENSE", bundle / "LICENSE")
    source = bundle / "source"
    source.mkdir(parents=True, exist_ok=True)
    shutil.copytree(root / "src/karaoke_forge", source / "src/karaoke_forge", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("pyproject.toml", "LICENSE", "README.md", "README_EN.md", "CHANGELOG.md"):
        shutil.copy2(root / name, source / name)
    shutil.copytree(root / "scripts", source / "scripts", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(root / "docs", source / "docs", dirs_exist_ok=True)
    shutil.copy2(root / "docs/windows-build.md", bundle / "BUILDING.md")
    notice = """# Third-party components

This folder contains an unmodified Python/Qt runtime and third-party libraries.
Karaoke Forge's own source is in source/ under its MIT license; that license does
not replace the licenses of its dependencies. licenses/inventory.json lists the
build environment's distributions and the copied upstream license documents.

PySide6 / Qt use their applicable LGPL/GPL/commercial license options. The LGPL
and GPL texts are included in licenses/. Qt DLLs stay separate in _internal/
and are not statically linked into Karaoke Forge. Qt and PySide source releases:
https://download.qt.io/official_releases/qt/
https://code.qt.io/cgit/pyside/pyside-setup.git/

pykakasi and alkana retain their GPL terms; copied package notices and the GPL
texts are included. Sources: https://github.com/miurahr/pykakasi and
https://github.com/cod-sushi/alkana.py (see inventory.json for package versions).
CTranslate2 source: https://github.com/OpenNMT/CTranslate2

The private FFmpeg executables are the Gyan essentials distribution already
verified by this repository's bootstrap script. Its LICENSE and README.txt are
included unchanged at _internal/ffmpeg/. Upstream build/source information:
https://www.gyan.dev/ffmpeg/builds/ and https://github.com/GyanD/codexffmpeg
Consult those bundled notices for FFmpeg's enabled components and license terms.

CPU PyTorch, Torchaudio and Demucs are included for vocal/accompaniment separation.
Their unmodified native libraries and package licenses remain in this distribution.
Upstream sources: https://github.com/pytorch/pytorch (v2.8.0),
https://github.com/pytorch/audio (v2.8.0), https://github.com/adefossez/demucs (v4.1.0).
Whisper and pretrained Demucs model weights are downloaded separately on demand.
Gradio and WebEngine are not part of this native portable build.
"""
    (bundle / "THIRD_PARTY_NOTICES.md").write_text(notice, encoding="utf-8")
    instructions = """Karaoke Forge · Windows x64 便携版

1. 将整个 ZIP 解压到一个文件夹。
2. 双击 KaraokeForge.exe。请保留旁边的 _internal 文件夹。
3. 不需要另行安装 Python、Qt 或 FFmpeg。

个人设置、模型、缓存和默认输出保存在：
%LOCALAPPDATA%\\KaraokeForge
可在导出时选择其他输出目录。首次自动对齐或人声分离需要联网下载对应模型。
本包已包含 CPU 人声分离运行库，可直接导出原声版和无人声伴奏版。
无需安装 Python 环境；模型下载完成后可复用本地缓存。

排查启动问题：查看 %LOCALAPPDATA%\\KaraokeForge\\logs\\desktop.log。
自检命令：KaraokeForge.exe --self-test C:\\Temp\\karaoke-self-test.json
自检不会登录账号或下载识别模型；成功时JSON中的ok为true。
"""
    (bundle / "开始使用.txt").write_text(instructions, encoding="utf-8-sig")
    manifest = {
        "version": version, "python": platform.python_version(), "architecture": platform.machine(),
        "ffmpeg": {name: sha256(ffmpeg_root / "bin" / name)
                   for name in ("ffmpeg.exe", "ffprobe.exe")},
        "distributions": [{"name": item["name"], "version": item["version"]} for item in inventory],
        "excluded": list(EXCLUDED_MODULES),
    }
    (bundle / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def make_zip(bundle: Path, destination: Path, version: str) -> Path:
    archive = destination / f"{APP_NAME}-{version}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(bundle.parent).as_posix())
    checksum = archive.with_suffix(".zip.sha256")
    checksum.write_text(f"{sha256(archive)}  {archive.name}\n", encoding="ascii")
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg-root", type=Path, default=PROJECT_ROOT / ".runtime/ffmpeg")
    parser.add_argument("--dist-dir", type=Path, default=PROJECT_ROOT / "dist/desktop")
    parser.add_argument("--work-dir", type=Path, default=PROJECT_ROOT / "build/desktop")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args(argv)
    ffmpeg_root, dist, work = (path.resolve() for path in
                              (args.ffmpeg_root, args.dist_dir, args.work_dir))
    command = pyinstaller_command(PROJECT_ROOT, dist, work, ffmpeg_root)
    if args.dry_run:
        print(subprocess.list2cmdline(command))
        return 0
    validate_environment(ffmpeg_root)
    work.mkdir(parents=True, exist_ok=True)
    dist.mkdir(parents=True, exist_ok=True)
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    bundle = dist / APP_NAME
    if not (bundle / f"{APP_NAME}.exe").is_file():
        raise FileNotFoundError("PyInstaller did not produce the expected executable")
    stage_distribution(PROJECT_ROOT, bundle, work, ffmpeg_root)
    print(f"Portable folder: {bundle}")
    if not args.no_zip:
        print(f"Release archive: {make_zip(bundle, dist, project_version())}")
    print("Run the packaged --self-test and a sample render before publishing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
