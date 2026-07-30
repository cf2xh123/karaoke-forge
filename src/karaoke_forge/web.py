from __future__ import annotations

import importlib.util
import html
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .ass import AssStyle
from .formats import (
    attach_reference_translation,
    export_formats,
    read_lyrics,
    write_format,
)
from .netease import (
    NeteaseAlignOptions,
    align_netease_song,
    download_netease_track,
    fetch_public_netease_info,
)
from .pipeline import AlignOptions, align_audio_and_lyrics
from .pronunciation import generate_pronunciation
from .workflows import MakeOptions, make_karaoke_video

WEB_CSS = """
:root {
  --kf-ink: #162033;
  --kf-muted: #697386;
  --kf-paper: #f7f3ea;
  --kf-card: rgba(255, 255, 255, 0.92);
  --kf-line: #e3ded2;
  --kf-orange: #ffad1f;
  --kf-orange-dark: #d87800;
  --kf-teal: #0b6671;
  --kf-teal-soft: #dceff0;
}

.gradio-container {
  background:
    radial-gradient(circle at 8% 3%, rgba(255, 173, 31, 0.16), transparent 22rem),
    radial-gradient(circle at 92% 10%, rgba(11, 102, 113, 0.12), transparent 26rem),
    var(--kf-paper) !important;
  color: var(--kf-ink) !important;
  min-height: 100vh;
}

.kf-shell {
  max-width: 1240px;
  margin: 0 auto;
}

.kf-hero {
  position: relative;
  overflow: hidden;
  padding: 34px 38px;
  border-radius: 30px;
  background: var(--kf-ink);
  color: #fff;
  box-shadow: 0 22px 60px rgba(22, 32, 51, 0.18);
  margin: 10px 0 22px;
}

.kf-hero::after {
  content: "";
  position: absolute;
  right: -45px;
  top: -62px;
  width: 230px;
  height: 230px;
  border-radius: 50%;
  border: 42px solid rgba(255, 173, 31, 0.88);
  box-shadow: 0 0 0 18px rgba(255, 255, 255, 0.08);
}

.kf-kicker {
  color: #ffd27a;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: .18em;
  text-transform: uppercase;
  margin-bottom: 10px;
}

.kf-title {
  font-size: clamp(28px, 4vw, 52px);
  line-height: 1.05;
  font-weight: 900;
  letter-spacing: -.04em;
  margin: 0;
  max-width: 760px;
}

.kf-subtitle {
  color: rgba(255,255,255,.72);
  font-size: 16px;
  margin: 14px 0 0;
  max-width: 690px;
}

.kf-steps {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 22px;
}

.kf-step {
  padding: 8px 12px;
  border: 1px solid rgba(255,255,255,.16);
  border-radius: 999px;
  color: rgba(255,255,255,.82);
  font-size: 13px;
  background: rgba(255,255,255,.06);
}

.kf-step b {
  color: #ffd27a;
  margin-right: 5px;
}

.kf-card {
  background: var(--kf-card) !important;
  border: 1px solid var(--kf-line) !important;
  border-radius: 22px !important;
  box-shadow: 0 8px 30px rgba(22, 32, 51, .06) !important;
  padding: 18px !important;
}

.kf-card h2, .kf-card h3 {
  color: var(--kf-ink);
  letter-spacing: -.02em;
}

.kf-section-label {
  color: var(--kf-teal);
  font-size: 12px;
  font-weight: 800;
  letter-spacing: .12em;
  text-transform: uppercase;
  margin-bottom: 3px;
}

.kf-tip {
  border-left: 4px solid var(--kf-orange);
  background: #fff8e9;
  padding: 12px 14px;
  border-radius: 4px 12px 12px 4px;
  color: #6f5525;
  font-size: 13px;
}

.kf-primary button {
  min-height: 52px !important;
  border: 0 !important;
  border-radius: 14px !important;
  color: #172033 !important;
  font-size: 16px !important;
  font-weight: 850 !important;
  background: linear-gradient(135deg, #ffc44f, #ff9e12) !important;
  box-shadow: 0 10px 24px rgba(216, 120, 0, .22) !important;
}

.kf-primary button:hover {
  transform: translateY(-1px);
  box-shadow: 0 13px 28px rgba(216, 120, 0, .28) !important;
}

.kf-status {
  border-radius: 16px;
  min-height: 54px;
}

.kf-subtitle-preview {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  overflow: hidden;
  border-radius: 18px;
  background:
    linear-gradient(180deg, rgba(10,18,30,.08), rgba(5,10,18,.5)),
    radial-gradient(circle at 72% 32%, rgba(255,190,92,.55), transparent 18%),
    linear-gradient(135deg, #537f91 0%, #28495d 42%, #101d2b 100%);
  box-shadow: inset 0 0 70px rgba(0,0,0,.32);
}

.kf-preview-vignette {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse at 50% 40%, transparent 28%, rgba(0,0,0,.42) 100%),
    linear-gradient(155deg, transparent 45%, rgba(255,255,255,.08) 46%, transparent 48%);
}

.kf-preview-badge {
  position: absolute;
  top: 12px;
  left: 12px;
  padding: 5px 9px;
  border-radius: 999px;
  color: rgba(255,255,255,.8);
  background: rgba(5,10,18,.38);
  border: 1px solid rgba(255,255,255,.16);
  font-size: 11px;
}

.kf-footer {
  color: var(--kf-muted);
  text-align: center;
  padding: 22px 0 8px;
  font-size: 12px;
}

@media (max-width: 720px) {
  .kf-hero { padding: 26px 22px; border-radius: 22px; }
  .kf-hero::after { opacity: .32; right: -105px; }
  .kf-card { padding: 12px !important; border-radius: 17px !important; }
}
"""


@dataclass(frozen=True)
class UiJobResult:
    status: str
    video: str | None
    files: list[str]
    log: str
    output_dir: str | None


def _file_path(value: object | None) -> Path | None:
    if value is None:
        return None
    if isinstance(value, (str, os.PathLike)):
        return Path(value)
    for attribute in ("path", "name"):
        candidate = getattr(value, attribute, None)
        if candidate:
            return Path(candidate)
    return None


def _safe_stem(value: str | None, fallback: str = "karaoke") -> str:
    stem = Path((value or "").strip()).stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", stem)
    stem = re.sub(r"\s+", "-", stem).strip(" .-_")
    return stem[:80] or fallback


def _new_job_dir(kind: str) -> Path:
    configured = os.environ.get("KARAOKE_FORGE_OUTPUT_DIR")
    root = Path(configured).expanduser() if configured else Path.cwd() / "outputs"
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    directory = root / f"{kind}-{stamp}-{uuid4().hex[:6]}"
    directory.mkdir(parents=True, exist_ok=False)
    return directory.resolve()


