from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BATCH_FILES = (
    PROJECT_ROOT / "首次安装.bat",
    PROJECT_ROOT / "安装人声分离（Demucs）.bat",
    PROJECT_ROOT / "启动网页版.bat",
)


def test_windows_batch_entrypoints_are_ascii() -> None:
    for path in BATCH_FILES:
        path.read_bytes().decode("ascii")


def test_first_setup_bootstraps_project_private_python() -> None:
    setup = (PROJECT_ROOT / "首次安装.bat").read_text(encoding="ascii")

    assert "scripts\\bootstrap_windows.ps1" in setup
    assert '".runtime\\python\\tools\\python.exe" -m venv ".venv"' in setup
    assert "where python" not in setup.lower()
    assert "where conda" not in setup.lower()


def test_private_runtime_download_is_pinned_and_hashed() -> None:
    bootstrap = (PROJECT_ROOT / "scripts" / "bootstrap_windows.ps1").read_text(
        encoding="ascii"
    )

    assert "python/3.12.10/python.3.12.10.nupkg" in bootstrap
    assert "Get-FileHash" in bootstrap
    assert "SHA512" in bootstrap
