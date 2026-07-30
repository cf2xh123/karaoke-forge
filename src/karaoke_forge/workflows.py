from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .align import AlignmentReport
from .ass import AssStyle
from .formats import export_formats, read_lyrics
from .media import render_karaoke_video
from .models import LyricsDocument
from .pipeline import AlignOptions, align_audio_and_lyrics


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


@dataclass(frozen=True)
class MakeResult:
    document: LyricsDocument
    exports: dict[str, Path]
    video: Path
    alignment_report: AlignmentReport | None
    alignment_skipped: bool


def make_karaoke_video(
    audio_path: str | Path,
    video_path: str | Path,
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
    video_source = Path(video_path)
    output = Path(output_path)
    assets = Path(assets_dir)
    if not audio.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio}")
    if not video_source.is_file():
        raise FileNotFoundError(f"Video file not found: {video_source}")
    if output.exists() and not options.overwrite:
        raise FileExistsError(f"Output already exists: {output}. Pass --overwrite to replace it.")
    assets.mkdir(parents=True, exist_ok=True)

    source_document = read_lyrics(lyrics_path)
    report: AlignmentReport | None = None
    alignment_skipped = source_document.is_timed
    if alignment_skipped:
        document = source_document
        if progress:
            progress("Lyrics already contain a timeline; alignment was skipped.")
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
        audio_offset=options.audio_offset,
        crf=options.crf,
        preset=options.preset,
        audio_bitrate=options.audio_bitrate,
        overwrite=options.overwrite,
        progress=progress,
    )
    return MakeResult(
        document=document,
        exports=exports,
        video=video,
        alignment_report=report,
        alignment_skipped=alignment_skipped,
    )
