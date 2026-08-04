from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache

_KANJI_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF々〆ヵヶ]")
_KANA_RE = re.compile(r"[\u3040-\u30FF]")
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
_METADATA_RE = re.compile(
    r"^\s*(?:作词|作詞|作曲|编曲|編曲|制作人|歌手|演唱|lyrics?|music|composer)"
    r"\s*[:：]",
    re.IGNORECASE,
)

# Dictionary spellings are occasionally less natural than the conventional
# katakana seen in Japanese karaoke. Keep a small override table for common
# function words and high-confidence karaoke spellings.
_ENGLISH_OVERRIDES = {
    "a": "ア",
    "an": "アン",
    "and": "アンド",
    "beyond": "ビヨンド",
    "i": "アイ",
    "i'm": "アイム",
    "its": "イッツ",
    "it's": "イッツ",
    "of": "オブ",
    "ocean": "オーシャン",
    "the": "ザ",
    "this": "ディス",
    "to": "トゥ",
    "you": "ユー",
    "your": "ユア",
    "you're": "ユア",
}


@dataclass(frozen=True)
class PronunciationUnit:
    source: str
    reading: str
    start: int = 0
    end: int = 0


@dataclass(frozen=True)
class PronunciationLine:
    units: tuple[PronunciationUnit, ...]
    separator: str = "　"

    @property
    def text(self) -> str:
        return self.separator.join(unit.reading for unit in self.units).rstrip()


EnglishLookup = Callable[[str], str | None]
JapaneseConverter = Callable[[str], Iterable[Mapping[str, object]]]


@lru_cache(maxsize=1)
def _default_english_lookup() -> EnglishLookup | None:
    try:
        import alkana
    except ImportError:
        return None
    return alkana.get_kana


@lru_cache(maxsize=1)
def _default_japanese_converter() -> JapaneseConverter | None:
    try:
        from pykakasi import kakasi
    except ImportError:
        return None
    return kakasi().convert


def english_pronunciation(
    text: str,
    *,
    lookup: EnglishLookup | None = None,
) -> PronunciationLine | None:
    lookup = lookup or _default_english_lookup()
    units: list[PronunciationUnit] = []
    for match in _ENGLISH_WORD_RE.finditer(text):
        source = match.group(0)
        key = source.lower().replace("’", "'")
        reading = _ENGLISH_OVERRIDES.get(key)
        if reading is None and lookup is not None:
            query = source.replace("'", "").replace("’", "")
            try:
                reading = lookup(query)
            except (KeyError, TypeError, ValueError):
                reading = None
        if reading:
            units.append(
                PronunciationUnit(
                    source=source,
                    reading=str(reading),
                    start=match.start(),
                    end=match.end(),
                )
            )
    if not units:
        return None
    return PronunciationLine(tuple(units))


def japanese_pronunciation(
    text: str,
    *,
    converter: JapaneseConverter | None = None,
) -> PronunciationLine | None:
    converter = converter or _default_japanese_converter()
    if converter is None:
        return None

    units: list[PronunciationUnit] = []
    cursor = 0
    for item in converter(text):
        source = str(item.get("orig") or "")
        if not source:
            continue
        if _KANJI_RE.search(source):
            reading = str(item.get("hira") or "")
            units.append(
                PronunciationUnit(
                    source=source,
                    reading=reading,
                    start=cursor,
                    end=cursor + len(source),
                )
            )
        cursor += len(source)
    return PronunciationLine(tuple(units)) if units else None


def generate_pronunciation(
    text: str,
    *,
    include_english: bool = True,
) -> PronunciationLine | None:
    """Generate Japanese furigana and, when enabled, English katakana."""

    value = text.strip()
    if not value or _METADATA_RE.match(value):
        return None
    if _KANA_RE.search(value) and _KANJI_RE.search(value):
        return japanese_pronunciation(value)
    if include_english and _ENGLISH_WORD_RE.search(value):
        return english_pronunciation(value)
    return None


def contains_english_word(text: str) -> bool:
    """Return whether *text* contains a Latin-script word eligible for reading."""

    return _ENGLISH_WORD_RE.search(text) is not None


def pronunciation_dependencies_available() -> bool:
    return _default_english_lookup() is not None and _default_japanese_converter() is not None
