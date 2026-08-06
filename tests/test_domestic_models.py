from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from karaoke_forge import domestic_models as dm


def _manifest(data: bytes = b"verified model bytes") -> dm.ModelManifest:
    return dm.ModelManifest(
        repo="trusted/test-model",
        revision="a" * 40,
        files=(dm.ModelFile("model.bin", len(data), hashlib.sha256(data).hexdigest()),),
    )


class _Response:
    def __init__(
        self,
        data: bytes,
        *,
        url: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
        fail_after_first_read: bool = False,
        first_read_size: int = 0,
    ) -> None:
        self.status = status
        self.headers = headers or {"Content-Length": str(len(data))}
        self._url = url
        self._data = io.BytesIO(data)
        self._fail_after_first_read = fail_after_first_read
        self._first_read_size = first_read_size
        self._reads = 0
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        if self._fail_after_first_read and self._reads:
            raise urllib.error.URLError("connection interrupted")
        self._reads += 1
        if self._first_read_size and self._reads == 1:
            amount = min(amount, self._first_read_size)
        return self._data.read(amount)

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        self.closed = True


class _BytesOpener:
    def __init__(self, data: bytes, *, final_url: str | None = None) -> None:
        self.data = data
        self.final_url = final_url
        self.requests = []
        self._guard = threading.Lock()

    def open(self, request, *, timeout):
        assert timeout > 0
        with self._guard:
            self.requests.append(request)
        final_url = self.final_url or request.full_url
        range_header = request.get_header("Range")
        if range_header:
            start = int(range_header.removeprefix("bytes=").removesuffix("-"))
            body = self.data[start:]
            return _Response(
                body,
                url=final_url,
                status=206,
                headers={
                    "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {start}-{len(self.data) - 1}/{len(self.data)}",
                },
            )
        return _Response(self.data, url=final_url)


def _download(tmp_path: Path, manifest: dm.ModelManifest, opener, progress=None) -> Path:
    return dm._download_verified_modelscope_model(
        "toy",
        tmp_path,
        progress,
        manifests={"toy": manifest},
        opener=opener,
        timeout=1,
        lock_timeout=2,
    )


def test_built_in_manifests_pin_exact_modelscope_repositories_and_weights() -> None:
    small = dm.MODELSCOPE_MODEL_MANIFESTS["small"]
    turbo = dm.MODELSCOPE_MODEL_MANIFESTS["large-v3-turbo"]
    large = dm.MODELSCOPE_MODEL_MANIFESTS["large-v3"]

    assert small.repo == "Systran/faster-whisper-small"
    assert turbo.repo == "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    assert large.repo == "Systran/faster-whisper-large-v3"
    assert small.revision == "ace8b2ad9dee031c53b6371f6c3c918b5e4f1db9"
    assert turbo.revision == "f4e944260beeb23b845ba08b4ef79ac21eed02d1"
    assert large.revision == "fb999d399593f8d6ac57a40cd2d036a43b489721"
    assert {item.path for item in small.files} == {
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
    }
    assert next(item for item in small.files if item.path == "model.bin") == dm.ModelFile(
        "model.bin",
        483_546_902,
        "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
    )
    assert next(item for item in turbo.files if item.path == "model.bin").size == 1_617_884_929
    assert next(item for item in large.files if item.path == "model.bin").sha256 == (
        "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1"
    )


def test_download_streams_verifies_marks_and_atomically_publishes(tmp_path) -> None:
    data = b"trusted bytes"
    opener = _BytesOpener(data)
    messages: list[str] = []

    result = _download(tmp_path, _manifest(data), opener, messages.append)

    assert result == tmp_path / "modelscope" / "toy"
    assert (result / "model.bin").read_bytes() == data
    marker = json.loads((result / dm.MARKER_FILENAME).read_text(encoding="utf-8"))
    assert marker["manifest"]["repo"] == "trusted/test-model"
    assert marker["manifest"]["revision"] == "a" * 40
    assert marker["manifest"]["files"][0]["sha256"] == hashlib.sha256(data).hexdigest()
    assert marker["verified_files"]["model.bin"]["mtime_ns"] > 0
    assert not (tmp_path / "modelscope" / ".staging" / "toy").exists()
    assert opener.requests[0].full_url == (
        f"https://modelscope.cn/models/trusted/test-model/resolve/{'a' * 40}/model.bin"
    )
    assert opener.requests[0].get_header("Accept-encoding") == "identity"
    assert any("SHA-256" in message for message in messages)


