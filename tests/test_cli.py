from pathlib import Path

from karaoke_forge.cli import main


def test_convert_command(tmp_path: Path) -> None:
    source = tmp_path / "input.srt"
    output = tmp_path / "output.vtt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHello\n",
        encoding="utf-8",
    )

    result = main(["convert", str(source), "-o", str(output)])

    assert result == 0
    assert output.read_text(encoding="utf-8").startswith("WEBVTT")
