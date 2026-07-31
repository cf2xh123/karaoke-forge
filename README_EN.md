# Karaoke Forge

Create word-highlighted karaoke videos from a song, its official lyrics, and an MV. Processing runs locally; media is not uploaded to a third-party service.

[中文说明](README.md) · [Changelog](CHANGELOG.md) · [Roadmap](TODO.md) · [Contributing](CONTRIBUTING.md) ·
[Issues](https://github.com/cf2xh123/karaoke-forge/issues)

## Features

- Force-align plain lyrics to a song with timestamped Whisper output;
- Keep the lyrics supplied by the user instead of replacing them with ASR text;
- Read TXT, LRC, enhanced LRC, SRT, VTT, ASS, and project JSON;
- Export LRC, enhanced LRC, SRT, VTT, karaoke ASS, and JSON;
- Burn subtitles into an MV and optionally replace its audio;
- Locate the actual song start inside an MV with multi-window audio fingerprints;
- Prefer real NetEase YRC word timing and refine ordinary line-timed lyrics from audio;
- Render translation at the top and paired original lyrics in a split KTV layout;
- Add Japanese furigana and English katakana readings above the corresponding lyric row;
- Edit source text, translation, timing, visibility, and line/word pronunciation in the web UI;
- Hide recoverable lines or permanently delete unwanted credits, speech, and duplicate lyrics;
- Choose `off`, `auto`, or `force` word-timing refinement consistently in web and CLI flows;
- Preview subtitle fonts, colours, sizes, and layout in the web interface;
- Optionally separate vocals with Demucs before recognition.

This is a usable `0.3.0` alpha. Check the generated timeline before a final render.

## Install

Python 3.10+ and [FFmpeg](https://ffmpeg.org/download.html) are required.

```bash
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[align]"
karaoke-forge doctor
```

Use `pip install -e ".[all]"` to include optional Demucs vocal separation.

## Quick start

For a visual local interface:

```bash
pip install -e ".[web,align,netease,pronunciation]"
karaoke-forge web
```

On Windows, non-technical users can double-click `首次安装.bat` once and use
`启动网页版.bat` afterwards. The browser interface covers complete video creation,
timeline-only export, lyric/pronunciation editing, format conversion, and environment
checks. Media is processed by the local service and is not automatically uploaded to
the public internet.

The “Lyrics & Pronunciation Editor” loads timed LRC/YRC/SRT/VTT/ASS or project JSON.
Each row keeps source text, translation, timing, pronunciation, and visibility together.
Hidden rows remain recoverable in JSON but are omitted from subtitle/video exports;
deleted rows are removed permanently. A second editable table supports character-range
pronunciation corrections with an immediate ruby-text preview.

The NetEase tab accepts single-song links. It uses anonymous public access by default,
or can read an existing NetEase login from a local Chrome, Edge, Firefox, or Brave
profile. In browser mode it detects the VIP/SVIP quality actually available for that
track and downloads only the highest quality the account is already allowed to play.
If NetEase returns only a short preview while the uploaded MV contains a complete audio
track, the full workflow automatically uses the MV audio instead. Public translated LRC
is placed at the top centre when available. Paired original lines use an upper-left and
lower-right KTV layout, and the web style panel includes a live 16:9 subtitle preview.
Japanese lines with kanji receive hiragana readings, while English words receive katakana
readings. Use `--no-show-pronunciation` to disable them; generated readings can be corrected
through each JSON lyric line's `pronunciation` field.
When NetEase exposes YRC, its real per-character start times and durations drive the
karaoke sweep directly. For ordinary line-timed LRC/SRT input, audio recognition refines
the timing inside each line while preserving its original boundaries. Use
`align`, `make`, and `netease` accept `--timing-refinement off|auto|force`: `off`
preserves all input timing, `auto` refines only synthetic word timing, and `force`
rechecks even trusted YRC/enhanced-LRC timing.
The legacy `--no-refine-word-timing` flag remains accepted for compatibility.
Cookies stay in local process memory and are never written to project outputs; the app
does not accept passwords, elevate membership access, bypass regional/DRM restrictions,
or decrypt NCM. A legally exported local MP3/FLAC/WAV/M4A remains supported.

For the command line:

```bash
karaoke-forge make song.flac mv.mp4 lyrics.txt \
  -o output/song-karaoke.mp4 \
  --language en
```

Generate timeline files only:

```bash
karaoke-forge align song.flac lyrics.txt -o build/song --language en
```

Convert timed lyrics:

```bash
karaoke-forge convert lyrics.lrc -o lyrics.srt
```

Render existing timed lyrics and replace the MV audio:

```bash
karaoke-forge render mv.mp4 lyrics.ass \
  --audio song.flac \
  -o karaoke.mp4
```

Run `karaoke-forge <command> --help` for all options. The Chinese [README](README.md) currently contains the full usage and troubleshooting guide.

## Updating

```bash
git pull --ff-only
python -m pip install --upgrade -e ".[align]"
karaoke-forge doctor
```

Review [CHANGELOG.md](CHANGELOG.md) before upgrading. Code is available under the [MIT License](LICENSE); no rights to songs, lyrics, videos, fonts, or models are granted.
