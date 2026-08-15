from __future__ import annotations

import json
import urllib.error

import pytest

from karaoke_forge import network
from karaoke_forge.network import (
    APPROVED_MIRROR_ENDPOINT,
    DOMESTIC_MODELSCOPE_ENDPOINT,
    MAX_SETTINGS_BYTES,
    OFFICIAL_HF_ENDPOINT,
    DetectedLocalProxy,
    ModelDownloadSettings,
    NetworkSettingsError,
    apply_model_download_environment,
    auto_detect_local_proxies,
    configure_model_download_settings,
    describe_model_download_settings,
    load_model_download_settings,
    model_cache_directory,
    model_download_environment,
    model_download_status_markdown,
    save_model_download_settings,
    settings_file_path,
)
from karaoke_forge.network import (
    test_model_download_network as probe_model_download_network,
)


def _mirror_settings() -> ModelDownloadSettings:
    return ModelDownloadSettings(
        mode="mirror",
        mirror_endpoint=APPROVED_MIRROR_ENDPOINT,
        mirror_confirmed=True,
    )


def test_model_tls_context_uses_certifi_instead_of_the_windows_store(monkeypatch) -> None:
    import certifi

    sentinel = object()
    observed: dict[str, str] = {}

    monkeypatch.setattr(certifi, "where", lambda: "C:/trusted/cacert.pem")

    def fake_create_default_context(*, cafile):
        observed["cafile"] = cafile
        return sentinel

    monkeypatch.setattr(network.ssl, "create_default_context", fake_create_default_context)

    assert network._model_ssl_context() is sentinel
    assert observed == {"cafile": "C:/trusted/cacert.pem"}


def test_missing_settings_default_to_verified_domestic_source(tmp_path) -> None:
    environment = {"LOCALAPPDATA": str(tmp_path / "Local")}

    settings = load_model_download_settings(environ=environment)

    assert settings == ModelDownloadSettings()
    assert settings.mode == "modelscope"
    assert settings.endpoint == DOMESTIC_MODELSCOPE_ENDPOINT
    assert settings_file_path(environ=environment) == (
        tmp_path / "Local" / "KaraokeForge" / "model-download.json"
    )


def test_settings_directory_environment_override_is_supported(tmp_path) -> None:
    override = tmp_path / "portable-settings"
    environment = {
        "LOCALAPPDATA": str(tmp_path / "ignored"),
        "KARAOKE_FORGE_SETTINGS_DIR": str(override),
    }

    assert settings_file_path(environ=environment) == override / "model-download.json"
    assert model_cache_directory(environ=environment).is_relative_to(override)


def test_proxy_settings_round_trip_atomically_without_secrets(tmp_path) -> None:
    settings = ModelDownloadSettings(mode="proxy", proxy_url="HTTP://LOCALHOST:7890/")

    path = save_model_download_settings(settings, tmp_path)

    assert path == tmp_path / "model-download.json"
    assert load_model_download_settings(tmp_path) == ModelDownloadSettings(
        mode="proxy", proxy_url="http://localhost:7890"
    )
    assert "password" not in path.read_text(encoding="utf-8").lower()
    assert not list(tmp_path.glob(".model-download.json.*.tmp"))


@pytest.mark.parametrize(
    "proxy_url",
    [
        "socks5://127.0.0.1:1080",
        "http://",
        "http://user@127.0.0.1:7890",
        "http://user:secret@127.0.0.1:7890",
        "http://127.0.0.1:7890?token=secret",
        "http://127.0.0.1:7890#secret",
        "http://127.0.0.1:7890/path",
        "http://127.0.0.1:99999",
    ],
)
def test_proxy_settings_reject_unsupported_or_secret_urls(proxy_url) -> None:
    with pytest.raises(NetworkSettingsError):
        ModelDownloadSettings(mode="proxy", proxy_url=proxy_url)


def test_proxy_mode_requires_a_proxy_url() -> None:
    with pytest.raises(NetworkSettingsError, match="不能为空"):
        ModelDownloadSettings(mode="proxy")


def test_mirror_requires_fixed_endpoint_and_persist_api_confirmation(tmp_path) -> None:
    settings = _mirror_settings()

    with pytest.raises(NetworkSettingsError, match="confirm_mirror=True"):
        save_model_download_settings(settings, tmp_path)

    save_model_download_settings(settings, tmp_path, confirm_mirror=True)
    assert load_model_download_settings(tmp_path) == settings


