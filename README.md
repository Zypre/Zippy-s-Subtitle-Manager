# Zippy-s-Subtitle-Manager
Claude made this. Therefore nobody made this. Use it as you will.

---

# dual_subs 2.0

Muxes two subtitle languages into an MKV as one styled ASS track — one line on
top, one on the bottom. Each side is resolved independently: an embedded track
if there is one, otherwise a matching subtitle file on disk, retimed to the
video if it needs it.

This is a Python port of `dual_subs.bat`. Same behaviour, no base64, and it
takes a path instead of having to live in the folder it processes.

---

## Running it

Double-click `dual_subs_gui.exe`, or drag files and folders onto its icon — they
arrive pre-queued. Fonts, sizes, margins, spacing and timing options all live in the window's
tabs and are written to the settings file when you run, save, or close it — so
the next run, and the command line, both pick them up. Everything the window
does is also on the command line:

```
dual_subs .                          # this folder, prompts for languages
dual_subs "D:\Shows\Season 1" -t eng -b chi
dual_subs episode.mkv -t eng -b chi
dual_subs . --list                   # what tracks and subtitle files exist
dual_subs . -t eng -b chi --dry-run  # resolve everything, write nothing
```

Without PyInstaller: `py -3 dual_subs.py .` or double-click `dual_subs.bat`.

| Flag | |
|---|---|
| `-t` / `-b` | top / bottom side: `eng`, `#4` (embedded track id), or a path to a `.srt`/`.ass`/`.vtt` |
| `-r` | descend into subfolders |
| `-o DIR` | write output elsewhere |
| `--overwrite` | replace an existing `.dual.mkv` |
| `--list` | inventory only |
| `--dry-run` | resolve and report, write nothing |
| `--keep-ass` | also save the merged `.ass` for inspection |
| `--no-stash` | don't move sources into `_originals\` |
| `--no-sync` | don't retime external subtitle files |
| `--no-rate-fix` | sync by constant offset only, never rescale |
| `--no-snap` / `--snap-window MS` | cue-pair alignment (default on, 500 ms) |
| `--align pairs\|grid\|off` | how the two lines are kept together |
| `--grid-window MS` / `--anchor top\|bottom\|auto` | grid mode settings |
| `--t2s` | convert traditional Chinese to simplified wherever it appears |
| `--reencode` | rewrite every subtitle file found as UTF-8, then exit |
| `--rename` | shorten release filenames (sidecars follow, undoable) |
| `--rename-style episode\|tags` | how much to strip |
| `--undo-rename` | put the last rename back |
| `-A` / `--set-audio` | make this the default audio track |
| `-S` / `--set-subs` | make this the default subtitle track |
| `--set-dual` | make the dual track this run creates the default |
| `--subs-off` | clear the default flag on every subtitle track |
| `--sidecar` | write only a `.ass` next to the video — seconds, not minutes |
| `--both` | write the `.ass` *and* remux |
| `--convert-top` / `--convert-bottom` | `t2s` (always), `auto` (only if it looks traditional), `none` |
| `--gui` / `--cli` | force the window / force the console |
| `--mkvtoolnix DIR` | where mkvmerge/mkvextract live |
| `--write-config` | write settings out and exit |

Exit codes: `0` all good, `1` at least one file failed, `2` couldn't find
MKVToolNix.

## Output modes

