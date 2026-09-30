"""Portable launcher and artifact assembly checks which do not build an executable."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import types
import zipfile
from pathlib import Path

import pytest

from karaoke_forge.desktop import launcher

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("desktop_builder", ROOT / "scripts/build_desktop.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_import_keeps_source_cli_and_environment_lazy():
    code = """
import json, os, sys
before = dict(os.environ)
from karaoke_forge.desktop import launcher
assert before == dict(os.environ)
assert 'karaoke_forge.cli' not in sys.modules
assert 'karaoke_forge.desktop.app' not in sys.modules
assert 'PySide6.QtWidgets' not in sys.modules
assert launcher.configure_frozen_environment() == {}
print(json.dumps({'ok': True}))
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            check=True, timeout=20)
    assert json.loads(result.stdout) == {"ok": True}


def test_frozen_data_paths_use_user_profile_and_private_media(monkeypatch, tmp_path):
    executable = tmp_path / "portable" / "KaraokeForge.exe"
    contents = executable.parent / "_internal"
    local = tmp_path / "profile"
    override = tmp_path / "custom outputs"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "_MEIPASS", str(contents), raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    for name in ("KARAOKE_FORGE_SETTINGS_DIR", "KARAOKE_FORGE_CACHE_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KARAOKE_FORGE_OUTPUT_DIR", str(override))
    monkeypatch.setenv("KARAOKE_FORGE_ROOT", "wrong")
    monkeypatch.setenv("KARAOKE_FORGE_FFMPEG_DIR", "wrong")
    paths = launcher.configure_frozen_environment()
    assert paths["root"] == executable.parent
    assert Path(os.environ["KARAOKE_FORGE_FFMPEG_DIR"]) == contents / "ffmpeg/bin"
    assert Path(os.environ["KARAOKE_FORGE_ROOT"]) == executable.parent
    assert Path(os.environ["KARAOKE_FORGE_SETTINGS_DIR"]) == local / "KaraokeForge"
    assert Path(os.environ["KARAOKE_FORGE_CACHE_DIR"]) == local / "KaraokeForge/cache"
    assert Path(os.environ["KARAOKE_FORGE_OUTPUT_DIR"]) == override
    assert override.is_dir() and paths["logs"].is_dir()
    assert not executable.parent.exists()


@pytest.fixture
def owned_streams(monkeypatch):
    handles = []
    monkeypatch.setattr(launcher, "_stream_handles", handles)
    yield handles
    for handle in handles:
        handle.close()


def test_windowed_streams_preserve_redirected_worker_output(monkeypatch, owned_streams):
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(launcher, "_inherited_windows_stream",
                        lambda handle: stdout if handle == -11 else stderr)
    launcher.ensure_standard_streams()
    print("模型下载进度", flush=True)
    print("diagnostic", file=sys.stderr, flush=True)
    assert stdout.getvalue() == "模型下载进度\n"
    assert stderr.getvalue() == "diagnostic\n"
    assert sys.stdin.read() == ""
    assert len(owned_streams) == 3


def test_windowed_streams_log_and_preserve_existing_streams(monkeypatch, tmp_path, owned_streams):
    existing_stdout = sys.stdout
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(launcher, "_inherited_windows_stream", lambda handle: None)
    launcher.ensure_standard_streams(tmp_path)
    print("窗口错误", file=sys.stderr, flush=True)
    assert sys.stdout is existing_stdout
    assert (tmp_path / "desktop.log").read_text(encoding="utf-8") == "窗口错误\n"
    assert len(owned_streams) == 1


def test_frozen_existing_ansi_streams_are_normalized_to_utf8(monkeypatch):
    stdout_bytes, stderr_bytes = io.BytesIO(), io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding="gbk")
    stderr = io.TextIOWrapper(stderr_bytes, encoding="gbk")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    launcher.ensure_standard_streams()
    message = "中文缓存/歌曲_🎵"
    print(message, flush=True)
    print(message, file=sys.stderr, flush=True)
    assert stdout_bytes.getvalue().decode("utf-8").strip() == message
    assert stderr_bytes.getvalue().decode("utf-8").strip() == message


def test_successful_worker_roundtrips_non_ascii_path_in_real_process(tmp_path):
    pytest.importorskip("faster_whisper")
    from karaoke_forge.network import (
        ModelDownloadSettings,
        model_cache_directory,
        save_model_download_settings,
    )
    from karaoke_forge.transcribe import PINNED_MODEL_REVISIONS

    data = tmp_path / "中文缓存_🎵"
    settings = ModelDownloadSettings(mode="offline")
    save_model_download_settings(settings, settings_dir=data)
    snapshot = (model_cache_directory(settings, settings_dir=data) / "hub"
                / "models--Systran--faster-whisper-small" / "snapshots"
                / PINNED_MODEL_REVISIONS["small"])
    snapshot.mkdir(parents=True)
    # Simulate the existing ANSI streams created by the windowed bootloader,
    # then run the actual model worker against a local path-only cache fixture.
    code = """
import io, sys
from karaoke_forge.desktop import launcher
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='gbk')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='gbk')
sys.frozen = True
launcher.ensure_standard_streams()
raise SystemExit(launcher.dispatch_worker(['-m', 'karaoke_forge.model_worker', 'small']))
"""
    environment = {**os.environ, "KARAOKE_FORGE_SETTINGS_DIR": str(data), "HF_HUB_OFFLINE": "1"}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, check=True,
                            env=environment, timeout=45)
    returned = Path(result.stdout.decode("utf-8").strip().splitlines()[-1])
    assert returned.resolve() == snapshot.resolve()
    assert returned.is_dir()


