from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_FILENAME = "karaoke-forge-project.json"
RECENT_FILENAME = ".karaoke-forge-last-project.json"


@dataclass(frozen=True)
class WorkspaceProject:
    manifest: Path
    name: str
    lyrics_project: Path
    audio: Path | None = None
    video: Path | None = None
    cover: Path | None = None
    font_files: tuple[Path, ...] = ()
    settings: dict[str, Any] | None = None


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _resolve_project_path(value: object, root: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


def persist_project_asset(
    source: str | Path | None,
    project_dir: str | Path,
    role: str,
) -> Path | None:
    """Keep an uploaded asset beside the project, using a hard link when possible."""

    if source is None:
        return None
    path = Path(source).resolve()
    if not path.is_file():
        return None
    root = Path(project_dir).resolve()
    try:
        relative = path.relative_to(root)
        if not relative.parts or relative.parts[0] not in {".source", ".work"}:
            return path
    except ValueError:
        pass
    assets = root / "project-assets"
    assets.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(char if char.isalnum() or char in ".-_" else "-" for char in path.name)
    target = assets / f"{role}-{safe_name}"
    index = 2
    while target.exists() and not os.path.samefile(path, target):
        target = assets / f"{role}-{index}-{safe_name}"
        index += 1
    if not target.exists():
        try:
            os.link(path, target)
        except OSError:
            shutil.copy2(path, target)
    return target.resolve()


def save_workspace_project(
    project_dir: str | Path,
    *,
    name: str,
    lyrics_project: str | Path,
    audio: str | Path | None = None,
    video: str | Path | None = None,
    cover: str | Path | None = None,
    font_files: tuple[str | Path, ...] = (),
    settings: dict[str, Any] | None = None,
    recent_root: str | Path | None = None,
) -> WorkspaceProject:
    root = Path(project_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    lyrics = Path(lyrics_project).resolve()
    saved_audio = persist_project_asset(audio, root, "audio")
    saved_video = persist_project_asset(video, root, "video")
    saved_cover = persist_project_asset(cover, root, "cover")
    saved_fonts = tuple(
        saved
        for index, font in enumerate(font_files, 1)
        if (saved := persist_project_asset(font, root, f"font-{index}")) is not None
    )
    manifest = root / PROJECT_FILENAME
    data = {
        "schema_version": 1,
        "app_version": "0.10.1",
        "name": name,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "lyrics_project": _relative_or_absolute(lyrics, root),
        "audio": _relative_or_absolute(saved_audio, root) if saved_audio else None,
        "video": _relative_or_absolute(saved_video, root) if saved_video else None,
        "cover": _relative_or_absolute(saved_cover, root) if saved_cover else None,
        "font_files": [_relative_or_absolute(font, root) for font in saved_fonts],
        "settings": settings or {},
    }
    temporary = manifest.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest)
    pointer_root = Path(recent_root).resolve() if recent_root else root.parent
    pointer_root.mkdir(parents=True, exist_ok=True)
    pointer = pointer_root / RECENT_FILENAME
    pointer.write_text(
        json.dumps({"manifest": str(manifest)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return load_workspace_project(manifest)


def load_workspace_project(manifest_path: str | Path) -> WorkspaceProject:
    manifest = Path(manifest_path).resolve()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("工程文件格式不受支持。")
    root = manifest.parent
    lyrics = _resolve_project_path(data.get("lyrics_project"), root)
    if lyrics is None or not lyrics.is_file():
        raise FileNotFoundError("工程中的歌词项目已丢失。")
    font_values = data.get("font_files")
    font_files = (
        tuple(
            path
            for value in font_values
            if (path := _resolve_project_path(value, root)) is not None and path.is_file()
        )
        if isinstance(font_values, list)
        else ()
    )
    settings = data.get("settings")
    return WorkspaceProject(
        manifest=manifest,
        name=str(data.get("name") or lyrics.stem),
        lyrics_project=lyrics,
        audio=_resolve_project_path(data.get("audio"), root),
        video=_resolve_project_path(data.get("video"), root),
        cover=_resolve_project_path(data.get("cover"), root),
        font_files=font_files,
        settings=settings if isinstance(settings, dict) else {},
    )


def load_recent_workspace(recent_root: str | Path) -> WorkspaceProject | None:
    pointer = Path(recent_root).resolve() / RECENT_FILENAME
    if not pointer.is_file():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        manifest = data.get("manifest") if isinstance(data, dict) else None
        return load_workspace_project(manifest) if isinstance(manifest, str) else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
