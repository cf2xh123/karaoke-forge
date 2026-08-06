from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Protocol

MODELSCOPE_ENDPOINT = "https://modelscope.cn"
MARKER_FILENAME = ".karaoke-forge-model.json"
MARKER_SCHEMA_VERSION = 1
DOWNLOAD_TIMEOUT_SECONDS = 120.0
LOCK_TIMEOUT_SECONDS = 60.0 * 60.0
LOCK_STALE_SECONDS = 24.0 * 60.0 * 60.0
MAX_FILE_BYTES = 4 * 1024**3
MAX_MODEL_BYTES = 4 * 1024**3
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
MAX_RESPONSE_ROUNDS = 8

ProgressCallback = Callable[[str], None]


class DomesticModelError(RuntimeError):
    """Base error for the verified domestic model cache."""


class DomesticModelDownloadError(DomesticModelError):
    """Raised when a ModelScope transfer cannot be completed."""


class DomesticModelIntegrityError(DomesticModelError):
    """Raised when downloaded bytes do not match the built-in trust anchor."""


class DomesticModelSecurityError(DomesticModelError):
    """Raised when a URL or local cache object violates a security invariant."""


@dataclass(frozen=True)
class ModelFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ModelManifest:
    repo: str
    revision: str
    files: tuple[ModelFile, ...]


# These values were read from ModelScope's official repository-files API on
# 2026-08-06.  Every snapshot is pinned to an immutable commit and every file
# is additionally anchored by byte length and SHA-256.  The turbo snapshot was
# selected because its bytes match the pinned upstream faster-whisper model.
MODELSCOPE_MODEL_MANIFESTS: Mapping[str, ModelManifest] = MappingProxyType(
    {
        "small": ModelManifest(
            repo="Systran/faster-whisper-small",
            revision="ace8b2ad9dee031c53b6371f6c3c918b5e4f1db9",
            files=(
                ModelFile(
                    "config.json",
                    2_370,
                    "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828",
                ),
                ModelFile(
                    "model.bin",
                    483_546_902,
                    "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
                ),
                ModelFile(
                    "tokenizer.json",
                    2_203_239,
                    "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab",
                ),
                ModelFile(
                    "vocabulary.txt",
                    459_861,
                    "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913",
                ),
            ),
        ),
        "large-v3-turbo": ModelManifest(
            repo="mobiuslabsgmbh/faster-whisper-large-v3-turbo",
            revision="f4e944260beeb23b845ba08b4ef79ac21eed02d1",
            files=(
                ModelFile(
                    "config.json",
                    2_263,
                    "b0253ea6c0d3bea6b1e19e91a02acfd3b53f4467362efcb5a3e6b16c9b3a9b7e",
                ),
                ModelFile(
                    "model.bin",
                    1_617_884_929,
                    "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da",
                ),
                ModelFile(
                    "preprocessor_config.json",
                    340,
                    "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711",
                ),
                ModelFile(
                    "tokenizer.json",
                    2_710_337,
                    "297b13372ac43916285644fb9687add3cc62ee2a1adb60da3dc25cc94c1871fd",
                ),
                ModelFile(
                    "vocabulary.json",
                    1_068_114,
                    "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1",
                ),
            ),
        ),
        "large-v3": ModelManifest(
            repo="Systran/faster-whisper-large-v3",
            revision="fb999d399593f8d6ac57a40cd2d036a43b489721",
            files=(
                ModelFile(
                    "config.json",
                    2_394,
                    "a9306624f5ec14270a014b647e5c316b6e03a662c369758d1b90697a7b0655b9",
                ),
                ModelFile(
                    "model.bin",
                    3_087_284_237,
                    "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1",
                ),
                ModelFile(
                    "preprocessor_config.json",
                    340,
                    "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711",
                ),
                ModelFile(
                    "tokenizer.json",
                    2_480_617,
                    "6d8cbd7cd0d8d5815e478dac67b85a26bbe77c1f5e0c6d76d1ce2abc0e5f21ca",
                ),
                ModelFile(
                    "vocabulary.json",
                    1_068_114,
                    "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1",
                ),
            ),
        ),
    }
)


