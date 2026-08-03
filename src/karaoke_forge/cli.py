from __future__ import annotations

import argparse
import importlib.util
import re
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__
from .ass import AssStyle
from .formats import attach_reference_translation, export_formats, read_lyrics, write_format
from .media import MediaError, find_ffmpeg, render_karaoke_video
from .pipeline import (
    AlignOptions,
    align_audio_and_lyrics,
    refine_audio_word_timing_with_fallback,
    should_refine_timing,
)
from .runtime import inspect_demucs_runtime
from .workflows import MakeOptions, make_karaoke_video

DEFAULT_FORMATS = "lrc,elrc,srt,vtt,ass,json"


def _progress(message: str) -> None:
    print(f"[karaoke-forge] {message}", file=sys.stderr, flush=True)


def _formats(value: str) -> list[str]:
    formats = [item.strip().lower() for item in value.split(",") if item.strip()]
    supported = {"lrc", "elrc", "srt", "vtt", "ass", "json"}
    invalid = sorted(set(formats) - supported)
    if invalid:
        raise argparse.ArgumentTypeError(f"unsupported format(s): {', '.join(invalid)}")
    return formats


def _language(value: str) -> str | None:
    return None if value.lower() == "auto" else value


def _style_from_args(args: argparse.Namespace) -> AssStyle:
    width, height = (int(value) for value in args.resolution.lower().split("x", 1))
    return AssStyle(
        font=args.font,
        font_size=args.font_size,
        text_color=args.text_color,
        highlight_color=args.highlight_color,
        outline_color=args.outline_color,
        margin_v=args.margin_v,
        resolution=(width, height),
        show_translation=args.show_translation,
        translation_font_size=args.translation_font_size,
        translation_color=args.translation_color,
        show_pronunciation=args.show_pronunciation,
        auto_english_pronunciation=args.auto_english_pronunciation,
        pronunciation_font_size=args.pronunciation_font_size,
        pronunciation_color=args.pronunciation_color,
    )


def _add_style_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("karaoke subtitle style")
    group.add_argument("--font", default="Microsoft YaHei", help="subtitle font family")
    group.add_argument("--font-size", type=int, default=58)
    group.add_argument("--text-color", default="#FFFFFF")
    group.add_argument("--highlight-color", default="#FFD54A")
    group.add_argument("--outline-color", default="#111111")
    group.add_argument("--margin-v", type=int, default=72, help="bottom margin in pixels")
    group.add_argument(
        "--show-translation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="show the current translation at the top center of the video",
    )
    group.add_argument("--translation-font-size", type=int, default=38)
    group.add_argument("--translation-color", default="#EAF4FF")
    group.add_argument(
        "--show-pronunciation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="show Japanese furigana and English katakana readings above lyric rows",
    )
    group.add_argument(
        "--auto-english-pronunciation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="automatically generate katakana readings for English words",
    )
    group.add_argument("--pronunciation-font-size", type=int, default=26)
    group.add_argument("--pronunciation-color", default="#FFFFFF")
    group.add_argument("--resolution", default="1920x1080", help="ASS design resolution")


def _add_alignment_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("alignment")
    group.add_argument("--model", default="small", help="faster-whisper model name or path")
    group.add_argument("--language", default="auto", help="language code such as zh, en, ja")
    group.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    group.add_argument("--compute-type", default="default")
    group.add_argument("--beam-size", type=int, default=5)
    group.add_argument("--minimum-coverage", type=float, default=0.2)
    group.add_argument(
        "--separate-vocals",
        action="store_true",
        help="run Demucs before recognition (slower, often better for dense mixes)",
    )
    group.add_argument("--demucs-model", default="htdemucs")


def _add_timing_refinement_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timing-refinement",
        choices=["off", "auto", "force"],
        default="auto",
        help=(
            "word timing policy: off preserves input, auto refines synthetic "
            "timing, force rechecks all timed lyrics"
        ),
    )
    parser.add_argument(
        "--refine-word-timing",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=argparse.SUPPRESS,
    )


