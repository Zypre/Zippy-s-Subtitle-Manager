"""Turn one video into a dual-subtitle video."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import align, discover, hanzi, langs, probe, render, sync
from .config import Config
from .formats import Event, load, load_events
from .probe import MediaInfo, ProbeError, Track
from .tools import Toolset


class SideError(RuntimeError):
    pass


@dataclass
class SideSpec:
    """What the user asked for on one side."""

    language: str = ""
    file: Path | None = None      # explicit subtitle file
    track_id: int | None = None   # explicit embedded track
    #: Words to match against the embedded track's name, so "chi:traditional"
    #: can pick between two Chinese tracks that are otherwise identical.
    prefer: str = ""

    @classmethod
    def parse(cls, value: str) -> "SideSpec":
        """Accept 'eng', '#3' (track id), or a path to a subtitle file."""
        value = (value or "").strip().strip('"')
        if not value:
            return cls()
        if value.startswith("#") and value[1:].isdigit():
            return cls(track_id=int(value[1:]))
        p = Path(value)
        if p.suffix.lower() in discover.SUBTITLE_EXTS and p.exists():
            return cls(file=p.resolve())
        language, _, prefer = value.partition(":")
        return cls(language=langs.normalize(language) or language.strip().lower(),
                   prefer=prefer.strip().lower())

    @property
    def label(self) -> str:
        if self.file:
            return self.file.name
        if self.track_id is not None:
            return f"track #{self.track_id}"
        if self.prefer:
            return f"{self.language}:{self.prefer}"
        return self.language or "?"


@dataclass
class SideResult:
    events: list[Event]
    source: str                       # human-readable description
    external_file: Path | None = None
    sync_note: str = ""
    convert_note: str = ""
    encoding: str = ""


@dataclass
class FileResult:
    video: Path
    ok: bool
    output: Path | None = None
    message: str = ""
    notes: list[str] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)


# --------------------------------------------------------------------------


@dataclass
class SourceChoice:
    """Where one side's text will come from, decided without extracting it."""

    track: Track | None = None
    file: Path | None = None
    problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem

    def short(self) -> str:
        """A compact description, for a table cell."""
        if self.problem:
            return self.problem
        if self.file is not None:
            return f"file: {self.file.name}"
        t = self.track
        name = f' "{t.name}"' if t.name else ""
        return f"#{t.id} {langs.display(t.raw_language)}{name}"


def choose_source(media: MediaInfo, spec: SideSpec,
                  claimed: set[Path] | None = None) -> SourceChoice:
    """Explicit file > explicit track > embedded text track > file on disk.

    Pure decision, no extraction, so the window can show for every queued
    file which track or file each line will come from before anything runs.
    `resolve_side` makes the same call and then does the work.
    """
    if spec.file:
        return SourceChoice(file=spec.file)

    if spec.track_id is not None:
        track = next((t for t in media.tracks if t.id == spec.track_id), None)
        if track is None:
            return SourceChoice(problem=f"no subtitle track #{spec.track_id}")
        if track.is_image:
            return SourceChoice(problem=f"track #{track.id} is image-based "
                                        f"({track.codec}); it has no text")
        return SourceChoice(track=track)

    if not spec.language:
        return SourceChoice(problem="no language given")

    track = _best_embedded(media, spec)
    if track is not None:
        return SourceChoice(track=track)
    return _external_choice(media, spec, claimed)


def _external_choice(media: MediaInfo, spec: SideSpec,
                     claimed: set[Path] | None) -> SourceChoice:
    cand = discover.pick(media.path, spec.language, exclude=claimed)
    if cand is not None:
        return SourceChoice(file=cand.path)
    image_only = media.subtitle_tracks(spec.language, text_only=False)
    hint = ""
    if image_only:
        hint = (f" (the only {langs.display(spec.language)} track is "
                f"image-based: {image_only[0].codec})")
    return SourceChoice(problem=(
        f"no {langs.display(spec.language)} subtitles found - "
        f"not embedded as text and no matching file on disk{hint}"))


