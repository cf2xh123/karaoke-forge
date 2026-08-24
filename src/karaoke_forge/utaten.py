from __future__ import annotations

import re
import ssl
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from .models import LyricLine, LyricsDocument, PronunciationSpan


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
class UtaTenPronunciationUnit:
    source: str
    reading: str
    start: int
    end: int


@dataclass(frozen=True)
class UtaTenLyricsInfo:
    lyric_id: str
    title: str
    artist: str
    canonical_url: str
    lyrics: tuple[str, ...]
    readings: tuple[str, ...]
    pronunciation_units: tuple[tuple[UtaTenPronunciationUnit, ...], ...] = ()

    @property
    def plain_lyrics(self) -> str:
        return "\n".join(self.lyrics) + "\n"


@dataclass(frozen=True)
class _Frame:
    tag: str
    classes: frozenset[str]


@dataclass(frozen=True)
class _CapturedRuby:
    line_index: int
    source: str
    reading: str


@dataclass(frozen=True)
class UtaTenPronunciationReport:
    local_lines: int
    official_lines: int
    matched_lines: int
    annotated_lines: int
    mapped_units: int
    cleared_lines: int


def _normalized_logical_lines(
    parts: list[list[str]],
) -> tuple[tuple[int, str], ...]:
    lines: list[tuple[int, str]] = []
    for raw_index, raw_parts in enumerate(parts):
        line = re.sub(r"\s+", " ", "".join(raw_parts)).strip()
        if line:
            lines.append((raw_index, line))
    return tuple(lines)


class _UtaTenPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_Frame] = []
        self.title_parts: list[str] = []
        self.artist_parts: list[str] = []
        self.lyric_line_parts: list[list[str]] = [[]]
        self.reading_line_parts: list[list[str]] = [[]]
        self.line_index = 0
        self.ruby_stack_index: int | None = None
        self.ruby_source_parts: list[str] = []
        self.ruby_reading_parts: list[str] = []
        self.captured_ruby: list[_CapturedRuby] = []

    def _inside(self, class_name: str) -> bool:
        return any(class_name in frame.classes for frame in self.stack)

    def _inside_tag(self, tag: str) -> bool:
        return any(frame.tag == tag for frame in self.stack)

    def _inside_kind(self, value: str) -> bool:
        return self._inside(value) or self._inside_tag(value)

    def _inside_lyrics(self) -> bool:
        return self._inside("lyricBody") and self._inside("hiragana")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        attributes = dict(attrs)
        classes = frozenset((attributes.get("class") or "").split())
        if normalized_tag == "br" and self._inside_lyrics():
            self.line_index += 1
            self.lyric_line_parts.append([])
            self.reading_line_parts.append([])
        if (
            self._inside_lyrics()
            and self.ruby_stack_index is None
            and (normalized_tag == "ruby" or "ruby" in classes)
        ):
            self.ruby_stack_index = len(self.stack)
            self.ruby_source_parts = []
            self.ruby_reading_parts = []
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
                if self.ruby_stack_index == index:
                    source = re.sub(r"\s+", " ", "".join(self.ruby_source_parts)).strip()
                    reading = re.sub(r"\s+", " ", "".join(self.ruby_reading_parts)).strip()
                    if source and reading and source != reading:
                        self.captured_ruby.append(
                            _CapturedRuby(self.line_index, source, reading)
                        )
                    self.ruby_stack_index = None
                    self.ruby_source_parts = []
                    self.ruby_reading_parts = []
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
        if not self._inside_kind("rt"):
            self.lyric_line_parts[self.line_index].append(data)
        if self._inside_kind("rt") or not self._inside_kind("ruby"):
            self.reading_line_parts[self.line_index].append(data)
        if self.ruby_stack_index is not None:
            if self._inside_kind("rt"):
                self.ruby_reading_parts.append(data)
            elif self._inside_kind("rb") or self._inside_kind("ruby"):
                self.ruby_source_parts.append(data)

    def result(
        self,
    ) -> tuple[
        str,
        str,
        tuple[str, ...],
        tuple[str, ...],
        tuple[tuple[UtaTenPronunciationUnit, ...], ...],
    ]:
        title = re.sub(r"\s+", " ", "".join(self.title_parts)).strip()
        artist = re.sub(r"\s+", " ", "".join(self.artist_parts)).strip()
        indexed_lyrics = _normalized_logical_lines(self.lyric_line_parts)
        lyrics = tuple(line for _raw_index, line in indexed_lyrics)
        readings = tuple(
            line for _raw_index, line in _normalized_logical_lines(self.reading_line_parts)
        )
        units = _position_captured_units(indexed_lyrics, self.captured_ruby)
        return title, artist, lyrics, readings, units