`--sidecar` skips the remux entirely and writes `<video name>.ass` beside the
video. Because the stem matches exactly, mpv and VLC pick it up on their own
with no configuration. It takes about a second per episode instead of however
long it takes to rewrite a few GB, and the original file is never touched — so
nothing is stashed into `_originals\` in this mode either.

Remuxing still earns its place when you want one self-contained file, or when
the player is a TV or a phone that won't load loose subtitle files. `--both`
does each.

## Two kinds of timing fix

These solve different problems and both are on by default.

**Sync** fixes the *file*. One constant offset for the whole subtitle, plus an
optional framerate rescale, estimated by cross-correlating cue onsets against
an embedded track. It only runs on external subtitle files, and only applies
the result when it is confident.

**Snapping** fixes the *pair*. Two subtitlers working from the same dialogue
rarely agree to the centisecond, so you get

```
bottom  0:02:23.35 -> 0:02:25.97   给我张支票，什么意思，让我踢假球啊？
top     0:02:23.65 -> 0:02:26.27   What is the meaning of this check?
```

— the top line arriving 300 ms late, which reads as a flicker. Neither track is
wrong, so no amount of resyncing helps. Note this happens between two
*embedded* tracks too, where sync never runs at all.

This runs in two stages, because the two failure modes need different cures.

**A whole-track lag.** If one side sits a consistent distance behind the other
across the film, that entire side is shifted. Doing it per cue cannot work: on
a busy stretch, pulling one cue back would start it before its own previous cue
ends, so roughly half the pairs get refused and the result looks more broken
than doing nothing. A uniform shift preserves spacing within a track, so it can
never create an overlap. It fires only when the median difference is inside the
snap window and at least 60% of matched pairs agree with it.

**Residual jitter.** Whatever is left is handled pair by pair: earliest start,
latest end. Where a neighbour is in the way, the pair meets it at the boundary
rather than giving up — but only if that trims less than the snap window, since
losing half a second of a cue would be a worse artefact than the flicker it
fixes. Pairs further apart than the window are left as authored, on the
assumption the difference is deliberate.

On a real 984-pair film this took the mismatched pairs from 461 to 18, and all
18 were more than 600 ms apart — genuinely different cues, correctly untouched.

### When there is no pair to match: `--align grid`

Both of the above assume the two tracks split dialogue the same way. A dub does
not. English dialogue is rewritten to fit the mouth, so three Chinese cues can
cover what the English says in two:

```
chi  17:02.54 我这双名贵球鞋原价两块钱
eng  17:03.48 Here are some fine sneakers / Worth 2 whole dollars.
chi  17:04.54 因为你，算一块五
eng  17:05.44 I'll give them to you, if you let me have one more...
chi  17:06.54 我就多拿你一个馒头就好啦
```

There is no one-to-one pairing here, so pair snapping cannot touch it, and
inside a fast exchange the two sides drift a second or more apart. Measuring
one such film: 233 Chinese cues had no English counterpart at all, and the
median difference was 0 in every twenty-minute block — so it is not drift or a
constant offset either. It is segmentation.

`--align grid` stops matching cues and matches *moments* instead. Every start
on the moving side is pulled to the nearest start on the anchor side, every end
to the nearest end. The moving track keeps its own segmentation — it simply
stops changing at times the anchor does not, so the Chinese may change three
times while the English holds, but never half a beat off. On that film it took
the English cues sharing a Chinese boundary from 660 to 905 of 996.

This deliberately moves one side away from its own audio, which is why the
anchor matters. `--anchor auto` reads the video's default audio track and pins
whichever line matches it; `--anchor top` or `bottom` decides for yourself.

## Default tracks

Changing which audio and subtitle track a player picks does **not** need a
remux. Those flags live in the Matroska header, so `mkvpropedit` rewrites them
in place in well under a second whether the file is 200 MB or 30 GB.

```
dual_subs "D:\Anime\Season 1" -A eng -S eng            # English audio + English subs
dual_subs "D:\Anime" -r -A jpn -S chi:traditional       # Japanese audio, traditional subs
dual_subs "D:\Films" -A eng --subs-off                  # English audio, subtitles off
```

Nothing is re-encoded, no new file is written, and re-running is a no-op.
`--dry-run` shows the decisions without writing.

### Seeing what is actually there

`--list` on more than one file ends with the options for the whole queue,
built from the files themselves rather than a fixed menu of languages:

```
Audio  (-A)
  eng          English · English  (3 of 4)
  jpn          Japanese · Japanese  (3 of 4)
  hun:magyar   Hungarian · Magyar  (1 of 4)

Subtitles  (-S, -t, -b)
  eng               English · Dialogue  (all files)
  chi:simplified    Chinese · Dialogue (Simplified)  (3 of 4)
  chi:traditional   Chinese · Dialogue (Traditional)  (3 of 4)
  eng:signs songs   English · Signs & Songs  (3 of 4)
