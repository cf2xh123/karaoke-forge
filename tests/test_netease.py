from pathlib import Path
from types import ModuleType
from typing import ClassVar

import pytest

from karaoke_forge.netease import (
    NeteaseAccessError,
    NeteaseAlignOptions,
    NeteaseLinkError,
    NeteaseSongInfo,
    align_netease_song,
    download_netease_track,
    download_public_netease_track,
    fetch_public_netease_info,
    resolve_netease_song_url,
)


def test_resolve_direct_netease_song_links() -> None:
    song_id, canonical = resolve_netease_song_url(
        "分享歌曲：https://music.163.com/#/song?id=17241424 （网易云音乐）"
    )
    assert song_id == "17241424"
    assert canonical == "https://music.163.com/song?id=17241424"

    mobile_id, _ = resolve_netease_song_url(
        "https://y.music.163.com/m/song?app_version=9&id=95670&uct2=example"
    )
    assert mobile_id == "95670"


def test_public_info_prefers_available_yrc_word_timing(monkeypatch) -> None:
    monkeypatch.setattr(
        "karaoke_forge.netease.resolve_netease_song_url",
        lambda *_args, **_kwargs: ("42", "https://music.163.com/song?id=42"),
    )
    responses = iter(
        [
            {
                "songs": [
                    {
                        "name": "Example",
                        "duration": 5000,
                        "artists": [{"name": "Artist"}],
                        "album": {"picUrl": "https://p1.music.126.net/cover.jpg"},
                    }
                ]
            },
            {
                "lrc": {"lyric": "[00:01.00]Hello"},
                "yrc": {"lyric": "[1000,500](1000,500,0)Hello"},
                "tlyric": {"lyric": "[00:01.00]你好"},
            },
        ]
    )
    monkeypatch.setattr(
        "karaoke_forge.netease._download_public_json",
        lambda *_args, **_kwargs: next(responses),
    )

    info = fetch_public_netease_info("song 42")

    assert info.word_lyrics == "[1000,500](1000,500,0)Hello\n"
    assert info.page_lyrics == "[00:01.00]Hello\n"
    assert info.cover_url == "https://p1.music.126.net/cover.jpg"


def test_rejects_non_song_netease_links() -> None:
    with pytest.raises(NeteaseLinkError, match="单曲"):
        resolve_netease_song_url("https://music.163.com/playlist?id=123")


def test_netease_alignment_requires_rights_confirmation(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        align_netease_song(
            "https://music.163.com/song?id=1",
            None,
            tmp_path,
        )


def test_local_ncm_is_not_decrypted(
    tmp_path: Path,
) -> None:
    source = tmp_path / "member-download.ncm"
    source.write_bytes(b"encrypted")

    with pytest.raises(NeteaseAccessError, match="NCM"):
        align_netease_song(
            "https://music.163.com/song?id=1",
            None,
            tmp_path / "output",
            local_audio_path=source,
            options=NeteaseAlignOptions(rights_confirmed=True),
        )


def test_local_audio_uses_public_page_lrc_without_downloading_audio(
    tmp_path: Path,
    monkeypatch,
) -> None:
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"authorized local audio")
    info = NeteaseSongInfo(
        song_id="42",
        title="Example Song",
        artists=("Example Artist",),
        canonical_url="https://music.163.com/song?id=42",
        duration=10.0,
        page_lyrics="[00:01.00]Hello\n[00:02.00]World\n",
    )
    monkeypatch.setattr(
        "karaoke_forge.netease.fetch_public_netease_info",
        lambda _link: info,
    )

    result = align_netease_song(
        info.canonical_url,
        None,
        tmp_path / "output",
        local_audio_path=audio,
        options=NeteaseAlignOptions(rights_confirmed=True, refine_word_timing=False),
    )

    assert result.alignment_skipped
    assert result.alignment_report is None
    assert len(result.exports) == 6
    assert result.kept_audio is None
    assert audio.is_file()
    assert "Example Artist" in result.exports["lrc"].read_text(encoding="utf-8")


def test_netease_auto_refinement_fallback_is_reported(tmp_path: Path, monkeypatch) -> None:
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"authorized local audio")
    info = NeteaseSongInfo(
        song_id="42",
        title="Example Song",
        artists=("Example Artist",),
        canonical_url="https://music.163.com/song?id=42",
        page_lyrics="[00:01.00]Hello\n",
    )
    monkeypatch.setattr(
        "karaoke_forge.netease.fetch_public_netease_info",
        lambda _link: info,
    )
    monkeypatch.setattr(
        "karaoke_forge.netease.refine_audio_word_timing_with_fallback",
        lambda *_args, **_kwargs: None,
    )

    result = align_netease_song(
        info.canonical_url,
        None,
        tmp_path / "output",
        local_audio_path=audio,
        options=NeteaseAlignOptions(rights_confirmed=True, timing_refinement="auto"),
    )

    assert result.alignment_skipped
    assert result.timing_refinement_warning is not None
    assert "已保留原时间轴" in result.timing_refinement_warning


