from __future__ import annotations

import ssl
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class ArtworkError(RuntimeError):
    pass


_ALLOWED_COVER_HOSTS = {
    "p1.music.126.net",
    "p2.music.126.net",
    "p3.music.126.net",
    "p4.music.126.net",
    "y.gtimg.cn",
}
_MAX_IMAGE_BYTES = 15 * 1024 * 1024
_IMAGE_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _open_url(request: Request, *, timeout: float):
    try:
        import certifi
    except ImportError:
        return urlopen(request, timeout=timeout)
    context = ssl.create_default_context(cafile=certifi.where())
    return urlopen(request, timeout=timeout, context=context)


def download_public_cover(
    url: str,
    output_stem: str | Path,
    *,
    timeout: float = 20.0,
) -> Path:
    """Download one cover from a trusted public music-artwork host."""

    parsed = urlsplit((url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in _ALLOWED_COVER_HOSTS:
        raise ArtworkError("专辑封面链接不在受支持的公开图片来源中。")
    request = Request(
        url,
        headers={
            "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.5",
            "Referer": "https://music.163.com/" if "126.net" in host else "https://y.qq.com/",
            "User-Agent": "Mozilla/5.0 Karaoke-Forge/0.13.0",
        },
    )
    try:
        with _open_url(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type().lower()
            suffix = _IMAGE_SUFFIXES.get(content_type)
            if suffix is None:
                raise ArtworkError(f"封面返回了不支持的图片格式：{content_type}")
            payload = response.read(_MAX_IMAGE_BYTES + 1)
    except ArtworkError:
        raise
    except Exception as exc:
        raise ArtworkError(f"无法读取在线专辑封面：{exc}") from exc
    if not payload or len(payload) > _MAX_IMAGE_BYTES:
        raise ArtworkError("在线专辑封面为空或超过 15 MB。")
    target = Path(output_stem).with_suffix(suffix).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target