def _timing_refinement_from_args(args: argparse.Namespace) -> str:
    legacy = getattr(args, "refine_word_timing", None)
    if legacy is not None:
        return "auto" if legacy else "off"
    return str(getattr(args, "timing_refinement", "auto"))


def _alignment_options(args: argparse.Namespace) -> AlignOptions:
    return AlignOptions(
        model=args.model,
        language=_language(args.language),
        device=args.device,
        compute_type=args.compute_type,
        beam_size=args.beam_size,
        minimum_coverage=args.minimum_coverage,
        separate_vocals=args.separate_vocals,
        demucs_model=args.demucs_model,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="karaoke-forge",
        description="Create timed lyrics and karaoke videos from a song, lyrics, and an MV.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    align = subparsers.add_parser("align", help="align plain lyrics to a song")
    align.add_argument("audio", type=Path)
    align.add_argument("lyrics", type=Path)
    align.add_argument("-o", "--output-dir", type=Path, default=Path("build"))
    align.add_argument("--name", help="output basename (defaults to the lyrics filename)")
    align.add_argument("--formats", type=_formats, default=_formats(DEFAULT_FORMATS))
    _add_alignment_arguments(align)
    _add_timing_refinement_arguments(align)
    _add_style_arguments(align)
    align.set_defaults(handler=_handle_align)

    convert = subparsers.add_parser("convert", help="convert timed lyrics between formats")
    convert.add_argument("input", type=Path)
    convert.add_argument("-o", "--output", type=Path, required=True)
    convert.add_argument(
        "--format",
        choices=["lrc", "elrc", "srt", "vtt", "ass", "json"],
        help="output format (defaults to output extension)",
    )
    convert.add_argument("--overwrite", action="store_true")
    _add_style_arguments(convert)
    convert.set_defaults(handler=_handle_convert)

    render = subparsers.add_parser("render", help="burn timed lyrics into an MV")
    render.add_argument("video", type=Path)
    render.add_argument("lyrics", type=Path, help="timed LRC/SRT/VTT/ASS/JSON")
    render.add_argument("-o", "--output", type=Path, required=True)
    render.add_argument("--audio", type=Path, help="replace the MV audio with this song")
    render.add_argument(
        "--audio-offset",
        type=float,
        default=0.0,
        help="delay external audio by N seconds; negative values advance it",
    )
    render.add_argument("--crf", type=int, default=18)
    render.add_argument("--preset", default="medium")
    render.add_argument("--audio-bitrate", default="320k")
    render.add_argument("--overwrite", action="store_true")
    _add_style_arguments(render)
    render.set_defaults(handler=_handle_render)

    make = subparsers.add_parser(
        "make", help="align lyrics, export all formats, and render the karaoke MV"
    )
    make.add_argument("audio", type=Path)
    make.add_argument("video", type=Path)
    make.add_argument("lyrics", type=Path)
    make.add_argument("-o", "--output", type=Path, required=True)
    make.add_argument(
        "--assets-dir",
        type=Path,
        help="timeline output directory (defaults to <output>.assets)",
    )
    make.add_argument("--formats", type=_formats, default=_formats(DEFAULT_FORMATS))
    make.add_argument("--audio-offset", type=float, default=0.0)
    make.add_argument(
        "--auto-sync",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="locate the reference song inside the MV audio before rendering",
    )
    _add_timing_refinement_arguments(make)
    make.add_argument("--crf", type=int, default=18)
    make.add_argument("--preset", default="medium")
    make.add_argument("--audio-bitrate", default="320k")
    make.add_argument("--overwrite", action="store_true")
    _add_alignment_arguments(make)
    _add_style_arguments(make)
    make.set_defaults(handler=_handle_make)

    doctor = subparsers.add_parser("doctor", help="check local runtime dependencies")
    doctor.set_defaults(handler=_handle_doctor)

    web = subparsers.add_parser("web", help="open the visual local web interface")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=7860)
    web.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    web.set_defaults(handler=_handle_web)

    netease = subparsers.add_parser(
        "netease",
        help="create timed lyrics from a NetEase Music song link",
    )
    netease.add_argument("url", help="NetEase single-song URL or shared text")
    netease.add_argument(
        "lyrics",
        type=Path,
        nargs="?",
        help="optional lyrics file; uses public page lyrics when omitted",
    )
    netease.add_argument("-o", "--output-dir", type=Path, default=Path("build/netease"))
    netease.add_argument("--audio", type=Path, help="authorized local MP3/FLAC/WAV/M4A")
    netease.add_argument("--name", help="output basename")
    netease.add_argument("--formats", type=_formats, default=_formats(DEFAULT_FORMATS))
    netease.add_argument(
        "--use-page-lyrics",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    netease.add_argument("--keep-audio", action="store_true")
    _add_timing_refinement_arguments(netease)
    netease.add_argument(
        "--cookies-from-browser",
        choices=["brave", "chrome", "edge", "firefox"],
        help=(
            "use the existing NetEase login from this local browser; "
            "detects and uses only the account's available VIP/SVIP quality"
        ),
    )
    netease.add_argument(
        "--browser-profile",
        help="optional browser profile name or path, for example 'Profile 1'",
    )
    netease.add_argument(
        "--i-have-rights",
        action="store_true",
        required=True,
        help="confirm that you have the right to obtain and process this song",
    )
    _add_alignment_arguments(netease)
    _add_style_arguments(netease)
    netease.set_defaults(handler=_handle_netease)

    qqmusic = subparsers.add_parser(
        "qqmusic",
        help="export public timed lyrics from a QQ Music song link",
    )
    qqmusic.add_argument("url", help="QQ Music single-song URL or shared text")
    qqmusic.add_argument("-o", "--output-dir", type=Path, default=Path("build/qqmusic"))
    qqmusic.add_argument("--name", help="output basename")
    qqmusic.add_argument("--formats", type=_formats, default=_formats(DEFAULT_FORMATS))
    qqmusic.add_argument(
        "--i-have-rights",
        action="store_true",
        required=True,
        help="confirm that you have the right to use and process the lyrics",
    )
    _add_style_arguments(qqmusic)
    qqmusic.set_defaults(handler=_handle_qqmusic)

    utaten = subparsers.add_parser(
        "utaten",
        help="import public plain lyrics and furigana from an UtaTen lyric page",
    )
    utaten.add_argument("url", help="UtaTen lyric URL or shared text")
    utaten.add_argument("-o", "--output-dir", type=Path, default=Path("build/utaten"))
    utaten.add_argument("--name", help="output basename")
    utaten.add_argument(
        "--i-have-rights",
        action="store_true",
        required=True,
        help="confirm that you have the right to use and process the lyrics",
    )
    utaten.set_defaults(handler=_handle_utaten)
    return parser


def _print_exports(paths: dict[str, Path]) -> None:
    for fmt, path in paths.items():
        print(f"{fmt:>5}  {path.resolve()}")


def _handle_align(args: argparse.Namespace) -> int:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    source = read_lyrics(args.lyrics)
    result = None
    if source.is_timed:
        timing_mode = _timing_refinement_from_args(args)
        if should_refine_timing(source, timing_mode):
            result = refine_audio_word_timing_with_fallback(
                args.audio,
                source,
                timing_mode=timing_mode,
                options=_alignment_options(args),
                work_dir=args.output_dir / ".work",
                progress=_progress,
            )
            document = result.document if result is not None else source
        else:
            document = source
            detail = (
                "timing refinement disabled; preserved input timing"
                if timing_mode == "off"
                else "trusted word timing detected; skipped recognition"
            )
            print(detail)
    else:
        result = align_audio_and_lyrics(
            args.audio,
            args.lyrics,
            options=_alignment_options(args),
            work_dir=args.output_dir / ".work",
            progress=_progress,
        )
        document = result.document
    basename = args.name or args.lyrics.stem
    paths = export_formats(
        document,
        args.output_dir,
        basename,
        args.formats,
        ass_style=_style_from_args(args),
    )
    if result is not None:
        print(
            f"Alignment: {result.report.matched_units}/{result.report.target_units} units "
            f"({result.report.coverage:.1%}), exact {result.report.exact_units}, "
            f"mean similarity {result.report.mean_similarity:.2f}"
        )
        if result.transcription.detected_language:
            probability = result.transcription.language_probability
            suffix = f" ({probability:.1%})" if probability is not None else ""
            print(f"Language: {result.transcription.detected_language}{suffix}")
    _print_exports(paths)
    return 0


def _handle_convert(args: argparse.Namespace) -> int:
    if args.output.exists() and not args.overwrite:
        raise FileExistsError(f"Output already exists: {args.output}. Pass --overwrite.")
    document = read_lyrics(args.input)
    document.require_timed()
    fmt = args.format or args.output.suffix.lstrip(".").lower()
    if fmt == "enhanced":
        fmt = "elrc"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        write_format(document, fmt, ass_style=_style_from_args(args)),
        encoding="utf-8",
    )
    print(args.output.resolve())
    return 0


