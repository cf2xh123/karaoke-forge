from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BATCH_FILES = (
    PROJECT_ROOT / "首次安装.bat",
    PROJECT_ROOT / "安装人声分离（Demucs）.bat",
    PROJECT_ROOT / "启动网页版.bat",
    PROJECT_ROOT / "模型下载设置.bat",
)


def test_windows_batch_entrypoints_are_ascii() -> None:
    for path in BATCH_FILES:
        path.read_bytes().decode("ascii")


def test_first_setup_bootstraps_project_private_python() -> None:
    setup = (PROJECT_ROOT / "首次安装.bat").read_text(encoding="ascii")

    assert "scripts\\bootstrap_windows.ps1" in setup
    assert '".runtime\\python\\tools\\python.exe" -m venv --clear ".venv"' in setup
    assert "where python" not in setup.lower()
    assert "where conda" not in setup.lower()


def test_first_setup_installs_project_private_ffmpeg() -> None:
    setup = (PROJECT_ROOT / "首次安装.bat").read_text(encoding="ascii")

    assert "scripts\\bootstrap_ffmpeg_windows.ps1" in setup
    assert '".runtime\\ffmpeg\\bin\\ffmpeg.exe"' in setup
    assert '".runtime\\ffmpeg\\bin\\ffprobe.exe"' in setup
    assert "where ffmpeg" not in setup.lower()
    assert "add it to path" not in setup.lower()


def test_first_setup_opens_the_beginner_model_download_wizard() -> None:
    setup = (PROJECT_ROOT / "首次安装.bat").read_text(encoding="ascii")

    assert "-m karaoke_forge model-download" in setup
    assert "[6/6] Configuring model downloads" in setup
    assert "Setup can still finish" in setup


def test_dedicated_model_download_launcher_uses_private_python() -> None:
    launcher = (PROJECT_ROOT / "模型下载设置.bat").read_text(encoding="ascii")

    assert '".venv\\Scripts\\python.exe" -m karaoke_forge model-download' in launcher
    assert "No third-party mirror was enabled automatically" in launcher


def test_web_launcher_repairs_missing_or_outdated_components() -> None:
    launcher = (PROJECT_ROOT / "启动网页版.bat").read_text(encoding="ascii")

    assert "import inspect, websocket" in launcher
    assert "from faster_whisper.utils import download_model" in launcher
    assert '-m pip install --upgrade -e ".[web,align,netease,pronunciation]"' in launcher
    assert launcher.count("if errorlevel 1") >= 2
    assert "[ERROR] Could not install" in launcher
    assert "exit /b 1" in launcher


def test_web_launcher_repairs_and_uses_private_ffmpeg() -> None:
    launcher = (PROJECT_ROOT / "启动网页版.bat").read_text(encoding="ascii")

    assert "scripts\\bootstrap_ffmpeg_windows.ps1" in launcher
    assert "%KARAOKE_FORGE_FFMPEG_DIR%\\ffmpeg.exe" in launcher
    assert "%KARAOKE_FORGE_FFMPEG_DIR%\\ffprobe.exe" in launcher
    assert 'set "PATH=%KARAOKE_FORGE_FFMPEG_DIR%;%PATH%"' in launcher
    assert 'set "KARAOKE_FORGE_ROOT=%CD%"' in launcher
    assert "'revision' in inspect.signature(download_model).parameters" in launcher
    assert 'pip install --upgrade -e ".[web,align,netease,pronunciation]"' in launcher
    assert launcher.count("-hide_banner -version") >= 2
    assert "administrator" not in launcher.lower()


def test_private_runtime_download_is_pinned_and_hashed() -> None:
    bootstrap = (PROJECT_ROOT / "scripts" / "bootstrap_windows.ps1").read_text(
        encoding="ascii"
    )

    assert "python/3.12.10/python.3.12.10.nupkg" in bootstrap
    assert "Get-FileHash" in bootstrap
    assert "SHA512" in bootstrap


def test_private_ffmpeg_download_is_pinned_hashed_and_has_fallback() -> None:
    bootstrap = (
        PROJECT_ROOT / "scripts" / "bootstrap_ffmpeg_windows.ps1"
    ).read_text(encoding="ascii")

    assert "GyanD/codexffmpeg/releases/download/$FfmpegVersion/$ArchiveName" in bootstrap
    assert "www.gyan.dev/ffmpeg/builds/packages/$ArchiveName" in bootstrap
    assert "DB580001CAA24AC104C8CB856CD113A87B0A443F7BDF47D8C12B1D740584A2EC" in bootstrap
    assert "Get-FileHash" in bootstrap
    assert "SHA256" in bootstrap
    assert "Save-HttpsFileLimited" in bootstrap
    assert "$Request.Timeout = 60000" in bootstrap
    assert "$DownloadedBytes -gt $MaximumBytes" in bootstrap


def test_private_ffmpeg_bootstrap_hardens_extraction_and_rolls_back() -> None:
    bootstrap = (
        PROJECT_ROOT / "scripts" / "bootstrap_ffmpeg_windows.ps1"
    ).read_text(encoding="ascii")

    assert "ZipFile]::OpenRead" in bootstrap
    assert "GetInvalidFileNameChars" in bootstrap
    assert "Test-ReservedDeviceName" in bootstrap
    assert "duplicate paths" in bootstrap
    assert "MaximumExpandedBytes" in bootstrap
    assert "MaximumCompressionRatio" in bootstrap
    assert "ffmpeg-bootstrap.lock" in bootstrap
    assert "FileShare]::None" in bootstrap
    assert "ffmpeg-backup-" in bootstrap
    assert "Move-Item -LiteralPath $BackupDir -Destination $RuntimeDir" in bootstrap