def test_default_modelscope_downloader_explicitly_bypasses_environment_proxy(
    tmp_path,
    monkeypatch,
) -> None:
    data = b"direct bytes"
    opener = _BytesOpener(data)
    handlers: list[object] = []

    def fake_build_opener(*received):
        handlers.extend(received)
        return opener

    monkeypatch.setattr(dm.urllib.request, "build_opener", fake_build_opener)

    result = dm._download_verified_modelscope_model(
        "toy",
        tmp_path,
        manifests={"toy": _manifest(data)},
        timeout=1,
        lock_timeout=2,
    )

    assert (result / "model.bin").read_bytes() == data
    assert len(handlers) == 2
    assert isinstance(handlers[0], dm.urllib.request.ProxyHandler)
    assert handlers[0].proxies == {}
    assert isinstance(handlers[1], dm._TrustedModelScopeRedirectHandler)


def test_hash_failure_keeps_partial_and_never_replaces_existing_model(tmp_path) -> None:
    expected = b"good"
    destination = tmp_path / "modelscope" / "toy"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("keep until verified", encoding="utf-8")

    with pytest.raises(dm.DomesticModelIntegrityError, match="SHA-256"):
        _download(tmp_path, _manifest(expected), _BytesOpener(b"evil"))

    partial = tmp_path / "modelscope" / ".staging" / "toy" / "model.bin.partial"
    assert partial.read_bytes() == b"evil"
    assert (destination / "old.txt").read_text(encoding="utf-8") == "keep until verified"
    assert not (destination / dm.MARKER_FILENAME).exists()

    result = _download(tmp_path, _manifest(expected), _BytesOpener(expected))
    assert (result / "model.bin").read_bytes() == expected
    assert not (result / "old.txt").exists()
    assert not (result / "model.bin.partial.rejected").exists()


def test_staging_extra_file_is_never_published(tmp_path) -> None:
    data = b"trusted"
    staging = tmp_path / "modelscope" / ".staging" / "toy"
    staging.mkdir(parents=True)
    extra = staging / "remote_code.py"
    extra.write_text("raise AssertionError('must never ship')", encoding="utf-8")

    with pytest.raises(dm.DomesticModelSecurityError, match="清单外文件"):
        _download(tmp_path, _manifest(data), _BytesOpener(data))

    assert not (tmp_path / "modelscope" / "toy").exists()
    assert extra.is_file()


def test_interrupted_download_resumes_from_stable_partial(tmp_path) -> None:
    data = b"abcdefgh"

    class _InterruptedOpener:
        def open(self, request, *, timeout):
            del timeout
            return _Response(
                data,
                url=request.full_url,
                fail_after_first_read=True,
                first_read_size=3,
            )

    with pytest.raises(dm.DomesticModelDownloadError, match="断点已保留"):
        _download(tmp_path, _manifest(data), _InterruptedOpener())

    partial = tmp_path / "modelscope" / ".staging" / "toy" / "model.bin.partial"
    assert partial.read_bytes() == b"abc"

    resumed = _BytesOpener(data)
    result = _download(tmp_path, _manifest(data), resumed)

    assert (result / "model.bin").read_bytes() == data
    assert resumed.requests[0].get_header("Range") == "bytes=3-"


def test_oversized_response_is_rejected_before_partial_is_overwritten(tmp_path) -> None:
    data = b"good"
    staging = tmp_path / "modelscope" / ".staging" / "toy"
    staging.mkdir(parents=True)
    partial = staging / "model.bin.partial"
    partial.write_bytes(b"go")

    class _OversizedOpener:
        def open(self, request, *, timeout):
            del timeout
            return _Response(
                b"12345",
                url=request.full_url,
                status=200,
                headers={"Content-Length": "5"},
            )

    with pytest.raises(dm.DomesticModelSecurityError, match="大小上限"):
        _download(tmp_path, _manifest(data), _OversizedOpener())

    assert partial.read_bytes() == b"go"


@pytest.mark.parametrize(
    "url",
    [
        "http://modelscope.cn/models/a/b",
        "https://modelscope.cn.evil.example/models/a/b",
        "https://evil.example/models/a/b",
        "https://user@modelscope.cn/models/a/b",
        "https://modelscope.cn:444/models/a/b",
    ],
)
def test_url_policy_rejects_http_credentials_ports_and_lookalike_hosts(url) -> None:
    assert not dm._trusted_modelscope_url(url)


def test_redirect_final_host_must_remain_a_modelscope_domain(tmp_path) -> None:
    with pytest.raises(dm.DomesticModelSecurityError, match="未信任地址"):
        _download(
            tmp_path,
            _manifest(b"data"),
            _BytesOpener(b"data", final_url="https://objects.evil.example/model.bin"),
        )

    assert not (tmp_path / "modelscope" / "toy").exists()


