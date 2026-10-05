from __future__ import annotations

import json
import math
import os
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .models import LyricsDocument

PROJECT_FILENAME = "karaoke-forge-project.json"
RECENT_FILENAME = ".karaoke-forge-last-project.json"
PROJECT_INDEX_FILENAME = ".karaoke-forge-projects.json"
_PROJECT_CATALOG_LOCK = threading.RLock()


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
    updated_at: datetime | None = None


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _resolve_project_path(value: object, root: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("工程资产路径必须位于工程目录内。") from exc
    return resolved


def _write_json_atomic(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _indexed_manifest_paths(recent_root: str | Path) -> list[Path]:
    index = Path(recent_root).resolve() / PROJECT_INDEX_FILENAME
    if not index.is_file():
        return []
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
        values = data.get("manifests") if isinstance(data, dict) else None
        if not isinstance(values, list):
            return []
        return [
            Path(value).resolve()
            for value in values
            if isinstance(value, str) and value.strip()
        ]
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return []


def _remember_manifest(recent_root: Path, manifest: Path) -> None:
    manifests = [manifest.resolve(), *_indexed_manifest_paths(recent_root)]
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in manifests:
        key = os.path.normcase(str(candidate.resolve()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(str(candidate.resolve()))
    _write_json_atomic(
        recent_root / PROJECT_INDEX_FILENAME,
        {"schema_version": 1, "manifests": unique},
    )


def _parse_updated_at(value: object, manifest: Path) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(manifest.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _recent_manifest_path(recent_root: str | Path) -> Path | None:
    pointer = Path(recent_root).resolve() / RECENT_FILENAME
    if not pointer.is_file():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        manifest = data.get("manifest") if isinstance(data, dict) else None
        return Path(manifest).resolve() if isinstance(manifest, str) and manifest.strip() else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


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


def validate_project_assets(
    *,
    audio: str | Path | None = None,
    video: str | Path | None = None,
    cover: str | Path | None = None,
    font_files: tuple[str | Path, ...] = (),
) -> None:
    """Reject missing explicit selections before an existing project is overwritten."""
    assets = [("音频", audio), ("MV", video), ("封面", cover)]
    assets.extend((f"字体 {index}", path) for index, path in enumerate(font_files, 1))
    for label, source in assets:
        if source and not Path(source).is_file():
            raise FileNotFoundError(f"所选{label}文件不存在，请重新选择或清空该素材后保存：{source}")


def safe_lyrics_export_stem(directory: str | Path, stem: str) -> str:
    """Keep lyric JSON separate from the manifest, without replacing a fallback file."""
    if f"{stem}.json".casefold() != PROJECT_FILENAME.casefold():
        return stem
    root = Path(directory)
    candidate = f"{stem}-lyrics"
    number = 2
    while any(root.glob(f"{candidate}.*")):
        candidate = f"{stem}-lyrics-{number}"
        number += 1
    return candidate


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
    validate_project_assets(audio=audio, video=video, cover=cover, font_files=font_files)
    if Path(lyrics_project).resolve() == root / PROJECT_FILENAME:
        raise ValueError("歌词文件与工程索引重名，请使用独立的歌词 JSON 文件名。")
    root.mkdir(parents=True, exist_ok=True)
    lyrics = persist_project_asset(lyrics_project, root, "lyrics")
    if lyrics is None:
        raise FileNotFoundError("歌词项目不存在，无法保存工程。")
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
        "app_version": __version__,
        "name": name,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "lyrics_project": _relative_or_absolute(lyrics, root),
        "audio": _relative_or_absolute(saved_audio, root) if saved_audio else None,
        "video": _relative_or_absolute(saved_video, root) if saved_video else None,
        "cover": _relative_or_absolute(saved_cover, root) if saved_cover else None,
        "font_files": [_relative_or_absolute(font, root) for font in saved_fonts],
        "settings": settings or {},
    }
    _write_json_atomic(manifest, data)
    pointer_root = Path(recent_root).resolve() if recent_root else root.parent
    pointer_root.mkdir(parents=True, exist_ok=True)
    pointer = pointer_root / RECENT_FILENAME
    # Pointer and catalog form one process-local transaction. Gradio callbacks can
    # save different projects concurrently, so the catalog must be re-read while
    # holding the same lock that protects its atomic replacement.
    with _PROJECT_CATALOG_LOCK:
        _write_json_atomic(pointer, {"manifest": str(manifest)})
        _remember_manifest(pointer_root, manifest)
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
        updated_at=_parse_updated_at(data.get("updated_at"), manifest),
    )


def read_workspace_lyrics(workspace: WorkspaceProject) -> LyricsDocument:
    """Read editable lyrics on the source audio clock, including old render jobs.

    Old render jobs saved their video-clock JSON under ``*.assets``. Native
    0.16 also persisted the sync controls, so those projects can be recovered
    without rewriting any original files. Earlier projects lack enough data to
    infer their applied offset: keep them intact and attach an explicit warning.
    Call this in a worker because old automatic sync may need audio fingerprinting.
    """
    from .formats import read_lyrics
    from .media import detect_audio_sync, probe_media_has_audio

    document = read_lyrics(workspace.lyrics_project)
    settings = workspace.settings or {}
    if settings.get("lyrics_timebase") == "audio":
        return document
    try:
        relative = workspace.lyrics_project.relative_to(workspace.manifest.parent)
    except ValueError:
        return document
    if len(relative.parts) != 2 or not relative.parts[0].endswith(".assets"):
        return document

    def uncertain(detail: str) -> LyricsDocument:
        document.metadata["legacy_timing_warning"] = (
            "此旧版成片工程保存的是视频时间轴，无法可靠恢复歌曲原时间："
            f"{detail}。原文件未改动，请核对音频与时间轴后再导出。"
        )
        return document

    if "audio_offset" not in settings or "auto_sync" not in settings:
        return uncertain("旧版未保存完整偏移设置")
    try:
        applied_offset = float(settings["audio_offset"])
        if not math.isfinite(applied_offset):
            return uncertain("保存的偏移量无效")
        audio, video = workspace.audio, workspace.video
        if settings["auto_sync"] and video is not None:
            if audio is None or not audio.is_file() or not video.is_file():
                return uncertain("原音频或 MV 已丢失")
            if audio.resolve() != video.resolve():
                audio_state = probe_media_has_audio(video)
                if audio_state is None:
                    return uncertain("无法检测 MV 音轨")
                if audio_state:
                    sync = detect_audio_sync(audio, video)
                    if not sync.reliable:
                        return uncertain("原音频与 MV 无法可靠匹配")
                    applied_offset += sync.offset
        if not math.isfinite(applied_offset):
            return uncertain("同步偏移量无效")
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        return uncertain(str(exc))
    if applied_offset < 0 and any(
        time == 0
        for line in document.lines
        for time in (line.start, *(token.start for token in line.tokens))
    ):
        return uncertain("负偏移已裁掉开头的部分时间，无法反推出原始位置")
    if applied_offset:
        document = document.shifted(-applied_offset)
    return document


def load_recent_workspace(recent_root: str | Path) -> WorkspaceProject | None:
    try:
        manifest = _recent_manifest_path(recent_root)
        return load_workspace_project(manifest) if manifest is not None else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def list_workspace_projects(recent_root: str | Path) -> list[WorkspaceProject]:
    """List valid saved projects newest-first without recursively scanning user storage."""

    root = Path(recent_root).resolve()
    candidates: list[Path] = []
    recent = _recent_manifest_path(root)
    if recent is not None:
        candidates.append(recent)
    candidates.extend(_indexed_manifest_paths(root))
    candidates.extend(root.glob(f"*/{PROJECT_FILENAME}"))
    root_manifest = root / PROJECT_FILENAME
    if root_manifest.is_file():
        candidates.append(root_manifest)

    projects: list[WorkspaceProject] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate.resolve()))
        if key in seen:
            continue
        seen.add(key)
        try:
            projects.append(load_workspace_project(candidate))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    projects.sort(
        key=lambda project: project.updated_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return projects
