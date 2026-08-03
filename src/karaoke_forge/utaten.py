from __future__ import annotations

import re
import ssl
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from .models import LyricLine, LyricsDocument


class UtaTenLinkError(ValueError):
    pass


class UtaTenAccessError(RuntimeError):
    pass


_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_LYRIC_PATH_RE = re.compile(r"^/lyric/([A-Za-z0-9]+)/?$", re.IGNORECASE)
_ALLOWED_HOSTS = {"utaten.com", "www.utaten.com"}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_MAX_PAGE_BYTES = 4 * 1024 * 1024


def _open_url(request: Request, *, timeout: float):
    try:
        import certifi
    except ImportError:
        return urlopen(request, timeout=timeout)
    context = ssl.create_default_context(cafile=certifi.where())
    return urlopen(request, timeout=timeout, context=context)


@dataclass(frozen=True)
class UtaTenLyricsInfo:
    lyric_id: str
    title: str
    artist: str
    canonical_url: str
    lyrics: tuple[str, ...]
    readings: tuple[str, ...]

    @property
    def plain_lyrics(self) -> str:
        return "\n".join(self.lyrics) + "\n"


@dataclass(frozen=True)
class _Frame:
    tag: str
    classes: frozenset[str]


def _normalized_lines(text: str) -> tuple[str, ...]:
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"[ \t\u00a0]+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return tuple(lines)


class _UtaTenPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_Frame] = []
        self.title_parts: list[str] = []
        self.artist_parts: list[str] = []
        self.lyric_parts: list[str] = []
        self.reading_parts: list[str] = []

    def _inside(self, class_name: str) -> bool:
        return any(class_name in frame.classes for frame in self.stack)

    def _inside_lyrics(self) -> bool:
        return self._inside("lyricBody") and self._inside("hiragana")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        attributes = dict(attrs)
        classes = frozenset((attributes.get("class") or "").split())
        if normalized_tag == "br" and self._inside_lyrics():
            self.lyric_parts.append("\n")
            self.reading_parts.append("\n")
        if normalized_tag not in _VOID_TAGS:
            self.stack.append(_Frame(normalized_tag, classes))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == normalized_tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self._inside("newLyricTitle__main") and not (
            self._inside("newLyricTitle_afterTxt")
            or self._inside("newLyricTitle__subTitle")
        ):
            self.title_parts.append(data)
        if self._inside("newLyricWork__name"):
            self.artist_parts.append(data)
        if not self._inside_lyrics():
            return
        if not self._inside("rt"):
            self.lyric_parts.append(data)
        if self._inside("rt") or not self._inside("ruby"):
            self.reading_parts.append(data)

    def result(self) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
        title = re.sub(r"\s+", " ", "".join(self.title_parts)).strip()
        artist = re.sub(r"\s+", " ", "".join(self.artist_parts)).strip()
        lyrics = _normalized_lines("".join(self.lyric_parts))
        readings = _normalized_lines("".join(self.reading_parts))
        return title, artist, lyrics, readings


def resolve_utaten_lyric_url(value: str) -> tuple[str, str]:
    """Validate UtaTen share text and return its lyric ID and canonical URL."""

    match = _URL_RE.search((value or "").strip())
    if not match:
        raise UtaTenLinkError("没有找到有效的 UtaTen http/https 歌词链接。")
    url = match.group(0).rstrip("。；;，、,.!?！？)]}")
    parsed = urlsplit(unquote(url))
    host = (parsed.hostname or "").lower()
    path_match = _LYRIC_PATH_RE.fullmatch(parsed.path)
    if parsed.scheme not in {"http", "https"} or host not in _ALLOWED_HOSTS or not path_match:
        raise UtaTenLinkError("目前只支持 utaten.com/lyric/... 格式的 UtaTen 歌词页。")
    lyric_id = path_match.group(1)
    return lyric_id, f"https://utaten.com/lyric/{lyric_id}/"


def parse_utaten_page(html_text: str, *, canonical_url: str) -> UtaTenLyricsInfo:
    """Parse an UtaTen lyric page without including its ruby readings twice."""

    lyric_id, normalized_url = resolve_utaten_lyric_url(canonical_url)
    parser = _UtaTenPageParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception as exc:
        raise UtaTenAccessError(f"UtaTen 歌词页面结构无法解析：{exc}") from exc
    title, artist, lyrics, readings = parser.result()
    if not title:
        raise UtaTenAccessError("UtaTen 页面中没有找到歌名。")
    if not lyrics:
        raise UtaTenAccessError("UtaTen 页面中没有找到可导入的歌词正文。")
    if len(readings) != len(lyrics):
        readings = tuple("" for _line in lyrics)
    return UtaTenLyricsInfo(
        lyric_id=lyric_id,
        title=title,
        artist=artist or "未知歌手",
        canonical_url=normalized_url,
        lyrics=lyrics,
        readings=readings,
    )


def fetch_public_utaten_info(value: str, *, timeout: float = 20.0) -> UtaTenLyricsInfo:
    """Fetch the publicly rendered lyrics and furigana from one UtaTen lyric page."""

    _lyric_id, canonical_url = resolve_utaten_lyric_url(value)
    request = Request(
        canonical_url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ja,en;q=0.8",
            "User-Agent": "Mozilla/5.0 Karaoke-Forge/0.9.0",
        },
        method="GET",
    )
    try:
        with _open_url(request, timeout=timeout) as response:
            final_url = response.geturl()
            _final_id, final_canonical_url = resolve_utaten_lyric_url(final_url)
            payload = response.read(_MAX_PAGE_BYTES + 1)
            if len(payload) > _MAX_PAGE_BYTES:
                raise UtaTenAccessError("UtaTen 歌词页面过大，已停止读取。")
            charset = response.headers.get_content_charset() or "utf-8"
            html_text = payload.decode(charset, errors="replace")
    except UtaTenAccessError:
        raise
    except Exception as exc:
        raise UtaTenAccessError(f"无法读取 UtaTen 公开歌词页面：{exc}") from exc
    return parse_utaten_page(html_text, canonical_url=final_canonical_url)


def build_utaten_document(info: UtaTenLyricsInfo) -> LyricsDocument:
    """Create an untimed document while retaining UtaTen's per-line readings."""

    lines = []
    for index, text in enumerate(info.lyrics):
        reading = info.readings[index] if index < len(info.readings) else ""
        lines.append(
            LyricLine(
                text=text,
                pronunciation=reading if reading and reading != text else None,
            )
        )
    return LyricsDocument(
        lines=lines,
        metadata={
            "source": "UtaTen",
            "source_url": info.canonical_url,
            "source_id": info.lyric_id,
            "ti": info.title,
            "ar": info.artist,
        },
        source_format="utaten",
    )