def _handle_render(args: argparse.Namespace) -> int:
    if args.lyrics.suffix.lower() == ".ass":
        ass_path = args.lyrics
        return _render_with_ass(args, ass_path)
    document = read_lyrics(args.lyrics)
    document.require_timed()
    with tempfile.TemporaryDirectory(prefix="karaoke-forge-style-") as temp_name:
        ass_path = Path(temp_name) / "karaoke.ass"
        ass_path.write_text(
            write_format(document, "ass", ass_style=_style_from_args(args)),
            encoding="utf-8",
        )
        return _render_with_ass(args, ass_path)


def _render_with_ass(args: argparse.Namespace, ass_path: Path) -> int:
    output = render_karaoke_video(
        args.video,
        ass_path,
        args.output,
        audio_path=args.audio,
        audio_offset=args.audio_offset,
        crf=args.crf,
        preset=args.preset,
        audio_bitrate=args.audio_bitrate,
        overwrite=args.overwrite,
        progress=_progress,
    )
    print(output)
    return 0


def _handle_make(args: argparse.Namespace) -> int:
    output = args.output
    assets_dir = args.assets_dir or output.with_suffix("").with_name(output.stem + ".assets")
    result = make_karaoke_video(
        args.audio,
        args.video,
        args.lyrics,
        output,
        assets_dir,
        options=MakeOptions(
            align=_alignment_options(args),
            style=_style_from_args(args),
            formats=tuple(args.formats),
            audio_offset=args.audio_offset,
            crf=args.crf,
            preset=args.preset,
            audio_bitrate=args.audio_bitrate,
            overwrite=args.overwrite,
            auto_sync=args.auto_sync,
            timing_refinement=_timing_refinement_from_args(args),
        ),
        progress=_progress,
    )
    if result.timing_refinement_warning:
        print(f"Warning: {result.timing_refinement_warning}")
    elif result.alignment_skipped:
        print("Lyrics already contain a timeline; alignment was skipped.")
    elif result.alignment_report:
        print(
            f"Alignment: {result.alignment_report.matched_units}/"
            f"{result.alignment_report.target_units} units "
            f"({result.alignment_report.coverage:.1%})"
        )
    _print_exports(result.exports)
    print(f"video  {result.video}")
    return 0


