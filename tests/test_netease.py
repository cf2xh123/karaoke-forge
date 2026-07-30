from pathlib import Path
from types import ModuleType

import pytest

from karaoke_forge.netease import (
    NeteaseAccessError,
    NeteaseAlignOptions,
    NeteaseLinkError,
    NeteaseSongInfo,
    align_netease_song,
    download_public_netease_track,
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
        options=NeteaseAlignOptions(rights_confirmed=True),
    )

    assert result.alignment_skipped
    assert result.alignment_report is None
    assert len(result.exports) == 6
    assert result.kept_audio is None
    assert audio.is_file()
    assert "Example Artist" in result.exports["lrc"].read_text(encoding="utf-8")


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
    assert "cookiefile" not in observed_options
