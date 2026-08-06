from __future__ import annotations

import importlib
import json
import math
import os
import shutil
import socket
import stat
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit


class NeteaseLoginError(RuntimeError):
    """Raised when the managed NetEase login session cannot be used safely."""


class NeteaseLoginTimeoutError(NeteaseLoginError):
    """Raised when Edge starts, but the user does not finish logging in in time."""


_NETEASE_LOGIN_URL = "https://music.163.com/"
_PROFILE_PARTS = ("Karaoke Forge", "Browser", "EdgeNetease")
_PROFILE_MARKER_NAME = ".karaoke-forge-profile"
_PROFILE_MARKER_CONTENT = "karaoke-forge:netease-edge-profile:v1\n"
_DEVTOOLS_ACTIVE_PORT = "DevToolsActivePort"
_STARTUP_TIMEOUT_SECONDS = 15.0
_CDP_CALL_TIMEOUT_SECONDS = 5.0
_COOKIE_POLL_INTERVAL_SECONDS = 0.5
_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_CAPTURE_LOCK = threading.Lock()


def _local_app_data_dir() -> Path:
    value = os.environ.get("LOCALAPPDATA", "").strip()
    if not value:
        raise NeteaseLoginError("无法确定 Windows 的本地应用数据目录，暂时不能启动专用 Edge 登录。")
    return Path(os.path.abspath(os.path.normpath(str(Path(value).expanduser()))))


def _same_lexical_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
        os.path.abspath(str(right))
    )


def _managed_profile_dir() -> Path:
    local_app_data = _local_app_data_dir()
    profile = Path(os.path.abspath(str(local_app_data.joinpath(*_PROFILE_PARTS))))
    default_edge_data = Path(
        os.path.abspath(str(local_app_data / "Microsoft" / "Edge" / "User Data"))
    )
    if _same_lexical_path(profile, default_edge_data) or any(
        _same_lexical_path(parent, default_edge_data) for parent in profile.parents
    ):
        raise NeteaseLoginError("专用 Edge 资料目录配置不安全，已停止登录以保护日常浏览数据。")
    try:
        common = Path(os.path.commonpath((str(local_app_data), str(profile))))
    except ValueError as exc:
        raise NeteaseLoginError("专用 Edge 资料目录超出允许范围，已停止登录。") from exc
    if not _same_lexical_path(common, local_app_data):
        raise NeteaseLoginError("专用 Edge 资料目录超出允许范围，已停止登录。")
    return profile


def _owned_profile_layers(profile: Path) -> tuple[Path, Path, Path]:
    expected = _managed_profile_dir()
    if not _same_lexical_path(profile, expected):
        raise NeteaseLoginError("专用 Edge 资料目录不属于 Karaoke Forge，已停止操作。")
    app_root = _local_app_data_dir() / _PROFILE_PARTS[0]
    browser_root = app_root / _PROFILE_PARTS[1]
    return app_root, browser_root, expected


