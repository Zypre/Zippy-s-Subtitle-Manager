"""What tracks the queued files actually contain.

The alternative is a hardcoded list of language codes, which is wrong in both
directions: it offers Chinese audio for a release that has only Japanese and
English, and it cannot offer Hungarian at all. Worse, a list that looks
authoritative invites you to pick something that does not exist and get a
silent no-op.

So the selectors are built from the files in front of you. Each option carries
how many of them actually have that track, because an eight-episode queue can
easily be seven files with Chinese subtitles and one without.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import discover, langs
from .probe import MediaInfo, ProbeError, probe
from .tools import Toolset

__all__ = ["TrackOption", "Survey", "survey", "spec_of"]

#: Separates the spec from its human description inside a combobox entry, so
#: the spec can be recovered by splitting. Also means a hand-typed spec with
#: no description still parses, which keeps free text working.
SEPARATOR = "   —   "

#: Words that say nothing about which track this is, within its language.
GENERIC_WORDS = frozenset({
    "dialogue", "dialog", "subtitles", "subtitle", "subs", "track", "full",
    "main", "complete", "audio", "stereo", "surround", "default",
})


@dataclass
class TrackOption:
    kind: str                 # "audio" or "subtitle"
    language: str             # canonical code, e.g. "chi"
    raw_language: str
    prefer: str = ""          # distinguishing words, e.g. "traditional"
    names: set[str] = field(default_factory=set)
    files: int = 0
    total: int = 0
    #: Files where this came from a subtitle file on disk, not an embedded track.
    on_disk: int = 0

    @property
    def spec(self) -> str:
        return f"{self.language}:{self.prefer}" if self.prefer else self.language

    @property
    def everywhere(self) -> bool:
        return self.files >= self.total

    def describe(self) -> str:
        name = sorted(self.names)[0] if self.names else ""
        if not name and self.on_disk:
            name = "subtitle file on disk"
        where = "all files" if self.everywhere else f"{self.files} of {self.total}"
        bits = [b for b in (langs.display(self.language), name) if b]
        return f"{' · '.join(bits)}  ({where})"

    def entry(self) -> str:
        """The string a combobox shows: spec first, so it round-trips."""
        return f"{self.spec}{SEPARATOR}{self.describe()}"


@dataclass
class Survey:
    audio: list[TrackOption] = field(default_factory=list)
    subtitles: list[TrackOption] = field(default_factory=list)
    scanned: int = 0
    failed: list[str] = field(default_factory=list)
    #: What each file contained, so callers can decide per file without
    #: running mkvmerge a second time.
    media: dict = field(default_factory=dict)

    def audio_entries(self) -> list[str]:
        return [o.entry() for o in self.audio]

    def subtitle_entries(self, embedded_only: bool = False) -> list[str]:
        """`embedded_only` drops options that exist only as files on disk,
        which a default-track flag cannot point at."""
        return [o.entry() for o in self.subtitles
                if not (embedded_only and o.on_disk >= o.files)]

    def summary(self) -> str:
        if not self.scanned:
            return ""
        bits = (f"{self.scanned} file(s): {len(self.audio)} audio, "
                f"{len(self.subtitles)} subtitle option(s)")
        return bits + (f", {len(self.failed)} unreadable" if self.failed else "")


def spec_of(entry: str) -> str:
    """Recover the spec from a combobox entry, or pass typed text through."""
    return (entry or "").split(SEPARATOR)[0].strip()


def entry_for(spec: str, options: list[TrackOption]) -> str:
    """The full entry matching a stored spec, so the box shows the description."""
    spec = (spec or "").strip()
    for option in options:
        if option.spec == spec:
            return option.entry()
    return spec


def distinguishing_words(name: str, language: str = "") -> str:
    """Reduce a track name to the words that identify it within its language.

    "Dialogue (Traditional)" -> "traditional"
    "Dialogue (Dub) (SDH)"   -> "dub sdh"
    "Signs & Songs"          -> "signs songs"
    "Dialogue"               -> ""           (the plain language is enough)
    "English" on an eng track -> ""          (says nothing we did not know)
    """
    # Only the language's own name is dropped, never every alias of it:
    # "simplified" and "traditional" are aliases for Chinese in the language
    # table, and stripping those would collapse the two Chinese tracks this
    # whole feature exists to tell apart.
    redundant = set()
    if language:
        redundant = {language, langs.display(language).lower()}

    words = [w for w in re.split(r"[^0-9a-z]+", (name or "").lower()) if w]
    kept = [w for w in words
            if w not in GENERIC_WORDS and len(w) > 1 and w not in redundant]
    seen: list[str] = []
    for w in kept:
        if w not in seen:
            seen.append(w)
    return " ".join(seen)




def survey(paths, tools: Toolset, progress=None, should_stop=None) -> Survey:
    """Probe each file and union what it offers.

    `progress(done, total)` is called as it goes, and `should_stop()` lets a
    caller abandon the scan when the queue changes underneath it.
    """
    paths = list(paths)
    total = len(paths)
    result = Survey()
    audio: dict = {}
    subs: dict = {}
    audio_seen: dict = {}
    subs_seen: dict = {}

    for index, path in enumerate(paths, 1):
        if should_stop is not None and should_stop():
            break
        try:
            media: MediaInfo = probe(path, tools)
        except (ProbeError, OSError) as exc:
            result.failed.append(f"{getattr(path, 'name', path)}: {exc}")
            if progress:
                progress(index, total)
            continue

        result.scanned += 1
        result.media[path] = media
        for kind, tracks_, bucket, seen in (("audio", media.audio, audio, audio_seen),
                                            ("subtitle", media.tracks, subs, subs_seen)):
            keys = set()
            for track in tracks_:
                language = langs.normalize(track.raw_language) or track.raw_language or "und"
                prefer = distinguishing_words(getattr(track, "name", ""), language)
                key = (language, prefer)
                keys.add(key)
                option = bucket.get(key)
                if option is None:
                    option = TrackOption(kind=kind, language=language,
                                         raw_language=track.raw_language,
                                         prefer=prefer, total=total)
                    bucket[key] = option
                if getattr(track, "name", ""):
                    option.names.add(track.name)
            if kind == "subtitle":
                # Files on disk count too: an external .chi.srt is as much an
                # option for the top or bottom line as an embedded track.
                for cand in discover.find_candidates(path):
                    if not cand.language:
                        continue
                    key = (cand.language, "")
                    option = bucket.get(key)
                    if option is None:
                        option = bucket[key] = TrackOption(
                            kind=kind, language=cand.language,
                            raw_language=cand.language, total=total)
                    if key not in keys:
                        option.on_disk += 1
                        keys.add(key)
            for key in keys:
                seen[key] = seen.get(key, 0) + 1
        if progress:
            progress(index, total)

    for bucket, seen, out in ((audio, audio_seen, result.audio),
                              (subs, subs_seen, result.subtitles)):
        for key, option in bucket.items():
            option.files = seen.get(key, 0)
            option.total = result.scanned or total
            out.append(option)
        # Commonest first, then by language, so the useful ones are at the top.
        out.sort(key=lambda o: (-o.files, o.language, o.prefer))

    # No synthetic options. An earlier version invented a bare "chi" entry
    # whenever every Chinese track was qualified, meaning "the best dialogue
    # track" -- but it appeared in the list next to real tracks while
    # corresponding to none of them, and for Simplified vs Traditional it is
    # a coin toss, which is the exact ambiguity these selectors exist to
    # remove. Every entry below is a track that is really in the files.
    return result
