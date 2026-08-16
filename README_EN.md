# Karaoke Forge

> The current release is `0.15.0` (Alpha). Windows setup and later launches can now
> provision private Python and FFmpeg runtimes. ModelScope is the recommended default
> for anonymous direct model downloads in mainland China, with no administrator access,
> system `PATH` changes, or proxy configuration required.

Create word-highlighted karaoke videos from a song, its official lyrics, and an MV. Processing runs locally; media is not uploaded to a third-party service.

[中文说明](README.md) · [Changelog](CHANGELOG.md) · [Roadmap](TODO.md) · [Contributing](CONTRIBUTING.md) ·
[Issues](https://github.com/cf2xh123/karaoke-forge/issues)

## Features

- Force-align plain lyrics to a song with timestamped Whisper output;
- Keep the lyrics supplied by the user instead of replacing them with ASR text;
- Choose Fast, Balanced, or KTV Precise recognition; the precise preset line-bounds
  CTranslate2 alignment to the supplied lyrics and accepts only timings that pass quality checks;
- Preserve first-word delays and real pauses between tokens in ASS karaoke sweeps;
- Keep whitespace out of preview and ASS sweep duration, so long lyric gaps do not delay
  the highlighted letter or race across the line before the next word;
- Correct fixed offsets and gradual or local tempo drift in line-timed lyrics with reliable anchors;
- Keep low-confidence, abnormally long, or context-free ASR matches from controlling final timing;
- Read TXT, LRC, enhanced LRC, SRT, VTT, ASS, and project JSON;
- Export LRC, enhanced LRC, SRT, VTT, karaoke ASS, and JSON;
- Burn subtitles into an MV and optionally replace its audio;
- Export the original-audio version, a Demucs no-vocals accompaniment version, or both;
- Render a no-MV version from local or NetEase/QQ Music cover art using five backgrounds
  and five audio-reactive layouts, for 25 combinations with project-persistent settings;
- Locate the actual song start inside an MV with multi-window audio fingerprints;
- Use Vmoe karaoke ASS or public UtaTen/QQ Music/NetEase lyrics, and refine timing from audio;
- When a project is created from a NetEase, QQ Music, or UtaTen link, use the official
  fetched song title if the name field is left blank; an explicit custom name still wins;
- When no local or MV audio is available, open a dedicated Edge window for one-click
  login on NetEase's official site; everyday Edge can stay open, with no DevTools or
  Firefox required; a valid dedicated login is restored on the next launch and the
  official login window reopens only when that session expires and account audio is needed;
- Prefer `exhigh`, `higher`, or `standard` NetEase audio for alignment and MV creation
  even when the account exposes Hi-Res or master formats, avoiding unnecessarily large
  downloads while retaining authenticated song access;
- Render translation at an adjustable distance from the top and paired original lyrics in a split KTV layout;
- Clear lyrics across long instrumental gaps, then cue the next line with a configurable
  three-dot countdown during the final three seconds before singing resumes;
- Add Japanese furigana and optional English katakana above the lyric row; disabling
  English readings also filters readings already stored in older/imported projects;
- Bundle uploaded TTF/OTF/TTC fonts with a project without installing them system-wide;
- Persist lyrics, media, cover art, fonts, and settings; on the next web launch, select the
  newest valid project in a saved-project list without replacing the current page until the
  user confirms. Load it into the Make page, load and edit it in one click, or keep the current
  page without deleting saved work. Projects created under custom output roots remain indexed;
  if browser file inputs lose their temporary values, recover assets from the saved manifest;
- Edit source text, translation, timing, visibility, and line/word pronunciation in the web UI;
- Hide recoverable lines or permanently delete unwanted credits, speech, and duplicate lyrics;
- Preserve timed blank LRC interludes, and explicitly delete a cleared lyric row with project-wide
  undo that restores and focuses the actual edited row even after playback has moved elsewhere;
- Keep interior word timings fixed when nudging a line edge; when an extended line reaches
  the next lyric, optional Ripple mode shifts only the affected following lines while preserving
  their durations and token spacing;
- Keep the detailed per-line workflow and add a continuous global mode that loads the whole
  song once, supports continuous seeks from lyric blocks or the red playhead, keeps the KTV
  preview following the active line and word timing, and shifts the global timeline as one operation;
- Choose `off`, `auto`, or `force` word-timing refinement consistently in web and CLI flows;
- Preview the song's actual lyrics against a matching MV frame, or against the selected
  cover-art scene when no MV is available; a link-only source uses online cover art and a
  waveform-layout sample until real audio is downloaded;
- Optionally separate vocals with Demucs before recognition;
- On Windows, install and verify a project-private FFmpeg build, and cap libx264 subtitle-render
  threads to prevent native crashes near the end of long videos; model downloads prefer
  direct ModelScope access and retain official Hugging Face, a local proxy, an explicitly
  selected third-party mirror, and a predownloaded offline cache as alternatives.

This is a usable `0.15.0` alpha. Check the generated timeline before a final render.

## Install

Manual installation requires Python 3.10+ and
[FFmpeg](https://ffmpeg.org/download.html). On Windows, the first-time setup batch file
downloads pinned private Python 3.12.10 and Gyan.dev FFmpeg Essentials runtimes into the
project and verifies their hashes. No system Python, Conda, or FFmpeg installation is
required; setup does not request administrator access or change the global `PATH`.

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
Python NuGet runtime and the pinned Gyan.dev FFmpeg Essentials build into `.runtime`,
verifies both downloads, and creates the isolated `.venv`. The FFmpeg package is the
approximately 110 MB Gyan 8.1.2 Essentials ZIP, a GPLv3 static build whose license and
source notices remain in the installed runtime. The launcher automatically
repairs a missing or incomplete private FFmpeg runtime. The browser
interface covers complete video creation,
timeline-only export, lyric/pronunciation editing, format conversion, and environment
checks. Media is processed by the local service and is not automatically uploaded to
the public internet.

The wizard recommends anonymous direct downloads from ModelScope in mainland China and
supports all three built-in models: `small`, `large-v3-turbo`, and `large-v3`. Pressing
Enter runs automatic selection, which tests ModelScope first, then official Hugging Face,
then common loopback proxy ports. Official Hugging Face, an explicitly entered local
proxy, opt-in `hf-mirror`, and fully offline operation remain available. The wizard can
predownload the Fast, Balanced, or KTV Precise model into a source-isolated cache in the
current user's local application data.

Those ModelScope repositories are public user-uploaded replicas, not publisher-verified
repositories operated by the original model authors. Karaoke Forge therefore pins a
specific ModelScope commit and independently matches every required file's exact byte
length and SHA-256 against the pinned official Hugging Face revision. A snapshot is
published to the local cache and loaded only after every file passes verification.

Automatic selection never enables `hf-mirror`; that third-party service requires explicit
consent, and no Hugging Face token is sent to it. Proxy variables are scoped to model
loading or prefetch and restored afterwards, so they do not alter browser, Windows,
NetEase, or artwork traffic. Double-click `模型下载设置.bat` or use the Environment tab
to test, change, or prefetch later.

After media and lyrics are selected, the Make page automatically shows an in-context
subtitle preview without rendering a full video. Timed lyrics use a matching MV frame;
untimed lyrics remain a clearly labelled layout preview. Without an MV, the preview uses
the current cover art, background theme, and record/spectrum layout, with animation
indicated separately.

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

The “Lyrics & Pronunciation Editor” loads timed LRC/YRC/SRT/VTT/ASS or lyric JSON directly.
Open complete workspaces through the saved-project selector: uploading a manifest alone through
the browser does not carry its adjacent assets. The selector chooses the newest valid project,
or a project that matches a pasted NetEase/QQ Music/UtaTen source link, but this selection never
replaces the current page until the user clicks **Load selected project** or **Load and edit**.
Keeping the current page does not delete any saved project, and the list can be refreshed in place.
Each row keeps source text, translation, timing, pronunciation, and visibility together.
Hidden rows remain recoverable in JSON but are omitted from subtitle/video exports;
deleted rows are removed permanently. A second editable table supports character-range
pronunciation corrections with an immediate ruby-text preview.
The per-token timeline also supports direct text edits: clearing and saving one token
removes an unwanted character or space without changing the remaining token times.
Current-line looping, automatic next-line playback, and Space-bar pause/resume are
available while timing lyrics by ear.

Per-line mode remains available for precise word and line-edge work. If extending a line end
would intrude into the next lyric, Ripple can move the newly affected following lines without
changing their internal durations or word spacing, and can be disabled when only the current
line should change. Global mode keeps one whole-song audio source loaded: click a lyric block
or drag the red playhead for continuous seeking while the KTV preview follows the active line
and word timing, then use global shift to correct a whole-song offset in one operation.

The NetEase tab accepts single-song links and uses anonymous public access by default.
When authenticated audio is needed on Windows, click **One-click NetEase login**. The
app opens the official NetEase site in a dedicated Edge window with a separate local
profile; after the user completes the official login, Karaoke Forge
automatically obtains the session required for the current job. This window never reads
the everyday Edge profile or its locked Cookie database, so everyday Edge can remain
open and users do not need DevTools, Firefox, or any manual Cookie lookup. The dedicated
profile is retained locally, so the login can normally be reused after the first sign-in.
Automatic extraction from an already-closed Chrome, Edge, Firefox, or Brave profile and
manual `MUSIC_U` entry remain available under advanced settings as fallback methods.
The captured session stays in server-side state for the current local web session and is
not filled into a browser password field. A visible **Sign in again / switch account**
action handles expired sessions; non-loopback server mode disables access to local
accounts and browser data.
Authenticated mode detects the VIP/SVIP quality actually available for the track and
downloads only audio the account is already allowed to play.
If NetEase returns only a short preview while the uploaded MV contains a complete audio
track, the full workflow automatically uses the MV audio instead. Public translated LRC
is placed at the top centre when available. Paired original lines use an upper-left and
lower-right KTV layout. The same subtitle parameters drive both the in-context preview
and the final KTV render.
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

The web app defaults to **Balanced**. The same presets are available from the CLI as
`--model profile:fast`, `--model profile:balanced`, and `--model profile:precise`:

| Preset | Effective configuration | Intended use |
| --- | --- | --- |
| Fast | `small`, beam 3 | Quick editable drafts, CPUs, and slower computers |
| Balanced (default) | `large-v3-turbo`, beam 5 | The usual speed/accuracy trade-off |
| KTV Precise | `large-v3`, beam 5 | Tries a Demucs vocal stem, then runs line-bounded CTranslate2 forced alignment against the supplied lyrics |

Demucs is a soft preference in KTV Precise: if it is missing or fails, the job reports the
fallback and continues on the original mix. A line that cannot be aligned safely keeps its
0.12 coarse timing without affecting successful lines. Explicitly selecting **Separate
vocals first** remains a strict requirement and reports an error instead of silently falling
back. `auto` still leaves trusted YRC/enhanced-LRC word timing untouched; select `force` to
check those source timings.

Each preset downloads its Whisper model on first use. `large-v3-turbo` and especially
`large-v3` are substantially larger than `small`, so their first download and load can take
some time; subsequent runs reuse the per-user, source-isolated cache. On Windows, anonymous
ModelScope direct download is the recommended default; the wizard can instead use official
Hugging Face, an auto-detected local proxy, explicitly selected `hf-mirror`, or a
predownloaded offline cache. The mirror is never enabled automatically, and Karaoke Forge
does not send a Hugging Face token to it.
One-click login takes place only on NetEase's official site, and its login state is kept
in a dedicated local Edge profile. Cookies obtained by Karaoke Forge are never written
back into the web page or to projects, output directories, or logs; a manually supplied
`MUSIC_U` remains in local
process memory only. The app does not accept passwords, elevate membership access,
bypass regional/DRM restrictions, or decrypt NCM. A legally exported local
MP3/FLAC/WAV/M4A remains supported.

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
python -m pip install --upgrade -e ".[web,align,netease,pronunciation]"
karaoke-forge doctor
```

On Windows, `启动网页版.bat` uses the existing `.venv` to repair missing or outdated web,
alignment, NetEase-login, and pronunciation components, and repairs a missing private
FFmpeg runtime; the first-time setup does not need to be run again.

Review [CHANGELOG.md](CHANGELOG.md) before upgrading. Code is available under the [MIT License](LICENSE); no rights to songs, lyrics, videos, fonts, or models are granted.