@pytest.mark.parametrize(
    "location",
    [
        "http://modelscope.cn/model.bin",
        "https://modelscope.cn.evil.example/model.bin",
        "https://user@modelscope.cn/model.bin",
        "https://modelscope.cn:444/model.bin",
        "//127.0.0.1/private",
    ],
)
def test_redirect_handler_rejects_location_before_it_can_be_followed(location) -> None:
    handler = dm._TrustedModelScopeRedirectHandler()
    request = dm.urllib.request.Request("https://modelscope.cn/models/trusted/start")

    with pytest.raises(dm.DomesticModelSecurityError, match="未信任重定向"):
        handler.redirect_request(request, None, 302, "Found", {}, location)


def test_redirect_handler_allows_relative_and_modelscope_cdn_targets() -> None:
    handler = dm._TrustedModelScopeRedirectHandler()
    request = dm.urllib.request.Request("https://modelscope.cn/models/trusted/start")

    relative = handler.redirect_request(request, None, 302, "Found", {}, "../next")
    cdn = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://cdn-lfs-cn-1.modelscope.cn/prod/model.bin?auth_key=short-lived",
    )

    assert relative is not None
    assert relative.full_url == "https://modelscope.cn/models/next"
    assert cdn is not None
    assert cdn.full_url.startswith("https://cdn-lfs-cn-1.modelscope.cn/")


def test_resume_requires_an_exact_content_range(tmp_path) -> None:
    data = b"abcdefgh"
    staging = tmp_path / "modelscope" / ".staging" / "toy"
    staging.mkdir(parents=True)
    (staging / "model.bin.partial").write_bytes(data[:3])

    class _WrongRangeOpener:
        def open(self, request, *, timeout):
            del timeout
            return _Response(
                data[3:],
                url=request.full_url,
                status=206,
                headers={
                    "Content-Length": "5",
                    "Content-Range": "bytes 2-6/8",
                },
            )

    with pytest.raises(dm.DomesticModelSecurityError, match="字节范围"):
        _download(tmp_path, _manifest(data), _WrongRangeOpener())

    assert (staging / "model.bin.partial").read_bytes() == data[:3]


def test_unchanged_marker_avoids_rehash_but_changed_mtime_rehashes(tmp_path, monkeypatch) -> None:
    data = b"cache me"
    manifest = _manifest(data)
    directory = _download(tmp_path, manifest, _BytesOpener(data))
    real_sha256 = dm._sha256
    hashed: list[Path] = []

    def _recording_sha256(path: Path) -> str:
        hashed.append(path)
        return real_sha256(path)

    monkeypatch.setattr(dm, "_sha256", _recording_sha256)
    assert dm._verified_directory(
        directory, "toy", manifest, refresh_marker=True
    ) == directory
    assert hashed == []

    model_file = directory / "model.bin"
    stat = model_file.stat()
    os.utime(model_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert dm._verified_directory(
        directory, "toy", manifest, refresh_marker=True
    ) == directory
    assert hashed == [model_file]

    refreshed_marker = json.loads(
        (directory / dm.MARKER_FILENAME).read_text(encoding="utf-8")
    )
    assert refreshed_marker["verified_files"]["model.bin"]["mtime_ns"] == (
        model_file.stat().st_mtime_ns
    )


def test_changed_mtime_and_same_size_tampering_invalidates_cache(tmp_path) -> None:
    data = b"trusted"
    manifest = _manifest(data)
    directory = _download(tmp_path, manifest, _BytesOpener(data))
    model_file = directory / "model.bin"
    old_mtime = model_file.stat().st_mtime_ns
    model_file.write_bytes(b"altered")
    os.utime(model_file, ns=(old_mtime + 1_000_000, old_mtime + 1_000_000))

    assert dm._verified_directory(
        directory, "toy", manifest, refresh_marker=True
    ) is None


def test_concurrent_callers_share_one_download_and_one_atomic_result(tmp_path) -> None:
    data = b"one network transfer"
    manifest = _manifest(data)
    opener = _BytesOpener(data)
    barrier = threading.Barrier(2)

    def _run() -> Path:
        barrier.wait(timeout=2)
        return _download(tmp_path, manifest, opener)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: _run(), range(2)))

    assert results[0] == results[1]
    assert len(opener.requests) == 1
    assert (results[0] / "model.bin").read_bytes() == data
    assert not list((tmp_path / "modelscope" / ".locks").glob("*.lock"))


