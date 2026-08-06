from __future__ import annotations

import json
import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from .align import AlignmentReport
from .ass import AssStyle
from .formats import attach_reference_translation, export_formats, read_lyrics
from .media import probe_media_duration
from .pipeline import (
    AlignOptions,
    align_audio_and_lyrics,
    normalize_timing_refinement,
    refine_audio_word_timing_with_fallback,
    should_refine_timing,
)


class NeteaseLinkError(ValueError):
    pass


class NeteaseAccessError(RuntimeError):
    pass


_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_ID_RE = re.compile(r"[?&#]id=(\d+)", re.IGNORECASE)
_DIRECT_HOSTS = {"music.163.com", "y.music.163.com"}
_SHORT_HOSTS = {"163cn.tv", "www.163cn.tv"}
_COOKIE_BROWSERS = {"brave", "chrome", "edge", "firefox"}
_QUALITY_LEVELS = (
    "standard",
    "higher",
    "exhigh",
    "lossless",
    "hires",
    "jyeffect",
    "jymaster",
    "sky",
)
_VIP_LEVELS = {"lossless", "hires", "jyeffect"}
_SVIP_LEVELS = {"jymaster", "sky"}
_QUALITY_LABELS = {
    "standard": "标准",
    "higher": "较高",
    "exhigh": "极高",
    "lossless": "无损",
    "hires": "Hi-Res",
    "jyeffect": "高清臻音",
    "jymaster": "超清母带",
    "sky": "沉浸环绕声",
}


def _open_url(request: Request, *, timeout: float):
    try:
        import certifi
    except ImportError:
        return urlopen(request, timeout=timeout)
    context = ssl.create_default_context(cafile=certifi.where())
    return urlopen(request, timeout=timeout, context=context)


@dataclass(frozen=True)
class NeteaseTrack:
    song_id: str
    title: str
    artists: tuple[str, ...]
    canonical_url: str
    audio_path: Path
    duration: float | None = None
    page_lyrics: str | None = None
    word_lyrics: str | None = None
    translated_lyrics: str | None = None
    authenticated: bool = False
    quality_level: str | None = None
    access_tier: str = "anonymous"
    audio_duration: float | None = None
    is_preview: bool = False
    cover_url: str | None = None

    @property
    def artist_text(self) -> str:
        return " / ".join(self.artists) if self.artists else "未知歌手"

    @property
    def access_text(self) -> str | None:
        if not self.quality_level:
            return None
        quality = _QUALITY_LABELS.get(self.quality_level, self.quality_level)
        session = "已登录" if self.authenticated else "匿名"
        tier = {
            "svip": "SVIP",
            "vip": "VIP",
            "free": "普通权限",
        }.get(self.access_tier, self.access_tier)
        return f"{session} · {tier} · {quality}"


@dataclass(frozen=True)
class NeteaseSongInfo:
    song_id: str
    title: str
    artists: tuple[str, ...]
    canonical_url: str
    duration: float | None = None
    page_lyrics: str | None = None
    word_lyrics: str | None = None
    translated_lyrics: str | None = None
    cover_url: str | None = None

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
    cookie_browser: str | None = None
    cookie_browser_profile: str | None = None
    timing_refinement: str = "auto"
    refine_word_timing: bool | None = None


@dataclass(frozen=True)
class NeteaseAlignResult:
    track: NeteaseTrack
    exports: dict[str, Path]
    alignment_report: AlignmentReport | None
    alignment_skipped: bool
    kept_audio: Path | None
    timing_refinement_warning: str | None = None


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
        headers={"User-Agent": "Mozilla/5.0 Karaoke-Forge/0.12.0"},
        method="GET",
    )
    try:
        with _open_url(request, timeout=timeout) as response:
            final_url = response.geturl()
    except Exception as exc:
        raise NeteaseLinkError(f"网易云分享短链接解析失败：{exc}") from exc
    return _validate_direct_song_url(final_url)


