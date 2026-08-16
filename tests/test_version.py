import re
from pathlib import Path

from karaoke_forge import __version__

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
        "src/karaoke_forge/artwork.py": f"Karaoke-Forge/{__version__}",
        "src/karaoke_forge/domestic_models.py": f"Karaoke-Forge/{__version__}",
        "src/karaoke_forge/netease.py": f"Karaoke-Forge/{__version__}",
        "src/karaoke_forge/projects.py": f'"app_version": "{__version__}"',
        "src/karaoke_forge/qqmusic.py": f"Karaoke-Forge/{__version__}",
        "src/karaoke_forge/utaten.py": f"Karaoke-Forge/{__version__}",
    }
    for relative_path, expected in expected_snippets.items():
        contents = (ROOT / relative_path).read_text(encoding="utf-8")
        assert expected in contents, f"{relative_path} is not using version {__version__}"