class _Response(Protocol):
    status: int
    headers: Any

    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...

    def geturl(self) -> str: ...


class _Opener(Protocol):
    def open(
        self, request: urllib.request.Request, *, timeout: float
    ) -> _Response: ...


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise DomesticModelSecurityError(f"模型清单包含不安全路径：{value!r}")


def _validate_manifest(model: str, manifest: ModelManifest) -> None:
    if not model or "/" in model or "\\" in model or model in {".", ".."}:
        raise DomesticModelSecurityError(f"模型名称不安全：{model!r}")
    repo_parts = manifest.repo.split("/")
    if len(repo_parts) != 2 or not all(repo_parts):
        raise DomesticModelSecurityError(f"ModelScope 仓库名称不安全：{manifest.repo!r}")
    if not re.fullmatch(r"[0-9a-f]{40}", manifest.revision):
        raise DomesticModelSecurityError(f"ModelScope 提交版本无效：{manifest.revision!r}")
    seen: set[str] = set()
    total = 0
    for item in manifest.files:
        _validate_relative_path(item.path)
        if item.path in seen:
            raise DomesticModelSecurityError(f"模型清单包含重复文件：{item.path}")
        seen.add(item.path)
        if type(item.size) is not int or not 0 < item.size <= MAX_FILE_BYTES:
            raise DomesticModelSecurityError(f"模型文件大小越界：{item.path}")
        if not re.fullmatch(r"[0-9a-f]{64}", item.sha256):
            raise DomesticModelSecurityError(f"模型文件 SHA-256 无效：{item.path}")
        total += item.size
    if not manifest.files or total > MAX_MODEL_BYTES:
        raise DomesticModelSecurityError("模型清单总大小越界。")


for _model_name, _model_manifest in MODELSCOPE_MODEL_MANIFESTS.items():
    _validate_manifest(_model_name, _model_manifest)


def _manifest_payload(model: str, manifest: ModelManifest) -> dict[str, object]:
    return {
        "model": model,
        "repo": manifest.repo,
        "source": MODELSCOPE_ENDPOINT,
        "revision": manifest.revision,
        "files": [
            {"path": item.path, "size": item.size, "sha256": item.sha256}
            for item in manifest.files
        ],
    }


def _model_directory(cache_root: str | os.PathLike[str], model: str) -> Path:
    return Path(cache_root).expanduser() / "modelscope" / model


def _absolute_path(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _path_is_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _is_reparse_point(path: Path) -> bool:
    """Detect symlinks, junctions, mount-like reparse points, and broken links."""

    try:
        if path.is_symlink() or _path_is_junction(path):
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_attribute)


def _assert_not_reparse(path: Path) -> None:
    try:
        unsafe = _is_reparse_point(path)
    except OSError as exc:
        raise DomesticModelSecurityError(f"无法安全检查模型缓存路径：{path}：{exc}") from exc
    if unsafe:
        raise DomesticModelSecurityError(f"模型缓存路径不能是符号链接、目录联接或重解析点：{path}")


def _assert_safe_cache_path(path: Path, cache_root: Path) -> tuple[Path, Path]:
    """Require a non-reparse path whose textual and resolved forms stay in the cache."""

    root = _absolute_path(cache_root)
    candidate = _absolute_path(path)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise DomesticModelSecurityError(f"模型缓存路径逃逸出预期目录：{candidate}") from exc

    _assert_not_reparse(root)
    current = root
    for part in relative.parts:
        current /= part
        _assert_not_reparse(current)

    try:
        resolved_root = root.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=False)
        resolved_candidate.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise DomesticModelSecurityError(f"模型缓存解析后逃逸出预期目录：{candidate}") from exc
    return candidate, root


@dataclass(frozen=True)
class _CachePaths:
    root: Path
    source: Path
    destination: Path
    staging_root: Path
    staging: Path
    locks_root: Path
    lock: Path


