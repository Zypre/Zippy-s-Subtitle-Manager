"""Pair loose subtitle files with the video they belong to.

Two naming conventions dominate:

  episodic   S01E01--0001.srt          ->  S01E01--0001-Some Title.mkv
             (the subtitle name is a prefix of the video name)

  movie      Some.Movie.2019.chi.srt   ->  Some.Movie.2019.1080p.BluRay.x264.mkv
             (same title + year, different release metadata)

Both are scored, and a trailing language token in the filename is respected:
a file ending `.eng.srt` will never be handed to the Chinese side. Files with
no language token at all are treated as neutral and may fill either side.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import langs

SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".sub")

#: Sibling folders worth searching, in addition to the video's own folder.
SUBTITLE_DIRS = ("subs", "subtitles", "sub", "subtitle")

#: Tokens that mark the end of a movie *title* — release metadata starts here.
RELEASE_TOKENS = frozenset({
    "bluray", "blu", "bdrip", "brrip", "bdremux", "remux", "webrip", "webdl",
    "web", "hdtv", "dvdrip", "hdrip", "uhd", "hddvd", "x264", "x265", "h264",
    "h265", "hevc", "avc", "av1", "xvid", "divx", "10bit", "8bit", "hdr",
    "dv", "sdr", "aac", "ac3", "eac3", "dts", "ddp", "dd", "atmos", "truehd",
    "flac", "opus", "2audio", "2ch", "5", "7", "dual", "yts", "yify", "mx",
    "rarbg", "fgt", "evo", "proper", "repack", "extended", "unrated",
    "remastered", "imax", "internal", "limited",
})


@dataclass
class Candidate:
    path: Path
    score: int
    language: str | None      # canonical code from the filename, if any
    neutral: bool             # no language token at all


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[.\-_ ()\[\]]+", name.lower()) if t]


def _is_resolution(t: str) -> bool:
    return bool(re.fullmatch(r"\d{3,4}[pi]", t))


def _is_year(t: str) -> bool:
    return bool(re.fullmatch(r"(19|20)\d{2}", t))


def movie_signature(name: str) -> tuple[str, str | None]:
    """(title, year): the leading tokens before any release/language/year token."""
    title: list[str] = []
    year: str | None = None
    for index, t in enumerate(_tokens(name)):
        if _is_year(t):
            year = t
            break
        # Never end the title on its own first token: a title that starts
        # with something language-shaped ("Ne Zha", "La La Land") would
        # otherwise reduce to nothing.
        if index and (t in RELEASE_TOKENS or _is_resolution(t)
                      or t in langs.FILENAME_TOKENS):
            break
        title.append(t)
    if year is None:
        for t in _tokens(name):
            if _is_year(t):
                year = t
                break
    return " ".join(title), year


def trailing_language(name: str) -> str | None:
    """The last language-looking token in a filename, e.g. 'Movie.2019.chs'."""
    for t in reversed(_tokens(name)):
        if t in langs.QUALIFIERS:
            continue
        if t in langs.FILENAME_TOKENS:
            return langs.normalize(t)
        if _is_year(t) or _is_resolution(t) or t in RELEASE_TOKENS:
            continue
        break          # a non-language trailing token: stop looking
    return None


def search_dirs(video: Path) -> list[Path]:
    """The video's own folder, plus any `Subs/`-style sibling.

    Matched case-insensitively: NTFS does not care about case but ext4 does,
    and release folders are inconsistent about it ("Subs", "subs", "SUBS").
    """
    folder = video.parent
    out = [folder]
    wanted = set(SUBTITLE_DIRS) | {video.stem.lower()}
    try:
        for entry in sorted(folder.iterdir()):
            if entry.is_dir() and entry.name.lower() in wanted:
                out.append(entry)
    except OSError:
        pass
    return out


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def find_candidates(video: Path, exclude: set[Path] | None = None) -> list[Candidate]:
    """All subtitle files that plausibly belong to `video`, best first."""
    exclude_n = {os.path.normcase(os.path.abspath(p)) for p in (exclude or set())}
    video_name = video.stem.lower()
    v_title, v_year = movie_signature(video.stem)

    out: list[Candidate] = []
    seen: set[str] = set()
    for folder in search_dirs(video):
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            continue
        for fn in entries:
            if not fn.lower().endswith(SUBTITLE_EXTS):
                continue
            path = (folder / fn).resolve()
            key = os.path.normcase(str(path))
            if key in exclude_n or key in seen:
                continue
            seen.add(key)

            base = os.path.splitext(fn)[0].lower()
            score = _score(video_name, base, v_title, v_year)
            if score is None:
                continue
            lang = trailing_language(base)
            out.append(Candidate(path=path, score=score, language=lang,
                                 neutral=lang is None))

    out.sort(key=lambda c: c.score, reverse=True)
    return out


def _score(video_name: str, sub_base: str,
           v_title: str, v_year: str | None) -> int | None:
    if video_name == sub_base:
        return 1000
    if video_name.startswith(sub_base):
        return 700 + len(sub_base)                       # episodic prefix match
    if sub_base.startswith(video_name):
        return 690 + len(video_name)                     # video name + .chs suffix
    s_title, s_year = movie_signature(sub_base)
    if v_title and s_title and v_title == s_title and (
        not v_year or not s_year or v_year == s_year
    ):
        return 600 + (50 if v_year and v_year == s_year else 0)
    cp = _common_prefix_len(video_name, sub_base)
    if cp >= max(5, int(0.6 * min(len(video_name), len(sub_base) or 1))):
        return 100 + cp
    return None


def pick(video: Path, language: str, exclude: set[Path] | None = None
         ) -> Candidate | None:
    """Best subtitle file for one language side, or None."""
    cands = find_candidates(video, exclude)
    if not cands:
        return None
    wanted = langs.normalize(language)
    exact = [c for c in cands if c.language == wanted]
    if exact:
        return exact[0]
    neutral = [c for c in cands if c.neutral]
    if neutral:
        return neutral[0]
    return None    # everything left is explicitly a *different* language
