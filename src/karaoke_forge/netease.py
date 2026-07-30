from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from .align import AlignmentReport
from .ass import AssStyle
from .formats import export_formats, read_lyrics
from .pipeline import AlignOptions, align_audio_and_lyrics


class NeteaseLinkError(ValueError):
    pass


class NeteaseAccessError(RuntimeError):
    pass


_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_ID_RE = re.compile(r"[?&#]id=(\d+)", re.IGNORECASE)
_DIRECT_HOSTS = {"music.163.com", "y.music.163.com"}
_SHORT_HOSTS = {"163cn.tv", "www.163cn.tv"}


@dataclass(frozen=True)
class NeteaseTrack:
    song_id: str
    title: str
    artists: tuple[str, ...]
    canonical_url: str
    audio_path: Path
    duration: float | None = None
    page_lyrics: str | None = None

    @property
    def artist_text(self) -> str:
        return " / ".join(self.artists) if self.artists else "未知歌手"


@dataclass(frozen=True)
class NeteaseSongInfo:
    song_id: str
    title: str
    artists: tuple[str, ...]
    canonical_url: str
    duration: float | None = None
    page_lyrics: str | None = None

    @property
    def artist_text(self) -> str:
        return " / ".join(self.artists) if self.artists else "未知歌手"


@dataclass(frozen=True)
class NeteaseAlignOptions:
    align: AlignOptions = field(default_factory=AlignOptions)
    style: AssStyle = field(default_factory=AssStyle)
    formats: tuple[str, ...] = ("lrc", "elrc", "srt", "vtt", "ass", "json")
    use_page_lyrics: bool = True
    keep_audio: bool = False
    rights_confirmed: bool = False


@dataclass(frozen=True)
class NeteaseAlignResult:
    track: NeteaseTrack
    exports: dict[str, Path]
    alignment_report: AlignmentReport | None
    alignment_skipped: bool
    kept_audio: Path | None


def _extract_shared_url(value: str) -> str:
    match = _URL_RE.search(value.strip())
    if not match:
        raise NeteaseLinkError("没有找到有效的 http/https 链接。")
    return match.group(0).rstrip("。，、；;！!）)]}")


def _validate_direct_song_url(url: str) -> tuple[str, str]:
    decoded = unquote(url)
    parsed = urlsplit(decoded)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in _DIRECT_HOSTS:
        raise NeteaseLinkError("目前只支持 music.163.com 的网易云单曲链接。")
    if not re.search(r"(?:^|/|#/)(?:m/)?song(?:[/?#]|$)", decoded, re.IGNORECASE):
        raise NeteaseLinkError("链接不是网易云单曲页面；暂不支持歌单、专辑或电台链接。")
    song_id_match = _ID_RE.search(decoded)
    if not song_id_match:
        raise NeteaseLinkError("链接中没有找到歌曲 ID。")
    song_id = song_id_match.group(1)
    return song_id, f"https://music.163.com/song?id={song_id}"


def resolve_netease_song_url(value: str, *, timeout: float = 15.0) -> tuple[str, str]:
    """Resolve share text or a short URL into a canonical NetEase song URL."""

    url = _extract_shared_url(value)
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host in _DIRECT_HOSTS:
        return _validate_direct_song_url(url)
    if host not in _SHORT_HOSTS:
        raise NeteaseLinkError("目前只支持网易云音乐的单曲链接或 163cn.tv 分享短链接。")

    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 Karaoke-Forge/0.1"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
    except Exception as exc:
        raise NeteaseLinkError(f"网易云分享短链接解析失败：{exc}") from exc
    return _validate_direct_song_url(final_url)


def _download_public_json(url: str, *, timeout: float = 15.0) -> dict[str, object]:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 Karaoke-Forge/0.1",
            "Referer": "https://music.163.com/",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise NeteaseAccessError(f"无法读取网易云公开歌曲信息：{exc}") from exc
    if not isinstance(payload, dict):
        raise NeteaseAccessError("网易云返回了无法识别的歌曲信息。")
    return payload


