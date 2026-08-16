from __future__ import annotations

import html
import json
import re
import ssl
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import Request, urlopen


class QQMusicLinkError(ValueError):
    pass


class QQMusicAccessError(RuntimeError):
    pass


_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SONG_PATH_RE = re.compile(
    r"/(?:n/)?ryqq(?:_v2)?/songDetail/([A-Za-z0-9]+)(?:[/?#]|$)",
    re.IGNORECASE,
)
_LRC_METADATA_RE = re.compile(r"^\[(ti|ar|al):\s*(.*?)\]\s*$", re.IGNORECASE)
_DIRECT_HOSTS = {"y.qq.com", "i.y.qq.com"}
_SHORT_HOSTS = {"c6.y.qq.com", "i.y.qq.com", "link.y.qq.com"}


def _open_url(request: Request, *, timeout: float):
    try:
        import certifi
    except ImportError:
        return urlopen(request, timeout=timeout)
    context = ssl.create_default_context(cafile=certifi.where())
    return urlopen(request, timeout=timeout, context=context)


@dataclass(frozen=True)
class QQMusicSongInfo:
    song_mid: str
    title: str
    artists: tuple[str, ...]
    canonical_url: str
    page_lyrics: str
    translated_lyrics: str | None = None
    album: str | None = None
    cover_url: str | None = None

    @property
    def artist_text(self) -> str:
        return " / ".join(self.artists) if self.artists else "未知歌手"


def _extract_shared_url(value: str) -> str:
    match = _URL_RE.search((value or "").strip())
    if not match:
        raise QQMusicLinkError("没有找到有效的 QQ 音乐 http/https 单曲链接。")
    return match.group(0).rstrip("。，、；;：:！!？?)]}")


def _song_mid_from_url(url: str) -> str | None:
    decoded = unquote(url)
    parsed = urlsplit(decoded)
    query = parse_qs(parsed.query)
    for key in ("songmid", "song_mid", "mid"):
        values = query.get(key)
        if values and re.fullmatch(r"[A-Za-z0-9]+", values[0]):
            return values[0]
    path_match = _SONG_PATH_RE.search(parsed.path)
    if path_match:
        return path_match.group(1)
    fragment_match = _SONG_PATH_RE.search("/" + parsed.fragment.lstrip("/"))
    return fragment_match.group(1) if fragment_match else None


def _validate_direct_song_url(url: str) -> tuple[str, str]:
    parsed = urlsplit(unquote(url))
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in _DIRECT_HOSTS:
        raise QQMusicLinkError("目前只支持 y.qq.com 或 i.y.qq.com 的 QQ 音乐单曲链接。")
    song_mid = _song_mid_from_url(url)
    if not song_mid:
        raise QQMusicLinkError("链接不是 QQ 音乐单曲页，或其中没有找到 songmid。")
    canonical = f"https://y.qq.com/n/ryqq_v2/songDetail/{song_mid}"
    return song_mid, canonical


def resolve_qqmusic_song_url(value: str, *, timeout: float = 15.0) -> tuple[str, str]:
    """Resolve QQ Music share text or a short URL to a canonical single-song URL."""

    url = _extract_shared_url(value)
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host in _DIRECT_HOSTS and _song_mid_from_url(url):
        return _validate_direct_song_url(url)
    if host not in _SHORT_HOSTS:
        raise QQMusicLinkError("目前只支持 QQ 音乐单曲链接或 QQ 音乐官方分享短链接。")

    request = Request(
        url,
            headers={"User-Agent": "Mozilla/5.0 Karaoke-Forge/0.15.0"},
        method="GET",
    )
    try:
        with _open_url(request, timeout=timeout) as response:
            final_url = response.geturl()
    except Exception as exc:
        raise QQMusicLinkError(f"QQ 音乐分享链接解析失败：{exc}") from exc
    return _validate_direct_song_url(final_url)


def _download_public_json(url: str, *, timeout: float = 15.0) -> dict[str, object]:
    request = Request(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://y.qq.com/",
            "User-Agent": "Mozilla/5.0 Karaoke-Forge/0.15.0",
        },
    )
    try:
        with _open_url(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise QQMusicAccessError(f"无法读取 QQ 音乐公开歌词：{exc}") from exc
    if not isinstance(payload, dict):
        raise QQMusicAccessError("QQ 音乐返回了无法识别的歌词数据。")
    return payload


def _lrc_metadata(lyrics: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in lyrics.splitlines():
        match = _LRC_METADATA_RE.match(line.strip())
        if match and match.group(2).strip():
            metadata[match.group(1).lower()] = html.unescape(match.group(2).strip())
    return metadata


def fetch_public_qqmusic_info(
    value: str,
    *,
    timeout: float = 15.0,
) -> QQMusicSongInfo:
    """Fetch QQ Music's public line-timed lyrics without requesting song audio."""

    song_mid, canonical_url = resolve_qqmusic_song_url(value, timeout=timeout)
    endpoint = (
        "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
        f"?songmid={quote(song_mid)}&format=json&nobase64=1"
    )
    payload = _download_public_json(endpoint, timeout=timeout)
    code = payload.get("retcode", payload.get("code", 0))
    if code not in {0, "0", None}:
        raise QQMusicAccessError(f"QQ 音乐没有返回可用歌词（错误码 {code}）。")
    raw_lyrics = payload.get("lyric")
    if not isinstance(raw_lyrics, str) or not raw_lyrics.strip():
        raise QQMusicAccessError("该 QQ 音乐单曲没有可用的公开 LRC 歌词。")
    page_lyrics = html.unescape(raw_lyrics.strip()) + "\n"

    translated_lyrics = None
    raw_translation = payload.get("trans")
    if isinstance(raw_translation, str) and raw_translation.strip():
        translated_lyrics = html.unescape(raw_translation.strip()) + "\n"

    metadata = _lrc_metadata(page_lyrics)
    title = metadata.get("ti") or f"qqmusic-{song_mid}"
    artist_text = metadata.get("ar", "")
    artists = tuple(part.strip() for part in artist_text.split("/") if part.strip())
    cover_url = None
    try:
        detail = _download_public_json(
            "https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg"
            f"?songmid={quote(song_mid)}&format=json",
            timeout=timeout,
        )
        songs = detail.get("data")
        if isinstance(songs, list) and songs and isinstance(songs[0], dict):
            album_data = songs[0].get("album")
            if isinstance(album_data, dict) and album_data.get("mid"):
                album_mid = str(album_data["mid"])
                cover_url = f"https://y.gtimg.cn/music/photo_new/T002R800x800M000{album_mid}.jpg"
    except QQMusicAccessError:
        # Lyrics remain useful when the optional public metadata endpoint is
        # unavailable or changes shape.
        cover_url = None
    return QQMusicSongInfo(
        song_mid=song_mid,
        title=title,
        artists=artists,
        canonical_url=canonical_url,
        page_lyrics=page_lyrics,
        translated_lyrics=translated_lyrics,
        album=metadata.get("al"),
        cover_url=cover_url,
    )
