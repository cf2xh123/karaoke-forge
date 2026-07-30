from karaoke_forge.pronunciation import (
    english_pronunciation,
    generate_pronunciation,
    japanese_pronunciation,
)


def test_english_words_get_katakana_with_karaoke_overrides() -> None:
    dictionary = {
        "Its": "イッツ",
        "silence": "サイレンス",
    }
    result = english_pronunciation(
        "It's silence beyond this ocean?",
        lookup=dictionary.get,
    )

    assert result is not None
    assert result.text == "イッツ　サイレンス　ビヨンド　ディス　オーシャン"
    assert [unit.source for unit in result.units] == [
        "It's",
        "silence",
        "beyond",
        "this",
        "ocean",
    ]
    assert [(unit.start, unit.end) for unit in result.units] == [
        (0, 4),
        (5, 12),
        (13, 19),
        (20, 24),
        (25, 30),
    ]


def test_japanese_furigana_only_labels_kanji_segments() -> None:
    def convert(_text: str):
        return [
            {"orig": "吐い", "hira": "はい"},
            {"orig": "た", "hira": "た"},
            {"orig": "息", "hira": "いき"},
            {"orig": "は", "hira": "は"},
            {"orig": "空", "hira": "そら"},
        ]

    result = japanese_pronunciation("吐いた息は空", converter=convert)

    assert result is not None
    assert result.text == "はい　いき　そら"
    assert [(unit.start, unit.end) for unit in result.units] == [(0, 2), (3, 4), (5, 6)]


def test_metadata_lines_are_not_annotated() -> None:
    assert generate_pronunciation("作曲 : Aviel Kaei Tozzo") is None