def test_worker_protocol_dispatches_only_known_model_commands(monkeypatch):
    calls = []

    def fake_main(args):
        calls.append(args)
        return 7

    for name in ("karaoke_forge.model_worker", "karaoke_forge.cli", "demucs.separate"):
        monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(main=fake_main))
    assert launcher.dispatch_worker(["-m", "karaoke_forge.model_worker", "tiny"]) == 7
    assert launcher.dispatch_worker(["-m", "karaoke_forge", "model-download", "--mode", "status"]) == 7
    assert launcher.dispatch_worker(["-m", "demucs", "--two-stems", "vocals", "歌曲.wav"]) == 7
    assert calls == [["tiny"], ["model-download", "--mode", "status"],
                     ["--two-stems", "vocals", "歌曲.wav"]]
    assert launcher.dispatch_worker(["song.json"]) is None
    with pytest.raises(ValueError, match="Unsupported"):
        launcher.dispatch_worker(["-m", "os", "arbitrary"])
    with pytest.raises(ValueError, match="Missing"):
        launcher.dispatch_worker(["-m"])


def test_self_test_bootstrap_failure_still_writes_report(monkeypatch, tmp_path):
    def fail(report):
        raise ImportError("missing Qt DLL")

    monkeypatch.setattr(launcher, "configure_frozen_environment", dict)
    monkeypatch.setattr(launcher, "ensure_standard_streams", lambda logs: None)
    monkeypatch.setattr(launcher, "_hide_child_consoles", lambda: None)
    monkeypatch.setattr(launcher, "run_self_test", fail)
    report = tmp_path / "reports" / "result.json"
    assert launcher.main(["--self-test", str(report)]) == 1
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["ok"] is False
    assert data["bootstrap_error"] == "ImportError: missing Qt DLL"


def test_build_command_keeps_paths_and_dynamic_dependencies(tmp_path):
    root = tmp_path / "source with spaces"
    ffmpeg = tmp_path / "media tools"
    command = builder.pyinstaller_command(root, tmp_path / "dist", tmp_path / "work", ffmpeg)

    def options(name):
        return [command[index + 1] for index, value in enumerate(command) if value == name]

    assert command[:3] == [sys.executable, "-m", "PyInstaller"]
    assert "--onedir" in command and "--windowed" in command
    assert str(root / "src") in options("--paths")
    assert set(builder.COLLECT_ALL) <= set(options("--collect-all"))
    assert "PySide6" not in options("--collect-all")
    assert {"gradio", "PySide6.QtWebEngineCore"} <= set(options("--exclude-module"))
    assert not {"torch", "torchaudio", "demucs"} & set(options("--exclude-module"))
    assert {"torch", "torchaudio", "demucs.separate"} <= set(options("--hidden-import"))
    assert f"{ffmpeg / 'bin/ffmpeg.exe'}:ffmpeg/bin" in options("--add-binary")
    assert command[-1] == str(root / "src/karaoke_forge/desktop/launcher.py")


def test_dry_run_never_builds_downloads_or_creates_directories(monkeypatch, tmp_path, capsys):
    def unexpected(*args, **kwargs):
        pytest.fail("dry run must not perform build or network operations")

    monkeypatch.setattr(builder.subprocess, "run", unexpected)
    monkeypatch.setattr(builder, "_download_license", unexpected)
    destination = tmp_path / "not-created"
    assert builder.main(["--dry-run", "--dist-dir", str(destination),
                         "--work-dir", str(destination / "work")]) == 0
    assert "PyInstaller" in capsys.readouterr().out
    assert not destination.exists()


def test_archive_contains_complete_directory_and_matches_checksum(tmp_path):
    bundle = tmp_path / "KaraokeForge"
    internal = bundle / "_internal"
    internal.mkdir(parents=True)
    (bundle / "KaraokeForge.exe").write_bytes(b"fake exe")
    (internal / "media.dll").write_bytes(b"fake binary")
    (bundle / "LICENSE").write_text("redistribution license", encoding="utf-8")
    archive = builder.make_zip(bundle, tmp_path, "1.2.3")
    with zipfile.ZipFile(archive) as package:
        assert set(package.namelist()) == {
            "KaraokeForge/KaraokeForge.exe", "KaraokeForge/_internal/media.dll",
            "KaraokeForge/LICENSE",
        }
        assert package.read("KaraokeForge/_internal/media.dll") == b"fake binary"
    checksum, filename = archive.with_suffix(".zip.sha256").read_text().split()
    assert checksum == builder.sha256(archive)
    assert filename == archive.name


def test_license_cache_avoids_network_and_code_files(monkeypatch, tmp_path):
    def unexpected(*args, **kwargs):
        pytest.fail("valid cached licenses must not require a network request")

    monkeypatch.setattr(builder, "urlopen", unexpected)
    cached = tmp_path / "LGPL.txt"
    cached.write_text("license text\n" * 100, encoding="utf-8")
    builder._download_license("https://invalid.example/license", cached)
    assert builder._license_file(Path("package.dist-info/licenses/COPYING"))
    assert builder._license_file(Path("package/LICENSE-MIT.txt"))
    assert not builder._license_file(Path("package/license.py"))
    assert not builder._license_file(Path("package/native.dll"))


def test_project_version_matches_current_source():
    from karaoke_forge import __version__

    assert builder.project_version() == __version__
