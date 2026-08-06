from pathlib import Path
from types import SimpleNamespace

import pytest

from karaoke_forge.model_worker import main
from karaoke_forge.transcribe import (
    TranscriptionError,
    _download_hf_model_in_isolated_process,
)


def test_model_worker_downloads_only_a_known_pinned_model(tmp_path, monkeypatch) -> None:
    received: list[str] = []
    monkeypatch.setattr(
        "karaoke_forge.model_worker.predownload_faster_whisper_model",
        lambda model, **_kwargs: received.append(model) or (tmp_path / model),
    )

    result = main(["small"])

    assert result == 0
    assert received == ["small"]


def test_model_worker_reports_download_failure(monkeypatch, capsys) -> None:
    def fail(*_args, **_kwargs) -> Path:
        raise RuntimeError("network unavailable")

    monkeypatch.setattr("karaoke_forge.model_worker.predownload_faster_whisper_model", fail)

    result = main(["large-v3-turbo"])

    assert result == 2
    assert "network unavailable" in capsys.readouterr().err


def test_parent_download_uses_clean_worker_and_validates_returned_cache_path(
    tmp_path,
    monkeypatch,
) -> None:
    cache = tmp_path / "hub"
    downloaded = cache / "models--Systran--faster-whisper-small" / "snapshots" / "abc"
    downloaded.mkdir(parents=True)
    received: dict[str, object] = {}

    def fake_run(command, **kwargs):
        received["command"] = command
        received.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=f"ready\n{downloaded}\n", stderr="")

    monkeypatch.setattr("karaoke_forge.transcribe.subprocess.run", fake_run)

    result = _download_hf_model_in_isolated_process("small", cache_directory=cache)

    assert result == downloaded.resolve()
    assert received["command"][1:] == ["-m", "karaoke_forge.model_worker", "small"]
    assert received["env"]["HF_HUB_DISABLE_IMPLICIT_TOKEN"] == "1"
    assert received["env"]["PYTHONUTF8"] == "1"


def test_parent_download_rejects_worker_path_outside_selected_cache(
    tmp_path,
    monkeypatch,
) -> None:
    cache = tmp_path / "hub"
    cache.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(
        "karaoke_forge.transcribe.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"{outside}\n",
            stderr="",
        ),
    )

    with pytest.raises(TranscriptionError, match="缓存目录之外"):
        _download_hf_model_in_isolated_process("small", cache_directory=cache)