def _cache_paths(cache_root: str | os.PathLike[str], model: str) -> _CachePaths:
    root = _absolute_path(cache_root)
    source = root / "modelscope"
    destination = source / model
    staging_root = source / ".staging"
    staging = staging_root / model
    locks_root = source / ".locks"
    lock = locks_root / f"{model}.lock"
    for candidate in (
        root,
        source,
        destination,
        staging_root,
        staging,
        locks_root,
        lock,
    ):
        _assert_safe_cache_path(candidate, root)
    return _CachePaths(
        root=root,
        source=source,
        destination=destination,
        staging_root=staging_root,
        staging=staging,
        locks_root=locks_root,
        lock=lock,
    )


def _mkdir_safe(path: Path, cache_root: Path, *, parents: bool = False) -> None:
    _assert_safe_cache_path(path, cache_root)
    path.mkdir(parents=parents, exist_ok=True)
    _assert_safe_cache_path(path, cache_root)


def _sha256(path: Path) -> str:
    _assert_not_reparse(path)
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(DOWNLOAD_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_regular_file(path: Path) -> bool:
    return not _is_reparse_point(path) and path.is_file()


def _assert_tree_no_reparse(directory: Path) -> None:
    _assert_not_reparse(directory)
    if not directory.exists():
        return
    if not directory.is_dir():
        raise DomesticModelSecurityError(f"模型缓存目录不是普通目录：{directory}")
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as iterator:
                entries = tuple(iterator)
        except OSError as exc:
            raise DomesticModelSecurityError(f"无法安全扫描模型缓存：{current}：{exc}") from exc
        for entry in entries:
            child = Path(entry.path)
            _assert_not_reparse(child)
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(child)
            except OSError as exc:
                raise DomesticModelSecurityError(
                    f"无法安全检查模型缓存对象：{child}：{exc}"
                ) from exc


def _read_marker(path: Path) -> dict[str, object] | None:
    _assert_not_reparse(path)
    if not _safe_regular_file(path):
        return None
    try:
        if path.stat().st_size > 128 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_write_json(
    path: Path,
    value: Mapping[str, object],
    *,
    safe_root: Path,
) -> None:
    path, safe_root = _assert_safe_cache_path(path, safe_root)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    _assert_safe_cache_path(temporary, safe_root)
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _verified_directory(
    directory: Path,
    model: str,
    manifest: ModelManifest,
    *,
    refresh_marker: bool,
    safe_root: Path | None = None,
) -> Path | None:
    safety_root = directory if safe_root is None else safe_root
    directory, _ = _assert_safe_cache_path(directory, safety_root)
    if not directory.is_dir():
        return None
    _assert_tree_no_reparse(directory)
    marker_path = directory / MARKER_FILENAME
    _assert_safe_cache_path(marker_path, directory)
    marker = _read_marker(marker_path)
    expected_manifest = _manifest_payload(model, manifest)
    if (
        marker is None
        or marker.get("schema_version") != MARKER_SCHEMA_VERSION
        or marker.get("manifest") != expected_manifest
        or not isinstance(marker.get("verified_files"), dict)
    ):
        return None

    recorded = marker["verified_files"]
    assert isinstance(recorded, dict)
    refreshed: dict[str, dict[str, object]] = {}
    marker_changed = False
    for item in manifest.files:
        target = directory.joinpath(*PurePosixPath(item.path).parts)
        _assert_safe_cache_path(target, directory)
        if not _safe_regular_file(target):
            return None
        try:
            stat = target.stat()
        except OSError:
            return None
        if stat.st_size != item.size:
            return None
        previous = recorded.get(item.path)
        unchanged = (
            isinstance(previous, dict)
            and previous.get("size") == item.size
            and previous.get("sha256") == item.sha256
            and previous.get("mtime_ns") == stat.st_mtime_ns
        )
        if not unchanged:
            try:
                if _sha256(target) != item.sha256:
                    return None
            except OSError:
                return None
            marker_changed = True
        refreshed[item.path] = {
            "size": item.size,
            "sha256": item.sha256,
            "mtime_ns": stat.st_mtime_ns,
        }

    if set(recorded) != {item.path for item in manifest.files}:
        marker_changed = True
    if marker_changed and refresh_marker:
        updated = {
            "schema_version": MARKER_SCHEMA_VERSION,
            "manifest": expected_manifest,
            "verified_files": refreshed,
        }
        try:
            _atomic_write_json(marker_path, updated, safe_root=directory)
        except OSError:
            # The full hash check already established trust.  A read-only cache
            # remains usable; it will simply re-hash again on the next lookup.
            pass
    return directory


def _select_manifest(
    model: str,
    manifests: Mapping[str, ModelManifest] = MODELSCOPE_MODEL_MANIFESTS,
) -> ModelManifest:
    try:
        manifest = manifests[model]
    except KeyError as exc:
        supported = "、".join(sorted(manifests))
        raise ValueError(f"ModelScope 国内直连不支持模型 {model!r}；可选：{supported}") from exc
    _validate_manifest(model, manifest)
    return manifest


def find_verified_modelscope_model(
    model: str,
    cache_root: str | os.PathLike[str],
) -> Path | None:
    """Return a locally verified CTranslate2 model, without using the network.

    A file whose size and recorded mtime are unchanged is trusted from its
    previous SHA-256 verification.  A changed mtime always triggers a new hash.
    """

    manifest = _select_manifest(model)
    paths = _cache_paths(cache_root, model)
    return _verified_directory(
        paths.destination,
        model,
        manifest,
        refresh_marker=True,
        safe_root=paths.root,
    )


def _trusted_modelscope_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    return (
        parsed.scheme.lower() == "https"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and (hostname == "modelscope.cn" or hostname.endswith(".modelscope.cn"))
    )


class _TrustedModelScopeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject an unsafe Location header before urllib contacts its destination."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        target = urllib.parse.urljoin(request.full_url, new_url)
        if not _trusted_modelscope_url(target):
            raise DomesticModelSecurityError(
                f"拒绝跟随 ModelScope 返回的未信任重定向：{target}"
            )
        return super().redirect_request(
            request,
            file_pointer,
            code,
            message,
            headers,
            target,
        )


def _build_trusted_modelscope_opener() -> _Opener:
    # Never inherit HTTP(S)_PROXY for the no-proxy domestic path.  The redirect
    # policy is installed in the opener so an untrusted Location is rejected
    # before a socket is opened to that destination.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _TrustedModelScopeRedirectHandler(),
    )