def test_public_download_uses_anonymous_session_without_cookies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    info = NeteaseSongInfo(
        song_id="42",
        title="Example Song",
        artists=("Example Artist",),
        canonical_url="https://music.163.com/song?id=42",
        page_lyrics="[00:01.00]Hello\n",
    )
    monkeypatch.setattr(
        "karaoke_forge.netease.fetch_public_netease_info",
        lambda _link: info,
    )
    observed_options: dict[str, object] = {}

    class FakeDownloadError(Exception):
        pass

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            observed_options.update(options)
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def extract_info(self, _url: str, *, download: bool):
            assert download
            target = Path(str(self.options["outtmpl"]).replace("%(ext)s", "mp3"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"public audio")
            return {
                "id": "42",
                "title": "Example Song",
                "creators": ["Example Artist"],
                "duration": 10,
                "requested_downloads": [{"filepath": str(target)}],
                "subtitles": {"lyrics": [{"data": "[00:01.00]Hello\n"}]},
            }

        def prepare_filename(self, _info: dict[str, object]) -> str:
            return str(Path(str(self.options["outtmpl"]).replace("%(ext)s", "mp3")))

    yt_dlp_module = ModuleType("yt_dlp")
    yt_dlp_module.YoutubeDL = FakeYoutubeDL  # type: ignore[attr-defined]
    utils_module = ModuleType("yt_dlp.utils")
    utils_module.DownloadError = FakeDownloadError  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", yt_dlp_module)
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp.utils", utils_module)

    track = download_public_netease_track(info.canonical_url, tmp_path / "source")

    assert track.audio_path.is_file()
    assert observed_options["usenetrc"] is False
    assert observed_options["format"] == "exhigh/higher/standard"
    assert observed_options["retries"] == 5
    assert observed_options["socket_timeout"] == 45
    assert "cookiefile" not in observed_options


def test_browser_session_detects_vip_access_and_uses_browser_cookies(
    tmp_path: Path,
    monkeypatch,
) -> None:
    info = NeteaseSongInfo(
        song_id="42",
        title="VIP Song",
        artists=("Example Artist",),
        canonical_url="https://music.163.com/song?id=42",
        page_lyrics="[00:01.00]Hello\n",
    )
    monkeypatch.setattr(
        "karaoke_forge.netease.fetch_public_netease_info",
        lambda _link: info,
    )
    observed_options: dict[str, object] = {}

    class FakeDownloadError(Exception):
        pass

    class FakeCookie:
        name = "MUSIC_U"
        domain = ".music.163.com"

        def is_expired(self) -> bool:
            return False

    class FakeYoutubeDL:
        def __init__(self, options: dict[str, object]) -> None:
            observed_options.update(options)
            self.options = options
            self.cookiejar = [FakeCookie()]

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def extract_info(self, _url: str, *, download: bool):
            assert download
            extracted = {
                "formats": [
                    {"format_id": "standard"},
                    {"format_id": "exhigh"},
                    {"format_id": "lossless"},
                    {"format_id": "hires"},
                ],
            }
            self.options["match_filter"](extracted, incomplete=False)  # type: ignore[operator]
            target = Path(str(self.options["outtmpl"]).replace("%(ext)s", "flac"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"authorized vip audio")
            return {
                "id": "42",
                "title": "VIP Song",
                "creators": ["Example Artist"],
                **extracted,
                "requested_downloads": [
                    {"filepath": str(target), "format_id": "hires"},
                ],
            }

        def prepare_filename(self, _info: dict[str, object]) -> str:
            return str(Path(str(self.options["outtmpl"]).replace("%(ext)s", "flac")))

    yt_dlp_module = ModuleType("yt_dlp")
    yt_dlp_module.YoutubeDL = FakeYoutubeDL  # type: ignore[attr-defined]
    utils_module = ModuleType("yt_dlp.utils")
    utils_module.DownloadError = FakeDownloadError  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", yt_dlp_module)
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp.utils", utils_module)

    progress: list[str] = []
    track = download_netease_track(
        info.canonical_url,
        tmp_path / "source",
        cookie_browser="edge",
        cookie_browser_profile="Profile 1",
        progress=progress.append,
    )

    assert observed_options["cookiesfrombrowser"] == ("edge", "Profile 1", None, None)
    assert observed_options["format"] == "exhigh/higher/standard"
    assert track.authenticated
    assert track.quality_level == "hires"
    assert track.access_tier == "vip"
    assert any("VIP 音质权限" in message for message in progress)
    assert any("将使用极高音质" in message for message in progress)
    assert all("MUSIC_U" not in message for message in progress)


def test_browser_session_requires_netease_login_cookie(
    tmp_path: Path,
    monkeypatch,
) -> None:
    info = NeteaseSongInfo(
        song_id="42",
        title="VIP Song",
        artists=(),
        canonical_url="https://music.163.com/song?id=42",
    )
    monkeypatch.setattr(
        "karaoke_forge.netease.fetch_public_netease_info",
        lambda _link: info,
    )

    class FakeDownloadError(Exception):
        pass

    class FakeYoutubeDL:
        cookiejar: ClassVar[list[object]] = []

        def __init__(self, _options: dict[str, object]) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def extract_info(self, _url: str, *, download: bool):
            raise AssertionError("download must not start without a login cookie")

    yt_dlp_module = ModuleType("yt_dlp")
    yt_dlp_module.YoutubeDL = FakeYoutubeDL  # type: ignore[attr-defined]
    utils_module = ModuleType("yt_dlp.utils")
    utils_module.DownloadError = FakeDownloadError  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", yt_dlp_module)
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp.utils", utils_module)

    with pytest.raises(NeteaseAccessError, match="登录会话"):
        download_netease_track(
            info.canonical_url,
            tmp_path / "source",
            cookie_browser="chrome",
        )


def test_browser_session_reports_a_missing_cookie_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    info = NeteaseSongInfo(
        song_id="42",
        title="VIP Song",
        artists=(),
        canonical_url="https://music.163.com/song?id=42",
    )
    monkeypatch.setattr(
        "karaoke_forge.netease.fetch_public_netease_info",
        lambda _link: info,
    )

    class FakeDownloadError(Exception):
        pass

    class FakeYoutubeDL:
        def __init__(self, _options: dict[str, object]) -> None:
            return None

        def __enter__(self):
            raise FakeDownloadError(
                'ERROR: could not find chrome cookies database in "C:/missing/User Data"'
            )

        def __exit__(self, *_args) -> None:
            return None

    yt_dlp_module = ModuleType("yt_dlp")
    yt_dlp_module.YoutubeDL = FakeYoutubeDL  # type: ignore[attr-defined]
    utils_module = ModuleType("yt_dlp.utils")
    utils_module.DownloadError = FakeDownloadError  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", yt_dlp_module)
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp.utils", utils_module)

    with pytest.raises(NeteaseAccessError, match="没有找到 chrome.*Cookie 数据库"):
        download_netease_track(
            info.canonical_url,
            tmp_path / "source",
            cookie_browser="chrome",
        )


def test_browser_session_preserves_a_safe_download_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    info = NeteaseSongInfo(
        song_id="42",
        title="VIP Song",
        artists=(),
        canonical_url="https://music.163.com/song?id=42",
    )
    monkeypatch.setattr(
        "karaoke_forge.netease.fetch_public_netease_info",
        lambda _link: info,
    )

    class FakeDownloadError(Exception):
        pass

    class FakeCookie:
        name = "MUSIC_U"
        domain = ".music.163.com"

        def is_expired(self) -> bool:
            return False

    class FakeYoutubeDL:
        cookiejar: ClassVar[list[object]] = [FakeCookie()]

        def __init__(self, options: dict[str, object]) -> None:
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def extract_info(self, _url: str, *, download: bool):
            assert download
            self.options["logger"].error(  # type: ignore[union-attr]
                "ERROR: HTTP Error 403 while reading https://cdn.example/audio?token=secret"
            )
            raise FakeDownloadError("download failed")

    yt_dlp_module = ModuleType("yt_dlp")
    yt_dlp_module.YoutubeDL = FakeYoutubeDL  # type: ignore[attr-defined]
    utils_module = ModuleType("yt_dlp.utils")
    utils_module.DownloadError = FakeDownloadError  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", yt_dlp_module)
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp.utils", utils_module)

    with pytest.raises(NeteaseAccessError) as caught:
        download_netease_track(
            info.canonical_url,
            tmp_path / "source",
            cookie_browser="firefox",
        )

    message = str(caught.value)
    assert "HTTP Error 403" in message
    assert "https://cdn.example" not in message
    assert "token=secret" not in message
