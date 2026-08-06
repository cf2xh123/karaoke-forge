from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

SETTINGS_SCHEMA_VERSION = 1
MAX_SETTINGS_BYTES = 64 * 1024
OFFICIAL_HF_ENDPOINT = "https://huggingface.co"
APPROVED_MIRROR_ENDPOINT = "https://hf-mirror.com"
DOMESTIC_MODELSCOPE_ENDPOINT = "https://modelscope.cn"
DEFAULT_ETAG_TIMEOUT_SECONDS = 10
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 120

_MANAGED_ENVIRONMENT_KEYS = (
    "HF_ENDPOINT",
    "HF_HOME",
    "HF_HUB_CACHE",
    "HF_ASSETS_CACHE",
    "HF_HUB_ETAG_TIMEOUT",
    "HF_HUB_DOWNLOAD_TIMEOUT",
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN",
    "HF_HUB_DISABLE_XET",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "HF_TOKEN_PATH",
    "HF_DEBUG",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)

ModelDownloadMode = Literal["modelscope", "official", "proxy", "mirror", "offline"]
_VALID_MODES = frozenset({"modelscope", "official", "proxy", "mirror", "offline"})
_SETTINGS_FIELDS = frozenset(
    {
        "schema_version",
        "mode",
        "proxy_url",
        "mirror_endpoint",
        "mirror_confirmed",
    }
)


class NetworkSettingsError(ValueError):
    """Raised when the persisted model-download network settings are unsafe or invalid."""


def _validate_proxy_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NetworkSettingsError("代理地址不能为空。")
    raw = value.strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise NetworkSettingsError("代理地址中的端口无效。") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise NetworkSettingsError("代理地址只支持 http:// 或 https://。")
    if not parsed.hostname:
        raise NetworkSettingsError("代理地址缺少主机名。")
    if parsed.username is not None or parsed.password is not None:
        raise NetworkSettingsError("代理地址不能包含账号或密码；设置文件不会保存秘密。")
    if parsed.query or parsed.fragment:
        raise NetworkSettingsError("代理地址不能包含查询参数或片段。")
    if parsed.path not in {"", "/"}:
        raise NetworkSettingsError("代理地址不能包含路径。")

    hostname = parsed.hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname if port is None else f"{hostname}:{port}"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


@dataclass(frozen=True)
class ModelDownloadSettings:
    """Validated, non-secret settings for downloading speech-recognition models."""

    mode: ModelDownloadMode = "modelscope"
    proxy_url: str | None = None
    mirror_endpoint: str | None = None
    mirror_confirmed: bool = False
    schema_version: int = SETTINGS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SETTINGS_SCHEMA_VERSION:
            raise NetworkSettingsError(f"不支持的模型下载设置版本：{self.schema_version!r}。")
        if not isinstance(self.mode, str) or self.mode not in _VALID_MODES:
            raise NetworkSettingsError(f"不支持的模型下载模式：{self.mode!r}。")
        if self.proxy_url is not None and not isinstance(self.proxy_url, str):
            raise NetworkSettingsError("proxy_url 必须是字符串或 null。")
        if self.mirror_endpoint is not None and not isinstance(self.mirror_endpoint, str):
            raise NetworkSettingsError("mirror_endpoint 必须是字符串或 null。")
        if type(self.mirror_confirmed) is not bool:
            raise NetworkSettingsError("mirror_confirmed 必须是布尔值。")

        if self.mode == "proxy":
            normalized = _validate_proxy_url(self.proxy_url or "")
            object.__setattr__(self, "proxy_url", normalized)
            if self.mirror_endpoint is not None or self.mirror_confirmed:
                raise NetworkSettingsError("代理模式不能同时启用第三方镜像。")
        elif self.mode == "mirror":
            if self.proxy_url is not None:
                raise NetworkSettingsError("镜像模式不能同时保存代理地址。")
            if self.mirror_endpoint != APPROVED_MIRROR_ENDPOINT:
                raise NetworkSettingsError(
                    f"只允许使用经过限定的镜像：{APPROVED_MIRROR_ENDPOINT}。"
                )
            if not self.mirror_confirmed:
                raise NetworkSettingsError("启用第三方镜像前必须显式确认。")
        elif (
            self.proxy_url is not None or self.mirror_endpoint is not None or self.mirror_confirmed
        ):
            raise NetworkSettingsError(f"{self.mode} 模式不能包含代理或镜像设置。")

    @property
    def endpoint(self) -> str:
        if self.mode == "modelscope":
            return DOMESTIC_MODELSCOPE_ENDPOINT
        if self.mode == "mirror":
            return APPROVED_MIRROR_ENDPOINT
        return OFFICIAL_HF_ENDPOINT

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "proxy_url": self.proxy_url,
            "mirror_endpoint": self.mirror_endpoint,
            "mirror_confirmed": self.mirror_confirmed,
        }