def _download_url(repo: str, revision: str, path: str) -> str:
    quoted_repo = urllib.parse.quote(repo, safe="/")
    quoted_revision = urllib.parse.quote(revision, safe="")
    quoted_path = urllib.parse.quote(path, safe="/")
    url = (
        f"{MODELSCOPE_ENDPOINT}/models/{quoted_repo}/resolve/"
        f"{quoted_revision}/{quoted_path}"
    )
    if not _trusted_modelscope_url(url):  # pragma: no cover - constants are validated
        raise DomesticModelSecurityError(f"拒绝不安全的模型下载地址：{url}")
    return url


def _header(headers: Any, name: str) -> str | None:
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    return str(value).strip() if value is not None else None


def _response_status(response: _Response) -> int:
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        status = response.getcode()
    return int(status)


def _validate_response(
    response: _Response,
    *,
    expected_size: int,
    offset: int,
) -> tuple[str, int]:
    final_url = response.geturl()
    if not _trusted_modelscope_url(final_url):
        raise DomesticModelSecurityError(f"模型下载被重定向到未信任地址：{final_url}")
    status = _response_status(response)
    if status not in {200, 206}:
        raise DomesticModelDownloadError(f"ModelScope 返回异常 HTTP 状态：{status}")

    content_length_text = _header(response.headers, "Content-Length")
    content_length: int | None = None
    if content_length_text:
        try:
            content_length = int(content_length_text)
        except ValueError as exc:
            raise DomesticModelSecurityError("ModelScope 返回了无效的 Content-Length。") from exc
        if content_length < 0:
            raise DomesticModelSecurityError("ModelScope 返回了负数 Content-Length。")

    if status == 206:
        content_range = _header(response.headers, "Content-Range") or ""
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
        if not match:
            raise DomesticModelSecurityError("续传响应缺少有效的 Content-Range。")
        start, end, total = (int(value) for value in match.groups())
        if start != offset or end < start or total != expected_size or end >= total:
            raise DomesticModelSecurityError("续传响应的字节范围与固定模型清单不一致。")
        advertised = end - start + 1
        if content_length is not None and content_length != advertised:
            raise DomesticModelSecurityError("续传响应的长度与 Content-Range 不一致。")
        return "ab", advertised

    if content_length is not None and content_length > expected_size:
        raise DomesticModelSecurityError("下载响应超过固定模型文件大小上限。")
    # A 200 response means the server ignored Range.  Restart only after all
    # response metadata and the final redirect host have passed validation.
    return "wb", content_length if content_length is not None else expected_size