def test_configure_mirror_requires_explicit_confirmation(tmp_path) -> None:
    with pytest.raises(NetworkSettingsError, match="显式确认"):
        configure_model_download_settings("mirror", settings_dir=tmp_path)

    settings = configure_model_download_settings(
        "mirror", settings_dir=tmp_path, confirm_mirror=True
    )

    assert settings == _mirror_settings()


def test_unapproved_mirror_is_rejected_even_when_file_claims_confirmation(tmp_path) -> None:
    payload = _mirror_settings().to_dict()
    payload["mirror_endpoint"] = "https://example.invalid"
    settings_file_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(NetworkSettingsError, match="只允许"):
        load_model_download_settings(tmp_path)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda data: data.update(extra=True), "未知字段"),
        (lambda data: data.pop("mode"), "缺少字段"),
        (lambda data: data.update(schema_version=2), "不支持的模型下载设置版本"),
        (lambda data: data.update(mirror_confirmed=1), "必须是布尔值"),
    ],
)
def test_loader_rejects_non_strict_schema(tmp_path, mutator, message) -> None:
    payload = ModelDownloadSettings().to_dict()
    mutator(payload)
    settings_file_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(NetworkSettingsError, match=message):
        load_model_download_settings(tmp_path)


def test_loader_rejects_duplicate_fields(tmp_path) -> None:
    settings_file_path(tmp_path).write_text(
        '{"schema_version":1,"mode":"official","mode":"offline",'
        '"proxy_url":null,"mirror_endpoint":null,"mirror_confirmed":false}',
        encoding="utf-8",
    )

    with pytest.raises(NetworkSettingsError, match="重复字段"):
        load_model_download_settings(tmp_path)


def test_loader_rejects_files_larger_than_64_kib(tmp_path) -> None:
    settings_file_path(tmp_path).write_bytes(b"{" + b" " * MAX_SETTINGS_BYTES + b"}")

    with pytest.raises(NetworkSettingsError, match="64 KiB"):
        load_model_download_settings(tmp_path)


def test_endpoint_caches_are_user_scoped_and_isolated(tmp_path) -> None:
    official = model_cache_directory(ModelDownloadSettings(mode="official"), tmp_path)
    mirror = model_cache_directory(_mirror_settings(), tmp_path)
    proxy = model_cache_directory(
        ModelDownloadSettings(mode="proxy", proxy_url="http://127.0.0.1:7890"), tmp_path
    )

    assert official != mirror
    assert proxy == official
    assert official.is_relative_to(tmp_path / "model-cache")
    assert mirror.is_relative_to(tmp_path / "model-cache")


def test_apply_official_environment_is_ready_before_hf_import(tmp_path) -> None:
    environment = {
        "HF_ENDPOINT": "https://old.invalid",
        "HF_HUB_OFFLINE": "1",
        "HTTPS_PROXY": "http://127.0.0.1:7890",
    }

    applied = apply_model_download_environment(
        ModelDownloadSettings(mode="official"), environ=environment, settings_dir=tmp_path
    )

    assert environment["HF_ENDPOINT"] == OFFICIAL_HF_ENDPOINT
    assert environment["HF_HOME"] == str(applied.cache_directory)
    assert environment["HF_HUB_CACHE"] == str(applied.cache_directory / "hub")
    assert environment["HF_HUB_ETAG_TIMEOUT"] == "10"
    assert environment["HF_HUB_DOWNLOAD_TIMEOUT"] == "120"
    assert "HF_HUB_OFFLINE" not in environment
    assert "HTTPS_PROXY" not in environment
    assert applied.cache_directory.is_dir()


def test_apply_proxy_sets_both_upper_and_lower_case_proxy_variables(tmp_path) -> None:
    settings = ModelDownloadSettings(mode="proxy", proxy_url="http://127.0.0.1:7890")
    environment: dict[str, str] = {}

    apply_model_download_environment(settings, environ=environment, settings_dir=tmp_path)

    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert environment[key] == "http://127.0.0.1:7890"
    assert environment["HF_ENDPOINT"] == OFFICIAL_HF_ENDPOINT


def test_apply_mirror_removes_tokens_and_debug_and_disables_implicit_token(tmp_path) -> None:
    environment = {
        "HF_TOKEN": "top-secret",
        "HUGGING_FACE_HUB_TOKEN": "legacy-secret",
        "HF_TOKEN_PATH": "secret-file",
        "HF_DEBUG": "1",
        "HTTPS_PROXY": "http://127.0.0.1:7890",
    }

    applied = apply_model_download_environment(
        _mirror_settings(), environ=environment, settings_dir=tmp_path
    )

    assert environment["HF_ENDPOINT"] == APPROVED_MIRROR_ENDPOINT
    assert environment["HF_HUB_DISABLE_IMPLICIT_TOKEN"] == "1"
    assert environment["HF_HUB_DISABLE_XET"] == "1"
    for key in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_TOKEN_PATH", "HF_DEBUG"):
        assert key not in environment
        assert key in applied.removed
    assert "HTTPS_PROXY" not in environment