def resolve_side(media: MediaInfo, spec: SideSpec, tools: Toolset,
                 workdir: Path, cfg: Config, claimed: set[Path],
                 tag: str) -> SideResult:
    """Find this side's subtitles: explicit file > explicit track > embedded > external."""
    choice = choose_source(media, spec, claimed)

    if choice.track is not None:
        track = choice.track
        try:
            events = probe.extract_events(media, track, tools, workdir / tag)
        except ProbeError:
            if spec.track_id is not None:
                raise
            events = []
        if events:
            return SideResult(events, f"embedded {track.describe()}")
        if spec.track_id is not None:
            raise SideError(f"track #{track.id} contained no readable cues")
        # A language match that would not extract: try the files on disk.
        choice = _external_choice(media, spec, claimed)

    if choice.problem:
        raise SideError(choice.problem)

    path = choice.file
    events, encoding = load(path, spec.language)
    if not events:
        raise SideError(f"{path.name} contained no readable cues")
    label = "file" if spec.file else "external"
    result = SideResult(events, f"{label} {path.name}", path, encoding=encoding)
    _maybe_sync(media, result, tools, workdir, cfg, spec.language)
    return result


def _maybe_sync(media: MediaInfo, side: SideResult, tools: Toolset,
                workdir: Path, cfg: Config, language: str) -> None:
    """Retime an external file onto the video's own subtitle timing."""
    if not cfg.auto_sync:
        return
    reference, ref_desc = _sync_reference(media, language)
    if reference is None:
        side.sync_note = "no embedded track to sync against; timing left as-is"
        return
    onsets = probe.track_onsets_ms(media, reference, tools, workdir)
    if not onsets:
        side.sync_note = "could not read embedded timestamps; timing left as-is"
        return
    result = sync.estimate(onsets, sync.onsets_ms(side.events),
                           allow_rate_fix=cfg.allow_rate_fix)
    side.events = sync.apply(side.events, result)
    side.sync_note = f"{result.reason} (vs {ref_desc})"


def _sync_reference(media: MediaInfo, language: str) -> tuple[Track | None, str]:
    """Prefer a same-language embedded track; any embedded track will do.

    Either way it has to be a dialogue track. Releases often list a forced
    "Signs & Songs" track first, and a handful of sign cues cannot vote on
    where a thousand lines of dialogue belong -- sync then gives up with
    "low confidence" when a perfectly good reference was sitting next to it.
    """
    same = sorted(media.subtitle_tracks(language, text_only=False), key=_reference_rank)
    if same:
        return same[0], f"embedded {langs.display(language)} track {same[0].id}"
    others = sorted(media.tracks, key=_reference_rank)
    if others:
        t = others[0]
        return t, f"embedded {t.raw_language} track {t.id}"
    return None, ""


def _reference_rank(track: Track) -> tuple:
    from .tracks import COMMENTARY_WORDS, SIGNS_WORDS
    name = (track.name or "").lower()
    partial = track.forced or any(w in name for w in SIGNS_WORDS + COMMENTARY_WORDS)
    return (partial, track.id)


# --------------------------------------------------------------------------