def _clear_execute_bits(path: Path) -> None:
    _assert_not_reparse(path)
    try:
        path.chmod(path.stat().st_mode & ~0o111)
    except OSError:
        pass


def _quarantine(path: Path) -> None:
    _assert_safe_cache_path(path, path.parent)
    _assert_not_reparse(path)
    rejected = path.with_name(f"{path.name}.rejected")
    _assert_safe_cache_path(rejected, path.parent)
    os.replace(path, rejected)
    _assert_not_reparse(rejected)


def _emit_progress(
    callback: ProgressCallback | None,
    model: str,
    item: ModelFile,
    current: int,
    previous_percent: int,
) -> int:
    percent = min(100, int(current * 100 / item.size))
    if callback is not None and (percent == 100 or percent >= previous_percent + 5):
        callback(f"ModelScope 下载 {model}/{item.path}：{percent}%")
        return percent
    return previous_percent


def _download_file(
    model: str,
    manifest: ModelManifest,
    item: ModelFile,
    staging: Path,
    *,
    opener: _Opener,
    timeout: float,
    progress: ProgressCallback | None,
    heartbeat: Callable[[], None],
) -> None:
    staging, _ = _assert_safe_cache_path(staging, staging)
    destination = staging.joinpath(*PurePosixPath(item.path).parts)
    partial = destination.with_name(f"{destination.name}.partial")
    _assert_safe_cache_path(destination, staging)
    _assert_safe_cache_path(partial, staging)
    _mkdir_safe(destination.parent, staging, parents=True)

    if destination.exists():
        if (
            destination.is_file()
            and destination.stat().st_size == item.size
            and _sha256(destination) == item.sha256
        ):
            return
        _quarantine(destination)

    if partial.exists():
        if not partial.is_file():
            raise DomesticModelSecurityError(f"模型断点缓存不是普通文件：{partial}")
        partial_size = partial.stat().st_size
        if partial_size == item.size:
            if _sha256(partial) == item.sha256:
                os.replace(partial, destination)
                _clear_execute_bits(destination)
                return
            _quarantine(partial)
        elif partial_size > item.size:
            _quarantine(partial)

    current = partial.stat().st_size if partial.exists() else 0
    if progress is not None:
        progress(f"正在从 ModelScope 下载 {model}/{item.path}（支持断点续传）")
    last_percent = -5
    last_percent = _emit_progress(progress, model, item, current, last_percent)
    rounds = 0
    url = _download_url(manifest.repo, manifest.revision, item.path)
    while current < item.size:
        rounds += 1
        if rounds > MAX_RESPONSE_ROUNDS:
            raise DomesticModelDownloadError(f"模型文件多次中断，已保留断点：{item.path}")
        headers = {
            "Accept-Encoding": "identity",
            "User-Agent": "Karaoke-Forge/0.13.0",
        }
        if current:
            headers["Range"] = f"bytes={current}-"
        request = urllib.request.Request(url, headers=headers, method="GET")
        response: _Response | None = None
        try:
            response = opener.open(request, timeout=timeout)
            mode, advertised = _validate_response(
                response,
                expected_size=item.size,
                offset=current,
            )
            if mode == "wb":
                current = 0
            before = current
            _assert_safe_cache_path(partial, staging)
            with partial.open(mode) as output:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    if current + len(chunk) > item.size:
                        raise DomesticModelIntegrityError(
                            f"模型文件超过固定大小，已停止接收：{item.path}"
                        )
                    output.write(chunk)
                    current += len(chunk)
                    heartbeat()
                    last_percent = _emit_progress(
                        progress, model, item, current, last_percent
                    )
                output.flush()
                os.fsync(output.fileno())
            if current == before:
                raise DomesticModelDownloadError(f"ModelScope 未返回模型数据：{item.path}")
            if advertised and current - before > advertised:
                raise DomesticModelIntegrityError(f"模型响应长度异常：{item.path}")
        except DomesticModelError:
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            raise DomesticModelDownloadError(
                f"ModelScope 下载失败，断点已保留：{item.path}：{exc}"
            ) from exc
        except OSError:
            raise
        finally:
            if response is not None:
                response.close()

    if partial.stat().st_size != item.size or _sha256(partial) != item.sha256:
        # Deliberately keep the .partial bytes for diagnosis and resume logic.
        # The next invocation quarantines a complete-but-invalid partial before
        # trying again; unverified bytes are never published.
        raise DomesticModelIntegrityError(f"模型文件 SHA-256 校验失败：{item.path}")
    _assert_safe_cache_path(destination, staging)
    _assert_safe_cache_path(partial, staging)
    os.replace(partial, destination)
    _assert_safe_cache_path(destination, staging)
    _clear_execute_bits(destination)
    _emit_progress(progress, model, item, item.size, last_percent)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        # On Windows ``os.kill(pid, 0)`` calls TerminateProcess rather than
        # performing POSIX's harmless existence probe.  Query a process handle
        # instead so lock inspection can never terminate another downloader.
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        open_process.restype = ctypes.c_void_p
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        get_exit_code.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_int

        process_query_limited_information = 0x1000
        still_active = 259
        handle = open_process(process_query_limited_information, False, pid)
        if not handle:
            # Access denied means a process exists but cannot be queried.
            return ctypes.get_last_error() == 5
        try:
            exit_code = ctypes.c_ulong()
            return bool(get_exit_code(handle, ctypes.byref(exit_code))) and (
                exit_code.value == still_active
            )
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class _FileLock:
    def __init__(self, path: Path, timeout: float, *, safe_root: Path | None = None) -> None:
        self.path = path
        self.safe_root = path.parent if safe_root is None else safe_root
        self.timeout = timeout
        self.token = uuid.uuid4().hex
        self.acquired = False

    def __enter__(self) -> _FileLock:  # noqa: PYI034 - Python 3.10 has no typing.Self
        _mkdir_safe(self.path.parent, self.safe_root, parents=True)
        deadline = time.monotonic() + self.timeout
        while True:
            _assert_safe_cache_path(self.path, self.safe_root)
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                _assert_safe_cache_path(self.path, self.safe_root)
                if self._break_stale_lock():
                    continue
                if time.monotonic() >= deadline:
                    raise DomesticModelDownloadError("等待另一个模型下载任务超时。")
                time.sleep(0.05)
                continue
            try:
                _assert_safe_cache_path(self.path, self.safe_root)
            except BaseException:
                os.close(descriptor)
                raise
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump({"pid": os.getpid(), "token": self.token}, file)
                file.flush()
                os.fsync(file.fileno())
            self.acquired = True
            return self

    def _break_stale_lock(self) -> bool:
        _assert_safe_cache_path(self.path, self.safe_root)
        try:
            stat = self.path.stat()
            data = json.loads(self.path.read_text(encoding="utf-8"))
            pid = data.get("pid") if isinstance(data, dict) else None
            stale = (
                time.time() - stat.st_mtime > LOCK_STALE_SECONDS
                or type(pid) is not int
                or not _pid_is_alive(pid)
            )
            if stale:
                self.path.unlink()
                return True
        except FileNotFoundError:
            return True
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # A fresh partially-written lock belongs to its creator.  It will
            # become stale by age if that creator crashed before completing it.
            return False
        return False

    def heartbeat(self) -> None:
        if not self.acquired:
            return
        _assert_safe_cache_path(self.path, self.safe_root)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("token") == self.token:
                os.utime(self.path, None)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass

    def __exit__(self, *_exc: object) -> None:
        if not self.acquired:
            return
        _assert_safe_cache_path(self.path, self.safe_root)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("token") == self.token:
                self.path.unlink()
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        self.acquired = False


