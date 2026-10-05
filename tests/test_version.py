import json
import re
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from karaoke_forge import __version__
from karaoke_forge import domestic_models as dm
from karaoke_forge.projects import save_workspace_project

ROOT = Path(__file__).resolve().parents[1]


def test_release_version_sources_are_consistent() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project_version = re.search(
        r'^version = "([^"]+)"$',
        pyproject,
        flags=re.MULTILINE,
    )

    assert project_version is not None
    assert project_version.group(1) == __version__

    expected_snippets = {
        "README.md": f"当前发布版本：`{__version__}`",
        "README_EN.md": f"The current release is `{__version__}`",
        "CHANGELOG.md": f"## [{__version__}]",
    }
    for relative_path, expected in expected_snippets.items():
        contents = (ROOT / relative_path).read_text(encoding="utf-8")
        assert expected in contents, f"{relative_path} is not using version {__version__}"


@pytest.mark.parametrize(
    "module_name,function_name,url,error_name",
    [
        ("artwork", "download_public_cover", "https://p1.music.126.net/cover.jpg", "ArtworkError"),
        ("netease", "resolve_netease_song_url", "https://163cn.tv/example", "NeteaseLinkError"),
        ("netease", "_download_public_json", "https://music.163.com/api/song", "NeteaseAccessError"),
        ("qqmusic", "resolve_qqmusic_song_url", "https://c6.y.qq.com/example", "QQMusicLinkError"),
        ("qqmusic", "_download_public_json", "https://y.qq.com/api/song", "QQMusicAccessError"),
        ("utaten", "fetch_public_utaten_info", "https://utaten.com/lyric/yh15042710/",
         "UtaTenAccessError"),
    ],
)
def test_public_requests_identify_current_release(
    module_name, function_name, url, error_name, tmp_path, monkeypatch,
) -> None:
    module = import_module(f"karaoke_forge.{module_name}")
    requests = []

    def offline_open(request, *, timeout):
        requests.append(request)
        raise OSError("offline version test")

    # Capture the actual outgoing request without contacting any service.
    monkeypatch.setattr(module, "_open_url", offline_open)
    arguments = {"output_stem": tmp_path / "cover"} if module_name == "artwork" else {}
    with pytest.raises(getattr(module, error_name), match="offline version test"):
        getattr(module, function_name)(url, **arguments)

    assert len(requests) == 1
    assert requests[0].get_header("User-agent") == f"Mozilla/5.0 Karaoke-Forge/{__version__}"


def test_model_download_identifies_current_release(tmp_path) -> None:
    requests = []

    def offline_open(request, *, timeout):
        requests.append(request)
        raise OSError("offline version test")

    manifest = dm.MODELSCOPE_MODEL_MANIFESTS["small"]
    with pytest.raises(OSError, match="offline version test"):
        dm._download_file(
            "small", manifest, manifest.files[0], tmp_path,
            opener=SimpleNamespace(open=offline_open), timeout=1.0,
            progress=None, heartbeat=lambda: None,
        )

    assert len(requests) == 1
    assert requests[0].get_header("User-agent") == f"Karaoke-Forge/{__version__}"


def test_saved_project_records_current_release(tmp_path) -> None:
    lyrics = tmp_path / "lyrics.json"
    lyrics.write_text('{"version": 1, "lines": []}\n', encoding="utf-8")
    project = save_workspace_project(
        tmp_path / "project", name="Version check", lyrics_project=lyrics, recent_root=tmp_path,
    )

    manifest = json.loads(project.manifest.read_text(encoding="utf-8"))
    assert manifest["app_version"] == __version__