```

The left column is exactly what you pass to `-A`, `-S`, `-t` or `-b`. The
count matters: a season is often seven episodes with Chinese subtitles and one
without, and that is worth knowing before a batch run rather than after.

In the window the same scan fills every track selector — top line, bottom
line, default audio, default subtitles — and refreshes whenever the queue
changes. It runs in the background, one mkvmerge call per file, so a season is
quick and a library takes a little longer while the rest of the window stays
usable. You can still type a spec the scan did not find; it is passed through
unchanged.

### Picking the track you meant

This is the hard part, not the writing. The release this was built against had
**twelve subtitle tracks flagged default at once**, two default audio tracks,
and three separate English subtitle tracks:

```
eng  Signs & Songs          forced,  7 cues
eng  Dialogue                      430 cues
eng  Dialogue (Dub) (SDH)          550 cues
chi  Dialogue (Simplified)        1106 cues
chi  Dialogue (Traditional)       1106 cues
```

Matching on language alone picks the seven-cue signs track about a third of
the time. So a plain language code means *the main dialogue track*: signs,
songs, forced and commentary tracks are excluded outright, and SDH and dub
scripts are ranked below a clean dialogue track.

Add words after a colon to choose between tracks of the same language — they
are matched against the track name:

```
-S chi:traditional     -S chi:simplified     -S eng:dub     -S eng:signs     -S '#19'
```

The same syntax works for `-t` and `-b` when building a dual track, so
`-t chi:traditional -b eng` is unambiguous on a release carrying both Chinese
scripts. Asking for something that does not exist is an error naming the
tracks that *do*, rather than a silent wrong pick.

Every candidate track is written explicitly, not just the winner. Setting one
more default on a file that already has twelve changes nothing a player will
notice.

### Building the dual track and setting it default in one pass

```
dual_subs "D:\Anime\Season 1" -t chi -b eng --set-dual -A jpn
```

mkvmerge copies each source track's flags into the output, so the new dual
track being marked default does not stop the original Chinese and English
tracks from also being default. The flags are therefore settled after the mux,
on the file that was actually written.

## Shortening filenames

VLC on Windows cannot open a file whose path exceeds 260 characters, and
[setting `LongPathsEnabled` in the registry does not
help](https://code.videolan.org/videolan/vlc/-/issues/25640) — the reported
workaround is simply to shorten the name. Patching VLC would mean adding
`longPathAware` to its manifest, auditing its path handling and then
maintaining a Windows build forever; a plugin cannot reach it either, because
the failure happens in the access layer before plugins get a say. So: rename.

```
dual_subs "G:\Downloads\Some Show\Season 1" --rename --dry-run
dual_subs "G:\Downloads\Some Show\Season 1" --rename
dual_subs "G:\Downloads\Some Show\Season 1" --undo-rename
```

```
[SavI0r] Tensei Kizoku no Isekai Boukenroku (The Aristocrat's Otherworldly
Adventure) - S01E06 - A Date With the Strongest Captain of the Knights
[BD][1080p][AV1][OPUS][Dual Audio].mkv                        211 characters
    -> S01E06 - A Date With the Strongest Captain of the Knights.mkv      88