def _prepare_lyrics(
    lyrics_file: object | None,
    pasted_lyrics: str | None,
    job_dir: Path,
) -> Path:
    if pasted_lyrics and pasted_lyrics.strip():
        target = job_dir / "lyrics.txt"
        target.write_text(pasted_lyrics.strip() + "\n", encoding="utf-8")
        return target
    source = _file_path(lyrics_file)
    if source is None or not source.is_file():
        raise ValueError("请上传歌词文件，或在歌词框中直接粘贴歌词。")
    return source


def _quality_settings(label: str) -> tuple[int, str]:
    options = {
        "快速预览": (24, "veryfast"),
        "推荐质量": (18, "medium"),
        "高质量": (16, "slow"),
    }
    return options.get(label, options["推荐质量"])


def _build_style(
    font: str,
    font_size: float,
    text_color: str,
    highlight_color: str,
    margin_v: float,
    show_translation: bool = True,
    translation_font_size: float = 38,
    translation_color: str = "#EAF4FF",
    show_pronunciation: bool = True,
    pronunciation_font_size: float = 26,
    pronunciation_color: str = "#FFFFFF",
) -> AssStyle:
    return AssStyle(
        font=font or "Microsoft YaHei",
        font_size=int(font_size),
        text_color=text_color,
        highlight_color=highlight_color,
        margin_v=int(margin_v),
        show_translation=show_translation,
        translation_font_size=int(translation_font_size),
        translation_color=translation_color,
        show_pronunciation=show_pronunciation,
        pronunciation_font_size=int(pronunciation_font_size),
        pronunciation_color=pronunciation_color,
    )


def _lyrics_with_translation(
    lyrics_path: Path,
    translated_lrc: str | None,
    job_dir: Path,
    original_lrc: str | None = None,
) -> Path:
    if not translated_lrc:
        return lyrics_path
    document = read_lyrics(lyrics_path)
    attached = attach_reference_translation(document, original_lrc, translated_lrc)
    if not attached:
        return lyrics_path
    target = job_dir / "lyrics-bilingual.json"
    target.write_text(write_format(document, "json"), encoding="utf-8")
    return target


def subtitle_preview_html(
    font: str,
    font_size: float,
    text_color: str,
    highlight_color: str,
    margin_v: float,
    show_translation: bool,
    translation_font_size: float,
    translation_color: str,
    show_pronunciation: bool,
    pronunciation_font_size: float,
    pronunciation_color: str,
    sample_text: str = "让每一句歌词，都踩准拍子。",
    sample_translation: str = "让歌声与画面在这里相遇。",
) -> str:
    """Return a browser-native preview of the current ASS subtitle style."""

    safe_font = html.escape(font or "Microsoft YaHei", quote=True)
    raw_lines = [
        line.strip()
        for line in (sample_text or "让每一句歌词，都踩准拍子。").splitlines()
        if line.strip()
    ]
    if not raw_lines:
        raw_lines = ["让每一句歌词，都踩准拍子。"]
    if len(raw_lines) >= 2:
        upper_text, lower_text = raw_lines[-2:]
    else:
        upper_text = "The lyric before this line"
        lower_text = raw_lines[0]
    split_at = max(1, round(len(lower_text) * 0.4))
    safe_translation = html.escape(sample_translation or "让歌声与画面在这里相遇。")
    main_size = max(16, min(48, round(float(font_size) * 0.55)))
    translated_size = max(13, min(36, round(float(translation_font_size) * 0.55)))
    pronunciation_size = max(10, min(24, round(float(pronunciation_font_size) * 0.55)))
    bottom = max(12, min(92, round(float(margin_v) * 0.42)))
    translation_html = ""
    if show_translation:
        translation_html = (
            '<div style="position:absolute;left:15%;right:15%;top:8%;'
            f"text-align:center;font-family:'{safe_font}',sans-serif;"
            f'font-size:{translated_size}px;color:{translation_color};font-weight:700;'
            'text-shadow:-1px -1px 0 #111,1px -1px 0 #111,'
            '-1px 1px 0 #111,1px 1px 0 #111,0 2px 6px #000;">'
            f"{safe_translation}</div>"
        )

    def coloured_source(value: str, start: int, *, active: bool) -> str:
        if not active:
            return html.escape(value)
        local_split = split_at - start
        if local_split <= 0:
            return html.escape(value)
        if local_split >= len(value):
            return f'<span style="color:{highlight_color};">{html.escape(value)}</span>'
        return (
            f'<span style="color:{highlight_color};">'
            f"{html.escape(value[:local_split])}</span>{html.escape(value[local_split:])}"
        )

    def preview_line(value: str, *, active: bool) -> str:
        pronunciation = generate_pronunciation(value) if show_pronunciation else None
        if pronunciation is None:
            return coloured_source(value, 0, active=active)
        parts: list[str] = []
        cursor = 0
        for unit in pronunciation.units:
            start = max(cursor, min(len(value), unit.start))
            end = max(start, min(len(value), unit.end or start + len(unit.source)))
            parts.append(coloured_source(value[cursor:start], cursor, active=active))
            reading = unit.reading
            reading_color = pronunciation_color
            if active and start < split_at:
                reading_color = highlight_color
            parts.append(
                '<ruby style="ruby-position:over;ruby-align:center;">'
                f"{coloured_source(value[start:end], start, active=active)}"
                f'<rt style="font-size:{pronunciation_size}px;color:{reading_color};'
                'font-weight:700;text-shadow:-1px -1px 0 #111,1px -1px 0 #111,'
                '-1px 1px 0 #111,1px 1px 0 #111,0 2px 5px #000;">'
                f"{html.escape(reading)}</rt></ruby>"
            )
            cursor = end
        parts.append(coloured_source(value[cursor:], cursor, active=active))
        return "".join(parts)

    upper_line_html = preview_line(upper_text, active=False)
    lower_line_html = preview_line(lower_text, active=True)
    return f"""
    <div class="kf-subtitle-preview" data-kf-layout="ktv-split">
      <div class="kf-preview-vignette"></div>
      {translation_html}
      <div style="position:absolute;left:6%;right:16%;
                  bottom:{bottom + main_size + 28}px;text-align:left;
                  font-family:'{safe_font}',sans-serif;font-size:{main_size}px;
                  color:{text_color};font-weight:800;
                  text-shadow:-2px -2px 0 #111,2px -2px 0 #111,
                              -2px 2px 0 #111,2px 2px 0 #111,0 3px 8px #000;">
        {upper_line_html}
      </div>
      <div style="position:absolute;left:16%;right:6%;bottom:{bottom}px;
                  text-align:right;font-family:'{safe_font}',sans-serif;
                  font-size:{main_size}px;color:{text_color};font-weight:800;
                  text-shadow:-2px -2px 0 #111,2px -2px 0 #111,
                              -2px 2px 0 #111,2px 2px 0 #111,0 3px 8px #000;">
        {lower_line_html}
      </div>
      <div class="kf-preview-badge">实时字幕预览 · KTV 双行布局</div>
    </div>
    """


