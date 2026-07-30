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
class LyricLine:
    text: str
    start: float | None = None
    end: float | None = None
    tokens: list[KaraokeToken] = field(default_factory=list)

    @property
    def is_timed(self) -> bool:
        return self.start is not None and self.end is not None


@dataclass
class LyricsDocument:
    lines: list[LyricLine]
    metadata: dict[str, str] = field(default_factory=dict)
    source_format: str = "txt"

    @property
    def is_timed(self) -> bool:
        return bool(self.lines) and all(line.is_timed for line in self.lines)

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