def fetch_public_netease_info(
    value: str,
    *,
    timeout: float = 15.0,
) -> NeteaseSongInfo:
    """Fetch public metadata and LRC without requesting a playable audio URL."""

    song_id, canonical_url = resolve_netease_song_url(value, timeout=timeout)
    detail = _download_public_json(
        f"https://music.163.com/api/song/detail?id={song_id}&ids=%5B{song_id}%5D",
        timeout=timeout,
    )
    songs = detail.get("songs")
    if not isinstance(songs, list) or not songs or not isinstance(songs[0], dict):
        raise NeteaseAccessError("网易云页面没有返回这首歌的公开信息。")
    song = songs[0]
    artists_data = song.get("artists")
    artists: tuple[str, ...] = ()
    if isinstance(artists_data, list):
        artists = tuple(
            str(artist["name"])
            for artist in artists_data
            if isinstance(artist, dict) and artist.get("name")
        )
    duration_value = song.get("duration")
    duration = float(duration_value) / 1000 if isinstance(duration_value, (int, float)) else None

    lyric_info = _download_public_json(
        f"https://music.163.com/api/song/lyric?id={song_id}&lv=-1&tv=-1",
        timeout=timeout,
    )
    original = lyric_info.get("lrc")
    page_lyrics = None
    if isinstance(original, dict) and isinstance(original.get("lyric"), str):
        value = original["lyric"].strip()
        if value and value != "[99:00.00]纯音乐，请欣赏":
            page_lyrics = value + "\n"
    return NeteaseSongInfo(
        song_id=song_id,
        title=str(song.get("name") or f"netease-{song_id}"),
        artists=artists,
        canonical_url=canonical_url,
        duration=duration,
        page_lyrics=page_lyrics,
    )


def _page_lyrics(info: dict[str, object]) -> str | None:
    subtitles = info.get("subtitles")
    if not isinstance(subtitles, dict):
        return None
    for key in ("lyrics", "lyrics_merged", "lyrics_translated"):
        entries = subtitles.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("data"), str):
                value = entry["data"].strip()
                if value:
                    return value + "\n"
    return None


def _downloaded_path(info: dict[str, object], prepared: Path, source_dir: Path) -> Path:
    requested = info.get("requested_downloads")
    if isinstance(requested, list):
        for item in requested:
            if isinstance(item, dict):
                filepath = item.get("filepath")
                if isinstance(filepath, str) and Path(filepath).is_file():
                    return Path(filepath).resolve()
    if prepared.is_file():
        return prepared.resolve()
    candidates = [
        path
        for path in source_dir.glob("audio.*")
        if path.is_file() and path.suffix.lower() not in {".json", ".lrc", ".part"}
    ]
    if not candidates:
        raise NeteaseAccessError("下载过程结束，但没有找到音频文件。")
    return max(candidates, key=lambda path: path.stat().st_mtime).resolve()


