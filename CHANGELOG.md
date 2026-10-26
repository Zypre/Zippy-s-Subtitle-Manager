# Changelog

Every release gets a version number here and in `dualsubs/__init__.py`, which
is what the window title, the console banner and the header of every
generated `.ass` read. Numbering is `major.minor.patch`: patch for fixes only,
minor for new features, major for changes that alter what existing settings
or commands do.

To cut a release: bump `__version__`, add a section below, commit, then tag it
`v<version>` (for example `v2.1.0`) and attach the zip to a GitHub release of
the same name.

## 2.2.0 — 2026-10-26

### The window

- **The appearance preview shows the worst moment in the queued files**:
  the instant with the most rows of text on screen, top and bottom combined
  (wider text wins a tie), drawn with your fonts and settings. A caption
  names the file and time, so you can find it in the video. Until the files
  have been read it shows sample text, as before.
- Each file is read once, when it is added, in the background. Every text
  track comes out of that one pass: extracting one embedded track means
  reading the whole video, and mkvextract writes all the tracks it is asked
  for during that single read (measured: 3 tracks plus timestamps from a
  360 MB file read exactly 360 MB). Changing languages, fonts or sizes
  afterwards only recalculates.
- Any job (a run, a rename, setting flags...) stops the reading at once —
  the read in progress is killed, since Windows will not rename a file
  another program has open — and it carries on afterwards. Closing the
  window stops it too.
- The preview frames as much of the screen as the stack needs, and draws
  rows broken exactly where the script will break them.
- The "preview with a two-row bottom line" option is gone; the worst case
  replaces it. "Preview the worst moment" can be switched off.

### Output

- **Long Chinese and Japanese lines no longer run off the screen.** Players
  wrap only at spaces by default, so a space-free line wider than the frame
  did not wrap at all (confirmed with libass 0.17.1). The script now breaks
  such lines itself, evenly and after punctuation where possible.
- **Fixed in 2.1.0's auto-lift:** it assumed long Chinese lines wrap, which
  they do not by default, so it could lift the top line for a second row
  that never appeared. Lift, line breaks and the preview now share one
  model of where rows break.
- **Bilingual Chinese tracks** (简日双语, simplified Chinese + Japanese) no
  longer put their Japanese on the top line. Every Japanese row on the
  Chinese side is left out, whether it is its own event or a second row of
  one; both used to be drawn over the Chinese. Kana is the tell: Chinese
  does not use it, so a stray stylistic の does not count.
- **Signs stay where they were placed.** A sign whose position is set in a
  later override block (`{\fad(..)}{\an7\pos(..)}`) used to be missed, and
  one placed by its style (a top-left status window) had that placement
  lost; both were pulled down into the bottom line. A minor style's
  placement is now written into its lines, and a style carrying a good
  share of the track counts as dialogue wherever it sits.

### Tests

- 110 tests (from 99): line breaking, the worst-moment search, one-pass
  extraction, killing a cancelled read, and the preview finding the worst
  moment in a real queued file.

## 2.1.0 — 2026-10-26

### The window

- **The queue shows what will happen before it happens.** Files are grouped
  by folder, and for every file the *Top line* and *Bottom line* columns say
  exactly which embedded track or subtitle file will be used — recomputed as
  you change the selectors. A file that would fail is marked in orange before
  you run, with the reason; a summary under the options says how many are
  ready and warns when a plain `chi` matches both a Simplified and a
  Traditional track.
- After a run, a *Result* column shows ✓ or ✗ per file.
- Long release names no longer look identical: the part every file in a
  folder shares is trimmed (`… S01E06 - Title …`); hover for the full name.
- **Fixed: the Default tracks tab was ignored by Run.** It is now applied
  after every file — to the new MKV after a remux, or to the original in
  subtitle-file mode (a header edit, no remux).
- The window no longer freezes: Inspect, Set flags, UTF-8 conversion and
  renaming run in the background. Only one job runs at a time, and **Run
  becomes Stop** while a batch is going (it stops after the current file).