def _handle_doctor(_args: argparse.Namespace) -> int:
    demucs_runtime = inspect_demucs_runtime()
    checks = [
        ("Python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0]),
        ("FFmpeg", shutil.which("ffmpeg") is not None, shutil.which("ffmpeg") or "not found"),
        (
            "faster-whisper",
            importlib.util.find_spec("faster_whisper") is not None,
            "installed"
            if importlib.util.find_spec("faster_whisper")
            else "optional, not installed",
        ),
        (
            "Demucs",
            demucs_runtime.ready,
            demucs_runtime.detail_zh,
        ),
        (
            "Gradio web UI",
            importlib.util.find_spec("gradio") is not None,
            "installed" if importlib.util.find_spec("gradio") else "optional, not installed",
        ),
        (
            "NetEase adapter",
            importlib.util.find_spec("yt_dlp") is not None,
            "installed" if importlib.util.find_spec("yt_dlp") else "optional, not installed",
        ),
        (
            "Pronunciation",
            importlib.util.find_spec("pykakasi") is not None
            and importlib.util.find_spec("alkana") is not None,
            "installed"
            if importlib.util.find_spec("pykakasi") and importlib.util.find_spec("alkana")
            else "optional, not installed",
        ),
    ]
    for name, ok, detail in checks:
        print(f"{'OK' if ok else '--':>2}  {name:<18} {detail}")
    find_ffmpeg()
    return 0


def _handle_web(args: argparse.Namespace) -> int:
    from .web import launch_web_app

    launch_web_app(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )
    return 0


def _handle_netease(args: argparse.Namespace) -> int:
    from .netease import NeteaseAlignOptions, align_netease_song

    result = align_netease_song(
        args.url,
        args.lyrics,
        args.output_dir,
        local_audio_path=args.audio,
        name=args.name,
        options=NeteaseAlignOptions(
            align=_alignment_options(args),
            style=_style_from_args(args),
            formats=tuple(args.formats),
            use_page_lyrics=args.use_page_lyrics,
            keep_audio=args.keep_audio,
            rights_confirmed=args.i_have_rights,
            cookie_browser=args.cookies_from_browser,
            cookie_browser_profile=args.browser_profile,
            timing_refinement=_timing_refinement_from_args(args),
        ),
        progress=_progress,
    )
    print(f"Track: {result.track.title} — {result.track.artist_text}")
    if result.track.access_text:
        print(f"Access: {result.track.access_text}")
    if result.timing_refinement_warning:
        print(f"Warning: {result.timing_refinement_warning}")
    elif result.alignment_report:
        print(
            f"Alignment: {result.alignment_report.matched_units}/"
            f"{result.alignment_report.target_units} units "
            f"({result.alignment_report.coverage:.1%})"
        )
    else:
        print("Lyrics already contained a timeline; alignment was skipped.")
    _print_exports(result.exports)
    if result.kept_audio:
        print(f"audio  {result.kept_audio}")
    return 0


def _handle_qqmusic(args: argparse.Namespace) -> int:
    from .qqmusic import fetch_public_qqmusic_info

    info = fetch_public_qqmusic_info(args.url)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    source = args.output_dir / ".qqmusic-source.lrc"
    source.write_text(info.page_lyrics, encoding="utf-8")
    try:
        document = read_lyrics(source)
    finally:
        source.unlink(missing_ok=True)
    if info.translated_lyrics:
        attach_reference_translation(document, info.page_lyrics, info.translated_lyrics)
    document.metadata.update(
        {
            "source": "QQ Music",
            "source_url": info.canonical_url,
            "source_id": info.song_mid,
            "ti": info.title,
            "ar": info.artist_text,
        }
    )
    exports = export_formats(
        document,
        args.output_dir,
        args.name or info.title,
        args.formats,
        ass_style=_style_from_args(args),
    )
    print(f"Track: {info.title} — {info.artist_text}")
    _print_exports(exports)
    return 0


def _handle_utaten(args: argparse.Namespace) -> int:
    from .utaten import build_utaten_document, fetch_public_utaten_info

    info = fetch_public_utaten_info(args.url)
    document = build_utaten_document(info)
    fallback = f"utaten-{info.lyric_id}"
    basename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", args.name or info.title).strip(" .")
    basename = basename or fallback
    args.output_dir.mkdir(parents=True, exist_ok=True)
    plain_path = args.output_dir / f"{basename}.txt"
    project_path = args.output_dir / f"{basename}.json"
    plain_path.write_text(info.plain_lyrics, encoding="utf-8")
    project_path.write_text(write_format(document, "json"), encoding="utf-8")
    print(f"Track: {info.title} — {info.artist}")
    print(f"  txt  {plain_path.resolve()}")
    print(f" json  {project_path.resolve()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError, MediaError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