def process(video: Path, top_spec: SideSpec, bot_spec: SideSpec, tools: Toolset,
            cfg: Config, *, output_dir: Path | None = None,
            overwrite: bool = False, dry_run: bool = False,
            keep_ass: bool = False, log=print) -> FileResult:
    notes: list[str] = []
    dest = output_dir or video.parent
    mode = (cfg.output_mode or "mux").lower()
    want_mux = mode in ("mux", "both")
    want_sidecar = mode in ("sidecar", "both")

    # mkvmerge always writes Matroska, whatever the source container was.
    out_mkv = dest / f"{video.stem}{cfg.output_suffix}.mkv"
    out_ass = dest / f"{video.stem}{cfg.sidecar_suffix}.ass"

    clashes = [p for p, wanted in ((out_mkv, want_mux), (out_ass, want_sidecar))
               if wanted and p.exists()]
    if want_sidecar and out_ass.exists() and not discover.is_generated(out_ass):
        # Releases do ship "<episode>.ass" beside "<episode>.mkv". Overwriting
        # it would destroy the very subtitles this run may be reading from.
        return FileResult(video, False, message=(
            f"{out_ass.name} already exists and was not made by dual_subs, so "
            f"it is never overwritten. Set a sidecar suffix (e.g. .dual) to "
            f"write alongside it."))
    if clashes and not overwrite:
        names = ", ".join(p.name for p in clashes)
        return FileResult(video, False, message=f"{names} already exists (use --overwrite)")

    workdir = probe.make_workdir()
    try:
        media = probe.probe(video, tools)
        claimed: set[Path] = set()

        top = resolve_side(media, top_spec, tools, workdir, cfg, claimed, "top")
        if top.external_file:
            claimed.add(top.external_file)
        _report(top, "top", cfg.top_convert, notes, log)

        bot = resolve_side(media, bot_spec, tools, workdir, cfg, claimed, "bot")
        if bot.external_file:
            claimed.add(bot.external_file)
        _report(bot, "bottom", cfg.bottom_convert, notes, log)

        top_events, bot_events = top.events, bot.events
        mode = (cfg.align_mode or "pairs").lower()
        if not cfg.snap_sides:
            mode = "off"
        anchor = _choose_anchor(media, top_spec, bot_spec, top, bot, cfg)

        if mode in ("pairs", "grid"):
            snapped = align.snap(top_events, bot_events,
                                 window_cs=max(0, cfg.snap_window_ms // 10),
                                 anchor=anchor)
            top_events, bot_events = snapped.top, snapped.bottom
            if snapped.changed or snapped.offset.applied:
                log(f"    align  : {snapped.describe()}")
                notes.append(f"align: {snapped.describe()}")

        if mode == "grid":
            # One side keeps its timing; the other stops changing at moments
            # the first one does not.
            window = max(0, cfg.grid_window_ms // 10)
            if anchor == "bottom":
                res = align.grid_snap(bot_events, top_events, window)
                top_events = res.moving
            else:
                res = align.grid_snap(top_events, bot_events, window)
                bot_events = res.moving
            moving = "top" if anchor == "bottom" else "bottom"
            log(f"    grid   : anchored on the {anchor} line; {res.describe()}")
            notes.append(f"grid ({moving} moved): {res.describe()}")

        script = render.build_ass(top_events, bot_events, cfg.top, cfg.bottom,
                                  cfg.layout)

        written: list[Path] = []
        if dry_run:
            targets = ([out_mkv] if want_mux else []) + ([out_ass] if want_sidecar else [])
            return FileResult(video, True, targets[0] if targets else None,
                              message="dry run - nothing written", notes=notes,
                              written=targets)

        dest.mkdir(parents=True, exist_ok=True)

        if want_sidecar:
            _write(out_ass, script)
            written.append(out_ass)

        if want_mux:
            ass_path = workdir / f"{video.stem}.dual.ass"
            _write(ass_path, script)
            if keep_ass and not want_sidecar:
                kept = dest / ass_path.name
                shutil.copy2(ass_path, kept)
                notes.append(f"kept {kept.name}")
            track_name = cfg.track_name_template.format(
                top=top_spec.label, bottom=bot_spec.label)
            probe.mux(video, ass_path, out_mkv, tools, track_name)
            written.append(out_mkv)

            # Only stash when a new container replaced the old one. In sidecar
            # mode the video must stay put or the .ass stops pairing with it.
            if cfg.stash_originals:
                stash = video.parent / cfg.stash_dir_name
                _stash(video, stash, notes)
                for f in claimed:
                    _stash(f, stash, notes)

        return FileResult(video, True, written[0] if written else None,
                          notes=notes, written=written)

    except (SideError, ProbeError) as exc:
        return FileResult(video, False, message=str(exc), notes=notes)
    except subprocess.TimeoutExpired as exc:
        return FileResult(video, False, notes=notes,
                          message=f"{Path(exc.cmd[0]).stem} gave no answer after "
                                  f"{int(exc.timeout)} s")
    except OSError as exc:
        # A read-only folder, a full disk, a file locked by a player: one
        # file fails and the rest of the batch carries on.
        where = f" ({exc.filename})" if getattr(exc, "filename", None) else ""
        return FileResult(video, False, notes=notes,
                          message=f"{exc.strerror or exc}{where}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _best_embedded(media: MediaInfo, spec: SideSpec):
    """Pick an embedded track, sharing the signs/SDH scoring used for flags.

    Without this, "chi" on a release carrying both Simplified and Traditional
    is a coin toss, and an English "Signs & Songs" track with seven cues can
    win over the real dialogue.
    """
    from .tracks import TrackWish, _pick, _score_subtitle

    wish = TrackWish.parse(f"{spec.language}:{spec.prefer}" if spec.prefer
                           else spec.language)
    track, _ = _pick([t for t in media.tracks if not t.is_image],
                     wish, _score_subtitle, "subtitle")
    if track is not None:
        return track
    return media.best_track(spec.language, text_only=True) if not spec.prefer else None


def _choose_anchor(media: MediaInfo, top_spec: SideSpec, bot_spec: SideSpec,
                   top: SideResult, bot: SideResult, cfg: Config) -> str:
    """Which side keeps its own timing.

    The right answer is whichever language you are listening to, so the
    video's default audio track decides it when we can read one. Failing
    that, an embedded subtitle came with the video and is timed to it, while
    an external file is a guess we already retimed once -- so move that one.
    """
    if (cfg.anchor or "auto").lower() in ("top", "bottom"):
        return cfg.anchor.lower()

    audio = media.default_audio_language
    if audio:
        if langs.matches(audio, top_spec.language):
            return "top"
        if langs.matches(audio, bot_spec.language):
            return "bottom"

    if top.external_file and not bot.external_file:
        return "bottom"
    return "top"


def _report(side: SideResult, which: str, convert_mode: str,
            notes: list[str], log) -> None:
    """Log a resolved side, applying any script conversion first."""
    if convert_mode:
        side.events, note = hanzi.convert_events(side.events, convert_mode)
        if note:
            side.convert_note = note
    detail = f"{len(side.events)} cues"
    if side.encoding and side.encoding not in ("utf-8", "utf-8-sig"):
        detail += f", read as {side.encoding}"
    log(f"    {which:<7}: {side.source}  ({detail})")
    for label, text in (("sync", side.sync_note), ("hanzi", side.convert_note)):
        if text:
            log(f"             {label}: {text}")
            notes.append(f"{which} {label}: {text}")


def _write(path: Path, text: str) -> None:
    """newline="" so the ASS keeps exactly the line endings we generated."""
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(text)


def _stash(path: Path, stash_dir: Path, notes: list[str]) -> None:
    if not path.exists():
        return
    stash_dir.mkdir(parents=True, exist_ok=True)
    target = stash_dir / path.name
    if target.exists():
        target = stash_dir / f"{path.stem}.{id(path)}{path.suffix}"
    try:
        shutil.move(str(path), str(target))
    except OSError as exc:
        notes.append(f"could not move {path.name} to {stash_dir.name}/: {exc}")


def collect_videos(targets: list[Path], exts: list[str],
                   recursive: bool, suffix: str) -> list[Path]:
    """Expand files/folders into a de-duplicated, sorted list of videos."""
    exts_l = tuple(e.lower() for e in exts)
    out: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        if p.suffix.lower() not in exts_l:
            return
        if p.stem.lower().endswith(suffix.lower()):
            return                    # don't re-process our own output
        key = str(p.resolve()).lower()
        if key not in seen:
            seen.add(key)
            out.append(p.resolve())

    for t in targets:
        t = Path(t)
        if t.is_file():
            add(t)
        elif t.is_dir():
            it = t.rglob("*") if recursive else t.glob("*")
            for p in it:
                if p.is_file() and "_originals" not in p.parts:
                    add(p)
    return sorted(out)