def _write_verified_marker(staging: Path, model: str, manifest: ModelManifest) -> None:
    staging, _ = _assert_safe_cache_path(staging, staging)
    verified: dict[str, dict[str, object]] = {}
    for item in manifest.files:
        target = staging.joinpath(*PurePosixPath(item.path).parts)
        _assert_safe_cache_path(target, staging)
        if not _safe_regular_file(target):
            raise DomesticModelIntegrityError(f"模型文件在发布前消失：{item.path}")
        stat = target.stat()
        if stat.st_size != item.size or _sha256(target) != item.sha256:
            raise DomesticModelIntegrityError(f"模型文件在发布前校验失败：{item.path}")
        verified[item.path] = {
            "size": item.size,
            "sha256": item.sha256,
            "mtime_ns": stat.st_mtime_ns,
        }
    marker = {
        "schema_version": MARKER_SCHEMA_VERSION,
        "manifest": _manifest_payload(model, manifest),
        "verified_files": verified,
    }
    _atomic_write_json(staging / MARKER_FILENAME, marker, safe_root=staging)


def _discard_rejected_files(staging: Path, manifest: ModelManifest) -> None:
    """Keep rejected bytes while a retry is pending, but never publish them."""

    staging, _ = _assert_safe_cache_path(staging, staging)
    for item in manifest.files:
        target = staging.joinpath(*PurePosixPath(item.path).parts)
        candidates = (
            target.with_name(f"{target.name}.rejected"),
            target.with_name(f"{target.name}.partial.rejected"),
        )
        for candidate in candidates:
            _assert_safe_cache_path(candidate, staging)
            if candidate.is_file():
                candidate.unlink()
            elif candidate.exists():
                raise DomesticModelSecurityError(
                    f"拒绝发布包含异常缓存对象的模型：{candidate}"
                )