```

`--rename-style episode` (the default) drops the series name, since the
containing folder already carries it. `tags` keeps the whole name and removes
only bracketed groups and release metadata. Scene-style dotted names are
handled too: `The.Matrix.1999.2160p.UHD.BluRay.x265.HDR.Atmos-GROUP` becomes
`The Matrix 1999`.

Sidecar subtitles are renamed to match, so they keep pairing with their video.
Every rename is logged to `dual_subs_renames.json` beside the files, which is
what `--undo-rename` reads. Nothing is ever overwritten: a collision gets a
numeric suffix instead. The window has the same thing as **Shorten names**.

If the folder itself is the problem, renaming the file cannot fix it, and the
output says so rather than pretending otherwise.

## Encodings

Subtitles are a graveyard of legacy encodings. A Chinese `.srt` from a mainland
release is usually **GB18030**; from Taiwan or Hong Kong, **Big5**. Neither
announces itself, and opening one as UTF-8 does not fail — it quietly renders
要我踢假球？ as `ÒªÎÒÌß¼ÙÇò£¿` and carries on.

So every subtitle file is sniffed before it is read, and everything written out
is UTF-8. Nothing needs converting first; the log says what it found:

```
bottom : external Shaolin.Soccer.2001.chi.srt  (1178 cues, read as gb18030)
```

`--reencode` normalises files on disk instead, keeping the original as
`<name>.srt.orig`. It needs neither MKVToolNix nor a video, is safe to re-run,
and the GUI has the same thing as **Subtitles to UTF-8**.

```
dual_subs "D:\Films" --reencode -b chi -r
```

How the guess is made: a BOM decides outright, then a strict UTF-8 decode, then
the multi-byte CJK codecs — which are strict, so a clean decode is real
evidence. `cp1252` and `latin-1` accept *any* byte sequence, so they are tried
last and can never win on a technicality. Where several codecs decode cleanly,
the results are scored on whether they look like language.

Two findings from building that scorer are worth recording, since both are
counter-intuitive:

- **Rewarding recognisable Chinese characters made it worse.** The only hanzi
  this program has a list of are variant forms, which are comparatively exotic,
  so garbage decodes scored *higher* on them than correct ones did — 0.54
  against 0.32 on a real sample. That signal was removed.
- **Script mixing is the good signal.** Korean and Chinese do not interleave, so
  a heavy Hangul/ideograph mixture means one codec is reading the other's bytes:
  measured at 0.47 on a wrong decode and 0.00 on every correct one. Kana with
  ideographs is ordinary Japanese and is not penalised.

The honest limit: Korean text and the same bytes read as Chinese are close to
indistinguishable from bytes alone. That is why the language you asked for is
used as a prior — it costs nothing and settles the case. With the prior the
built-in detector gets 14/15 on a round-trip suite; the one failure is Korean
with no hint at all, which cannot arise in normal use. `charset_normalizer` or
`chardet` are used instead when installed, but neither is required.

## Chinese script conversion

Tick **Convert traditional Chinese to simplified** in the window, or pass
`--t2s`, and each side is examined and converted only if it actually is
traditional (繁體 → 简体, *fántǐ* → *jiǎntǐ*). English and already-simplified
text come back untouched, so there is no need to say which side is which.

`--convert-top` and `--convert-bottom` still take `t2s` (always), `auto`
(the above), or `none`, if you want per-side control.

The tables come from [OpenCC](https://github.com/BYVoid/OpenCC) (Apache 2.0),
baked into `dualsubs/_hanzi_data.py` so there is no runtime dependency;
regenerate with `build/gen_hanzi_data.py`. If the `opencc` package happens to
be installed it gets used instead, since its segmentation is better.

Two stages run, matching OpenCC's `tw2s` chain: Taiwan/HK variant forms are
normalised first (裏 → 裡), then traditional maps to simplified. Phrases are
applied before single characters, which is what makes 頭髮 → 头发 (hair) rather
than the wrong 头發, and 一目瞭然 → 一目了然 rather than a bare 瞭 → 了
everywhere.

**S → T is deliberately not offered.** T → S is essentially many-to-one — 發
*fā* "emit" and 髮 *fà* "hair" both collapse into 发 — so a table gets it right.
The reverse is one-to-many and needs a ~1 MB phrase dictionary plus real
disambiguation to avoid writing 头發. Vocabulary differences (資訊 vs 信息) are
untouched in either direction; only the glyphs change.

## Settings

`dual_subs.config.json`, written next to the program on first run. If your old
`dual_subs.settings.bat` is sitting there it gets imported once, so fonts,
sizes and margins carry over.

Fonts: the bottom side defaults to **Microsoft YaHei** (微软雅黑, *Wēiruǎn
Yǎhēi* — 微 wēi "micro", 软 ruǎn "soft", 雅 yǎ "elegant", 黑 hēi "black", i.e.
Microsoft's elegant gothic). Other CJK-safe choices: SimHei (黑体 *hēitǐ*,
"black/gothic form"), SimSun (宋体 *sòngtǐ*, the Song-dynasty serif), KaiTi
(楷体 *kǎitǐ*, the brush-regular style). Arial has no hanzi and will show
tofu boxes.

## Portability

The program itself is pure standard-library Python — no pip installs. What it
needs from outside is `mkvmerge` and `mkvextract`.

Search order:

`mkvpropedit` is needed only for changing default tracks; it ships with
MKVToolNix alongside mkvmerge, so add it to the portable folder if you want
that feature. Search order is the same as for mkvmerge:

1. `--mkvtoolnix DIR`
2. `mkvtoolnix_dir` in the config
3. `tools\mkvtoolnix\` next to the exe ← **the portable case**
4. anything on `PATH`
5. the usual `C:\Program Files\MKVToolNix` spots

To make a self-contained folder:

```
py -3 -m pip install pyinstaller py7zr
py -3 build\build_exe.py                                  # both exes
py -3 build\fetch_mkvtoolnix.py --dest dist\tools\mkvtoolnix
```

Giving you:

```
dist\
  dual_subs.exe          console
  dual_subs_gui.exe      window; drop files on the icon
  dual_subs.config.json
  tools\mkvtoolnix\mkvmerge.exe
                   mkvextract.exe
                   COPYING
