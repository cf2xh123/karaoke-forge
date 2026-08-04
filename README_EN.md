# Karaoke Forge

> Version 0.10.0 adds resumable projects, bundled custom fonts, and three
> audio-reactive album-art video styles for songs without an MV.

Create word-highlighted karaoke videos from a song, its official lyrics, and an MV. Processing runs locally; media is not uploaded to a third-party service.

[中文说明](README.md) · [Changelog](CHANGELOG.md) · [Roadmap](TODO.md) · [Contributing](CONTRIBUTING.md) ·
[Issues](https://github.com/cf2xh123/karaoke-forge/issues)

## Features

- Force-align plain lyrics to a song with timestamped Whisper output;
- Keep the lyrics supplied by the user instead of replacing them with ASR text;
- Read TXT, LRC, enhanced LRC, SRT, VTT, ASS, and project JSON;
- Export LRC, enhanced LRC, SRT, VTT, karaoke ASS, and JSON;
- Burn subtitles into an MV and optionally replace its audio;
- Render a no-MV version from local or NetEase/QQ Music cover art using a vinyl
  turntable, cover glow, or frequency-stage style with real audio-driven visuals;
- Locate the actual song start inside an MV with multi-window audio fingerprints;
- Use Vmoe karaoke ASS or public UtaTen/QQ Music/NetEase lyrics, and refine timing from audio;
- Render translation at the top and paired original lyrics in a split KTV layout;
- Add Japanese furigana and optional English katakana above the lyric row; disabling
  English readings also filters readings already stored in older/imported projects;
- Bundle uploaded TTF/OTF/TTC fonts with a project without installing them system-wide;
- Persist lyrics, media, cover art, fonts, and settings, then restore the latest project
  automatically on the next web launch;
- Edit source text, translation, timing, visibility, and line/word pronunciation in the web UI;
- Hide recoverable lines or permanently delete unwanted credits, speech, and duplicate lyrics;
- Choose `off`, `auto`, or `force` word-timing refinement consistently in web and CLI flows;
- Preview subtitle fonts, colours, sizes, and layout in the web interface;
- Optionally separate vocals with Demucs before recognition.

This is a usable `0.10.0` alpha. Check the generated timeline before a final render.

## Install

Manual installation requires Python 3.10+ and
[FFmpeg](https://ffmpeg.org/download.html). On Windows, the first-time setup batch file
downloads a pinned private Python 3.12.10 runtime into the project, so no system Python
or Conda installation is required and the global `PATH` is not changed.

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
`启动网页版.bat` afterwards. The first setup downloads an approximately 14 MB official
Python NuGet runtime into `.runtime` and creates the isolated `.venv`. The browser
interface covers complete video creation,
timeline-only export, lyric/pronunciation editing, format conversion, and environment
checks. Media is processed by the local service and is not automatically uploaded to
the public internet.

The Make page includes the official [Vmoe karaoke search](https://karaoke.vmoe.info/).
Vmoe requires its own reCAPTCHA for search and ASS downloads, so the user completes that
step on the official page and uploads the downloaded ASS; Karaoke Forge does not bypass
the challenge. A separate QQ Music tab and the `qqmusic` CLI command accept official
single-song links and export public line-timed LRC, available translations, ASS, and
project JSON without requesting audio, accounts, cookies, or passwords:

```bash
karaoke-forge qqmusic \
  "https://y.qq.com/n/ryqq_v2/songDetail/001gQnW91BEDaN" \
  --i-have-rights -o build/qqmusic
```

The Make page also accepts an `https://utaten.com/lyric/.../` URL. Karaoke Forge imports
the publicly rendered lyric text and keeps UtaTen's per-line furigana as pronunciation
metadata instead of mixing it into the source lyric. The equivalent CLI command exports
plain text and project JSON; UtaTen does not provide timing, so audio alignment still runs:

```bash
karaoke-forge utaten \
  "https://utaten.com/lyric/yh15042710/" \
  --i-have-rights -o build/utaten
```

When an uploaded lyric file or edited project should remain authoritative, enable
“Use only official UtaTen pronunciation.” Karaoke Forge clears the old pronunciation,
matches local and UtaTen lines in order while tolerating punctuation and spacing changes,
then transfers only ruby spans whose source characters can be verified. Local lyric text,
translations, line timing, and word timing stay unchanged; unmatched text remains without
pronunciation instead of receiving a guessed reading.

English katakana has a separate Make-page switch. Turning it off keeps Japanese furigana,
but filters English readings even when an older project, manual edit, or imported source
already stored them. ASS-producing CLI commands expose the same final-output policy as
`--no-auto-english-pronunciation`.

The “Lyrics & Pronunciation Editor” loads timed LRC/YRC/SRT/VTT/ASS or project JSON.
Each row keeps source text, translation, timing, pronunciation, and visibility together.
Hidden rows remain recoverable in JSON but are omitted from subtitle/video exports;
deleted rows are removed permanently. A second editable table supports character-range
pronunciation corrections with an immediate ruby-text preview.
The per-token timeline also supports direct text edits: clearing and saving one token
removes an unwanted character or space without changing the remaining token times.
Current-line looping, automatic next-line playback, and Space-bar pause/resume are
available while timing lyrics by ear.

The NetEase tab accepts single-song links. It uses anonymous public access by default,
or can read an existing NetEase login from a local Chrome, Edge, Firefox, or Brave
profile. In browser mode it detects the VIP/SVIP quality actually available for that
track and downloads only the highest quality the account is already allowed to play.
If NetEase returns only a short preview while the uploaded MV contains a complete audio
track, the full workflow automatically uses the MV audio instead. Public translated LRC
is placed at the top centre when available. Paired original lines use an upper-left and
lower-right KTV layout, and the web style panel includes a live 16:9 subtitle preview.
Japanese lines with kanji receive hiragana readings, while English words receive katakana
readings by default. Use `--no-auto-english-pronunciation` to disable only automatic English
katakana, or `--no-show-pronunciation` to hide all readings; generated readings can be
corrected through each JSON lyric line's `pronunciation` field.
When NetEase exposes YRC, its real per-character start times and durations drive the
karaoke sweep directly. For ordinary line-timed LRC/SRT input, audio recognition refines
the timing inside each line while preserving its original boundaries. Use
`align`, `make`, and `netease` accept `--timing-refinement off|auto|force`: `off`
preserves all input timing, `auto` refines only synthetic word timing, and `force`
rechecks even trusted YRC/enhanced-LRC timing but adopts only high-confidence,
line-local changes that do not substantially disagree with trusted source timing.
The legacy `--no-refine-word-timing` flag remains accepted for compatibility.
Cookies stay in local process memory and are never written to project outputs; the app
does not accept passwords, elevate membership access, bypass regional/DRM restrictions,
or decrypt NCM. A legally exported local MP3/FLAC/WAV/M4A remains supported.

When plain-lyric matching falls below the safety threshold, web calibration now keeps
the formal lyrics and creates an editable recovery timeline instead of stopping. It
lists unmatched lines, the language detected by Whisper, model and vocal-separation
state, and suggested next steps. A recovery timeline should be auditioned and manually
checked before final rendering.

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