def _assert_staging_allowlist(staging: Path, manifest: ModelManifest) -> None:
    """Do not let stale or attacker-planted files hitchhike into the model cache."""

    staging, _ = _assert_safe_cache_path(staging, staging)
    _assert_tree_no_reparse(staging)
    allowed_files = {item.path for item in manifest.files} | {MARKER_FILENAME}
    allowed_directories: set[str] = set()
    for filename in allowed_files:
        parent = PurePosixPath(filename).parent
        while parent != PurePosixPath("."):
            allowed_directories.add(parent.as_posix())
            parent = parent.parent

    pending = [staging]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as iterator:
                entries = tuple(iterator)
        except OSError as exc:
            raise DomesticModelSecurityError(f"无法安全扫描模型暂存目录：{current}：{exc}") from exc
        for entry in entries:
            child = Path(entry.path)
            _assert_safe_cache_path(child, staging)
            relative = child.relative_to(staging).as_posix()
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise DomesticModelSecurityError(f"无法安全检查暂存对象：{child}：{exc}") from exc
            if is_directory:
                if relative not in allowed_directories:
                    raise DomesticModelSecurityError(f"模型暂存目录包含清单外目录：{relative}")
                pending.append(child)
            elif relative not in allowed_files:
                raise DomesticModelSecurityError(f"模型暂存目录包含清单外文件：{relative}")


def _remove_owned_tree(path: Path) -> None:
    _assert_tree_no_reparse(path)
    if path.exists():
        shutil.rmtree(path)