def download_public_netease_track(
    value: str,
    output_dir: str | Path,
    *,
    progress: Callable[[str], None] | None = None,
) -> NeteaseTrack:
    """Download only audio exposed to an anonymous yt-dlp NetEase session."""

    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ImportError as exc:
        raise NeteaseAccessError(
            '网易云适配器尚未安装。请运行 `pip install -e ".[netease]"`。'
        ) from exc

    public_info = fetch_public_netease_info(value)
    song_id = public_info.song_id
    canonical_url = public_info.canonical_url
    source_dir = Path(output_dir).resolve()
    source_dir.mkdir(parents=True, exist_ok=True)
    last_bucket = -1

    def hook(data: dict[str, object]) -> None:
        nonlocal last_bucket
        if not progress or data.get("status") != "downloading":
            return
        downloaded = data.get("downloaded_bytes")
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        if isinstance(downloaded, (int, float)) and isinstance(total, (int, float)) and total:
            percent = min(100, int(downloaded * 100 / total))
            bucket = percent // 10
            if bucket != last_bucket:
                last_bucket = bucket
                progress(f"正在获取公开音频：{percent}%")

    class QuietLogger:
        def debug(self, _message: str) -> None:
            return

        def warning(self, message: str) -> None:
            if progress:
                progress(f"下载器提示：{message}")

        def error(self, _message: str) -> None:
            return

    options = {
        "format": "bestaudio/best",
        "outtmpl": str(source_dir / "audio.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "usenetrc": False,
        "cachedir": False,
        "progress_hooks": [hook],
        "logger": QuietLogger(),
    }
    if progress:
        progress("正在读取网易云单曲信息")
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(canonical_url, download=True)
            if not isinstance(info, dict):
                raise NeteaseAccessError("网易云返回了无法识别的歌曲信息。")
            prepared = Path(downloader.prepare_filename(info))
    except DownloadError as exc:
        raise NeteaseAccessError(
            "无法从匿名公开页面获取这首歌的音频。它可能需要会员或登录、"
            "存在地区限制，或已经下架。请改用你合法拥有的本地音频文件。"
        ) from exc

    audio_path = _downloaded_path(info, prepared, source_dir)
    creators = info.get("creators")
    if isinstance(creators, list):
        artists = tuple(str(item) for item in creators if item)
    else:
        creator = info.get("artist") or info.get("creator")
        artists = (str(creator),) if creator else ()
    duration_value = info.get("duration")
    duration = float(duration_value) if isinstance(duration_value, (int, float)) else None
    return NeteaseTrack(
        song_id=str(info.get("id") or song_id),
        title=str(info.get("title") or public_info.title),
        artists=artists or public_info.artists,
        canonical_url=canonical_url,
        audio_path=audio_path,
        duration=duration or public_info.duration,
        page_lyrics=_page_lyrics(info) or public_info.page_lyrics,
    )


def align_netease_song(
    link: str,
    lyrics_path: str | Path | None,
    output_dir: str | Path,
    *,
    local_audio_path: str | Path | None = None,
    name: str | None = None,
    options: NeteaseAlignOptions | None = None,
    progress: Callable[[str], None] | None = None,
) -> NeteaseAlignResult:
    options = options or NeteaseAlignOptions()
    if not options.rights_confirmed:
        raise PermissionError("请先确认你有权获取并处理该歌曲，且只处理网易云公开可播放的内容。")

    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source_directory = directory / ".source"
    source_directory.mkdir(parents=True, exist_ok=True)
    downloaded_by_tool = local_audio_path is None
    if local_audio_path is None:
        track = download_public_netease_track(
            link,
            source_directory,
            progress=progress,
        )
    else:
        local_audio = Path(local_audio_path)
        if not local_audio.is_file():
            raise FileNotFoundError(f"Audio file not found: {local_audio}")
        if local_audio.suffix.lower() == ".ncm":
            raise NeteaseAccessError(
                "不支持转换或解密 NCM 文件。请通过官方允许的方式取得 "
                "MP3、FLAC、WAV 或 M4A 后再上传。"
            )
        info = fetch_public_netease_info(link)
        track = NeteaseTrack(
            song_id=info.song_id,
            title=info.title,
            artists=info.artists,
            canonical_url=info.canonical_url,
            audio_path=local_audio.resolve(),
            duration=info.duration,
            page_lyrics=info.page_lyrics,
        )
        if progress:
            progress("已使用用户提供的本地音频；未请求网易云音频")

    effective_lyrics: Path
    if lyrics_path is not None:
        effective_lyrics = Path(lyrics_path)
    elif options.use_page_lyrics and track.page_lyrics:
        effective_lyrics = source_directory / "platform-lyrics.lrc"
        effective_lyrics.write_text(track.page_lyrics, encoding="utf-8")
        if progress:
            progress("已使用网易云页面公开歌词")
    else:
        raise ValueError("请提供歌词；该歌曲页面没有可用的公开歌词。")

    source = read_lyrics(effective_lyrics)
    report: AlignmentReport | None = None
    alignment_skipped = source.is_timed
    if alignment_skipped:
        document = source
        if progress:
            progress("歌词已有时间轴，跳过语音识别")
    else:
        aligned = align_audio_and_lyrics(
            track.audio_path,
            effective_lyrics,
            options=options.align,
            work_dir=directory / ".work",
            progress=progress,
        )
        document = aligned.document
        report = aligned.report

    document.metadata.update(
        {
            "source": "NetEase Music",
            "source_url": track.canonical_url,
            "source_id": track.song_id,
            "ti": track.title,
            "ar": track.artist_text,
        }
    )
    stem = _safe_filename(name or track.title)
    exports = export_formats(
        document,
        directory,
        stem,
        list(options.formats),
        ass_style=options.style,
    )

    kept_audio: Path | None = track.audio_path if downloaded_by_tool else None
    if downloaded_by_tool and not options.keep_audio:
        track.audio_path.unlink(missing_ok=True)
        kept_audio = None
        if progress:
            progress("时间轴生成完成，临时音频已清理")
    return NeteaseAlignResult(
        track=track,
        exports=exports,
        alignment_report=report,
        alignment_skipped=alignment_skipped,
        kept_audio=kept_audio,
    )


def _safe_filename(value: str) -> str:
    result = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", value)
    result = re.sub(r"\s+", " ", result).strip(" .-_")
    return result[:100] or "netease-song"
