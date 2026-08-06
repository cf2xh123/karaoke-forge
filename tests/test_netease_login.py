import json
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from karaoke_forge import netease_login as login


class _FakeProcess:
    def __init__(self, *, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.waited = False
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.waited = True
        return self.returncode or 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = 0


class _FakeCdpClient:
    def __init__(
        self,
        cookie_results: list[dict[str, object]] | None = None,
        *,
        product: str = "Edg/140.0.0.0",
    ) -> None:
        self.cookie_results = list(cookie_results or [])
        self.product = product
        self.calls: list[str] = []
        self.closed = False

    def call(self, method: str, params=None) -> dict[str, object]:
        del params
        self.calls.append(method)
        if method == "Browser.getVersion":
            return {"product": self.product}
        if method == "Storage.getCookies":
            if self.cookie_results:
                return self.cookie_results.pop(0)
            return {"cookies": []}
        return {}

    def close(self) -> None:
        self.closed = True


def _prepare_capture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: _FakeCdpClient,
    *,
    process: _FakeProcess | None = None,
) -> _FakeProcess:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setattr(login, "_edge_user_data_policy", lambda: None)
    monkeypatch.setattr(login, "_find_edge_executable", lambda: tmp_path / "msedge.exe")
    monkeypatch.setattr(login, "_load_websocket_client", lambda: ModuleType("websocket"))
    fake_process = process or _FakeProcess()

    def launch(_edge: Path, profile: Path) -> _FakeProcess:
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "DevToolsActivePort").write_text(
            "43123\n/devtools/browser/1234-abcd\n",
            encoding="utf-8",
        )
        return fake_process

    monkeypatch.setattr(login, "_launch_edge", launch)
    monkeypatch.setattr(login, "_connect_cdp", lambda *_args, **_kwargs: client)
    return fake_process


def test_capture_returns_only_exact_netease_music_u_and_closes_edge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeCdpClient(
        [
            {
                "cookies": [
                    {
                        "name": "MUSIC_U",
                        "domain": "evil.music.163.com",
                        "value": "must-not-leak",
                    },
                    {"name": "MUSIC_U", "domain": ".music.163.com", "value": "safe-token"},
                    {"name": "OTHER", "domain": ".music.163.com", "value": "ignored"},
                ]
            }
        ]
    )
    process = _prepare_capture(monkeypatch, tmp_path, client)

    assert login.capture_netease_music_u(timeout_seconds=1.0) == "safe-token"
    assert client.calls == ["Browser.getVersion", "Storage.getCookies", "Browser.close"]
    assert client.closed is True
    assert process.waited is True
    assert process.terminated is False
    profile = login._managed_profile_dir()
    assert (profile / login._PROFILE_MARKER_NAME).read_text(encoding="utf-8") == (
        login._PROFILE_MARKER_CONTENT
    )


def test_capture_waits_for_login_then_returns_cookie(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeCdpClient(
        [
            {"cookies": []},
            {"cookies": [{"name": "MUSIC_U", "domain": "music.163.com", "value": "later"}]},
        ]
    )
    _prepare_capture(monkeypatch, tmp_path, client)
    monkeypatch.setattr(login, "_COOKIE_POLL_INTERVAL_SECONDS", 0.0)

    assert login.capture_netease_music_u(timeout_seconds=1.0) == "later"
    assert client.calls.count("Storage.getCookies") == 2
    assert client.calls[-1] == "Browser.close"


def test_capture_timeout_still_uses_browser_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeCdpClient()
    process = _prepare_capture(monkeypatch, tmp_path, client)

    with pytest.raises(login.NeteaseLoginTimeoutError, match="等待网易云登录超时"):
        login.capture_netease_music_u(timeout_seconds=0.02)

    assert "Browser.close" in client.calls
    assert client.closed is True
    assert process.waited is True


def test_capture_rejects_non_edge_endpoint_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeCdpClient(product="Chrome/140.0.0.0")
    _prepare_capture(monkeypatch, tmp_path, client)

    with pytest.raises(login.NeteaseLoginError, match="不是 Microsoft Edge"):
        login.capture_netease_music_u(timeout_seconds=1.0)

    assert client.calls[-1] == "Browser.close"
    assert client.closed is True


def test_capture_stops_when_policy_can_override_managed_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(login, "_edge_user_data_policy", lambda: "${local_app_data}/Edge")

    def unexpected_edge_lookup() -> Path:
        raise AssertionError("Edge must not be launched when UserDataDir policy is mandatory")

    monkeypatch.setattr(login, "_find_edge_executable", unexpected_edge_lookup)
    with pytest.raises(login.NeteaseLoginError, match="保护日常 Edge 数据"):
        login.capture_netease_music_u(timeout_seconds=1.0)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_capture_rejects_invalid_timeout_value(timeout) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        login.capture_netease_music_u(timeout)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [True, "30"])