def _settings_directory(
    settings_dir: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    if settings_dir is not None:
        return Path(settings_dir).expanduser()
    environment = os.environ if environ is None else environ
    configured = environment.get("KARAOKE_FORGE_SETTINGS_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    local_app_data = environment.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data).expanduser() / "KaraokeForge"
    xdg_config = environment.get("XDG_CONFIG_HOME", "").strip()
    if xdg_config:
        return Path(xdg_config).expanduser() / "karaoke-forge"
    return Path.home() / ".config" / "karaoke-forge"


def settings_file_path(
    settings_dir: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the per-user settings file, with an override useful to tests and portable runs."""

    return _settings_directory(settings_dir, environ=environ) / "model-download.json"


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NetworkSettingsError(f"模型下载设置包含重复字段：{key}。")
        result[key] = value
    return result


def _settings_from_dict(data: object) -> ModelDownloadSettings:
    if not isinstance(data, dict):
        raise NetworkSettingsError("模型下载设置必须是 JSON 对象。")
    fields = frozenset(data)
    missing = _SETTINGS_FIELDS - fields
    unknown = fields - _SETTINGS_FIELDS
    if missing:
        raise NetworkSettingsError(f"模型下载设置缺少字段：{', '.join(sorted(missing))}。")
    if unknown:
        raise NetworkSettingsError(f"模型下载设置包含未知字段：{', '.join(sorted(unknown))}。")
    return ModelDownloadSettings(
        schema_version=data["schema_version"],
        mode=data["mode"],
        proxy_url=data["proxy_url"],
        mirror_endpoint=data["mirror_endpoint"],
        mirror_confirmed=data["mirror_confirmed"],
    )


def load_model_download_settings(
    settings_dir: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ModelDownloadSettings:
    """Load strict per-user settings; a missing file safely means the official endpoint."""

    path = settings_file_path(settings_dir, environ=environ)
    try:
        with path.open("rb") as file:
            payload = file.read(MAX_SETTINGS_BYTES + 1)
    except FileNotFoundError:
        return ModelDownloadSettings()
    except OSError as exc:
        raise NetworkSettingsError(f"无法读取模型下载设置：{exc}") from exc
    if len(payload) > MAX_SETTINGS_BYTES:
        raise NetworkSettingsError("模型下载设置超过 64 KiB 安全上限。")
    try:
        text = payload.decode("utf-8")
        data = json.loads(text, object_pairs_hook=_strict_json_object)
    except UnicodeDecodeError as exc:
        raise NetworkSettingsError("模型下载设置不是有效的 UTF-8 文件。") from exc
    except json.JSONDecodeError as exc:
        raise NetworkSettingsError(f"模型下载设置不是有效的 JSON：{exc.msg}。") from exc
    return _settings_from_dict(data)


def save_model_download_settings(
    settings: ModelDownloadSettings,
    settings_dir: str | os.PathLike[str] | None = None,
    *,
    confirm_mirror: bool = False,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Atomically save non-secret settings, requiring fresh consent for a mirror write."""

    if not isinstance(settings, ModelDownloadSettings):
        raise TypeError("settings 必须是 ModelDownloadSettings。")
    if settings.mode == "mirror" and confirm_mirror is not True:
        raise NetworkSettingsError("保存第三方镜像设置时必须传入 confirm_mirror=True。")
    payload = (json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_SETTINGS_BYTES:
        raise NetworkSettingsError("模型下载设置超过 64 KiB 安全上限。")

    path = settings_file_path(settings_dir, environ=environ)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    except OSError as exc:
        raise NetworkSettingsError(f"无法保存模型下载设置：{exc}") from exc
    return path


def configure_model_download_settings(
    mode: ModelDownloadMode,
    *,
    proxy_url: str | None = None,
    confirm_mirror: bool = False,
    settings_dir: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ModelDownloadSettings:
    """Validate and persist one of the four user-facing modes."""

    if mode == "mirror":
        if confirm_mirror is not True:
            raise NetworkSettingsError("启用第三方镜像前必须显式确认。")
        settings = ModelDownloadSettings(
            mode="mirror",
            mirror_endpoint=APPROVED_MIRROR_ENDPOINT,
            mirror_confirmed=True,
        )
    elif mode == "proxy":
        settings = ModelDownloadSettings(mode="proxy", proxy_url=proxy_url)
    else:
        settings = ModelDownloadSettings(mode=mode, proxy_url=proxy_url)
    save_model_download_settings(
        settings,
        settings_dir,
        confirm_mirror=confirm_mirror,
        environ=environ,
    )
    return settings


def _endpoint_cache_name(settings: ModelDownloadSettings) -> str:
    hostname = urllib.parse.urlsplit(settings.endpoint).hostname or "unknown"
    readable = "".join(char if char.isalnum() else "-" for char in hostname.lower()).strip("-")
    digest = hashlib.sha256(settings.endpoint.encode("utf-8")).hexdigest()[:12]
    return f"{readable}-{digest}"


def model_cache_directory(
    settings: ModelDownloadSettings | None = None,
    settings_dir: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return a per-user cache root isolated by the selected endpoint."""

    selected = settings or load_model_download_settings(settings_dir, environ=environ)
    return (
        _settings_directory(settings_dir, environ=environ)
        / "model-cache"
        / _endpoint_cache_name(selected)
    )


@dataclass(frozen=True)
class AppliedModelDownloadEnvironment:
    settings: ModelDownloadSettings
    endpoint: str
    cache_directory: Path
    updated: tuple[str, ...]
    removed: tuple[str, ...]


def apply_model_download_environment(
    settings: ModelDownloadSettings | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
    settings_dir: str | os.PathLike[str] | None = None,
) -> AppliedModelDownloadEnvironment:
    """Apply settings before importing faster-whisper or huggingface_hub."""

    environment = os.environ if environ is None else environ
    selected = settings or load_model_download_settings(settings_dir, environ=environment)
    cache = model_cache_directory(selected, settings_dir, environ=environment)
    cache.mkdir(parents=True, exist_ok=True)

    hub_endpoint = selected.endpoint if selected.mode == "mirror" else OFFICIAL_HF_ENDPOINT
    updates = {
        "HF_ENDPOINT": hub_endpoint,
        "HF_HOME": str(cache),
        "HF_HUB_CACHE": str(cache / "hub"),
        "HF_ASSETS_CACHE": str(cache / "assets"),
        "HF_HUB_ETAG_TIMEOUT": str(DEFAULT_ETAG_TIMEOUT_SECONDS),
        "HF_HUB_DOWNLOAD_TIMEOUT": str(DEFAULT_DOWNLOAD_TIMEOUT_SECONDS),
    }
    removed: list[str] = []

    proxy_keys = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    if selected.mode == "proxy":
        assert selected.proxy_url is not None
        updates.update({key: selected.proxy_url for key in proxy_keys})
    else:
        for key in proxy_keys:
            if key in environment:
                environment.pop(key, None)
                removed.append(key)

    if selected.mode == "offline":
        updates["HF_HUB_OFFLINE"] = "1"
        updates["TRANSFORMERS_OFFLINE"] = "1"
    else:
        for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
            if key in environment:
                environment.pop(key, None)
                removed.append(key)

    if selected.mode == "mirror":
        updates["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        updates["HF_HUB_DISABLE_XET"] = "1"
        for key in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_TOKEN_PATH", "HF_DEBUG"):
            if key in environment:
                environment.pop(key, None)
                removed.append(key)
    else:
        for key in ("HF_HUB_DISABLE_IMPLICIT_TOKEN", "HF_HUB_DISABLE_XET"):
            if key in environment:
                environment.pop(key, None)
                removed.append(key)

    environment.update(updates)
    return AppliedModelDownloadEnvironment(
        settings=selected,
        endpoint=selected.endpoint,
        cache_directory=cache,
        updated=tuple(sorted(updates)),
        removed=tuple(sorted(set(removed))),
    )


@contextmanager
def model_download_environment(
    settings: ModelDownloadSettings | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
    settings_dir: str | os.PathLike[str] | None = None,
) -> Iterator[AppliedModelDownloadEnvironment]:
    """Temporarily scope Hub/proxy variables to a model load or pre-download."""

    environment = os.environ if environ is None else environ
    missing = object()
    previous: dict[str, object] = {
        key: environment.get(key, missing) for key in _MANAGED_ENVIRONMENT_KEYS
    }
    try:
        applied = apply_model_download_environment(
            settings,
            environ=environment,
            settings_dir=settings_dir,
        )
        yield applied
    finally:
        for key, value in previous.items():
            if value is missing:
                environment.pop(key, None)
            else:
                environment[key] = str(value)


@dataclass(frozen=True)
class ModelDownloadStatus:
    mode: ModelDownloadMode
    label_zh: str
    detail_zh: str
    endpoint: str
    cache_directory: Path
    third_party: bool


def describe_model_download_settings(
    settings: ModelDownloadSettings | None = None,
    *,
    settings_dir: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ModelDownloadStatus:
    selected = settings or load_model_download_settings(settings_dir, environ=environ)
    cache = model_cache_directory(selected, settings_dir, environ=environ)
    if selected.mode == "proxy":
        label = "官方 Hugging Face（本机代理）"
        detail = f"经 {selected.proxy_url} 访问官方源；失败时不会自动切换第三方镜像。"
    elif selected.mode == "modelscope":
        label = "国内直连（ModelScope 魔搭）"
        detail = (
            "从 ModelScope 国内节点下载公开模型；每个文件都会按 Karaoke Forge "
            "内置的官方版本大小和 SHA-256 校验，通过后才会加载。"
        )
    elif selected.mode == "mirror":
        label = "第三方镜像（已确认）"
        detail = (
            f"使用 {APPROVED_MIRROR_ENDPOINT}；不会发送本机 Hugging Face 令牌，"
            "并与官方源使用不同缓存。"
        )
    elif selected.mode == "offline":
        label = "仅使用本机缓存"
        detail = "完全离线，可读取已校验的 ModelScope 或官方源缓存；缺少模型时会直接提示。"
    else:
        label = "官方 Hugging Face"
        detail = "直接访问官方源；失败时不会自动切换第三方镜像。"
    return ModelDownloadStatus(
        mode=selected.mode,
        label_zh=label,
        detail_zh=detail,
        endpoint=selected.endpoint,
        cache_directory=cache,
        third_party=selected.mode in {"modelscope", "mirror"},
    )


def model_download_status_markdown(
    settings: ModelDownloadSettings | None = None,
    *,
    settings_dir: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    status = describe_model_download_settings(
        settings,
        settings_dir=settings_dir,
        environ=environ,
    )
    return (
        f"**模型下载：{status.label_zh}**  \n{status.detail_zh}  \n缓存：`{status.cache_directory}`"
    )


class _UrlOpener(Protocol):
    def open(self, request: urllib.request.Request, timeout: float) -> Any: ...


@dataclass(frozen=True)
class ModelDownloadNetworkTest:
    ok: bool
    code: str
    summary_zh: str
    detail_zh: str
    endpoint: str
    elapsed_ms: int
    status_code: int | None = None


def test_model_download_network(
    settings: ModelDownloadSettings | None = None,
    *,
    timeout: float = 5.0,
    settings_dir: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
    opener: _UrlOpener | None = None,
) -> ModelDownloadNetworkTest:
    """Probe only the selected endpoint. It deliberately has no mirror fallback."""

    selected = settings or load_model_download_settings(settings_dir, environ=environ)
    if selected.mode == "offline":
        return ModelDownloadNetworkTest(
            ok=True,
            code="offline",
            summary_zh="已启用离线模式",
            detail_zh="未发起网络请求，可使用已校验的 ModelScope 或官方源缓存。",
            endpoint=selected.endpoint,
            elapsed_ms=0,
        )
    if timeout <= 0 or timeout > 60:
        raise ValueError("timeout 必须大于 0 且不超过 60 秒。")

    proxy_map: dict[str, str] = {}
    if selected.mode == "proxy":
        assert selected.proxy_url is not None
        proxy_map = {"http": selected.proxy_url, "https": selected.proxy_url}
    selected_opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler(proxy_map))
    if selected.mode == "modelscope":
        probe_url = f"{selected.endpoint}/api/v1/models/Systran/faster-whisper-small"
    else:
        probe_url = f"{selected.endpoint}/api/models/Systran/faster-whisper-small"
    request = urllib.request.Request(
        probe_url,
        headers={"Accept": "application/json", "User-Agent": "Karaoke-Forge/network-check"},
        method="GET",
    )
    started = time.monotonic()
    try:
        response = selected_opener.open(request, timeout=timeout)
        try:
            status_code = int(getattr(response, "status", 200))
            response.read(1)
        finally:
            response.close()
        elapsed = round((time.monotonic() - started) * 1000)
        if 200 <= status_code < 400:
            return ModelDownloadNetworkTest(
                ok=True,
                code="reachable",
                summary_zh="模型下载网络可用",
                detail_zh=f"已连接 {selected.endpoint}，耗时 {elapsed} 毫秒。",
                endpoint=selected.endpoint,
                elapsed_ms=elapsed,
                status_code=status_code,
            )
        detail = f"目标返回 HTTP {status_code}；未尝试其他下载源。"
    except urllib.error.HTTPError as exc:
        elapsed = round((time.monotonic() - started) * 1000)
        status_code = exc.code
        detail = f"目标返回 HTTP {exc.code}；未尝试其他下载源。"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        elapsed = round((time.monotonic() - started) * 1000)
        status_code = None
        reason = str(getattr(exc, "reason", exc)).replace("\n", " ")[:300]
        detail = f"连接失败：{reason}。未尝试其他下载源。"
    return ModelDownloadNetworkTest(
        ok=False,
        code="unreachable",
        summary_zh="模型下载网络不可用",
        detail_zh=detail,
        endpoint=selected.endpoint,
        elapsed_ms=elapsed,
        status_code=status_code,
    )


@dataclass(frozen=True)
class DetectedLocalProxy:
    url: str
    source_zh: str


def _is_loopback_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _default_port_probe(host: str, port: int, timeout: float) -> bool:
    try:
        connection = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return False
    connection.close()
    return True


def auto_detect_local_proxies(
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float = 0.08,
    port_probe: Callable[[str, int, float], bool] | None = None,
) -> tuple[DetectedLocalProxy, ...]:
    """Return opt-in local proxy candidates; never returns or enables a mirror."""

    if timeout <= 0 or timeout > 2:
        raise ValueError("timeout 必须大于 0 且不超过 2 秒。")
    environment = os.environ if environ is None else environ
    results: list[DetectedLocalProxy] = []
    seen: set[str] = set()

    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        raw = environment.get(key, "").strip()
        if not raw:
            continue
        try:
            normalized = _validate_proxy_url(raw)
        except NetworkSettingsError:
            continue
        hostname = urllib.parse.urlsplit(normalized).hostname
        if _is_loopback_hostname(hostname) and normalized not in seen:
            seen.add(normalized)
            results.append(DetectedLocalProxy(normalized, f"环境变量 {key}"))

    probe = port_probe or _default_port_probe
    common_ports = (
        (7890, "常见 Clash HTTP 端口"),
        (7897, "常见 Clash Verge HTTP 端口"),
        (10809, "常见 v2rayN HTTP 端口"),
        (20171, "常见本机 HTTP 代理端口"),
    )
    for port, source in common_ports:
        candidate = f"http://127.0.0.1:{port}"
        if candidate in seen or not probe("127.0.0.1", port, timeout):
            continue
        seen.add(candidate)
        results.append(DetectedLocalProxy(candidate, source))
    return tuple(results)