def _position_captured_units(
    indexed_lyrics: tuple[tuple[int, str], ...],
    captured: list[_CapturedRuby],
) -> tuple[tuple[UtaTenPronunciationUnit, ...], ...]:
    lyrics = tuple(line for _raw_index, line in indexed_lyrics)
    positioned: list[list[UtaTenPronunciationUnit]] = [[] for _line in lyrics]
    if not lyrics:
        return ()
    normalized_indexes = {
        raw_index: normalized_index
        for normalized_index, (raw_index, _line) in enumerate(indexed_lyrics)
    }
    line_offsets: dict[int, int] = {}
    for ruby in captured:
        line_index = normalized_indexes.get(ruby.line_index)
        if line_index is None:
            continue
        start = lyrics[line_index].find(ruby.source, line_offsets.get(line_index, 0))
        if start < 0:
            continue
        end = start + len(ruby.source)
        positioned[line_index].append(
            UtaTenPronunciationUnit(
                source=ruby.source,
                reading=ruby.reading,
                start=start,
                end=end,
            )
        )
        line_offsets[line_index] = end
    return tuple(tuple(line_units) for line_units in positioned)


def _normalized_character_map(value: str) -> tuple[str, list[int]]:
    characters: list[str] = []
    original_indexes: list[int] = []
    for index, character in enumerate(value):
        normalized = unicodedata.normalize("NFKC", character).casefold()
        for item in normalized:
            if unicodedata.category(item)[:1] in {"L", "N"}:
                characters.append(item)
                original_indexes.append(index)
    return "".join(characters), original_indexes


def _normalized_lyric_text(value: str) -> str:
    return _normalized_character_map(value)[0]


def _line_similarity(left: str, right: str) -> float:
    left_normalized = _normalized_lyric_text(left)
    right_normalized = _normalized_lyric_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    if left_normalized == right_normalized:
        return 1.0
    if min(len(left_normalized), len(right_normalized)) < 2:
        return 0.0
    matcher = SequenceMatcher(None, left_normalized, right_normalized, autojunk=False)
    common = sum(block.size for block in matcher.get_matching_blocks())
    ratio = matcher.ratio()
    coverage = common / min(len(left_normalized), len(right_normalized))
    return ratio if ratio >= 0.72 and coverage >= 0.68 and common >= 2 else 0.0


def match_utaten_lines(
    local_lines: tuple[str, ...],
    official_lines: tuple[str, ...],
) -> tuple[tuple[int, int], ...]:
    """Order-preserving fuzzy line matching for local and UtaTen lyrics."""

    local_count = len(local_lines)
    official_count = len(official_lines)
    scores = [[0.0] * (official_count + 1) for _ in range(local_count + 1)]
    choices = [[""] * (official_count + 1) for _ in range(local_count + 1)]
    for local_index in range(1, local_count + 1):
        choices[local_index][0] = "local"
    for official_index in range(1, official_count + 1):
        choices[0][official_index] = "official"
    for local_index in range(1, local_count + 1):
        for official_index in range(1, official_count + 1):
            best_score = scores[local_index - 1][official_index]
            choice = "local"
            if scores[local_index][official_index - 1] > best_score:
                best_score = scores[local_index][official_index - 1]
                choice = "official"
            similarity = _line_similarity(
                local_lines[local_index - 1],
                official_lines[official_index - 1],
            )
            matched_score = scores[local_index - 1][official_index - 1] + 1.0 + similarity
            if similarity and matched_score >= best_score:
                best_score = matched_score
                choice = "match"
            scores[local_index][official_index] = best_score
            choices[local_index][official_index] = choice
    matches: list[tuple[int, int]] = []
    local_index, official_index = local_count, official_count
    while local_index and official_index:
        choice = choices[local_index][official_index]
        if choice == "match":
            matches.append((local_index - 1, official_index - 1))
            local_index -= 1
            official_index -= 1
        elif choice == "local":
            local_index -= 1
        else:
            official_index -= 1
    matches.reverse()
    return tuple(matches)