def _download_public_json(url: str, *, timeout: float = 15.0) -> dict[str, object]:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 Karaoke-Forge/0.12.0",
            "Referer": "https://music.163.com/",
        },
    )
    try:
        with _open_url(request, timeout=timeout) as response:
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
    album_data = song.get("album")
    cover_url = None
    if isinstance(album_data, dict) and isinstance(album_data.get("picUrl"), str):
        cover_url = album_data["picUrl"].strip() or None

    lyric_info = _download_public_json(
        f"https://music.163.com/api/song/lyric?id={song_id}&lv=-1&kv=-1&tv=-1&rv=-1&yv=-1",
        timeout=timeout,
    )
    original = lyric_info.get("lrc")
    page_lyrics = None
    if isinstance(original, dict) and isinstance(original.get("lyric"), str):
        value = original["lyric"].strip()
        if value and value != "[99:00.00]纯音乐，请欣赏":
            page_lyrics = value + "\n"
    translated = lyric_info.get("tlyric")
    translated_lyrics = None
    if isinstance(translated, dict) and isinstance(translated.get("lyric"), str):
        value = translated["lyric"].strip()
        if value:
            translated_lyrics = value + "\n"
    word_timed = lyric_info.get("yrc")
    word_lyrics = None
    if isinstance(word_timed, dict) and isinstance(word_timed.get("lyric"), str):
        value = word_timed["lyric"].strip()
        if value:
            word_lyrics = value + "\n"
    return NeteaseSongInfo(
        song_id=song_id,
        title=str(song.get("name") or f"netease-{song_id}"),
        artists=artists,
        canonical_url=canonical_url,
        duration=duration,
        page_lyrics=page_lyrics,
        word_lyrics=word_lyrics,
        translated_lyrics=translated_lyrics,
        cover_url=cover_url,
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


def _cookie_browser_options(
    browser: str | None,
    profile: str | None,
) -> dict[str, object]:
    normalized = (browser or "").strip().lower()
    if not normalized:
        return {}
    if normalized not in _COOKIE_BROWSERS:
        supported = "、".join(sorted(_COOKIE_BROWSERS))
        raise ValueError(f"不支持从 {browser!r} 读取登录状态；可选浏览器：{supported}。")
    normalized_profile = (profile or "").strip() or None
    return {
        "cookiesfrombrowser": (
            normalized,
            normalized_profile,
            None,
            None,
        )
    }


def _has_netease_login_cookie(cookie_jar: object) -> bool:
    try:
        cookies = iter(cookie_jar)  # type: ignore[arg-type]
    except TypeError:
        return False
    for cookie in cookies:
        domain = str(getattr(cookie, "domain", "")).lstrip(".").lower()
        expiry_check = getattr(cookie, "is_expired", None)
        expired = bool(expiry_check()) if callable(expiry_check) else False
        if (
            getattr(cookie, "name", None) == "MUSIC_U"
            and (
                domain == "163.com"
                or domain == "music.163.com"
                or domain.endswith(".music.163.com")
            )
            and not expired
        ):
            return True
    return False


def _quality_access(info: dict[str, object]) -> tuple[str | None, str]:
    available: set[str] = set()
    formats = info.get("formats")
    if isinstance(formats, list):
        for item in formats:
            if isinstance(item, dict) and isinstance(item.get("format_id"), str):
                available.add(item["format_id"])

    requested = info.get("requested_downloads")
    if isinstance(requested, list):
        for item in requested:
            if isinstance(item, dict) and isinstance(item.get("format_id"), str):
                available.add(item["format_id"])

    highest = next((level for level in reversed(_QUALITY_LEVELS) if level in available), None)
    if highest in _SVIP_LEVELS:
        return highest, "svip"
    if highest in _VIP_LEVELS:
        return highest, "vip"
    return highest, "free"


def _report_access(
    *,
    progress: Callable[[str], None] | None,
    browser: str | None,
    authenticated: bool,
    quality_level: str | None,
    access_tier: str,
) -> None:
    if not progress:
        return
    if authenticated:
        progress(f"已从 {browser} 检测到网易云登录会话（Cookie 仅在本机内存中使用）")
    else:
        progress("未启用浏览器登录会话，将按匿名权限获取")

    quality = _QUALITY_LABELS.get(quality_level or "", quality_level or "未知")
    if access_tier == "svip":
        progress(f"已确认当前会话对本曲具有 SVIP 音质权限，最高可用：{quality}")
    elif access_tier == "vip":
        progress(f"已确认当前会话对本曲具有 VIP 音质权限，最高可用：{quality}")
    elif authenticated:
        progress(f"已登录，但本曲未返回 VIP 音质权限；将使用最高可用音质：{quality}")
    else:
        progress(f"本曲匿名最高可用音质：{quality}")


def download_netease_track(
    value: str,
    output_dir: str | Path,
    *,
    cookie_browser: str | None = None,
    cookie_browser_profile: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> NeteaseTrack:
    """Download audio available to an anonymous or user-authorized browser session."""

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
    browser_options = _cookie_browser_options(cookie_browser, cookie_browser_profile)
    normalized_browser = (cookie_browser or "").strip().lower() or None
    source_dir = Path(output_dir).resolve()
    source_dir.mkdir(parents=True, exist_ok=True)
    last_bucket = -1
    authenticated = False
    quality_level: str | None = None
    access_tier = "free"
    access_reported = False

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
                source_label = "账号可用音频" if normalized_browser else "公开音频"
                progress(f"正在获取{source_label}：{percent}%")

    def report_access_before_download(
        info: dict[str, object],
        *,
        incomplete: bool,
    ) -> None:
        nonlocal quality_level, access_tier, access_reported
        if incomplete or access_reported:
            return
        quality_level, access_tier = _quality_access(info)
        if quality_level:
            _report_access(
                progress=progress,
                browser=normalized_browser,
                authenticated=authenticated,
                quality_level=quality_level,
                access_tier=access_tier,
            )
            access_reported = True

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
        "match_filter": report_access_before_download,
        "logger": QuietLogger(),
        **browser_options,
    }
    if progress:
        progress("正在读取网易云单曲信息")
    try:
        with YoutubeDL(options) as downloader:
            authenticated = (
                _has_netease_login_cookie(downloader.cookiejar) if normalized_browser else False
            )
            if normalized_browser and not authenticated:
                profile_hint = (
                    f"（配置：{cookie_browser_profile}）" if cookie_browser_profile else ""
                )
                raise NeteaseAccessError(
                    f"没有在 {normalized_browser}{profile_hint} 中找到有效的网易云登录会话。"
                    "请先在该浏览器打开 music.163.com 并登录；若浏览器正在占用 Cookie，"
                    "请关闭浏览器后重试。"
                )
            info = downloader.extract_info(canonical_url, download=True)
            if not isinstance(info, dict):
                raise NeteaseAccessError("网易云返回了无法识别的歌曲信息。")
            prepared = Path(downloader.prepare_filename(info))
            quality_level, access_tier = _quality_access(info)
            if not access_reported:
                _report_access(
                    progress=progress,
                    browser=normalized_browser,
                    authenticated=authenticated,
                    quality_level=quality_level,
                    access_tier=access_tier,
                )
    except NeteaseAccessError:
        raise
    except DownloadError as exc:
        error_text = str(exc)
        normalized_error = error_text.casefold()
        if (
            normalized_browser
            and "could not find" in normalized_error
            and "cookies database" in normalized_error
        ):
            profile_hint = (
                f"（配置：{cookie_browser_profile}）" if cookie_browser_profile else ""
            )
            raise NeteaseAccessError(
                f"没有找到 {normalized_browser}{profile_hint} 的 Cookie 数据库。"
                "请确认“账号权限”选择的是实际登录网易云的浏览器；"
                "便携版或非标准 Chromium 浏览器需要在“浏览器配置”填写用户配置目录。"
            ) from exc
        if (
            normalized_browser
            and "could not copy" in normalized_error
            and "cookie database" in normalized_error
        ):
            raise NeteaseAccessError(
                f"{normalized_browser} 正在占用登录数据库，暂时无法安全读取网易云会话。"
                "请关闭该浏览器的全部窗口，并在任务管理器确认浏览器进程已退出后，"
                "改用命令行运行；或者在 Firefox 登录网易云后选择 Firefox。"
            ) from exc
        if normalized_browser:
            raise NeteaseAccessError(
                "已读取浏览器登录会话，但网易云没有向当前账号返回这首歌的可用音频。"
                "请确认会员仍有效、账号能在官方网页播放该曲，并检查地区或版权限制。"
            ) from exc
        raise NeteaseAccessError(
            "无法从匿名公开页面获取这首歌的音频。它可能需要会员或登录、"
            "存在地区限制，或已经下架。请改用你合法拥有的本地音频文件。"
        ) from exc
    except Exception as exc:
        if normalized_browser:
            raise NeteaseAccessError(
                f"无法从 {normalized_browser} 读取或使用网易云登录会话：{exc}"
            ) from exc
        raise

    audio_path = _downloaded_path(info, prepared, source_dir)
    audio_duration = probe_media_duration(audio_path)
    duration_value = info.get("duration")
    duration = float(duration_value) if isinstance(duration_value, (int, float)) else None
    expected_duration = duration or public_info.duration
    is_preview = bool(
        audio_duration is not None
        and expected_duration is not None
        and audio_duration + max(5.0, expected_duration * 0.1) < expected_duration
    )
    if is_preview and progress:
        progress(
            f"平台只返回了 {audio_duration:.1f} 秒试听片段，"
            f"完整歌曲应为约 {expected_duration:.1f} 秒"
        )
    creators = info.get("creators")
    if isinstance(creators, list):
        artists = tuple(str(item) for item in creators if item)
    else:
        creator = info.get("artist") or info.get("creator")
        artists = (str(creator),) if creator else ()
    return NeteaseTrack(
        song_id=str(info.get("id") or song_id),
        title=str(info.get("title") or public_info.title),
        artists=artists or public_info.artists,
        canonical_url=canonical_url,
        audio_path=audio_path,
        duration=duration or public_info.duration,
        page_lyrics=_page_lyrics(info) or public_info.page_lyrics,
        word_lyrics=public_info.word_lyrics,
        translated_lyrics=public_info.translated_lyrics,
        authenticated=authenticated,
        quality_level=quality_level,
        access_tier=access_tier,
        audio_duration=audio_duration,
        is_preview=is_preview,
        cover_url=public_info.cover_url,
    )


def download_public_netease_track(
    value: str,
    output_dir: str | Path,
    *,
    progress: Callable[[str], None] | None = None,
) -> NeteaseTrack:
    """Download only audio exposed to an anonymous NetEase session."""

    return download_netease_track(value, output_dir, progress=progress)


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
        track = download_netease_track(
            link,
            source_directory,
            cookie_browser=options.cookie_browser,
            cookie_browser_profile=options.cookie_browser_profile,
            progress=progress,
        )
        if track.is_preview:
            raise NeteaseAccessError(
                "网易云只返回了试听片段，无法据此生成完整时间轴。"
                "请提供完整本地音频，或在“制作卡拉 OK MV”中上传带完整音轨的 MV。"
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
            word_lyrics=info.word_lyrics,
            translated_lyrics=info.translated_lyrics,
            cover_url=info.cover_url,
        )
        if progress:
            progress("已使用用户提供的本地音频；未请求网易云音频")

    effective_lyrics: Path
    if lyrics_path is not None:
        effective_lyrics = Path(lyrics_path)
    elif options.use_page_lyrics and (track.word_lyrics or track.page_lyrics):
        if track.word_lyrics:
            effective_lyrics = source_directory / "platform-lyrics.yrc"
            effective_lyrics.write_text(track.word_lyrics, encoding="utf-8")
        else:
            effective_lyrics = source_directory / "platform-lyrics.lrc"
            effective_lyrics.write_text(track.page_lyrics or "", encoding="utf-8")
        if progress:
            detail = "逐字时间歌词" if track.word_lyrics else "行级时间歌词"
            progress(f"已使用网易云页面公开{detail}")
    else:
        raise ValueError("请提供歌词；该歌曲页面没有可用的公开歌词。")

    source = read_lyrics(effective_lyrics)
    report: AlignmentReport | None = None
    timing_refinement_warning: str | None = None
    alignment_skipped = source.is_timed
    if source.is_timed:
        timing_mode = normalize_timing_refinement(
            options.timing_refinement,
            legacy_refine_word_timing=options.refine_word_timing,
        )
        needs_refinement = should_refine_timing(source, timing_mode)
        if needs_refinement:
            if progress:
                detail = "强制" if timing_mode == "force" else "自动"
                progress(f"逐字时间精修策略：{detail}，将使用演唱音频重新检查时间")
            refined = refine_audio_word_timing_with_fallback(
                track.audio_path,
                source,
                timing_mode=timing_mode,
                options=options.align,
                work_dir=directory / ".work",
                progress=progress,
            )
            if refined is None:
                document = source
                timing_refinement_warning = (
                    "Whisper 暂不可用，自动逐字时间精修未完成；已保留原时间轴。"
                )
            else:
                document = refined.document
                report = refined.report
                alignment_skipped = False
        else:
            document = source
            if progress:
                if timing_mode == "off":
                    progress("逐字时间精修已关闭，完全保留输入文件时间")
                elif source.metadata.get("word_timing") == "source":
                    progress("歌词已包含真实逐字时间，直接用于卡拉 OK 扫色")
                else:
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
    if track.translated_lyrics:
        attached = attach_reference_translation(
            document,
            track.page_lyrics,
            track.translated_lyrics,
        )
        if attached and progress:
            progress(f"已附加 {attached} 行网易云中文翻译")

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
        timing_refinement_warning=timing_refinement_warning,
    )


def _safe_filename(value: str) -> str:
    result = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", value)
    result = re.sub(r"\s+", " ", result).strip(" .-_")
    return result[:100] or "netease-song"