def test_capture_rejects_invalid_timeout_type(timeout) -> None:
    with pytest.raises(TypeError, match="timeout_seconds"):
        login.capture_netease_music_u(timeout)  # type: ignore[arg-type]


def test_websocket_client_is_a_lazy_friendly_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_name: str):
        raise ImportError("not installed")

    monkeypatch.setattr(login.importlib, "import_module", missing)
    with pytest.raises(login.NeteaseLoginError, match="websocket-client"):
        login._load_websocket_client()


def test_launch_uses_only_isolated_profile_and_random_loopback_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    profile = login._managed_profile_dir()
    login._prepare_owned_profile(profile)
    commands: list[list[str]] = []
    process = _FakeProcess()

    def fake_popen(command, **kwargs):
        commands.append(command)
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        return process

    monkeypatch.setattr(login.subprocess, "Popen", fake_popen)
    assert login._launch_edge(tmp_path / "msedge.exe", profile) is process

    command = commands[0]
    assert f"--user-data-dir={profile}" in command
    assert "--remote-debugging-port=0" in command
    assert "--remote-debugging-address=127.0.0.1" in command
    assert "--profile-directory=Default" in command
    assert "--window-size=1080,760" in command
    assert str(tmp_path / "Microsoft" / "Edge" / "User Data") not in " ".join(command)
    assert command[-1] == "--app=https://music.163.com/"


def test_launch_refuses_to_replace_live_managed_devtools_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    profile = login._managed_profile_dir()
    login._prepare_owned_profile(profile)
    port_file = profile / "DevToolsActivePort"
    original = "43123\n/devtools/browser/live-session\n"
    port_file.write_text(original, encoding="utf-8")
    monkeypatch.setattr(login, "_devtools_port_is_open", lambda _profile: True)

    def unexpected_popen(*_args, **_kwargs):
        raise AssertionError("a second Edge instance must not be started")

    monkeypatch.setattr(login.subprocess, "Popen", unexpected_popen)
    with pytest.raises(login.NeteaseLoginError, match="已经打开"):
        login._launch_edge(tmp_path / "msedge.exe", profile)
    assert port_file.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            "43123\n/devtools/browser/1234-abcd\n",
            "ws://127.0.0.1:43123/devtools/browser/1234-abcd",
        ),
        ("0\n/devtools/browser/id\n", None),
        ("43123\nhttp://attacker.example/\n", None),
        ("43123\n/devtools/browser/../../bad\n", None),
        ("not-a-port\n/devtools/browser/id\n", None),
        ("43123\n", None),
    ],
)
def test_parse_devtools_endpoint_accepts_only_loopback_browser_paths(
    content: str,
    expected: str | None,
) -> None:
    assert login._parse_devtools_endpoint(content) == expected