def _map_pronunciation_unit(
    local_text: str,
    official_text: str,
    unit: UtaTenPronunciationUnit,
) -> PronunciationSpan | None:
    official_normalized, official_indexes = _normalized_character_map(official_text)
    local_normalized, local_indexes = _normalized_character_map(local_text)
    target_indexes = [
        index
        for index, original_index in enumerate(official_indexes)
        if unit.start <= original_index < unit.end
    ]
    if not target_indexes:
        return None
    matcher = SequenceMatcher(None, official_normalized, local_normalized, autojunk=False)
    index_map: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            index_map[block.a + offset] = block.b + offset
    if any(index not in index_map for index in target_indexes):
        return None
    mapped_original_indexes = [local_indexes[index_map[index]] for index in target_indexes]
    start = min(mapped_original_indexes)
    end = max(mapped_original_indexes) + 1
    if not unit.reading.strip() or end <= start:
        return None
    return PronunciationSpan(
        source=local_text[start:end],
        reading=unit.reading,
        start=start,
        end=end,
    )


def apply_utaten_pronunciation(
    document: LyricsDocument,
    info: UtaTenLyricsInfo,
    *,
    replace_existing: bool,
) -> UtaTenPronunciationReport:
    """Transfer only verifiable official UtaTen readings onto local lyrics."""

    cleared_lines = 0
    if replace_existing:
        for line in document.lines:
            if line.pronunciation or line.pronunciation_units:
                cleared_lines += 1
            line.pronunciation = None
            line.pronunciation_units = []
    matches = match_utaten_lines(
        tuple(line.text for line in document.lines),
        info.lyrics,
    )
    annotated_lines = 0
    mapped_units = 0
    for local_index, official_index in matches:
        line = document.lines[local_index]
        official_text = info.lyrics[official_index]
        official_units = (
            info.pronunciation_units[official_index]
            if official_index < len(info.pronunciation_units)
            else ()
        )
        spans = [
            mapped
            for unit in official_units
            if (mapped := _map_pronunciation_unit(line.text, official_text, unit)) is not None
        ]
        spans.sort(key=lambda span: (span.start, span.end))
        non_overlapping: list[PronunciationSpan] = []
        for span in spans:
            if non_overlapping and span.start < non_overlapping[-1].end:
                continue
            non_overlapping.append(span)
        if non_overlapping:
            line.pronunciation = None
            line.pronunciation_units = non_overlapping
            annotated_lines += 1
            mapped_units += len(non_overlapping)
            continue
        reading = info.readings[official_index] if official_index < len(info.readings) else ""
        if (
            reading
            and reading != official_text
            and _normalized_lyric_text(line.text) == _normalized_lyric_text(official_text)
        ):
            line.pronunciation = reading
            line.pronunciation_units = []
            annotated_lines += 1
    document.metadata.update(
        {
            "pronunciation_source": "UtaTen",
            "pronunciation_source_url": info.canonical_url,
            "utaten_matched_lines": str(len(matches)),
            "utaten_annotated_lines": str(annotated_lines),
            "utaten_mapped_units": str(mapped_units),
        }
    )
    if replace_existing:
        document.metadata["auto_pronunciation"] = "false"
    return UtaTenPronunciationReport(
        local_lines=len(document.lines),
        official_lines=len(info.lyrics),
        matched_lines=len(matches),
        annotated_lines=annotated_lines,
        mapped_units=mapped_units,
        cleared_lines=cleared_lines,
    )


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
    title, artist, lyrics, readings, pronunciation_units = parser.result()
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
        pronunciation_units=pronunciation_units,
    )


def fetch_public_utaten_info(value: str, *, timeout: float = 20.0) -> UtaTenLyricsInfo:
    """Fetch the publicly rendered lyrics and furigana from one UtaTen lyric page."""

    _lyric_id, canonical_url = resolve_utaten_lyric_url(value)
    request = Request(
        canonical_url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ja,en;q=0.8",
            "User-Agent": "Mozilla/5.0 Karaoke-Forge/0.15.2",
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
        units = info.pronunciation_units[index] if index < len(info.pronunciation_units) else ()
        lines.append(
            LyricLine(
                text=text,
                pronunciation=reading if reading and reading != text else None,
                pronunciation_units=[
                    PronunciationSpan(
                        source=unit.source,
                        reading=unit.reading,
                        start=unit.start,
                        end=unit.end,
                    )
                    for unit in units
                ],
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