```

`tkinterdnd2` is optional: install it and you can also drop files *into* the
window. Without it, dropping onto the icon still works, because Windows passes
the paths as arguments.

Copy that folder anywhere. Two caveats worth knowing:

- **MKVToolNix is GPLv2.** Keeping a copy for yourself is unrestricted. If you
  hand the folder to someone else, ship `COPYING` with it and be able to point
  them at upstream source.
- **MKVToolNix ≥ 90 needs Windows 10 or newer.** For Windows 7/8 targets, grab
  an older build from `mkvtoolnix.download/windows/`.

`ffmpeg` is used only as a fallback when `mkvextract` refuses a track. Nothing
breaks without it.

## What changed from the .bat

Same core logic, so output should be identical for the cases the .bat handled.
Additions:

- takes files and/or folders as arguments; `-r` for recursion
- external subtitles can be `.ass`, `.ssa`, `.vtt` or `.sub`, not just `.srt`
- looks in `Subs\`, `Subtitles\` and a folder named after the video, not just
  the video's own folder
- sync can correct a **framerate mismatch** (25 fps PAL subs on 23.976 fps
  video), not just a constant offset — it only does so when the rescale beats
  a plain shift by a clear margin
- sync can fall back to a *different*-language embedded track as its timing
  reference; dialogue onsets are language-independent, and the confidence check
  still guards it
- picks the best track when several match a language (skips ones flagged
  forced, or named "signs"/"songs")
- `--dry-run`, `--list`, `--keep-ass`, `--output-dir`, `--overwrite`
- an image-based track (PGS/VobSub) no longer silently blocks the external
  search, and the error says so explicitly
- settings are JSON, not `set "KEY=value"`
- a tkinter window (`--gui`), with drag-onto-the-icon queueing
- `--sidecar` / `--both`
- traditional → simplified Chinese conversion
- cue-pair snapping, so the two lines stop flickering in a fifth of a second
  apart
- encoding detection, so GB18030 and Big5 subtitles read correctly instead of
  becoming mojibake
- default audio/subtitle track switching via mkvpropedit, with no remux
- track selectors built from the queued files, with per-option coverage counts
- filename shortening, so long paths stop breaking VLC

Not carried over: nothing intentionally. The `!`-in-filename workaround the
.bat needed is simply not a problem here.

## Layout

```
dualsubs\
  langs.py       language codes and aliases
  formats.py     SRT / ASS / VTT / MicroDVD -> Event
  render.py      Event -> styled ASS, including the anti-collision positioning
  sync.py        offset + framerate estimation
  discover.py    matching loose subtitle files to a video
  probe.py       mkvmerge -J, track extraction, muxing
  pipeline.py    per-file orchestration
  align.py       whole-track offset, cue-pair snapping, grid alignment
  tracks.py      choosing default audio/subtitle tracks, and writing the flags
  survey.py      what tracks the queued files contain, for the selectors
  rename.py      shortening release filenames, with an undo log
  textio.py      encoding detection and UTF-8 normalisation
  hanzi.py       traditional -> simplified, and script detection
  _hanzi_data.py generated OpenCC tables
  config.py      settings, incl. import from the old .bat
  cli.py         argument parsing and console output
  gui.py         the tkinter window
tests\           pure-logic tests, no MKVToolNix needed
```

`python -m pytest tests` (or `python tests/test_basics.py`) to check nothing
broke after an edit.