def _build_align_options(
    language: str,
    model: str,
    device: str,
    separate_vocals: bool,
) -> AlignOptions:
    return AlignOptions(
        model=model,
        language=None if language == "自动识别" else language,
        device=device,
        compute_type="int8" if device == "cpu" else "default",
        separate_vocals=separate_vocals,
    )


def run_make_job(
    audio_file: object | None,
    video_file: object | None,
    lyrics_file: object | None,
    pasted_lyrics: str,
    output_name: str,
    language: str,
    model: str,
    device: str,
    separate_vocals: bool,
    quality: str,
    audio_offset: float,
    font: str,
    font_size: int,
    text_color: str,
    highlight_color: str,
    margin_v: int,
    netease_link: str = "",
    use_netease_lyrics: bool = True,
    rights_confirmed: bool = False,
    cookie_browser: str = "",
    cookie_browser_profile: str = "",
    auto_sync: bool = True,
    refine_word_timing: bool = True,
    show_translation: bool = True,
    translation_font_size: float = 38,
    translation_color: str = "#EAF4FF",
    show_pronunciation: bool = True,
    pronunciation_font_size: float = 26,
    pronunciation_color: str = "#FFFFFF",
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> UiJobResult:
    logs: list[str] = []

    def report(message: str) -> None:
        logs.append(message)
        if progress_callback:
            progress_callback(message)

    job_dir: Path | None = None
    temporary_audio: Path | None = None
    try:
        audio = _file_path(audio_file)
        video = _file_path(video_file)
        if video is None or not video.is_file():
            raise ValueError("请先上传对应的 MV 视频。")

        job_dir = _new_job_dir("mv")
        netease_info = None
        link = (netease_link or "").strip()
        if link:
            if not rights_confirmed:
                raise PermissionError("请勾选版权与使用权确认后再使用网易云链接。")
            if audio is None:
                track = download_netease_track(
                    link,
                    job_dir / ".source",
                    cookie_browser=cookie_browser,
                    cookie_browser_profile=cookie_browser_profile,
                    progress=report,
                )
                netease_info = track
                if track.is_preview:
                    track.audio_path.unlink(missing_ok=True)
                    audio = video
                    report("网易云只返回试听片段，已自动改用 MV 内嵌的完整音轨")
                else:
                    audio = track.audio_path
                    temporary_audio = track.audio_path
            else:
                netease_info = fetch_public_netease_info(link)
                report("已使用本地音频，仅从网易云读取公开歌曲信息和歌词")

        if audio is None or not audio.is_file():
            raise ValueError("请上传歌曲音频，或提供可公开播放的网易云单曲链接。")
        if audio.suffix.lower() == ".ncm":
            raise ValueError(
                "不支持转换或解密 NCM 文件；请上传官方允许导出的 MP3、FLAC、WAV 或 M4A。"
            )

        if lyrics_file is not None or (pasted_lyrics and pasted_lyrics.strip()):
            lyrics = _prepare_lyrics(lyrics_file, pasted_lyrics, job_dir)
            if netease_info is not None:
                lyrics = _lyrics_with_translation(
                    lyrics,
                    netease_info.translated_lyrics,
                    job_dir,
                    netease_info.page_lyrics,
                )
        elif (
            link
            and use_netease_lyrics
            and netease_info
            and (netease_info.word_lyrics or netease_info.page_lyrics)
        ):
            if netease_info.word_lyrics:
                lyrics = job_dir / "netease-lyrics.yrc"
                lyrics.write_text(netease_info.word_lyrics, encoding="utf-8")
            else:
                lyrics = job_dir / "netease-lyrics.lrc"
                lyrics.write_text(netease_info.page_lyrics or "", encoding="utf-8")
            lyrics = _lyrics_with_translation(
                lyrics,
                netease_info.translated_lyrics,
                job_dir,
                netease_info.page_lyrics,
            )
            timing_detail = "逐字时间轴" if netease_info.word_lyrics else "行级时间轴"
            report(f"已使用网易云页面公开歌词和{timing_detail}")
            if netease_info.translated_lyrics and lyrics.suffix == ".json":
                report("已附加网易云中文翻译，将固定显示在画面顶部")
        else:
            raise ValueError("请提供歌词，或勾选使用网易云页面公开歌词。")

        fallback_stem = (
            f"{netease_info.title}-karaoke" if netease_info is not None else f"{video.stem}-karaoke"
        )
        stem = _safe_stem(output_name, fallback=fallback_stem)
        output = job_dir / f"{stem}.mp4"
        assets = job_dir / f"{stem}.assets"
        crf, preset = _quality_settings(quality)
        report("素材检查完成")

        result = make_karaoke_video(
            audio,
            video,
            lyrics,
            output,
            assets,
            options=MakeOptions(
                align=_build_align_options(language, model, device, separate_vocals),
                style=_build_style(
                    font,
                    font_size,
                    text_color,
                    highlight_color,
                    margin_v,
                    show_translation,
                    translation_font_size,
                    translation_color,
                    show_pronunciation,
                    pronunciation_font_size,
                    pronunciation_color,
                ),
                audio_offset=float(audio_offset),
                crf=crf,
                preset=preset,
                overwrite=False,
                auto_sync=auto_sync,
                refine_word_timing=refine_word_timing,
            ),
            progress=report,
        )
        if temporary_audio:
            temporary_audio.unlink(missing_ok=True)
            report("本次获取的临时音频已清理")
        files = [str(result.video), *(str(path) for path in result.exports.values())]
        if result.alignment_report:
            alignment = (
                f"歌词匹配覆盖率 **{result.alignment_report.coverage:.1%}**，"
                f"匹配 {result.alignment_report.matched_units}/"
                f"{result.alignment_report.target_units} 个词元。"
            )
        else:
            alignment = "检测到已有时间轴歌词，因此跳过了自动对齐。"
        sync_result = getattr(result, "sync_result", None)
        if sync_result is not None:
            sync_text = (
                f"\n\n自动定位到歌曲从 MV 第 **{sync_result.offset:.2f} 秒**开始，"
                f"置信度 **{sync_result.confidence:.0%}**。"
            )
        elif audio.resolve() == video.resolve():
            sync_text = "\n\n已直接使用 MV 内嵌完整音轨。"
        else:
            sync_text = ""
        status = (
            f"### ✅ 卡拉 OK MV 已生成\n{alignment}{sync_text}"
            "\n\n成品和所有歌词格式已保存，可以预览或下载。"
        )
        report("全部完成")
        return UiJobResult(status, str(result.video), files, "\n".join(logs), str(job_dir))
    except Exception as exc:
        if temporary_audio:
            temporary_audio.unlink(missing_ok=True)
        logs.append(f"失败：{exc}")
        return UiJobResult(
            "### ⚠️ 没有生成成功\n"
            f"{exc}\n\n请检查素材是否匹配；如果是首次使用，也可以到“环境检查”页面查看依赖。",
            None,
            [],
            "\n".join(logs),
            str(job_dir) if job_dir else None,
        )


def run_align_job(
    audio_file: object | None,
    lyrics_file: object | None,
    pasted_lyrics: str,
    output_name: str,
    language: str,
    model: str,
    device: str,
    separate_vocals: bool,
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> UiJobResult:
    logs: list[str] = []

    def report(message: str) -> None:
        logs.append(message)
        if progress_callback:
            progress_callback(message)

    job_dir: Path | None = None
    try:
        audio = _file_path(audio_file)
        if audio is None or not audio.is_file():
            raise ValueError("请先上传歌曲音频。")
        job_dir = _new_job_dir("lyrics")
        lyrics_path = _prepare_lyrics(lyrics_file, pasted_lyrics, job_dir)
        source = read_lyrics(lyrics_path)
        report("素材检查完成")

        alignment_text: str
        if source.is_timed:
            document = source
            alignment_text = "检测到已有时间轴，已直接进行格式导出。"
            report("已有时间轴，跳过识别")
        else:
            result = align_audio_and_lyrics(
                audio,
                lyrics_path,
                options=_build_align_options(language, model, device, separate_vocals),
                work_dir=job_dir / ".work",
                progress=report,
            )
            document = result.document
            alignment_text = (
                f"匹配覆盖率 **{result.report.coverage:.1%}**，"
                f"匹配 {result.report.matched_units}/{result.report.target_units} 个词元。"
            )

        stem = _safe_stem(output_name, fallback=lyrics_path.stem)
        exports = export_formats(
            document,
            job_dir,
            stem,
            ["lrc", "elrc", "srt", "vtt", "ass", "json"],
        )
        report("全部歌词格式已导出")
        return UiJobResult(
            f"### ✅ 时间轴歌词已生成\n{alignment_text}",
            None,
            [str(path) for path in exports.values()],
            "\n".join(logs),
            str(job_dir),
        )
    except Exception as exc:
        logs.append(f"失败：{exc}")
        return UiJobResult(
            f"### ⚠️ 没有生成成功\n{exc}",
            None,
            [],
            "\n".join(logs),
            str(job_dir) if job_dir else None,
        )


def run_netease_align_job(
    link: str,
    local_audio_file: object | None,
    lyrics_file: object | None,
    pasted_lyrics: str,
    output_name: str,
    language: str,
    model: str,
    device: str,
    separate_vocals: bool,
    use_page_lyrics: bool,
    keep_audio: bool,
    rights_confirmed: bool,
    cookie_browser: str = "",
    cookie_browser_profile: str = "",
    *,
    progress_callback: Callable[[str], None] | None = None,
) -> UiJobResult:
    logs: list[str] = []

    def report(message: str) -> None:
        logs.append(message)
        if progress_callback:
            progress_callback(message)

    job_dir: Path | None = None
    try:
        if not (link or "").strip():
            raise ValueError("请粘贴网易云音乐单曲链接。")
        job_dir = _new_job_dir("netease")
        local_audio = _file_path(local_audio_file)
        if lyrics_file is not None or (pasted_lyrics and pasted_lyrics.strip()):
            effective_lyrics: Path | None = _prepare_lyrics(
                lyrics_file,
                pasted_lyrics,
                job_dir,
            )
        else:
            effective_lyrics = None

        result = align_netease_song(
            link,
            effective_lyrics,
            job_dir,
            local_audio_path=local_audio,
            name=_safe_stem(output_name, fallback="netease-lyrics"),
            options=NeteaseAlignOptions(
                align=_build_align_options(
                    language,
                    model,
                    device,
                    separate_vocals,
                ),
                use_page_lyrics=use_page_lyrics,
                keep_audio=keep_audio,
                rights_confirmed=rights_confirmed,
                cookie_browser=cookie_browser or None,
                cookie_browser_profile=cookie_browser_profile or None,
            ),
            progress=report,
        )
        files = [str(path) for path in result.exports.values()]
        if result.kept_audio:
            files.append(str(result.kept_audio))
        if result.alignment_report:
            timing = (
                f"重新对齐覆盖率 **{result.alignment_report.coverage:.1%}**，"
                f"匹配 {result.alignment_report.matched_units}/"
                f"{result.alignment_report.target_units} 个词元。"
            )
        else:
            timing = (
                "使用了网易云提供的真实逐字时间轴。"
                if result.track.word_lyrics
                else "使用了歌词文件中已有的行级时间轴。"
            )
        access = (
            f"账号与音质：{result.track.access_text}  \n"
            if result.track.access_text
            else ""
        )
        status = (
            f"### ✅ {result.track.title} 的时间轴已生成\n"
            f"歌手：{result.track.artist_text}  \n"
            f"{access}"
            f"{timing}"
        )
        report("全部歌词格式已导出")
        return UiJobResult(status, None, files, "\n".join(logs), str(job_dir))
    except Exception as exc:
        logs.append(f"失败：{exc}")
        return UiJobResult(
            f"### ⚠️ 没有生成成功\n{exc}",
            None,
            [],
            "\n".join(logs),
            str(job_dir) if job_dir else None,
        )


def run_convert_job(
    lyrics_file: object | None,
    output_format: str,
) -> UiJobResult:
    job_dir: Path | None = None
    try:
        source = _file_path(lyrics_file)
        if source is None or not source.is_file():
            raise ValueError("请上传一个带时间轴的歌词文件。")
        document = read_lyrics(source)
        document.require_timed()
        job_dir = _new_job_dir("convert")
        suffix = "lrc" if output_format == "elrc" else output_format
        label = "enhanced" if output_format == "elrc" else output_format
        target = job_dir / f"{source.stem}.{label}.{suffix}"
        target.write_text(write_format(document, output_format), encoding="utf-8")
        return UiJobResult(
            f"### ✅ 已转换为 {output_format.upper()}\n文件可以直接下载。",
            None,
            [str(target)],
            f"读取：{source.name}\n输出：{target.name}",
            str(job_dir),
        )
    except Exception as exc:
        return UiJobResult(
            f"### ⚠️ 转换失败\n{exc}",
            None,
            [],
            f"失败：{exc}",
            str(job_dir) if job_dir else None,
        )


def environment_markdown() -> str:
    checks = [
        ("Python 3.10+", sys.version_info >= (3, 10), sys.version.split()[0]),
        ("FFmpeg", shutil.which("ffmpeg") is not None, shutil.which("ffmpeg") or "未找到"),
        (
            "faster-whisper",
            importlib.util.find_spec("faster_whisper") is not None,
            "已安装" if importlib.util.find_spec("faster_whisper") else "未安装",
        ),
        (
            "Demucs（可选）",
            importlib.util.find_spec("demucs") is not None,
            "已安装" if importlib.util.find_spec("demucs") else "未安装",
        ),
        (
            "Gradio 网页",
            importlib.util.find_spec("gradio") is not None,
            "已安装" if importlib.util.find_spec("gradio") else "未安装",
        ),
        (
            "网易云链接适配器",
            importlib.util.find_spec("yt_dlp") is not None,
            "已安装" if importlib.util.find_spec("yt_dlp") else "未安装",
        ),
        (
            "日语/英语注音",
            importlib.util.find_spec("pykakasi") is not None
            and importlib.util.find_spec("alkana") is not None,
            "已安装"
            if importlib.util.find_spec("pykakasi") and importlib.util.find_spec("alkana")
            else "未安装；请重新运行首次安装.bat",
        ),
    ]
    rows = ["### 本机环境"]
    for name, ok, detail in checks:
        icon = "✅" if ok else "⚪"
        rows.append(f"- {icon} **{name}**：{detail}")
    rows.extend(
        [
            "",
            (
                "> faster-whisper 用于从无时间轴歌词生成时间；Demucs 只在勾选"
                "“先分离人声”时需要；yt-dlp 用于获取当前匿名或已登录账号有权播放的"
                "网易云音频；pykakasi 与 alkana 用于离线生成日语和英语注音。"
            ),
            "",
            "输出默认保存在项目的 `outputs` 目录。页面运行在本机，素材不会自动上传到公网。",
        ]
    )
    return "\n".join(rows)


def _open_output_directory(path: str | None) -> str:
    if not path:
        return "还没有可打开的输出目录。"
    directory = Path(path)
    if not directory.is_dir():
        return "输出目录已经不存在。"
    try:
        if sys.platform == "win32":
            os.startfile(directory)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(directory)])
        else:
            subprocess.Popen(["xdg-open", str(directory)])
    except Exception as exc:
        return f"无法打开目录：{exc}"
    return f"已打开：`{directory}`"


