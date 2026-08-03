from __future__ import annotations

from email.message import Message

import pytest

from karaoke_forge.models import LyricLine, LyricsDocument, PronunciationSpan
from karaoke_forge.utaten import (
    UtaTenLinkError,
    UtaTenLyricsInfo,
    UtaTenPronunciationUnit,
    apply_utaten_pronunciation,
    build_utaten_document,
    fetch_public_utaten_info,
    match_utaten_lines,
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
    assert [
        (unit.source, unit.reading, unit.start, unit.end)
        for unit in document.lines[0].pronunciation_units
    ] == [("迷", "まよ", 0, 1)]
    assert [
        (unit.source, unit.reading, unit.start, unit.end)
        for unit in document.lines[1].pronunciation_units
    ] == [("Wake", "ウェイク", 0, 4)]


def test_parser_keeps_ruby_on_its_exact_line_when_plain_lines_repeat() -> None:
    page = """
    <h2 class="newLyricTitle__main">Repeated</h2>
    <dt class="newLyricWork__name">Artist</dt>
    <div class="lyricBody"><div class="hiragana">
      <ruby>空<rt>そら</rt></ruby><br>
      空<br>
      <ruby>空<rt>から</rt></ruby><br>
    </div></div>
    """

    info = parse_utaten_page(page, canonical_url="https://utaten.com/lyric/repeated/")

    assert info.lyrics == ("空", "空", "空")
    assert [
        [(unit.source, unit.reading) for unit in line]
        for line in info.pronunciation_units
    ] == [[("空", "そら")], [], [("空", "から")]]


def test_utaten_line_matching_tolerates_punctuation_and_inserted_local_lines() -> None:
    matches = match_utaten_lines(
        ("[Intro]", "迷い。", "Wake  up!", "local-only"),
        ("迷い", "Wake up"),
    )

    assert matches == ((1, 0), (2, 1))


def test_utaten_only_mode_clears_wrong_readings_and_maps_verified_ruby_spans() -> None:
    document = LyricsDocument(
        lines=[
            LyricLine(
                text="前書き",
                pronunciation="まちがい",
                pronunciation_units=[PronunciationSpan("前", "ご", 0, 1)],
            ),
            LyricLine(text="迷い。", pronunciation="めい"),
            LyricLine(text="Wake  up!", pronunciation="ワケ アップ"),
            LyricLine(text="光は朝焼けに消える", pronunciation="ひかり"),
        ]
    )
    info = UtaTenLyricsInfo(
        lyric_id="example",
        title="Song",
        artist="Artist",
        canonical_url="https://utaten.com/lyric/example/",
        lyrics=("迷い", "Wake up", "光は朝焼けに消えた"),
        readings=("まよい", "ウェイク up", "ひかりはあさやけにきえた"),
        pronunciation_units=(
            (UtaTenPronunciationUnit("迷", "まよ", 0, 1),),
            (UtaTenPronunciationUnit("Wake", "ウェイク", 0, 4),),
            (
                UtaTenPronunciationUnit("光", "ひかり", 0, 1),
                UtaTenPronunciationUnit("朝焼", "あさや", 2, 4),
                UtaTenPronunciationUnit("消", "き", 6, 7),
            ),
        ),
    )

    report = apply_utaten_pronunciation(document, info, replace_existing=True)

    assert report.matched_lines == 3
    assert report.annotated_lines == 3
    assert report.cleared_lines == 4
    assert document.lines[0].pronunciation is None
    assert document.lines[0].pronunciation_units == []
    assert [
        (unit.source, unit.reading, unit.start, unit.end)
        for unit in document.lines[1].pronunciation_units
    ] == [("迷", "まよ", 0, 1)]
    assert [
        (unit.source, unit.reading, unit.start, unit.end)
        for unit in document.lines[2].pronunciation_units
    ] == [("Wake", "ウェイク", 0, 4)]
    assert [unit.source for unit in document.lines[3].pronunciation_units] == [
        "光",
        "朝焼",
        "消",
    ]
    assert document.metadata["auto_pronunciation"] == "false"


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