- Settings changed during a run no longer leak into that run.
- **Appearance preview**: the bottom of a 1080p frame drawn with your fonts,
  sizes, margins, colours and outline, including the lift over a two-row
  bottom line. New per-line *Italic*, *Colour* and *Outline* settings.
- **Swap** puts the bottom language on top; each language keeps its font,
  size and colour.
- A `...` button beside each line picks one particular subtitle file.
- Options that cannot apply are greyed out (stashing and the dual default in
  subtitle-file mode, the grid window outside grid mode, and so on), and
  hovering any option explains it.
- Output mode is three labelled choices instead of a drop-down.
- Menu bar (File / Tools / Help), a *Tools* button, right-click and
  double-click on the queue, and shortcuts: Ctrl+O, Ctrl+Shift+O, Delete,
  Ctrl+I, Ctrl+S, Ctrl+Enter.
- **Undo last rename** is now in the window (it used to need the command
  line).
- The selectors also offer languages found as subtitle files on disk, not
  only embedded tracks.
- Remembers its size, the last folder and *include subfolders*.
- `dual_subs_gui.pyw` opens the window without a console behind it.
- The version is in the title bar and Help → About.

### Fixes

- **An episode could use the next episode's subtitles.** With long shared
  names (`Show - S01E01 - Pilot.mkv` beside `Show - S01E02 - Ep.chi.srt`) the
  loose name match paired them, so episode 1 silently got episode 2's
  Chinese — and in remux mode moved it into `_originals\`, so episode 2 then
  failed. Different `SxxEyy` markers now never match, and a file that matches
  a neighbouring video better is left for that video.
- **Shorten names could rename a neighbour's subtitle** to this episode's
  name, for the same reason. It also wrote the undo log only into the first
  folder of a multi-folder queue; every folder now gets its own.
- **Traditional → simplified mangled protected words**: 著名 became 着名,
  扭轉乾坤 became 扭转干坤, 乾隆 became 干隆. OpenCC's protective phrase
  entries were being dropped by the table generator and then overridden
  character by character. Checked against OpenCC itself on 20,000 random
  strings: the only remaining differences are the intended Hong Kong variants.
- Chinese conversion no longer rewrites font names inside ASS tags
  (`{\fn微軟正黑體}`), which broke the font.
- Re-running with *replace output* no longer reads the previous run's `.ass`
  back in as a source (which produced three lines on screen).
- A release's own `<episode>.ass` beside the video is never overwritten in
  subtitle-file mode, even with *replace output*.
- A long bottom line that wraps by itself now lifts the top line too; only
  explicit line breaks used to count. Checked by rendering with libass.
- Subtitles inside `Subs\<episode name>\2_English.srt` (and `Subs\` beside a
  single film) are found.
- `.forced` / `.signs` subtitle files no longer win over the full dialogue
  file just by sorting first alphabetically; SDH ranks slightly lower too.
- Sync uses a dialogue track as its reference, never a forced signs track.
- "The only Chinese track is image-based: SubRip/SRT" — the message for a
  missing track now says what was actually missing.
- A read-only folder or a locked file fails that one file instead of
  crashing the whole command-line batch.
- *Subtitles to UTF-8* leaves UTF-8 files with a byte-order mark alone
  instead of rewriting them and leaving a `.orig` behind.
- In subtitle-file mode, `-A` / `-S` / `--subs-off` now apply to the
  original file instead of being ignored.
- A comma in a font name no longer corrupts the style line.
- A remux is always named `.mkv`, whatever the source container.
- The windowed build no longer flashes a console when checking MKVToolNix.

### Tests

- 99 tests (from 64): regression tests for each fix above, end-to-end runs
  on real MKVs (skipped without MKVToolNix) and window tests (skipped
  without a display; they never touch your settings file).

## 2.0.0

The Python port of `dual_subs.bat`, released on GitHub as "First Version".
