import os

import pytest

from karaoke_forge.runtime import DemucsRuntime, find_runtime_executable


def test_demucs_runtime_explains_cpu_fallback_on_nvidia() -> None:
    runtime = DemucsRuntime(
        installed=True,
        demucs_version="4.1.0",
        torch_version="2.13.0+cpu",
        device="cpu",
        nvidia_detected=True,
    )

    assert runtime.ready
    assert runtime.short_label_zh == "可用：CPU（可切换 NVIDIA）"
    assert "独立安装脚本" in runtime.detail_zh


def test_demucs_runtime_reports_dedicated_windows_installer() -> None:
    runtime = DemucsRuntime(installed=False, nvidia_detected=False)

    assert not runtime.ready
    assert "安装人声分离（Demucs）.bat" in runtime.detail_zh


def test_runtime_executable_prefers_explicit_private_directory(tmp_path, monkeypatch) -> None:
    binary = tmp_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    binary.write_bytes(b"private")
    monkeypatch.setenv("KARAOKE_FORGE_FFMPEG_DIR", str(tmp_path))
    monkeypatch.setattr("karaoke_forge.runtime.shutil.which", lambda _name: "system-ffmpeg")

    assert find_runtime_executable("ffmpeg") == str(binary.resolve())


def test_runtime_executable_falls_back_to_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KARAOKE_FORGE_FFMPEG_DIR", str(tmp_path))
    monkeypatch.setattr("karaoke_forge.runtime.shutil.which", lambda _name: "system-ffmpeg")

    assert find_runtime_executable("ffmpeg") == "system-ffmpeg"


def test_runtime_executable_does_not_trust_working_directory_ancestors(
    tmp_path,
    monkeypatch,
) -> None:
    nested = tmp_path / "untrusted" / "nested"
    private_bin = tmp_path / ".runtime" / "ffmpeg" / "bin"
    nested.mkdir(parents=True)
    private_bin.mkdir(parents=True)
    executable = private_bin / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    executable.write_bytes(b"untrusted")
    monkeypatch.chdir(nested)
    monkeypatch.delenv("KARAOKE_FORGE_FFMPEG_DIR", raising=False)
    monkeypatch.delenv("KARAOKE_FORGE_ROOT", raising=False)
    monkeypatch.setattr("karaoke_forge.runtime.shutil.which", lambda _name: "system-ffmpeg")

    assert find_runtime_executable("ffmpeg") == "system-ffmpeg"


def test_runtime_executable_rejects_path_input() -> None:
    with pytest.raises(ValueError, match="must not contain a path"):
        find_runtime_executable("../ffmpeg")
