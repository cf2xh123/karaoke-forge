from pathlib import Path

import pytest

from karaoke_forge.qqmusic import (
    QQMusicAccessError,
    QQMusicLinkError,
    fetch_public_qqmusic_info,
    resolve_qqmusic_song_url,
)


def test_resolve_direct_qqmusic_song_links() -> None:
    song_mid, canonical = resolve_qqmusic_song_url(
        "分享歌曲 https://y.qq.com/n/ryqq_v2/songDetail/001gQnW91BEDaN （QQ音乐）"
    )

    assert song_mid == "001gQnW91BEDaN"
    assert canonical == "https://y.qq.com/n/ryqq_v2/songDetail/001gQnW91BEDaN"

    mobile_mid, _ = resolve_qqmusic_song_url(
        "https://i.y.qq.com/v8/playsong.html?songmid=003OUlho2HcRHC"
    )
    assert mobile_mid == "003OUlho2HcRHC"


def test_rejects_non_song_qqmusic_links() -> None:
    with pytest.raises(QQMusicLinkError, match="单曲|songmid"):
        resolve_qqmusic_song_url("https://y.qq.com/n/ryqq/playlist/123")

    with pytest.raises(QQMusicLinkError, match="QQ 音乐"):
        resolve_qqmusic_song_url("https://example.com/song?id=1")


def test_fetch_public_qqmusic_info_reads_lrc_metadata_and_translation(monkeypatch) -> None:
    monkeypatch.setattr(
        "karaoke_forge.qqmusic.resolve_qqmusic_song_url",
        lambda *_args, **_kwargs: (
            "001gQnW91BEDaN",
            "https://y.qq.com/n/ryqq_v2/songDetail/001gQnW91BEDaN",
        ),
    )
    monkeypatch.setattr(
        "karaoke_forge.qqmusic._download_public_json",
        lambda *_args, **_kwargs: {
            "retcode": 0,
            "lyric": "[ti:Example &amp; Song]\n[ar:Artist A/Artist B]\n"
            "[al:Album]\n[00:01.00]Hello",
            "trans": "[00:01.00]你好",
        },
    )

    info = fetch_public_qqmusic_info("song link")

    assert info.song_mid == "001gQnW91BEDaN"
    assert info.title == "Example & Song"
    assert info.artists == ("Artist A", "Artist B")
    assert info.album == "Album"
    assert info.page_lyrics.endswith("[00:01.00]Hello\n")
    assert info.translated_lyrics == "[00:01.00]你好\n"


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"retcode": 1}, "错误码"),
        ({"retcode": 0, "lyric": ""}, "没有可用"),
    ],
)
def test_fetch_public_qqmusic_info_reports_unavailable_lyrics(
    monkeypatch,
    payload: dict[str, object],
    message: str,
) -> None:
    monkeypatch.setattr(
        "karaoke_forge.qqmusic.resolve_qqmusic_song_url",
        lambda *_args, **_kwargs: ("mid", "https://y.qq.com/song/mid"),
    )
    monkeypatch.setattr(
        "karaoke_forge.qqmusic._download_public_json",
        lambda *_args, **_kwargs: payload,
    )

    with pytest.raises(QQMusicAccessError, match=message):
        fetch_public_qqmusic_info("song link")


def test_web_qqmusic_job_exports_all_formats(tmp_path: Path, monkeypatch) -> None:
    from karaoke_forge.qqmusic import QQMusicSongInfo
    from karaoke_forge.web import run_qqmusic_job

    info = QQMusicSongInfo(
        song_mid="mid",
        title="Example Song",
        artists=("Artist",),
        canonical_url="https://y.qq.com/n/ryqq_v2/songDetail/mid",
        page_lyrics="[00:01.00]Hello\n[00:03.00]World\n",
        translated_lyrics="[00:01.00]你好\n[00:03.00]世界\n",
    )
    monkeypatch.setattr("karaoke_forge.web.fetch_public_qqmusic_info", lambda _link: info)

    result = run_qqmusic_job(
        info.canonical_url,
        "qq-export",
        True,
        str(tmp_path / "outputs"),
    )

    assert len(result.files) == 6
    assert "QQ 音乐歌词已生成" in result.status
    project = next(Path(path) for path in result.files if path.endswith(".json"))
    content = project.read_text(encoding="utf-8")
    assert '"source": "QQ Music"' in content
    assert '"translation": "你好"' in content