def test_model_download_environment_restores_unrelated_process_network_state(tmp_path) -> None:
    environment = {
        "HTTPS_PROXY": "http://system-proxy:8080",
        "HF_ENDPOINT": "https://existing.invalid",
        "HF_TOKEN": "keep-after-context",
    }
    original = dict(environment)

    with model_download_environment(
        _mirror_settings(),
        environ=environment,
        settings_dir=tmp_path,
    ) as applied:
        assert applied.endpoint == APPROVED_MIRROR_ENDPOINT
        assert environment["HF_ENDPOINT"] == APPROVED_MIRROR_ENDPOINT
        assert "HF_TOKEN" not in environment

    assert environment == original


def test_apply_offline_enables_hf_offline_flags(tmp_path) -> None:
    environment: dict[str, str] = {}

    apply_model_download_environment(
        ModelDownloadSettings(mode="offline"), environ=environment, settings_dir=tmp_path
    )

    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"


class _FakeResponse:
    status = 200

    def __init__(self) -> None:
        self.closed = False

    def read(self, _size: int) -> bytes:
        return b"{"

    def close(self) -> None:
        self.closed = True


class _RecordingOpener:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.urls: list[str] = []

    def open(self, request, timeout):
        del timeout
        self.urls.append(request.full_url)
        if self.failure is not None:
            raise self.failure
        return _FakeResponse()


def test_network_probe_only_uses_official_endpoint_without_silent_mirror_fallback() -> None:
    opener = _RecordingOpener(urllib.error.URLError("blocked"))

    result = probe_model_download_network(ModelDownloadSettings(mode="official"), opener=opener)

    assert not result.ok
    assert opener.urls == [f"{OFFICIAL_HF_ENDPOINT}/api/models/Systran/faster-whisper-small"]
    assert "未尝试其他下载源" in result.detail_zh
    assert APPROVED_MIRROR_ENDPOINT not in "".join(opener.urls)


def test_network_probe_uses_modelscope_domestic_api_by_default() -> None:
    opener = _RecordingOpener()

    result = probe_model_download_network(ModelDownloadSettings(), opener=opener)

    assert result.ok
    assert opener.urls == [
        f"{DOMESTIC_MODELSCOPE_ENDPOINT}/api/v1/models/Systran/faster-whisper-small"
    ]


def test_network_probe_uses_mirror_only_after_explicit_settings() -> None:
    opener = _RecordingOpener()

    result = probe_model_download_network(_mirror_settings(), opener=opener)

    assert result.ok
    assert opener.urls == [f"{APPROVED_MIRROR_ENDPOINT}/api/models/Systran/faster-whisper-small"]


def test_network_probe_offline_does_not_open_a_connection() -> None:
    opener = _RecordingOpener(AssertionError("must not open"))

    result = probe_model_download_network(ModelDownloadSettings(mode="offline"), opener=opener)

    assert result.ok
    assert result.code == "offline"
    assert opener.urls == []


def test_auto_detect_returns_local_candidates_but_never_a_mirror() -> None:
    open_ports = {7890, 10809}

    detected = auto_detect_local_proxies(
        environ={
            "HTTPS_PROXY": "http://localhost:7897",
            "HTTP_PROXY": "http://user:secret@127.0.0.1:9999",
        },
        port_probe=lambda _host, port, _timeout: port in open_ports,
    )

    assert detected == (
        DetectedLocalProxy("http://localhost:7897", "环境变量 HTTPS_PROXY"),
        DetectedLocalProxy("http://127.0.0.1:7890", "常见 Clash HTTP 端口"),
        DetectedLocalProxy("http://127.0.0.1:10809", "常见 v2rayN HTTP 端口"),
    )
    assert all("hf-mirror" not in candidate.url for candidate in detected)


def test_chinese_status_explains_mirror_security_and_cache(tmp_path) -> None:
    status = describe_model_download_settings(_mirror_settings(), settings_dir=tmp_path)
    markdown = model_download_status_markdown(_mirror_settings(), settings_dir=tmp_path)

    assert status.third_party
    assert "第三方镜像" in status.label_zh
    assert "不会发送" in status.detail_zh
    assert "缓存" in markdown
    assert str(status.cache_directory) in markdown