def test_manifest_rejects_traversal_and_files_over_hard_limit(tmp_path) -> None:
    traversal = dm.ModelManifest(
        "trusted/repo",
        "a" * 40,
        (dm.ModelFile("../model.bin", 1, hashlib.sha256(b"x").hexdigest()),),
    )
    oversized = dm.ModelManifest(
        "trusted/repo",
        "a" * 40,
        (
            dm.ModelFile(
                "model.bin",
                dm.MAX_FILE_BYTES + 1,
                hashlib.sha256(b"x").hexdigest(),
            ),
        ),
    )

    with pytest.raises(dm.DomesticModelSecurityError, match="不安全路径"):
        _download(tmp_path, traversal, _BytesOpener(b"x"))
    with pytest.raises(dm.DomesticModelSecurityError, match="大小越界"):
        _download(tmp_path, oversized, _BytesOpener(b"x"))


def test_reparse_detector_covers_path_is_junction(tmp_path, monkeypatch) -> None:
    candidate = tmp_path / "junction"
    candidate.mkdir()
    monkeypatch.setattr(dm, "_path_is_junction", lambda path: path == candidate)

    assert dm._is_reparse_point(candidate)


def test_reparse_detector_uses_windows_file_attribute_fallback(tmp_path, monkeypatch) -> None:
    candidate = tmp_path / "reparse"
    candidate.mkdir()
    real_lstat = Path.lstat
    actual_stat = real_lstat(candidate)

    def fake_lstat(path):
        if path == candidate:
            return SimpleNamespace(st_mode=actual_stat.st_mode, st_file_attributes=0x400)
        return real_lstat(path)

    monkeypatch.setattr(dm, "_path_is_junction", lambda _path: False)
    monkeypatch.setattr(Path, "lstat", fake_lstat)

    assert dm._is_reparse_point(candidate)


@pytest.mark.parametrize(
    "unsafe_relative",
    [
        Path("."),
        Path("modelscope"),
        Path("modelscope/.staging"),
        Path("modelscope/.locks"),
        Path("modelscope/toy"),
    ],
)
def test_reparse_cache_components_are_rejected_before_network(
    tmp_path,
    monkeypatch,
    unsafe_relative,
) -> None:
    cache = tmp_path / "cache"
    unsafe = (cache / unsafe_relative).resolve()
    unsafe.mkdir(parents=True, exist_ok=True)
    opener = _BytesOpener(b"model")
    real_detector = dm._is_reparse_point

    def fake_detector(path):
        return path.resolve(strict=False) == unsafe or real_detector(path)

    monkeypatch.setattr(dm, "_is_reparse_point", fake_detector)

    with pytest.raises(dm.DomesticModelSecurityError, match="重解析点"):
        _download(cache, _manifest(b"model"), opener)

    assert opener.requests == []


def test_reparse_model_file_invalidates_verified_directory(tmp_path, monkeypatch) -> None:
    data = b"trusted"
    manifest = _manifest(data)
    directory = _download(tmp_path, manifest, _BytesOpener(data))
    model_file = directory / "model.bin"
    real_detector = dm._is_reparse_point
    monkeypatch.setattr(
        dm,
        "_is_reparse_point",
        lambda path: path == model_file or real_detector(path),
    )

    with pytest.raises(dm.DomesticModelSecurityError, match="重解析点"):
        dm._verified_directory(directory, "toy", manifest, refresh_marker=True)


def test_resolved_staging_must_remain_inside_cache_root(tmp_path, monkeypatch) -> None:
    cache = (tmp_path / "cache").resolve()
    staging = cache / "modelscope" / ".staging" / "toy"
    outside = (tmp_path / "outside" / "toy").resolve()
    real_resolve = Path.resolve

    def fake_resolve(path, strict=False):
        absolute = Path(os.path.abspath(path))
        if absolute == staging:
            return outside
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    opener = _BytesOpener(b"model")

    with pytest.raises(dm.DomesticModelSecurityError, match="解析后逃逸"):
        _download(cache, _manifest(b"model"), opener)

    assert opener.requests == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_real_windows_junction_cache_is_rejected_without_touching_target(tmp_path) -> None:
    cache = tmp_path / "cache"
    outside = tmp_path / "outside"
    cache.mkdir()
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    junction = cache / "modelscope"
    creation = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False,
        capture_output=True,
    )
    if creation.returncode != 0:
        pytest.skip("This Windows account cannot create a junction")
    try:
        assert dm._is_reparse_point(junction)
        opener = _BytesOpener(b"model")
        with pytest.raises(dm.DomesticModelSecurityError, match="重解析点"):
            _download(cache, _manifest(b"model"), opener)
        assert opener.requests == []
        assert sentinel.read_text(encoding="utf-8") == "untouched"
    finally:
        os.rmdir(junction)


def test_unknown_public_model_is_rejected_without_network(tmp_path) -> None:
    with pytest.raises(ValueError, match="可选"):
        dm.download_verified_modelscope_model("unknown", tmp_path)
    assert dm.find_verified_modelscope_model("small", tmp_path) is None
