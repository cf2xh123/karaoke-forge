from types import SimpleNamespace

import pytest

from karaoke_forge.artwork import ArtworkError, download_public_cover


class _Response:
    def __init__(self, content_type: str = "image/jpeg") -> None:
        self.headers = SimpleNamespace(get_content_type=lambda: content_type)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _size):
        return b"jpeg-data"


def test_download_public_cover_validates_host_and_image_type(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("karaoke_forge.artwork._open_url", lambda *_args, **_kwargs: _Response())

    result = download_public_cover(
        "https://p1.music.126.net/example.jpg",
        tmp_path / "cover",
    )

    assert result.name == "cover.jpg"
    assert result.read_bytes() == b"jpeg-data"

    with pytest.raises(ArtworkError, match="不在受支持"):
        download_public_cover("https://example.com/private.jpg", tmp_path / "blocked")


def test_download_public_cover_accepts_netease_image_jpg_header(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "karaoke_forge.artwork._open_url",
        lambda *_args, **_kwargs: _Response("image/jpg"),
    )

    result = download_public_cover(
        "https://p1.music.126.net/example.jpg",
        tmp_path / "netease-cover",
    )

    assert result.suffix == ".jpg"
