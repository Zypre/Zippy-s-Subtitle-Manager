"""Choosing which tracks a player picks by default, and writing that back.

Nothing here touches the video or audio data. Default/forced flags live in the
Matroska header, so `mkvpropedit` rewrites them in place in well under a
second regardless of file size -- a 30 GB remux and a 200 MB episode cost the
same. That is the whole reason this is a separate operation from muxing.

The hard part is not writing the flags, it is deciding which track you meant.
Real releases are a mess: the example this was built against had *twelve*
subtitle tracks flagged default at once, two default audio tracks, and three
separate English subtitle tracks -- full dialogue, forced signs & songs, and
an SDH dub script. Picking "the English one" by language alone gets you the
signs track about a third of the time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import langs
from .probe import AudioTrack, MediaInfo, Track
from .tools import Toolset, run

__all__ = ["TrackWish", "Selection", "choose", "apply_flags", "FlagError",
           "FlagOutcome", "settle_flags", "wants_flags"]


class FlagError(RuntimeError):
    pass


#: Track-name words that mark a subtitle as *not* the main dialogue track.
#: Checked against the track name, which is where releases actually put this --
#: the forced flag alone is unreliable, plenty of signs tracks never set it.
SIGNS_WORDS = ("signs", "songs", "sign/song", "forced", "s&s")
SDH_WORDS = ("sdh", "cc", "hearing", "closed caption")
DUB_WORDS = ("dub", "dubtitle")
COMMENTARY_WORDS = ("commentary", "comment", "director")

#: Preferred flavours, in the order they are tried.
FLAVOURS = ("dialogue", "full", "main")


@dataclass
class TrackWish:
    """What the user asked for on one track slot."""

    language: str = ""
    #: Free text matched against the track name, e.g. "simplified", "dub".
    prefer: str = ""
    #: Exact track id, which overrides everything else.
    track_id: int | None = None
    #: Accept a signs/songs or SDH track if nothing else matches.
    allow_signs: bool = False
    allow_sdh: bool = False

    @classmethod
    def parse(cls, value: str) -> "TrackWish":
        """'eng', '#7', 'chi:simplified', 'eng:dub' -> a wish.

        The colon form is what makes a file like the example tractable: `chi`
        alone cannot choose between "Dialogue (Simplified)" and
        "Dialogue (Traditional)", both flagged default.
        """
        value = (value or "").strip().strip('"')
        if not value:
            return cls()
        if value.startswith("#") and value[1:].isdigit():
            return cls(track_id=int(value[1:]))
        language, _, prefer = value.partition(":")
        prefer = prefer.strip().lower()
        return cls(
            language=langs.normalize(language) or language.strip().lower(),
            prefer=prefer,
            allow_signs=any(w in prefer for w in SIGNS_WORDS),
            allow_sdh=any(w in prefer for w in SDH_WORDS),
        )

    @property
    def label(self) -> str:
        if self.track_id is not None:
            return f"#{self.track_id}"
        return f"{self.language}:{self.prefer}" if self.prefer else self.language


@dataclass
class Selection:
    """What `choose` decided, and why."""

    audio: AudioTrack | None = None
    subtitle: Track | None = None
    subtitle_off: bool = False
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _name_of(track) -> str:
    return (getattr(track, "name", "") or "").lower()


def _score_subtitle(track: Track, wish: TrackWish) -> tuple[int, str] | None:
    """Rank one candidate. Higher is better; None means never pick this."""
    name = _name_of(track)
    score = 0
    notes: list[str] = []

    is_signs = any(w in name for w in SIGNS_WORDS) or track.forced
    is_sdh = any(w in name for w in SDH_WORDS)
    is_dub = any(w in name for w in DUB_WORDS)
    is_commentary = any(w in name for w in COMMENTARY_WORDS)

    if is_commentary and "commentary" not in wish.prefer:
        return None
    if is_signs and not wish.allow_signs:
        # A signs & songs track carries a handful of cues, not the dialogue.
        # Picking it as the default subtitle is the classic failure here.
        return None
    if is_sdh and not wish.allow_sdh:
        score -= 40
        notes.append("SDH")
    if is_dub and "dub" not in wish.prefer:
        score -= 20
        notes.append("dub script")

    if wish.prefer:
        # Every word the user asked for that appears in the name.
        wanted = [w for w in re.split(r"[\s,/+-]+", wish.prefer) if w]
        hits = [w for w in wanted if w in name]
        if not hits and wanted:
            return None                      # they were specific; respect it
        score += 100 * len(hits)
        notes.append("matches " + "+".join(hits))

    if any(f in name for f in FLAVOURS):
        score += 30
        notes.append("dialogue track")
    if track.default:
        score += 5
    if track.is_image:
        score -= 60
        notes.append("image-based")

    score -= track.id * 0.001              # stable tie-break on track order
    return int(score * 1000), ", ".join(notes) or "language match"


def _score_audio(track: AudioTrack, wish: TrackWish) -> tuple[int, str] | None:
    name = _name_of(track)
    if any(w in name for w in COMMENTARY_WORDS) and "commentary" not in wish.prefer:
        return None
    score = 0
    notes: list[str] = []
    if wish.prefer:
        wanted = [w for w in re.split(r"[\s,/+-]+", wish.prefer) if w]
        hits = [w for w in wanted if w in name]
        if not hits and wanted:
            return None
        score += 100 * len(hits)
        notes.append("matches " + "+".join(hits))
    if track.default:
        score += 5
    return int(score * 1000) - track.id, ", ".join(notes) or "language match"


def choose(media: MediaInfo, audio_wish: TrackWish | None,
           subtitle_wish: TrackWish | None,
           subtitles_off: bool = False) -> Selection:
    """Work out which tracks should carry the default flag."""
    sel = Selection(subtitle_off=subtitles_off)

    if audio_wish and (audio_wish.language or audio_wish.track_id is not None):
        sel.audio, note = _pick(media.audio, audio_wish, _score_audio, "audio")
        if sel.audio is not None:
            sel.reasons.append(
                f"audio -> track {sel.audio.id} "
                f"{sel.audio.raw_language}"
                f"{' ' + sel.audio.name if sel.audio.name else ''} ({note})")
        else:
            sel.warnings.append(
                f"no {langs.display(audio_wish.language)} audio track"
                if audio_wish.track_id is None
                else f"no audio track #{audio_wish.track_id}")

    if subtitles_off:
        sel.reasons.append("subtitles -> off")
    elif subtitle_wish and (subtitle_wish.language or subtitle_wish.track_id is not None):
        sel.subtitle, note = _pick(media.tracks, subtitle_wish,
                                   _score_subtitle, "subtitle")
        if sel.subtitle is not None:
            sel.reasons.append(
                f"subtitles -> track {sel.subtitle.id} "
                f"{sel.subtitle.raw_language}"
                f"{' ' + sel.subtitle.name if sel.subtitle.name else ''} ({note})")
        else:
            sel.warnings.append(_no_subtitle_message(media, subtitle_wish))

    return sel


def _pick(tracks, wish: TrackWish, scorer, kind: str):
    if wish.track_id is not None:
        for t in tracks:
            if t.id == wish.track_id:
                return t, "chosen by id"
        return None, ""

    scored = []
    for t in tracks:
        if wish.language and not langs.matches(t.raw_language, wish.language):
            continue
        result = scorer(t, wish)
        if result is not None:
            scored.append((result[0], t.id, t, result[1]))
    if not scored:
        return None, ""
    scored.sort(key=lambda row: (-row[0], row[1]))
    return scored[0][2], scored[0][3]


def _no_subtitle_message(media: MediaInfo, wish: TrackWish) -> str:
    """Say *why* nothing matched -- usually it is the signs-track filter."""
    if wish.track_id is not None:
        return f"no subtitle track #{wish.track_id}"
    same_language = [t for t in media.tracks
                     if langs.matches(t.raw_language, wish.language)]
    if not same_language:
        return f"no {langs.display(wish.language)} subtitle track"
    if wish.prefer:
        names = ", ".join(f'"{t.name}"' for t in same_language if t.name) or "unnamed"
        return (f'no {langs.display(wish.language)} subtitle track matching '
                f'"{wish.prefer}" (found: {names})')
    return (f"the only {langs.display(wish.language)} subtitle tracks are "
            f"signs/songs or commentary; use "
            f"-S {wish.language}:signs to pick one anyway")


# --------------------------------------------------------------------------


def apply_flags(media: MediaInfo, sel: Selection, tools: Toolset,
                dry_run: bool = False) -> list[str]:
    """Write the flags with mkvpropedit. Returns the edits made.

    Every candidate track is set explicitly rather than only the winner. The
    example file had twelve subtitle tracks flagged default at once; setting
    one more without clearing the rest changes nothing a player will notice.
    """
    edits: list[str] = []
    cmd = [str(tools.mkvpropedit), str(media.path)]

    if sel.audio is not None:
        for t in media.audio:
            want = t.id == sel.audio.id
            if bool(t.default) != want:
                edits.append(f"audio {t.id} default={'yes' if want else 'no'}")
            cmd += ["--edit", f"track:={t.uid}" if t.uid else f"track:a{_ordinal(media.audio, t)}",
                    "--set", f"flag-default={1 if want else 0}"]

    if sel.subtitle is not None or sel.subtitle_off:
        for t in media.tracks:
            want = sel.subtitle is not None and t.id == sel.subtitle.id
            if bool(t.default) != want:
                edits.append(f"subtitle {t.id} default={'yes' if want else 'no'}")
            cmd += ["--edit", f"track:={t.uid}" if t.uid else f"track:s{_ordinal(media.tracks, t)}",
                    "--set", f"flag-default={1 if want else 0}"]

    if len(cmd) <= 2:
        return []
    if dry_run:
        return edits

    res = run(cmd)
    if res.returncode not in (0, 1):
        raise FlagError((res.stderr or res.stdout).strip()[:400])
    return edits


def _ordinal(tracks, track) -> int:
    """1-based position within its own kind, for mkvpropedit's track:s2 form."""
    return [t.id for t in tracks].index(track.id) + 1


