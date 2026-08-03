from __future__ import annotations

from email.message import Message

import pytest

from karaoke_forge.utaten import (
    UtaTenLinkError,
    build_utaten_document,
    fetch_public_utaten_info,
    parse_utaten_page,
    resolve_utaten_lyric_url,
)

SAMPLE_PAGE = """
<html><body>
  <h2 class="newLyricTitle__main">
    Example Song
    <span class="newLyricTitle_afterTxt">歌詞</span>
    <span class="newLyricTitle__subTitle">Example TV ED</span>
  </h2>
  <dt class="newLyricWork__name"><h3>Example Artist</h3></dt>
  <div class="lyricBody">
    <div class="medium"><div class="hiragana">
      <span class="ruby"><span class="rb">迷</span><span class="rt">まよ</span></span>い<br>
      <span class="ruby"><span class="rb">Wake</span><span class="rt">ウェイク</span></span> up<br>
    </div></div>
  </div>
  <div class="review">This text must not be imported.</div>
</body></html>
"""


def test_resolve_utaten_lyric_url_accepts_share_text() -> None:
    lyric_id, canonical = resolve_utaten_lyric_url(
        "この歌詞 https://www.utaten.com/lyric/yh15042710/?from=share を使う"
    )

    assert lyric_id == "yh15042710"
    assert canonical == "https://utaten.com/lyric/yh15042710/"


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/lyric/yh15042710/",
        "https://utaten.com/artist/9341/",
        "not a link",
    ],
)
def test_resolve_utaten_lyric_url_rejects_other_pages(value: str) -> None:
    with pytest.raises(UtaTenLinkError):
        resolve_utaten_lyric_url(value)


def test_parse_utaten_page_keeps_original_and_furigana_separate() -> None:
    info = parse_utaten_page(
        SAMPLE_PAGE,
        canonical_url="https://utaten.com/lyric/yh15042710/",
    )

    assert info.title == "Example Song"
    assert info.artist == "Example Artist"
    assert info.lyrics == ("迷い", "Wake up")
    assert info.readings == ("まよい", "ウェイク up")
    assert "review" not in info.plain_lyrics.lower()

    document = build_utaten_document(info)
    assert document.source_format == "utaten"
    assert document.metadata["source"] == "UtaTen"
    assert document.lines[0].pronunciation == "まよい"
    assert document.lines[1].pronunciation == "ウェイク up"


def test_fetch_public_utaten_info_reads_utf8_page(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self) -> None:
            self.headers = Message()
            self.headers["Content-Type"] = "text/html; charset=utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def geturl(self) -> str:
            return "https://utaten.com/lyric/yh15042710/"

        def read(self, _size: int) -> bytes:
            return SAMPLE_PAGE.encode("utf-8")

    monkeypatch.setattr(
        "karaoke_forge.utaten._open_url",
        lambda _request, timeout: FakeResponse(),
    )

    info = fetch_public_utaten_info("https://utaten.com/lyric/yh15042710/")

    assert info.lyric_id == "yh15042710"
    assert info.plain_lyrics == "迷い\nWake up\n"
