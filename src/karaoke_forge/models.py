from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class KaraokeToken:
    """A display token and its absolute timing in seconds."""

    text: str
    start: float
    end: float
    confidence: float | None = None

    def __post_init__(self) -> None:
        self.start = max(0.0, float(self.start))
        self.end = max(self.start + 0.01, float(self.end))


@dataclass
class PronunciationSpan:
    """A manually editable reading attached to a character range."""

    source: str
    reading: str
    start: int = 0
    end: int = 0

    def __post_init__(self) -> None:
        self.start = max(0, int(self.start))
        self.end = max(self.start, int(self.end))


@dataclass
class LyricLine:
    text: str
    start: float | None = None
    end: float | None = None
    tokens: list[KaraokeToken] = field(default_factory=list)
    translation: str | None = None
    pronunciation: str | None = None
    pronunciation_units: list[PronunciationSpan] = field(default_factory=list)
    hidden: bool = False

    @property
    def is_timed(self) -> bool:
        return self.start is not None and self.end is not None


@dataclass
class LyricsDocument:
    lines: list[LyricLine]
    metadata: dict[str, str] = field(default_factory=dict)
    source_format: str = "txt"

    @property
    def visible_lines(self) -> list[LyricLine]:
        return [line for line in self.lines if not line.hidden]

    @property
    def is_timed(self) -> bool:
        lines = self.visible_lines
        return bool(lines) and all(line.is_timed for line in lines)

    def require_timed(self) -> None:
        if not self.is_timed:
            raise ValueError("Lyrics do not contain a complete timeline. Run `align` first.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "metadata": self.metadata,
            "lines": [
                {
                    "text": line.text,
                    "translation": line.translation,
                    "pronunciation": line.pronunciation,
                    "pronunciation_units": [
                        {
                            "source": unit.source,
                            "reading": unit.reading,
                            "start": unit.start,
                            "end": unit.end,
                        }
                        for unit in line.pronunciation_units
                    ],
                    "hidden": line.hidden,
                    "start": line.start,
                    "end": line.end,
                    "tokens": [
                        {
                            "text": token.text,
                            "start": token.start,
                            "end": token.end,
                            "confidence": token.confidence,
                        }
                        for token in line.tokens
                    ],
                }
                for line in self.lines
            ],
        }

    def shifted(self, offset: float) -> LyricsDocument:
        """Return a copy with line and token timing shifted by offset seconds."""

        shifted_lines: list[LyricLine] = []
        for line in self.lines:
            start = max(0.0, line.start + offset) if line.start is not None else None
            end = max((start or 0.0) + 0.01, line.end + offset) if line.end is not None else None
            tokens = [
                KaraokeToken(
                    text=token.text,
                    start=max(0.0, token.start + offset),
                    end=max(0.01, token.end + offset),
                    confidence=token.confidence,
                )
                for token in line.tokens
            ]
            shifted_lines.append(
                LyricLine(
                    text=line.text,
                    start=start,
                    end=end,
                    tokens=tokens,
                    translation=line.translation,
                    pronunciation=line.pronunciation,
                    pronunciation_units=[
                        PronunciationSpan(
                            source=unit.source,
                            reading=unit.reading,
                            start=unit.start,
                            end=unit.end,
                        )
                        for unit in line.pronunciation_units
                    ],
                    hidden=line.hidden,
                )
            )
        return LyricsDocument(
            lines=shifted_lines,
            metadata=dict(self.metadata),
            source_format=self.source_format,
        )