@dataclass
class FlagOutcome:
    """What settle_flags did to one file, as lines ready to print."""

    lines: list[str] = field(default_factory=list)
    edits: list[str] = field(default_factory=list)
    ok: bool = True


def wants_flags(audio: str = "", subtitles: str = "", subtitles_off: bool = False,
                newest_subtitle: bool = False) -> bool:
    return bool(audio or subtitles or subtitles_off or newest_subtitle)


def settle_flags(path, tools: Toolset, *, audio: str = "", subtitles: str = "",
                 subtitles_off: bool = False, newest_subtitle: bool = False,
                 dry_run: bool = False) -> FlagOutcome:
    """Read `path`, choose the default tracks, and write them.

    The one place the command line and the window both go through, whether
    the file is a fresh remux or an original left in place.

    `newest_subtitle` makes the last subtitle track the default: after a mux
    that is the dual track this run appended. mkvmerge copies every source
    track's flags, so it is not enough for the new track to be marked
    default -- the originals have to be cleared as well.
    """
    from .probe import ProbeError, probe

    if tools.mkvpropedit is None:
        return FlagOutcome(["! mkvpropedit was not found; it ships with "
                            "MKVToolNix and is what changes default tracks"],
                           ok=False)
    try:
        media = probe(path, tools)
    except (ProbeError, OSError) as exc:
        return FlagOutcome([f"! {exc}"], ok=False)

    audio_wish = TrackWish.parse(audio) if audio else None
    subs_wish = TrackWish.parse(subtitles) if subtitles and not newest_subtitle else None
    sel = choose(media, audio_wish, subs_wish,
                 subtitles_off=subtitles_off and not newest_subtitle)
    if newest_subtitle and media.tracks:
        sel.subtitle = media.tracks[-1]
        sel.subtitle_off = False
        sel.reasons.append(f"subtitles -> track {sel.subtitle.id} "
                           f"{sel.subtitle.name or 'dual'} (the track just created)")

    out = FlagOutcome(list(sel.reasons) + [f"! {w}" for w in sel.warnings])
    if sel.audio is None and sel.subtitle is None and not sel.subtitle_off:
        out.ok = False
        return out
    try:
        out.edits = apply_flags(media, sel, tools, dry_run=dry_run)
    except FlagError as exc:
        out.lines.append(f"! mkvpropedit: {exc}")
        out.ok = False
        return out
    verb = "would set" if dry_run else "set"
    out.lines.append(f"{verb} {len(out.edits)} flag(s)" if out.edits
                     else "flags already correct")
    return out