def test_wait_uses_ready_endpoint_even_if_launcher_parent_has_exited(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text(
        "43123\n/devtools/browser/child-process\n",
        encoding="utf-8",
    )
    process = _FakeProcess(returncode=0)

    endpoint = login._wait_for_devtools_endpoint(
        profile,
        process,
        login.time.monotonic() + 1.0,
    )

    assert endpoint == "ws://127.0.0.1:43123/devtools/browser/child-process"


@pytest.mark.parametrize(
    "value",
    [
        "has a space",
        "has;a-semicolon",
        "line\nbreak",
        "trailing-newline\n",
        "x" * 4097,
        "nul\0byte",
    ],
)
def test_extract_music_u_applies_strict_token_validation(value: str) -> None:
    result = {"cookies": [{"name": "MUSIC_U", "domain": ".music.163.com", "value": value}]}
    with pytest.raises(login.NeteaseLoginError, match="格式无效"):
        login._extract_music_u(result)


def test_extract_music_u_ignores_explicitly_expired_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(login.time, "time", lambda: 2_000.0)
    result = {
        "cookies": [
            {
                "name": "MUSIC_U",
                "domain": ".music.163.com",
                "value": "expired-token",
                "expires": 1_999.0,
                "session": False,
            }
        ]
    }
    assert login._extract_music_u(result) is None


def test_extract_music_u_accepts_future_or_nonexpiring_session_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(login.time, "time", lambda: 2_000.0)
    future = {
        "cookies": [
            {
                "name": "MUSIC_U",
                "domain": "music.163.com",
                "value": "future-token",
                "expires": 2_001.0,
                "session": False,
            }
        ]
    }
    session = {
        "cookies": [
            {
                "name": "MUSIC_U",
                "domain": "music.163.com",
                "value": "session-token",
                "expires": -1,
                "session": True,
            }
        ]
    }
    assert login._extract_music_u(future) == "future-token"
    assert login._extract_music_u(session) == "session-token"


def test_extract_music_u_does_not_allow_session_flag_to_override_expired_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(login.time, "time", lambda: 2_000.0)
    result = {
        "cookies": [
            {
                "name": "MUSIC_U",
                "domain": "music.163.com",
                "value": "expired-token",
                "expires": 0,
                "session": True,
            }
        ]
    }
    assert login._extract_music_u(result) is None


def test_connect_cdp_uses_preconnected_loopback_socket_despite_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:9999")
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.invalid:9999")
    raw_socket = object()
    socket_calls: list[tuple[tuple[str, int], float]] = []

    def create_socket(address: tuple[str, int], timeout: float):
        socket_calls.append((address, timeout))
        return raw_socket

    class Connection:
        def close(self) -> None:
            return None

    websocket_module = ModuleType("websocket")
    websocket_calls: list[tuple[str, dict[str, object]]] = []

    def create_websocket(endpoint: str, **kwargs):
        websocket_calls.append((endpoint, kwargs))
        return Connection()

    websocket_module.create_connection = create_websocket  # type: ignore[attr-defined]
    monkeypatch.setattr(login.socket, "create_connection", create_socket)

    client = login._connect_cdp(
        websocket_module,
        "ws://127.0.0.1:43123/devtools/browser/session-id",
        3.0,
    )

    assert socket_calls == [(("127.0.0.1", 43123), 3.0)]
    endpoint, options = websocket_calls[0]
    assert endpoint == "ws://127.0.0.1:43123/devtools/browser/session-id"
    assert options["socket"] is raw_socket
    assert options["suppress_origin"] is True
    assert not any("proxy" in key.casefold() for key in options)
    client.close()


def test_connect_cdp_rejects_non_loopback_endpoint_before_opening_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_socket(*_args, **_kwargs):
        raise AssertionError("non-loopback endpoint must be rejected before connecting")

    monkeypatch.setattr(login.socket, "create_connection", unexpected_socket)
    with pytest.raises(login.NeteaseLoginError, match="端点格式不安全"):
        login._connect_cdp(
            ModuleType("websocket"),
            "ws://proxy.invalid:43123/devtools/browser/session-id",
            3.0,
        )


def test_connect_cdp_rejects_unsafe_browser_path_before_opening_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_socket(*_args, **_kwargs):
        raise AssertionError("unsafe browser path must be rejected before connecting")

    monkeypatch.setattr(login.socket, "create_connection", unexpected_socket)
    with pytest.raises(login.NeteaseLoginError, match="端点格式不安全"):
        login._connect_cdp(
            ModuleType("websocket"),
            "ws://127.0.0.1:43123/devtools/browser/../../other",
            3.0,
        )


def test_cdp_client_ignores_events_and_matches_response_id() -> None:
    class Connection:
        def __init__(self) -> None:
            self.sent = ""
            self.responses = iter(
                [
                    json.dumps({"method": "Target.targetCreated", "params": {}}),
                    json.dumps({"id": 1, "result": {"product": "Edg/140"}}),
                ]
            )
            self.closed = False

        def send(self, value: str) -> None:
            self.sent = value

        def recv(self) -> str:
            return next(self.responses)

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    client = login._CdpClient(connection)

    assert client.call("Browser.getVersion") == {"product": "Edg/140"}
    assert json.loads(connection.sent) == {"id": 1, "method": "Browser.getVersion"}
    client.close()
    assert connection.closed is True


def test_cdp_transport_errors_are_friendly() -> None:
    class BrokenConnection:
        def send(self, _value: str) -> None:
            return None

        def recv(self) -> str:
            raise TimeoutError("socket timed out")

    client = login._CdpClient(BrokenConnection())
    with pytest.raises(login.NeteaseLoginError, match="连接已中断"):
        client.call("Storage.getCookies")


def test_clear_profile_removes_only_managed_edge_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    local_app_data = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    profile = local_app_data / "Karaoke Forge" / "Browser" / "EdgeNetease"
    profile.mkdir(parents=True)
    (profile / login._PROFILE_MARKER_NAME).write_text(
        login._PROFILE_MARKER_CONTENT,
        encoding="utf-8",
    )
    (profile / "Cookies").write_text("managed", encoding="utf-8")
    daily_edge = local_app_data / "Microsoft" / "Edge" / "User Data" / "Default"
    daily_edge.mkdir(parents=True)
    daily_marker = daily_edge / "Cookies"
    daily_marker.write_text("personal", encoding="utf-8")
    monkeypatch.setattr(login, "_devtools_port_is_open", lambda _profile: False)

    message = login.clear_netease_login_profile()

    assert "已清除" in message
    assert not profile.exists()
    assert daily_marker.read_text(encoding="utf-8") == "personal"


def test_clear_profile_refuses_while_managed_edge_is_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    profile = login._managed_profile_dir()
    profile.mkdir(parents=True)
    (profile / login._PROFILE_MARKER_NAME).write_text(
        login._PROFILE_MARKER_CONTENT,
        encoding="utf-8",
    )
    marker = profile / "Cookies"
    marker.write_text("managed", encoding="utf-8")
    monkeypatch.setattr(login, "_devtools_port_is_open", lambda _profile: True)

    with pytest.raises(login.NeteaseLoginError, match="仍在运行"):
        login.clear_netease_login_profile()
    assert marker.is_file()


def test_clear_missing_profile_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert "无需清理" in login.clear_netease_login_profile()


@pytest.mark.parametrize("marker_content", [None, "someone-else:v1\n"])
def test_clear_refuses_profile_without_matching_ownership_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    marker_content: str | None,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    profile = login._managed_profile_dir()
    profile.mkdir(parents=True)
    protected = profile / "do-not-delete.txt"
    protected.write_text("keep", encoding="utf-8")
    if marker_content is not None:
        (profile / login._PROFILE_MARKER_NAME).write_text(marker_content, encoding="utf-8")

    with pytest.raises(login.NeteaseLoginError, match="所有权标记"):
        login.clear_netease_login_profile()

    assert protected.read_text(encoding="utf-8") == "keep"


def test_managed_profile_path_is_lexical_and_does_not_resolve_links(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    local_app_data = tmp_path / "local" / ".." / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    def unexpected_resolve(_path: Path, *args, **kwargs):
        del args, kwargs
        raise AssertionError("managed profile paths must not resolve filesystem links")

    monkeypatch.setattr(Path, "resolve", unexpected_resolve)
    profile = login._managed_profile_dir()

    assert profile == Path(
        os.path.abspath(
            os.path.join(str(local_app_data), "Karaoke Forge", "Browser", "EdgeNetease")
        )
    )


def test_capture_refuses_reparse_point_in_app_owned_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    profile = login._managed_profile_dir()
    browser_root = profile.parent
    original_check = login._path_is_reparse_point
    monkeypatch.setattr(
        login,
        "_path_is_reparse_point",
        lambda path: login._same_lexical_path(path, browser_root) or original_check(path),
    )

    with pytest.raises(login.NeteaseLoginError, match="链接或重解析点"):
        login.capture_netease_music_u(timeout_seconds=1.0)
    assert not profile.exists()


def test_clear_refuses_reparse_point_before_deleting_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    profile = login._managed_profile_dir()
    login._prepare_owned_profile(profile)
    protected = profile / "protected.txt"
    protected.write_text("keep", encoding="utf-8")
    original_check = login._path_is_reparse_point
    monkeypatch.setattr(
        login,
        "_path_is_reparse_point",
        lambda path: login._same_lexical_path(path, profile) or original_check(path),
    )

    with pytest.raises(login.NeteaseLoginError, match="链接或重解析点"):
        login.clear_netease_login_profile()
    assert protected.read_text(encoding="utf-8") == "keep"