def create_web_app() -> object:
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError(
            '网页依赖尚未安装。请运行 `pip install -e ".[web]"`，'
            '需要自动对齐和网易云链接时安装 `pip install -e ".[web,align,netease]"`。'
        ) from exc

    with gr.Blocks(
        title="Karaoke Forge｜本地卡拉 OK 工作台",
        fill_width=True,
        delete_cache=(3600, 86400),
    ) as app:
        make_output_directory = gr.State()
        align_output_directory = gr.State()
        netease_output_directory = gr.State()
        gr.HTML(
            """
            <div class="kf-shell">
              <section class="kf-hero">
                <div class="kf-kicker">Karaoke Forge · Local Studio</div>
                <h1 class="kf-title">让每一句歌词，<br>都踩准拍子。</h1>
                <p class="kf-subtitle">
                  上传歌曲、MV 和歌词，剩下的交给本地工作台。
                  自动生成时间轴、逐字高亮字幕和卡拉 OK 成片。
                </p>
                <div class="kf-steps">
                  <span class="kf-step"><b>01</b>选择素材</span>
                  <span class="kf-step"><b>02</b>调整效果</span>
                  <span class="kf-step"><b>03</b>点击生成</span>
                  <span class="kf-step"><b>04</b>预览下载</span>
                </div>
              </section>
            </div>
            """
        )

        with gr.Tabs():
            with gr.Tab("制作卡拉 OK MV", id="make"), gr.Row(equal_height=False):
                with gr.Column(scale=7, min_width=340):
                    with gr.Group(elem_classes="kf-card"):
                        gr.HTML('<div class="kf-section-label">Step 01 · 素材</div>')
                        gr.Markdown("## 把三个文件放进来")
                        with gr.Row():
                            make_audio = gr.File(
                                label="① 歌曲音频（或使用网易云公开音频）",
                                file_types=["audio"],
                                type="filepath",
                            )
                            make_video = gr.File(
                                label="② 对应 MV",
                                file_types=["video"],
                                type="filepath",
                            )
                            make_lyrics = gr.File(
                                label="③ 歌词文件",
                                file_types=[".txt", ".lrc", ".srt", ".vtt", ".ass", ".json"],
                                type="filepath",
                            )
                        with gr.Accordion("没有歌词文件？直接粘贴歌词", open=False):
                            make_pasted = gr.Textbox(
                                label="一行一句",
                                lines=8,
                                placeholder="第一句歌词\n第二句歌词\n第三句歌词",
                            )
                        with gr.Accordion("使用网易云链接补充音频或歌词", open=False):
                            make_netease_link = gr.Textbox(
                                label="网易云单曲链接",
                                placeholder="https://music.163.com/song?id=...",
                            )
                            with gr.Row():
                                make_cookie_browser = gr.Dropdown(
                                    label="账号权限",
                                    choices=[
                                        ("匿名（仅公开音频）", ""),
                                        ("Chrome 已登录账号", "chrome"),
                                        ("Edge 已登录账号", "edge"),
                                        ("Firefox 已登录账号", "firefox"),
                                        ("Brave 已登录账号", "brave"),
                                    ],
                                    value="",
                                )
                                make_cookie_profile = gr.Textbox(
                                    label="浏览器配置（可选）",
                                    placeholder="留空使用默认配置，如 Profile 1",
                                )
                            make_use_netease_lyrics = gr.Checkbox(
                                label="没有上传歌词时，使用网易云页面公开歌词",
                                value=True,
                            )
                            make_rights = gr.Checkbox(
                                label="我确认账号和歌曲归我合法使用，且不会绕过地区、版权或 DRM 限制",
                                value=False,
                            )
                            gr.Markdown(
                                "> 选择浏览器后，会检测其中的网易云登录状态和本曲 VIP/SVIP"
                                "音质权限，并使用账号实际有权播放的最高音质。Cookie 只在本机"
                                "内存中读取，不会保存；本工具不接收密码，也不转换 NCM。"
                            )
                        gr.HTML(
                            '<div class="kf-tip">歌曲和 MV 应是同一版本。'
                            "已有 LRC/SRT 等时间轴歌词时，系统会自动跳过识别。</div>"
                        )

                    with gr.Group(elem_classes="kf-card"):
                        gr.HTML('<div class="kf-section-label">Step 02 · 效果</div>')
                        gr.Markdown("## 选择字幕外观")
                        with gr.Row():
                            make_name = gr.Textbox(
                                label="成品名称",
                                value="我的卡拉OK",
                            )
                            make_language = gr.Dropdown(
                                label="歌曲语言",
                                choices=[
                                    ("自动识别", "自动识别"),
                                    ("中文", "zh"),
                                    ("英语", "en"),
                                    ("日语", "ja"),
                                    ("韩语", "ko"),
                                    ("粤语", "yue"),
                                ],
                                value="自动识别",
                            )
                            make_quality = gr.Radio(
                                label="视频质量",
                                choices=["快速预览", "推荐质量", "高质量"],
                                value="推荐质量",
                            )
                        with gr.Row():
                            make_font = gr.Dropdown(
                                label="字幕字体",
                                choices=[
                                    "Microsoft YaHei",
                                    "PingFang SC",
                                    "Noto Sans CJK SC",
                                    "Arial",
                                ],
                                value="Microsoft YaHei",
                                allow_custom_value=True,
                            )
                            make_font_size = gr.Slider(32, 88, value=58, step=1, label="字号")
                            make_margin = gr.Slider(30, 180, value=72, step=2, label="底部距离")
                        with gr.Row():
                            make_text_color = gr.ColorPicker(label="未唱颜色", value="#FFFFFF")
                            make_highlight_color = gr.ColorPicker(
                                label="唱到的颜色", value="#FFD54A"
                            )
                        with gr.Row():
                            make_show_translation = gr.Checkbox(
                                label="有中文翻译时固定显示在画面顶部",
                                value=True,
                            )
                            make_translation_size = gr.Slider(
                                24,
                                58,
                                value=38,
                                step=1,
                                label="翻译字号",
                            )
                            make_translation_color = gr.ColorPicker(
                                label="翻译颜色",
                                value="#EAF4FF",
                            )
                        with gr.Row():
                            make_show_pronunciation = gr.Checkbox(
                                label="显示日语振假名和英语片假名读音",
                                value=True,
                            )
                            make_pronunciation_size = gr.Slider(
                                18,
                                40,
                                value=26,
                                step=1,
                                label="注音字号",
                            )
                            make_pronunciation_color = gr.ColorPicker(
                                label="注音颜色",
                                value="#FFFFFF",
                            )

                        with gr.Accordion("字幕实时预览", open=True):
                            with gr.Row():
                                make_preview_text = gr.Textbox(
                                    label="原文双行预览（上一行 + 当前行）",
                                    value=(
                                        "I hear the flowers whisper.\n"
                                        "Let me bloom inside your garden."
                                    ),
                                    lines=2,
                                )
                                make_preview_translation = gr.Textbox(
                                    label="中文翻译预览",
                                    value="让我在你的花园里盛放。",
                                )
                            make_style_preview = gr.HTML(
                                subtitle_preview_html(
                                    "Microsoft YaHei",
                                    58,
                                    "#FFFFFF",
                                    "#FFD54A",
                                    72,
                                    True,
                                    38,
                                    "#EAF4FF",
                                    True,
                                    26,
                                    "#FFFFFF",
                                    (
                                        "I hear the flowers whisper.\n"
                                        "Let me bloom inside your garden."
                                    ),
                                    "让我在你的花园里盛放。",
                                )
                            )

                        with gr.Accordion("高级设置", open=False):
                            with gr.Row():
                                make_model = gr.Dropdown(
                                    label="识别模型",
                                    choices=["tiny", "base", "small", "medium", "large-v3"],
                                    value="small",
                                )
                                make_device = gr.Radio(
                                    label="运行设备",
                                    choices=[
                                        ("自动选择", "auto"),
                                        ("只用 CPU", "cpu"),
                                        ("NVIDIA 显卡", "cuda"),
                                    ],
                                    value="auto",
                                )
                            with gr.Row():
                                make_separate = gr.Checkbox(
                                    label="先分离人声（更慢，复杂伴奏可尝试）",
                                    value=False,
                                )
                                make_auto_sync = gr.Checkbox(
                                    label="自动定位 MV 中歌曲开始位置",
                                    value=True,
                                )
                                make_refine_word_timing = gr.Checkbox(
                                    label="普通 LRC/SRT 根据演唱速度精修逐字时间",
                                    value=True,
                                )
                                make_offset = gr.Number(
                                    label="定位后的手动微调（秒）",
                                    value=0.0,
                                    precision=2,
                                )

                    make_button = gr.Button(
                        "开始生成卡拉 OK MV",
                        variant="primary",
                        elem_classes="kf-primary",
                    )

                with gr.Column(scale=5, min_width=320):
                    with gr.Group(elem_classes="kf-card"):
                        gr.HTML('<div class="kf-section-label">Step 03 · 成品</div>')
                        make_status = gr.Markdown(
                            "### 等待开始\n选择素材后点击生成，这里会显示结果。",
                            elem_classes="kf-status",
                        )
                        make_preview = gr.Video(label="成品预览")
                        make_downloads = gr.File(
                            label="下载视频和歌词文件",
                            file_count="multiple",
                        )
                        make_log = gr.Textbox(
                            label="处理记录",
                            lines=8,
                            interactive=False,
                        )
                        open_make_dir = gr.Button("在电脑中打开输出文件夹")
                        open_make_message = gr.Markdown()

            with gr.Tab("只生成时间轴歌词", id="align"), gr.Row(equal_height=False):
                with gr.Column(scale=7), gr.Group(elem_classes="kf-card"):
                    gr.HTML('<div class="kf-section-label">Lyrics Lab</div>')
                    gr.Markdown("## 从歌曲得到时间轴歌词")
                    with gr.Row():
                        align_audio = gr.File(
                            label="歌曲音频",
                            file_types=["audio"],
                            type="filepath",
                        )
                        align_lyrics = gr.File(
                            label="原始歌词",
                            file_types=[".txt", ".lrc", ".srt", ".vtt", ".ass", ".json"],
                            type="filepath",
                        )
                    align_pasted = gr.Textbox(
                        label="或者直接粘贴歌词",
                        lines=9,
                        placeholder="一行一句；上传文件和粘贴内容二选一即可",
                    )
                    with gr.Row():
                        align_name = gr.Textbox(label="输出名称", value="歌词时间轴")
                        align_language = gr.Dropdown(
                            label="语言",
                            choices=[
                                ("自动识别", "自动识别"),
                                ("中文", "zh"),
                                ("英语", "en"),
                                ("日语", "ja"),
                                ("韩语", "ko"),
                                ("粤语", "yue"),
                            ],
                            value="自动识别",
                        )
                        align_model = gr.Dropdown(
                            label="识别模型",
                            choices=["tiny", "base", "small", "medium", "large-v3"],
                            value="small",
                        )
                    with gr.Accordion("高级设置", open=False):
                        align_device = gr.Radio(
                            label="运行设备",
                            choices=[
                                ("自动选择", "auto"),
                                ("只用 CPU", "cpu"),
                                ("NVIDIA 显卡", "cuda"),
                            ],
                            value="auto",
                        )
                        align_separate = gr.Checkbox(
                            label="先分离人声",
                            value=False,
                        )
                    align_button = gr.Button(
                        "生成全部歌词格式",
                        variant="primary",
                        elem_classes="kf-primary",
                    )
                with gr.Column(scale=5), gr.Group(elem_classes="kf-card"):
                    align_status = gr.Markdown("### 等待开始")
                    align_downloads = gr.File(
                        label="下载时间轴歌词",
                        file_count="multiple",
                    )
                    align_log = gr.Textbox(
                        label="处理记录",
                        lines=10,
                        interactive=False,
                    )
                    open_align_dir = gr.Button("在电脑中打开输出文件夹")
                    open_align_message = gr.Markdown()

            with gr.Tab("网易云链接生成歌词", id="netease"), gr.Row(equal_height=False):
                with gr.Column(scale=7), gr.Group(elem_classes="kf-card"):
                    gr.HTML('<div class="kf-section-label">NetEase Link</div>')
                    gr.Markdown("## 从网易云单曲链接生成时间轴歌词")
                    netease_link = gr.Textbox(
                        label="网易云单曲链接",
                        placeholder="可直接粘贴整段分享文字或 https://music.163.com/song?id=...",
                    )
                    with gr.Row():
                        netease_local_audio = gr.File(
                            label="本地音频（会员歌曲建议上传）",
                            file_types=["audio"],
                            type="filepath",
                        )
                        netease_lyrics = gr.File(
                            label="自己的歌词（可选）",
                            file_types=[".txt", ".lrc", ".srt", ".vtt", ".ass", ".json"],
                            type="filepath",
                        )
                    netease_pasted = gr.Textbox(
                        label="或者粘贴自己的歌词",
                        lines=7,
                        placeholder="自己的歌词优先；留空则可使用网易云页面公开歌词",
                    )
                    with gr.Row():
                        netease_name = gr.Textbox(label="输出名称", value="网易云歌词时间轴")
                        netease_language = gr.Dropdown(
                            label="语言",
                            choices=[
                                ("自动识别", "自动识别"),
                                ("中文", "zh"),
                                ("英语", "en"),
                                ("日语", "ja"),
                                ("韩语", "ko"),
                            ],
                            value="自动识别",
                        )
                        netease_model = gr.Dropdown(
                            label="识别模型",
                            choices=["tiny", "base", "small", "medium", "large-v3"],
                            value="small",
                        )
                    netease_use_page_lyrics = gr.Checkbox(
                        label="没有提供自己的歌词时，使用网易云页面公开 LRC",
                        value=True,
                    )
                    with gr.Row():
                        netease_cookie_browser = gr.Dropdown(
                            label="账号权限",
                            choices=[
                                ("匿名（仅公开音频）", ""),
                                ("Chrome 已登录账号", "chrome"),
                                ("Edge 已登录账号", "edge"),
                                ("Firefox 已登录账号", "firefox"),
                                ("Brave 已登录账号", "brave"),
                            ],
                            value="",
                        )
                        netease_cookie_profile = gr.Textbox(
                            label="浏览器配置（可选）",
                            placeholder="留空使用默认配置，如 Profile 1",
                        )
                    netease_rights = gr.Checkbox(
                        label="我确认账号和歌曲归我合法使用，且不会绕过地区、版权或 DRM 限制",
                        value=False,
                    )
                    with gr.Accordion("高级设置", open=False):
                        netease_device = gr.Radio(
                            label="运行设备",
                            choices=[
                                ("自动选择", "auto"),
                                ("只用 CPU", "cpu"),
                                ("NVIDIA 显卡", "cuda"),
                            ],
                            value="auto",
                        )
                        netease_separate = gr.Checkbox(label="先分离人声", value=False)
                        netease_keep_audio = gr.Checkbox(
                            label="保留本次获取的音频文件",
                            value=False,
                        )
                    gr.Markdown(
                        "> 选择浏览器后，会读取其中现有的网易云登录会话，自动检测本曲"
                        " VIP/SVIP 权限并获取账号有权播放的最高音质。Cookie 只在本机内存"
                        "中使用，不会保存；不接收密码、不提升账号权限，也不会转换 NCM。"
                    )
                    netease_button = gr.Button(
                        "读取链接并生成时间轴",
                        variant="primary",
                        elem_classes="kf-primary",
                    )
                with gr.Column(scale=5), gr.Group(elem_classes="kf-card"):
                    netease_status = gr.Markdown("### 等待网易云单曲链接")
                    netease_downloads = gr.File(
                        label="下载时间轴歌词",
                        file_count="multiple",
                    )
                    netease_log = gr.Textbox(
                        label="处理记录",
                        lines=11,
                        interactive=False,
                    )
                    open_netease_dir = gr.Button("在电脑中打开输出文件夹")
                    open_netease_message = gr.Markdown()

            with gr.Tab("歌词格式转换", id="convert"), gr.Row():
                with gr.Column(scale=6), gr.Group(elem_classes="kf-card"):
                    gr.HTML('<div class="kf-section-label">Format Desk</div>')
                    gr.Markdown("## 在常见歌词格式之间转换")
                    convert_source = gr.File(
                        label="上传带时间轴歌词",
                        file_types=[".lrc", ".srt", ".vtt", ".ass", ".json"],
                        type="filepath",
                    )
                    convert_format = gr.Dropdown(
                        label="转换成",
                        choices=[
                            ("普通 LRC", "lrc"),
                            ("增强 LRC（逐词）", "elrc"),
                            ("SRT", "srt"),
                            ("WebVTT", "vtt"),
                            ("ASS 卡拉 OK 字幕", "ass"),
                            ("Karaoke Forge JSON", "json"),
                        ],
                        value="srt",
                    )
                    convert_button = gr.Button(
                        "开始转换",
                        variant="primary",
                        elem_classes="kf-primary",
                    )
                with gr.Column(scale=6), gr.Group(elem_classes="kf-card"):
                    convert_status = gr.Markdown("### 等待文件")
                    convert_download = gr.File(
                        label="下载转换结果",
                        file_count="multiple",
                    )
                    convert_log = gr.Textbox(
                        label="转换记录",
                        lines=6,
                        interactive=False,
                    )

            with gr.Tab("环境检查与帮助", id="doctor"), gr.Row():
                with gr.Column(scale=7), gr.Group(elem_classes="kf-card"):
                    environment = gr.Markdown(environment_markdown())
                    refresh_environment = gr.Button("重新检查")
                with gr.Column(scale=5), gr.Group(elem_classes="kf-card"):
                    gr.Markdown(
                        """
                                ### 第一次使用

                                1. Windows 用户先双击项目根目录的 `首次安装.bat`。
                                2. 安装完成后双击 `启动网页版.bat`。
                                3. 首次自动对齐会下载 Whisper 模型，等待时间取决于网络。

                                ### 常见情况

                                - **只有格式转换需求**：不需要 Whisper。
                                - **歌词已有时间轴**：制作 MV 时不会运行 Whisper。
                                - **网易云会员歌曲**：可选择已登录浏览器来使用账号现有权限；
                                  也可上传官方允许导出的标准音频；不支持 NCM。
                                - **匹配率低**：确认歌词与歌曲是同一版本，或尝试分离人声。
                                - **字幕没有中文字体**：在样式里换成本机已安装字体。
                                """
                    )

        gr.HTML(
            '<div class="kf-footer">Karaoke Forge · 本地处理 · '
            "请确保你拥有歌曲、歌词和视频的使用权</div>"
        )

        def make_wrapper(
            audio: object,
            video: object,
            lyrics: object,
            pasted: str,
            name: str,
            language: str,
            model: str,
            device: str,
            separate: bool,
            quality: str,
            offset: float,
            font: str,
            font_size: int,
            text_color: str,
            highlight_color: str,
            margin: int,
            netease_link: str,
            use_netease_lyrics: bool,
            rights_confirmed: bool,
            cookie_browser: str,
            cookie_browser_profile: str,
            auto_sync: bool,
            refine_word_timing: bool,
            show_translation: bool,
            translation_font_size: float,
            translation_color: str,
            show_pronunciation: bool,
            pronunciation_font_size: float,
            pronunciation_color: str,
            progress: object = gr.Progress(),
        ) -> tuple[str, str | None, list[str], str, str | None]:
            def update(message: str) -> None:
                progress(0.5, desc=message)

            result = run_make_job(
                audio,
                video,
                lyrics,
                pasted,
                name,
                language,
                model,
                device,
                separate,
                quality,
                offset,
                font,
                font_size,
                text_color,
                highlight_color,
                margin,
                netease_link,
                use_netease_lyrics,
                rights_confirmed,
                cookie_browser,
                cookie_browser_profile,
                auto_sync,
                refine_word_timing,
                show_translation,
                translation_font_size,
                translation_color,
                show_pronunciation,
                pronunciation_font_size,
                pronunciation_color,
                progress_callback=update,
            )
            progress(1.0, desc="完成" if result.video else "未完成")
            return (
                result.status,
                result.video,
                result.files,
                result.log,
                result.output_dir,
            )

        make_button.click(
            make_wrapper,
            inputs=[
                make_audio,
                make_video,
                make_lyrics,
                make_pasted,
                make_name,
                make_language,
                make_model,
                make_device,
                make_separate,
                make_quality,
                make_offset,
                make_font,
                make_font_size,
                make_text_color,
                make_highlight_color,
                make_margin,
                make_netease_link,
                make_use_netease_lyrics,
                make_rights,
                make_cookie_browser,
                make_cookie_profile,
                make_auto_sync,
                make_refine_word_timing,
                make_show_translation,
                make_translation_size,
                make_translation_color,
                make_show_pronunciation,
                make_pronunciation_size,
                make_pronunciation_color,
            ],
            outputs=[
                make_status,
                make_preview,
                make_downloads,
                make_log,
                make_output_directory,
            ],
            show_progress="full",
        )

        preview_inputs = [
            make_font,
            make_font_size,
            make_text_color,
            make_highlight_color,
            make_margin,
            make_show_translation,
            make_translation_size,
            make_translation_color,
            make_show_pronunciation,
            make_pronunciation_size,
            make_pronunciation_color,
            make_preview_text,
            make_preview_translation,
        ]
        for preview_input in preview_inputs:
            preview_input.change(
                subtitle_preview_html,
                inputs=preview_inputs,
                outputs=make_style_preview,
                queue=False,
            )

        def align_wrapper(
            audio: object,
            lyrics: object,
            pasted: str,
            name: str,
            language: str,
            model: str,
            device: str,
            separate: bool,
            progress: object = gr.Progress(),
        ) -> tuple[str, list[str], str, str | None]:
            def update(message: str) -> None:
                progress(0.5, desc=message)

            result = run_align_job(
                audio,
                lyrics,
                pasted,
                name,
                language,
                model,
                device,
                separate,
                progress_callback=update,
            )
            progress(1.0, desc="完成" if result.files else "未完成")
            return result.status, result.files, result.log, result.output_dir

        align_button.click(
            align_wrapper,
            inputs=[
                align_audio,
                align_lyrics,
                align_pasted,
                align_name,
                align_language,
                align_model,
                align_device,
                align_separate,
            ],
            outputs=[align_status, align_downloads, align_log, align_output_directory],
            show_progress="full",
        )

        def netease_wrapper(
            link: str,
            local_audio: object,
            lyrics: object,
            pasted: str,
            name: str,
            language: str,
            model: str,
            device: str,
            separate: bool,
            use_page_lyrics: bool,
            keep_audio: bool,
            rights_confirmed: bool,
            cookie_browser: str,
            cookie_browser_profile: str,
            progress: object = gr.Progress(),
        ) -> tuple[str, list[str], str, str | None]:
            def update(message: str) -> None:
                progress(0.5, desc=message)

            result = run_netease_align_job(
                link,
                local_audio,
                lyrics,
                pasted,
                name,
                language,
                model,
                device,
                separate,
                use_page_lyrics,
                keep_audio,
                rights_confirmed,
                cookie_browser,
                cookie_browser_profile,
                progress_callback=update,
            )
            progress(1.0, desc="完成" if result.files else "未完成")
            return result.status, result.files, result.log, result.output_dir

        netease_button.click(
            netease_wrapper,
            inputs=[
                netease_link,
                netease_local_audio,
                netease_lyrics,
                netease_pasted,
                netease_name,
                netease_language,
                netease_model,
                netease_device,
                netease_separate,
                netease_use_page_lyrics,
                netease_keep_audio,
                netease_rights,
                netease_cookie_browser,
                netease_cookie_profile,
            ],
            outputs=[
                netease_status,
                netease_downloads,
                netease_log,
                netease_output_directory,
            ],
            show_progress="full",
        )

        def convert_wrapper(
            source: object,
            output_format: str,
        ) -> tuple[str, list[str], str]:
            result = run_convert_job(source, output_format)
            return result.status, result.files, result.log

        convert_button.click(
            convert_wrapper,
            inputs=[convert_source, convert_format],
            outputs=[convert_status, convert_download, convert_log],
        )
        refresh_environment.click(
            environment_markdown,
            outputs=environment,
            queue=False,
        )
        open_make_dir.click(
            _open_output_directory,
            inputs=make_output_directory,
            outputs=open_make_message,
            queue=False,
        )
        open_align_dir.click(
            _open_output_directory,
            inputs=align_output_directory,
            outputs=open_align_message,
            queue=False,
        )
        open_netease_dir.click(
            _open_output_directory,
            inputs=netease_output_directory,
            outputs=open_netease_message,
            queue=False,
        )

    return app


def launch_web_app(
    *,
    host: str = "127.0.0.1",
    port: int = 7860,
    open_browser: bool = True,
) -> None:
    import gradio as gr

    app = create_web_app()
    theme = gr.themes.Base(
        primary_hue="orange",
        secondary_hue="teal",
        neutral_hue="slate",
        radius_size="lg",
    )
    app.queue(default_concurrency_limit=1).launch(
        server_name=host,
        server_port=port,
        inbrowser=open_browser,
        share=False,
        show_error=True,
        theme=theme,
        css=WEB_CSS,
    )
