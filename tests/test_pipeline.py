from karaoke_forge.formats import read_lyrics
from karaoke_forge.pipeline import _build_initial_prompt


def test_plain_lyrics_prompt_preserves_stanza_breaks(tmp_path) -> None:
    lyrics_path = tmp_path / "lyrics.txt"
    raw_text = "第一段\n\n第二段\n"
    lyrics_path.write_text(raw_text, encoding="utf-8")

    prompt = _build_initial_prompt(lyrics_path, read_lyrics(lyrics_path))

    assert prompt == raw_text