def _publish(
    staging: Path,
    destination: Path,
    *,
    safe_root: Path,
    manifest: ModelManifest,
) -> None:
    staging, safe_root = _assert_safe_cache_path(staging, safe_root)
    destination, _ = _assert_safe_cache_path(destination, safe_root)
    _mkdir_safe(destination.parent, safe_root, parents=True)
    _assert_staging_allowlist(staging, manifest)
    replaced: Path | None = None
    if destination.exists():
        _assert_tree_no_reparse(destination)
        replaced = destination.with_name(
            f".{destination.name}.replaced.{os.getpid()}.{uuid.uuid4().hex}"
        )
        _assert_safe_cache_path(replaced, safe_root)
        os.replace(destination, replaced)
        _assert_safe_cache_path(replaced, safe_root)
    try:
        _assert_safe_cache_path(staging, safe_root)
        _assert_safe_cache_path(destination, safe_root)
        os.replace(staging, destination)
        _assert_safe_cache_path(destination, safe_root)
    except BaseException:
        if replaced is not None and not destination.exists():
            _assert_safe_cache_path(replaced, safe_root)
            os.replace(replaced, destination)
        raise
    if replaced is not None:
        _remove_owned_tree(replaced)


def _download_verified_modelscope_model(
    model: str,
    cache_root: str | os.PathLike[str],
    progress: ProgressCallback | None = None,
    *,
    manifests: Mapping[str, ModelManifest] = MODELSCOPE_MODEL_MANIFESTS,
    opener: _Opener | None = None,
    timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> Path:
    manifest = _select_manifest(model, manifests)
    if timeout <= 0 or lock_timeout <= 0:
        raise ValueError("下载和锁超时必须大于 0。")
    paths = _cache_paths(cache_root, model)
    cached = _verified_directory(
        paths.destination,
        model,
        manifest,
        refresh_marker=True,
        safe_root=paths.root,
    )
    if cached is not None:
        return cached

    _mkdir_safe(paths.root, paths.root, parents=True)
    _mkdir_safe(paths.source, paths.root)
    _mkdir_safe(paths.staging_root, paths.root)
    _mkdir_safe(paths.locks_root, paths.root)
    selected_opener = opener or _build_trusted_modelscope_opener()
    with _FileLock(paths.lock, lock_timeout, safe_root=paths.root) as lock:
        cached = _verified_directory(
            paths.destination,
            model,
            manifest,
            refresh_marker=True,
            safe_root=paths.root,
        )
        if cached is not None:
            return cached
        _mkdir_safe(paths.staging, paths.root)
        for item in manifest.files:
            _download_file(
                model,
                manifest,
                item,
                paths.staging,
                opener=selected_opener,
                timeout=timeout,
                progress=progress,
                heartbeat=lock.heartbeat,
            )
        _discard_rejected_files(paths.staging, manifest)
        _write_verified_marker(paths.staging, model, manifest)
        _publish(
            paths.staging,
            paths.destination,
            safe_root=paths.root,
            manifest=manifest,
        )
        verified = _verified_directory(
            paths.destination,
            model,
            manifest,
            refresh_marker=False,
            safe_root=paths.root,
        )
        if verified is None:  # pragma: no cover - defensive postcondition
            raise DomesticModelIntegrityError("模型发布后的完整性复核失败。")
    if progress is not None:
        progress(f"ModelScope 模型 {model} 下载完成，所有文件 SHA-256 校验通过。")
    return paths.destination


def download_verified_modelscope_model(
    model: str,
    cache_root: str | os.PathLike[str],
    progress: ProgressCallback | None = None,
) -> Path:
    """Download and atomically publish a pinned faster-whisper model.

    Downloads use only HTTPS ModelScope hosts, support stable ``.partial``
    resume files, and never expose a model directory until every required file
    matches its built-in size and SHA-256 manifest.
    """

    return _download_verified_modelscope_model(model, cache_root, progress)


__all__ = [
    "MARKER_FILENAME",
    "MODELSCOPE_ENDPOINT",
    "MODELSCOPE_MODEL_MANIFESTS",
    "DomesticModelDownloadError",
    "DomesticModelError",
    "DomesticModelIntegrityError",
    "DomesticModelSecurityError",
    "ModelFile",
    "ModelManifest",
    "download_verified_modelscope_model",
    "find_verified_modelscope_model",
]