def _path_is_reparse_point(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _require_plain_directory(path: Path) -> None:
    if _path_is_reparse_point(path):
        raise NeteaseLoginError(
            "网易云专用 Edge 资料路径包含链接或重解析点；为保护文件，已停止操作。"
        )
    if _lexists(path) and not path.is_dir():
        raise NeteaseLoginError("网易云专用 Edge 资料路径被其他文件占用，已停止操作。")


def _ensure_plain_directory(path: Path) -> None:
    _require_plain_directory(path)
    if not _lexists(path):
        try:
            path.mkdir()
        except FileExistsError:
            pass
    _require_plain_directory(path)
    if not path.is_dir():
        raise NeteaseLoginError("无法创建网易云专用 Edge 资料目录。")


def _assert_owned_path_is_plain(profile: Path, *, include_marker: bool = True) -> None:
    layers = _owned_profile_layers(profile)
    for layer in layers:
        _require_plain_directory(layer)
    if include_marker:
        marker = profile / _PROFILE_MARKER_NAME
        if _path_is_reparse_point(marker):
            raise NeteaseLoginError("网易云专用 Edge 所有权标记不安全，已停止操作。")


def _marker_path(profile: Path) -> Path:
    return profile / _PROFILE_MARKER_NAME


def _read_matching_marker(profile: Path) -> bool:
    marker = _marker_path(profile)
    if _path_is_reparse_point(marker) or not marker.is_file():
        return False
    try:
        if marker.stat().st_size > len(_PROFILE_MARKER_CONTENT.encode("utf-8")) + 8:
            return False
        return marker.read_text(encoding="utf-8") == _PROFILE_MARKER_CONTENT
    except (OSError, UnicodeError):
        return False


def _prepare_owned_profile(profile: Path) -> None:
    layers = _owned_profile_layers(profile)
    local_app_data = _local_app_data_dir()
    local_app_data.mkdir(parents=True, exist_ok=True)
    for layer in layers:
        _ensure_plain_directory(layer)
    marker = _marker_path(profile)
    if _lexists(marker):
        if not _read_matching_marker(profile):
            raise NeteaseLoginError("网易云专用 Edge 所有权标记不匹配，已停止操作。")
        return
    try:
        has_existing_data = any(profile.iterdir())
    except OSError as exc:
        raise NeteaseLoginError("无法检查网易云专用 Edge 资料目录。") from exc
    if has_existing_data:
        raise NeteaseLoginError(
            "网易云专用 Edge 资料目录缺少所有权标记；为保护现有文件，已停止操作。"
        )
    try:
        with marker.open("x", encoding="utf-8", newline="") as handle:
            handle.write(_PROFILE_MARKER_CONTENT)
    except FileExistsError:
        pass
    except OSError as exc:
        raise NeteaseLoginError("无法创建网易云专用 Edge 所有权标记。") from exc
    _assert_owned_path_is_plain(profile)
    if not _read_matching_marker(profile):
        raise NeteaseLoginError("网易云专用 Edge 所有权标记不匹配，已停止操作。")


def _require_owned_profile(profile: Path) -> None:
    _assert_owned_path_is_plain(profile)
    if not profile.is_dir() or not _read_matching_marker(profile):
        raise NeteaseLoginError(
            "网易云专用 Edge 资料缺少有效的 Karaoke Forge 所有权标记，已停止操作。"
        )


def _registry_edge_executables() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []

    candidates: list[Path] = []
    access_modes = [winreg.KEY_READ]
    for flag_name in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
        flag = getattr(winreg, flag_name, 0)
        if flag:
            access_modes.append(winreg.KEY_READ | flag)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for access in access_modes:
            try:
                with winreg.OpenKey(
                    hive,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                    0,
                    access,
                ) as key:
                    value, _ = winreg.QueryValueEx(key, "")
            except OSError:
                continue
            if isinstance(value, str) and value.strip():
                candidates.append(Path(value.strip().strip('"')))
    return candidates


def _find_edge_executable() -> Path:
    candidates = _registry_edge_executables()
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(variable, "").strip()
        if base:
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    discovered = shutil.which("msedge.exe")
    if discovered:
        candidates.append(Path(discovered))

    seen: set[str] = set()
    for candidate in candidates:
        normalized = os.path.normcase(str(candidate.expanduser().resolve()))
        if normalized in seen:
            continue
        seen.add(normalized)
        if candidate.is_file():
            return candidate.resolve()
    raise NeteaseLoginError(
        "没有找到 Microsoft Edge。请先确认 Edge 已安装并能正常打开，然后再试一次。"
    )


def _edge_user_data_policy() -> str | None:
    """Return a mandatory Edge UserDataDir policy, if one can override our profile."""

    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None

    access_modes = [winreg.KEY_READ]
    for flag_name in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
        flag = getattr(winreg, flag_name, 0)
        if flag:
            access_modes.append(winreg.KEY_READ | flag)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for access in access_modes:
            try:
                with winreg.OpenKey(
                    hive,
                    r"SOFTWARE\Policies\Microsoft\Edge",
                    0,
                    access,
                ) as key:
                    value, _ = winreg.QueryValueEx(key, "UserDataDir")
            except OSError:
                continue
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _lock_file(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _exclusive_profile_access(profile: Path) -> Iterator[None]:
    if not _CAPTURE_LOCK.acquire(blocking=False):
        raise NeteaseLoginError("已有网易云登录任务正在进行，请先完成或关闭那个登录窗口。")
    handle = None
    locked = False
    try:
        app_root, _, _ = _owned_profile_layers(profile)
        local_app_data = _local_app_data_dir()
        local_app_data.mkdir(parents=True, exist_ok=True)
        _ensure_plain_directory(app_root)
        lock_path = app_root / ".edge-netease.lock"
        if _path_is_reparse_point(lock_path):
            raise NeteaseLoginError("网易云专用 Edge 锁文件不安全，已停止操作。")
        handle = lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        try:
            _lock_file(handle)
            locked = True
        except (BlockingIOError, OSError) as exc:
            raise NeteaseLoginError(
                "另一个 Karaoke Forge 窗口正在使用网易云登录，请完成后再试。"
            ) from exc
        _require_plain_directory(app_root)
        if _path_is_reparse_point(lock_path):
            raise NeteaseLoginError("网易云专用 Edge 锁文件不安全，已停止操作。")
        yield
    finally:
        if handle is not None:
            if locked:
                try:
                    _unlock_file(handle)
                except OSError:
                    pass
            handle.close()
        _CAPTURE_LOCK.release()


def _load_websocket_client() -> ModuleType:
    try:
        return importlib.import_module("websocket")
    except ImportError as exc:
        raise NeteaseLoginError(
            "缺少 Edge 自动登录组件 websocket-client。请重新运行“首次安装.bat”后再试。"
        ) from exc


def _remove_stale_devtools_file(profile: Path) -> None:
    port_file = profile / _DEVTOOLS_ACTIVE_PORT
    try:
        port_file.unlink(missing_ok=True)
    except OSError as exc:
        raise NeteaseLoginError(
            "无法准备网易云专用 Edge 资料。请关闭之前弹出的网易云登录窗口后再试。"
        ) from exc


def _launch_edge(
    edge: Path,
    profile: Path,
    *,
    headless: bool = False,
) -> subprocess.Popen[Any]:
    _require_owned_profile(profile)
    if _devtools_port_is_open(profile):
        raise NeteaseLoginError(
            "网易云专用 Edge 登录窗口已经打开。请先关闭那个窗口，再重新点击登录。"
        )
    _remove_stale_devtools_file(profile)
    command = [
        str(edge),
        f"--user-data-dir={profile}",
        "--profile-directory=Default",
        "--remote-debugging-port=0",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
    ]
    if headless:
        command.extend(("--headless=new", "--disable-gpu", "about:blank"))
    else:
        command.extend(("--window-size=1080,760", f"--app={_NETEASE_LOGIN_URL}"))
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise NeteaseLoginError(
            "Microsoft Edge 启动失败。请确认 Edge 能正常打开，然后再试一次。"
        ) from exc


def _parse_devtools_endpoint(content: str) -> str | None:
    lines = content.splitlines()
    if len(lines) < 2:
        return None
    try:
        port = int(lines[0].strip())
    except ValueError:
        return None
    browser_path = lines[1].strip()
    if not 1 <= port <= 65535:
        return None
    prefix = "/devtools/browser/"
    identifier = browser_path.removeprefix(prefix)
    if not browser_path.startswith(prefix) or not identifier:
        return None
    if any(
        character not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-"
        for character in identifier
    ):
        return None
    return f"ws://127.0.0.1:{port}{browser_path}"


def _wait_for_devtools_endpoint(
    profile: Path,
    process: subprocess.Popen[Any],
    deadline: float,
) -> str:
    port_file = profile / _DEVTOOLS_ACTIVE_PORT
    while time.monotonic() < deadline:
        try:
            endpoint = _parse_devtools_endpoint(port_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, PermissionError, OSError, UnicodeError):
            endpoint = None
        if endpoint:
            return endpoint
        if process.poll() is not None:
            raise NeteaseLoginError(
                "网易云专用 Edge 提前退出。请检查 Edge 是否被安全软件或系统策略阻止。"
            )
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    raise NeteaseLoginTimeoutError(
        "Microsoft Edge 启动超时。请关闭刚才弹出的网易云登录窗口后再试。"
    )


class _CdpClient:
    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._next_id = 0

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params:
            message["params"] = params
        try:
            self._connection.send(json.dumps(message, separators=(",", ":")))
        except Exception as exc:
            raise NeteaseLoginError("Edge 登录连接已中断，请重新打开登录窗口。") from exc
        while True:
            try:
                raw_response = self._connection.recv()
            except Exception as exc:
                raise NeteaseLoginError("Edge 登录连接已中断，请重新打开登录窗口。") from exc
            try:
                response = json.loads(raw_response)
            except (TypeError, json.JSONDecodeError) as exc:
                raise NeteaseLoginError("Edge 登录通道返回了无法识别的数据。") from exc
            if not isinstance(response, dict) or response.get("id") != request_id:
                continue
            error = response.get("error")
            if isinstance(error, dict):
                message_text = str(error.get("message") or "未知错误")
                raise NeteaseLoginError(f"Edge 登录通道调用失败：{message_text}")
            result = response.get("result", {})
            if not isinstance(result, dict):
                raise NeteaseLoginError("Edge 登录通道返回的数据格式不正确。")
            return result

    def close(self) -> None:
        self._connection.close()


def _connect_cdp(websocket_module: ModuleType, endpoint: str, timeout: float) -> _CdpClient:
    parsed = urlsplit(endpoint)
    try:
        port = parsed.port
    except ValueError as exc:
        raise NeteaseLoginError("Edge 登录端点格式不安全，已停止连接。") from exc
    if (
        parsed.scheme != "ws"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1 <= port <= 65535
        or not parsed.path.startswith("/devtools/browser/")
        or parsed.query
        or parsed.fragment
    ):
        raise NeteaseLoginError("Edge 登录端点格式不安全，已停止连接。")
    identifier = parsed.path.removeprefix("/devtools/browser/")
    if not identifier or any(
        character not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-"
        for character in identifier
    ):
        raise NeteaseLoginError("Edge 登录端点格式不安全，已停止连接。")
    connection_timeout = max(0.1, min(_CDP_CALL_TIMEOUT_SECONDS, timeout))
    raw_socket: socket.socket | None = None
    try:
        raw_socket = socket.create_connection(
            ("127.0.0.1", port),
            timeout=connection_timeout,
        )
        connection = websocket_module.create_connection(
            endpoint,
            timeout=connection_timeout,
            suppress_origin=True,
            socket=raw_socket,
        )
    except Exception as exc:
        if raw_socket is not None:
            try:
                raw_socket.close()
            except OSError:
                pass
        raise NeteaseLoginError("无法连接网易云专用 Edge。请关闭弹出的登录窗口后再试。") from exc
    return _CdpClient(connection)


def _verify_edge(client: _CdpClient) -> None:
    version = client.call("Browser.getVersion")
    product = str(version.get("product") or "")
    if not product.lower().startswith(("edg/", "microsoft edge")):
        raise NeteaseLoginError("连接到的不是 Microsoft Edge，已停止读取登录状态。")


def _extract_music_u(result: dict[str, Any]) -> str | None:
    cookies = result.get("cookies")
    if not isinstance(cookies, list):
        raise NeteaseLoginError("Edge 返回的登录状态格式不正确。")
    for cookie in cookies:
        if not isinstance(cookie, dict) or cookie.get("name") != "MUSIC_U":
            continue
        domain = str(cookie.get("domain") or "").strip().lower().lstrip(".")
        if domain != "music.163.com":
            continue
        expires = cookie.get("expires")
        if (
            not isinstance(expires, bool)
            and isinstance(expires, (int, float))
            and math.isfinite(float(expires))
            and float(expires) >= 0
            and float(expires) <= time.time()
        ):
            continue
        value = cookie.get("value")
        if not isinstance(value, str):
            continue
        if len(value) > 16_384 or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise NeteaseLoginError(
                "Edge 返回的网易云登录状态格式无效，请退出网易云账号后重新登录。"
            )
        value = value.strip()
        if (
            not value
            or len(value) > 4096
            or ";" in value
            or any(character.isspace() or ord(character) == 127 for character in value)
        ):
            raise NeteaseLoginError(
                "Edge 返回的网易云登录状态格式无效，请退出网易云账号后重新登录。"
            )
        return value
    return None


def _close_managed_edge(
    client: _CdpClient | None,
    process: subprocess.Popen[Any] | None,
) -> None:
    if client is not None:
        try:
            client.call("Browser.close")
        except Exception:  # noqa: BLE001, S110 - CDP often closes before replying
            pass
        try:
            client.close()
        except Exception:  # noqa: BLE001, S110 - preserve the useful login result/error
            pass
    if process is None:
        return
    try:
        process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        return
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        process.terminate()
        process.wait(timeout=2.0)
    except (subprocess.TimeoutExpired, OSError):
        try:
            process.kill()
        except OSError:
            pass


def _validated_timeout(timeout_seconds: float) -> float:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise TypeError("timeout_seconds 必须是大于 0 的数字。")
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds 必须是大于 0 的有限数字。")
    return timeout


def _capture_from_owned_profile(
    profile: Path,
    timeout: float,
    *,
    interactive: bool,
) -> str | None:
    if _edge_user_data_policy() is not None:
        raise NeteaseLoginError(
            "这台电脑的系统策略会覆盖 Edge 资料目录。为了保护日常 Edge 数据，"
            "自动登录已停止，请联系电脑管理员或使用高级登录方式。"
        )
    edge = _find_edge_executable()
    websocket_module = _load_websocket_client()
    deadline = time.monotonic() + timeout
    process: subprocess.Popen[Any] | None = None
    client: _CdpClient | None = None
    try:
        process = (
            _launch_edge(edge, profile)
            if interactive
            else _launch_edge(edge, profile, headless=True)
        )
        startup_deadline = min(deadline, time.monotonic() + _STARTUP_TIMEOUT_SECONDS)
        endpoint = _wait_for_devtools_endpoint(profile, process, startup_deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if not interactive:
                return None
            raise NeteaseLoginTimeoutError(
                "等待网易云登录超时。请重新点击登录，并在弹出的 Edge 中完成登录。"
            )
        client = _connect_cdp(websocket_module, endpoint, remaining)
        _verify_edge(client)
        if not interactive:
            return _extract_music_u(client.call("Storage.getCookies"))
        while time.monotonic() < deadline:
            music_u = _extract_music_u(client.call("Storage.getCookies"))
            if music_u:
                return music_u
            time.sleep(
                min(
                    _COOKIE_POLL_INTERVAL_SECONDS,
                    max(0.0, deadline - time.monotonic()),
                )
            )
        raise NeteaseLoginTimeoutError(
            "等待网易云登录超时。请重新点击登录，并在弹出的 Edge 中完成登录。"
        )
    finally:
        _close_managed_edge(client, process)


def managed_netease_profile_exists() -> bool:
    """Return whether Karaoke Forge already owns a reusable Edge profile."""

    profile = _managed_profile_dir()
    _assert_owned_path_is_plain(profile)
    if not _lexists(profile):
        return False
    _require_owned_profile(profile)
    return True


def try_reuse_netease_music_u(timeout_seconds: float = 15.0) -> str | None:
    """Silently reuse a non-expired login from Karaoke Forge's own Edge profile."""

    timeout = _validated_timeout(timeout_seconds)
    profile = _managed_profile_dir()
    _assert_owned_path_is_plain(profile)
    if not _lexists(profile):
        return None
    _require_owned_profile(profile)
    with _exclusive_profile_access(profile):
        _require_owned_profile(profile)
        return _capture_from_owned_profile(profile, timeout, interactive=False)


def acquire_netease_music_u(
    reuse_timeout_seconds: float = 15.0,
    login_timeout_seconds: float = 300.0,
) -> str:
    """Reuse the saved login, opening the official login page only after it expires."""

    music_u = try_reuse_netease_music_u(reuse_timeout_seconds)
    if music_u:
        return music_u
    return capture_netease_music_u(login_timeout_seconds)


def capture_netease_music_u(timeout_seconds: float = 300.0) -> str:
    """Open an isolated Edge profile and return its NetEase MUSIC_U cookie.

    The managed profile is deliberately separate from the user's normal Edge data. The
    remote-debugging endpoint is bound to a random loopback port and is closed before this
    function returns or raises.
    """

    timeout = _validated_timeout(timeout_seconds)

    profile = _managed_profile_dir()
    with _exclusive_profile_access(profile):
        _prepare_owned_profile(profile)
        music_u = _capture_from_owned_profile(profile, timeout, interactive=True)
        if not music_u:  # pragma: no cover - interactive capture only returns or raises
            raise NeteaseLoginTimeoutError("等待网易云登录超时。")
        return music_u


def _devtools_port_is_open(profile: Path) -> bool:
    port_file = profile / _DEVTOOLS_ACTIVE_PORT
    try:
        endpoint = _parse_devtools_endpoint(port_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, OSError, UnicodeError):
        return False
    if not endpoint:
        return False
    port_text = endpoint.split(":", 2)[2].split("/", 1)[0]
    try:
        with socket.create_connection(("127.0.0.1", int(port_text)), timeout=0.2):
            return True
    except OSError:
        return False


def clear_netease_login_profile() -> str:
    """Delete only Karaoke Forge's dedicated Edge profile and its saved login state."""

    profile = _managed_profile_dir()
    with _exclusive_profile_access(profile):
        _assert_owned_path_is_plain(profile)
        if not _lexists(profile):
            return "网易云专用登录资料不存在，无需清理。"
        _require_owned_profile(profile)
        if _devtools_port_is_open(profile):
            raise NeteaseLoginError("网易云登录窗口仍在运行，请关闭后再清理登录资料。")
        _require_owned_profile(profile)
        try:
            shutil.rmtree(profile)
        except OSError as exc:
            raise NeteaseLoginError("无法清理网易云登录资料。请关闭网易云登录窗口后再试。") from exc
    return "已清除网易云专用登录资料；下次使用时需要重新登录。"
