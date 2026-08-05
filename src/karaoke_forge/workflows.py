from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .align import AlignmentReport
from .ass import AssStyle
from .formats import export_formats, read_lyrics
from .media import (
    AudioSyncResult,
    create_spinning_cover_video,
    detect_audio_sync,
    probe_media_has_audio,
    render_karaoke_video,
)
from .models import LyricsDocument
from .pipeline import (
    AlignOptions,
    align_audio_and_lyrics,
    normalize_timing_refinement,
    refine_audio_word_timing_with_fallback,
    should_refine_timing,
)


@dataclass(frozen=True)
class MakeOptions:
    align: AlignOptions = field(default_factory=AlignOptions)
    style: AssStyle = field(default_factory=AssStyle)
    formats: tuple[str, ...] = ("lrc", "elrc", "srt", "vtt", "ass", "json")
    audio_offset: float = 0.0
    crf: int = 18
    preset: str = "medium"
    audio_bitrate: str = "320k"
    overwrite: bool = False
    auto_sync: bool = False
    timing_refinement: str = "auto"
    refine_word_timing: bool | None = None
    cover_image: Path | None = None
    font_files: tuple[Path, ...] = ()
    cover_background: str = "adaptive"
    cover_style: str = "turntable"
    cover_waveform: bool = True


@dataclass(frozen=True)
class MakeResult:
    document: LyricsDocument
    exports: dict[str, Path]
    video: Path
    alignment_report: AlignmentReport | None
    alignment_skipped: bool
    audio_offset: float
    sync_result: AudioSyncResult | None
    timing_refinement_warning: str | None = None


def make_karaoke_video(
    audio_path: str | Path,
    video_path: str | Path | None,
    lyrics_path: str | Path,
    output_path: str | Path,
    assets_dir: str | Path,
    *,
    options: MakeOptions | None = None,
    progress: Callable[[str], None] | None = None,
) -> MakeResult:
    """Run the complete lyrics-to-karaoke workflow for CLI and web callers."""

    options = options or MakeOptions()
    audio = Path(audio_path)
    video_source = Path(video_path) if video_path is not None else None
    output = Path(output_path)
    assets = Path(assets_dir)
    if not audio.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio}")
    if video_source is not None and not video_source.is_file():
        raise FileNotFoundError(f"Video file not found: {video_source}")
    cover_image = Path(options.cover_image) if options.cover_image is not None else None
    if video_source is None and (cover_image is None or not cover_image.is_file()):
        raise ValueError("Video or cover image is required.")
    if output.exists() and not options.overwrite:
        raise FileExistsError(f"Output already exists: {output}. Pass --overwrite to replace it.")
    assets.mkdir(parents=True, exist_ok=True)

    generated_cover_background = video_source is None
    if generated_cover_background:
        assert cover_image is not None
        video_source = create_spinning_cover_video(
            cover_image,
            audio,
            assets / "spinning-cover-background.mp4",
            overwrite=options.overwrite,
            background_theme=options.cover_background,
            style=options.cover_style,
            show_waveform=options.cover_waveform,
            progress=progress,
        )
        if progress:
            progress("没有 MV，已改用旋转专辑封面作为画面")
    assert video_source is not None

    effective_offset = options.audio_offset
    sync_result: AudioSyncResult | None = None
    if options.auto_sync and not generated_cover_background:
        if audio.resolve() == video_source.resolve():
            if progress:
                progress("正在使用 MV 内嵌完整音轨，无需额外偏移")
        elif probe_media_has_audio(video_source) is False:
            if progress:
                progress(
                    "MV 没有内嵌音轨，已跳过音轨指纹自动同步；"
                    f"将使用上传音频并保留 {effective_offset:+.2f} 秒手动偏移"
                )
        else:
            sync_result = detect_audio_sync(audio, video_source, progress=progress)
            if not sync_result.reliable:
                raise ValueError(
                    "歌曲音频与 MV 音轨无法可靠匹配："
                    f"仅命中 {sync_result.matched_windows}/{sync_result.total_windows} 个指纹窗口，"
                    f"置信度 {sync_result.confidence:.0%}。请确认歌曲和 MV 是同一版本，"
                    "或关闭自动定位后手动设置偏移。"
                )
            effective_offset += sync_result.offset
            if progress:
                progress(
                    f"已定位歌曲开始位置：MV 第 {sync_result.offset:.2f} 秒"
                    f"（置信度 {sync_result.confidence:.0%}）"
                )

    source_document = read_lyrics(lyrics_path)
    report: AlignmentReport | None = None
    timing_refinement_warning: str | None = None
    alignment_skipped = source_document.is_timed
    if source_document.is_timed:
        timing_mode = normalize_timing_refinement(
            options.timing_refinement,
            legacy_refine_word_timing=options.refine_word_timing,
        )
        needs_refinement = should_refine_timing(source_document, timing_mode)
        if needs_refinement:
            if progress:
                detail = "强制" if timing_mode == "force" else "自动"
                progress(f"逐字时间精修策略：{detail}，将使用演唱音频重新检查时间")
            refined = refine_audio_word_timing_with_fallback(
                audio,
                source_document,
                timing_mode=timing_mode,
                options=options.align,
                work_dir=assets / ".work",
                progress=progress,
            )
            if refined is None:
                document = source_document
                timing_refinement_warning = (
                    "Whisper 暂不可用，自动逐字时间精修未完成；已保留原时间轴。"
                )
            else:
                document = refined.document
                report = refined.report
                alignment_skipped = False
        else:
            document = source_document
            if progress:
                if timing_mode == "off":
                    progress("逐字时间精修已关闭，完全保留输入文件时间")
                elif source_document.metadata.get("word_timing") == "source":
                    progress("歌词已包含真实逐字时间，直接用于卡拉 OK 扫色")
                else:
                    progress("歌词已有时间轴，已跳过语音识别")
    else:
        aligned = align_audio_and_lyrics(
            audio,
            lyrics_path,
            options=options.align,
            work_dir=assets / ".work",
            progress=progress,
        )
        document = aligned.document
        report = aligned.report
    if effective_offset:
        document = document.shifted(effective_offset)
        if progress:
            progress(f"已将歌词时间轴整体偏移 {effective_offset:+.2f} 秒")

    formats = list(dict.fromkeys([*options.formats, "ass"]))
    exports = export_formats(
        document,
        assets,
        output.stem,
        formats,
        ass_style=options.style,
    )
    video = render_karaoke_video(
        video_source,
        exports["ass"],
        output,
        audio_path=audio,
        audio_offset=effective_offset,
        crf=options.crf,
        preset=options.preset,
        audio_bitrate=options.audio_bitrate,
        font_files=options.font_files,
        overwrite=options.overwrite,
        progress=progress,
    )
    return MakeResult(
        document=document,
        exports=exports,
        video=video,
        alignment_report=report,
        alignment_skipped=alignment_skipped,
        audio_offset=effective_offset,
        sync_result=sync_result,
        timing_refinement_warning=timing_refinement_warning,
    )
