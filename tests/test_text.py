from karaoke_forge.text import alignment_key, split_display_units, split_edge_whitespace


def test_split_display_units_preserves_original_text() -> None:
    source = "“你好，世界！” Hello, world."
    units = split_display_units(source)
    assert "".join(unit.text for unit in units) == source
    assert [unit.key for unit in units] == ["你", "好", "世", "界", "hello", "world"]


def test_alignment_key_normalizes_width_and_case() -> None:
    assert alignment_key("Ｈｅｌｌｏ—WORLD!") == "helloworld"


def test_split_edge_whitespace_keeps_only_the_sung_core() -> None:
    assert split_edge_whitespace("  abc   ") == ("  ", "abc", "   ")
    assert split_edge_whitespace("   ") == ("   ", "", "")
