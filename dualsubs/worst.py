"""The worst moment in the queue, for the window's appearance preview.

"Worst" is the tallest stack on screen: the most rows of top and bottom text
visible at once, with the wider text winning a tie. That is the moment that
decides whether the two lines collide, how high the top line climbs, and
whether anything runs off the edge -- so it is what the preview shows,
instead of a made-up sample.

Getting there needs the text, and the text is the expensive part: an
embedded track can only be read by reading the whole video. mkvextract
writes every track it is asked for during that one read, though, so each
file is read once, for all its text tracks, and the result is kept. Choosing
other languages or another font only re-runs the arithmetic.
"""

from __future__ import annotations

import bisect
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import render
from .formats import Event, load
from .probe import MediaInfo, ProbeError, extract_texts, make_workdir
from .tools import Toolset

__all__ = ["Cue", "FileText", "Moment", "compact", "file_stamp",
           "read_file_text", "read_subtitle_file", "worst_moment", "worst_across"]

#: (start cs, end cs, text). Tags are stripped, \N breaks kept. A plain
#: tuple, since a season's worth of text is held in memory at once.
Cue = tuple[int, int, str]


def compact(events: list[Event]) -> list[Cue]:
    """Keep what the preview needs. Positioned signs are dropped: they sit
    wherever they were placed, not in the stack at the bottom."""
    out = []
    for e in events:
        if e.has_positional_override:
            continue
        text = render._TAGS_RE.sub("", e.text).strip()
        if text:
            out.append((e.start, e.end, text))
    out.sort()
    return out


def file_stamp(path: Path) -> tuple[int, int] | None:
    """Size and modification time: enough to notice the file has changed."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, int(st.st_mtime)


@dataclass
class FileText:
    """Every text subtitle track of one video, read in a single pass."""

    stamp: tuple[int, int] | None
    tracks: dict[int, list[Cue]] = field(default_factory=dict)
    problem: str = ""


def read_file_text(media: MediaInfo, tools: Toolset,
                   should_stop=None) -> FileText | None:
    """Read all of `media`'s text tracks at once. None if stopped part way."""
    stamp = file_stamp(media.path)
    workdir = make_workdir()
    try:
        texts = extract_texts(media, media.tracks, tools, workdir, should_stop)
    except (ProbeError, OSError) as exc:
        return FileText(stamp, problem=str(exc))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if texts is None:
        return None
    return FileText(stamp, {tid: compact(events) for tid, events in texts.items()})


def read_subtitle_file(path: Path) -> list[Cue]:
    events, _ = load(path)
    return compact(events)


@dataclass
class Moment:
    """One instant on screen: the top and bottom text showing together."""

    start: int
    top: str
    bottom: str
    top_rows: int
    bottom_rows: int
    width: float
    video: Path | None = None

    @property
    def rows(self) -> int:
        return self.top_rows + self.bottom_rows

    def key(self) -> tuple:
        return self.rows, self.width

    def when(self) -> str:
        seconds = self.start // 100
        return f"{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def worst_moment(top: list[Cue], bottom: list[Cue], top_style: render.SideStyle,
                 bottom_style: render.SideStyle, layout: render.Layout,
                 _cache: dict | None = None) -> Moment | None:
    """The tallest stack of rows that is ever on screen at once."""
    cache = {} if _cache is None else _cache
    avail = layout.play_res_x - render.SIDE_MARGINS

    def measure(text: str, style: render.SideStyle, side: int) -> tuple[int, float]:
        key = (side, text)
        hit = cache.get(key)
        if hit is None:
            width = render.text_width(text, style)
            widest = (style.size * style.scale_x / 100 + max(0, style.spacing)) * len(text)
            if "\\N" not in text and widest <= avail:
                rows = 1                       # short enough that it cannot wrap
            else:
                rows = len(render.wrap_rows(text, style, layout))
            hit = cache[key] = (rows, width)
        return hit

    best: Moment | None = None

    def consider(moment: Moment) -> None:
        nonlocal best
        if best is None or moment.key() > best.key():
            best = moment

    starts = [s for s, _, _ in bottom]
    longest = max((e - s for s, e, _ in bottom), default=0)
    paired: set[int] = set()
    for t_start, t_end, t_text in top:
        t_rows, t_width = measure(t_text, top_style, 0)
        lo = bisect.bisect_left(starts, t_start - longest)
        hi = bisect.bisect_left(starts, t_end)
        alone = True
        for j in range(lo, hi):
            b_start, b_end, b_text = bottom[j]
            if b_end <= t_start:
                continue
            alone = False
            paired.add(j)
            b_rows, b_width = measure(b_text, bottom_style, 1)
            consider(Moment(max(t_start, b_start), t_text, b_text, t_rows,
                            b_rows, t_width + b_width))
        if alone:
            consider(Moment(t_start, t_text, "", t_rows, 0, t_width))
    for j, (b_start, _, b_text) in enumerate(bottom):
        if j not in paired:
            b_rows, b_width = measure(b_text, bottom_style, 1)
            consider(Moment(b_start, "", b_text, 0, b_rows, b_width))
    return best


def worst_across(sources, top_style: render.SideStyle,
                 bottom_style: render.SideStyle, layout: render.Layout,
                 should_stop=None) -> Moment | None:
    """The worst moment over many files.

    `sources` yields (video, top cues, bottom cues). Rows are cached by text
    across files, since a season repeats its opening and closing lines.
    """
    cache: dict = {}
    best: Moment | None = None
    for video, top, bottom in sources:
        if should_stop is not None and should_stop():
            return None
        moment = worst_moment(top, bottom, top_style, bottom_style, layout, cache)
        if moment is not None and (best is None or moment.key() > best.key()):
            moment.video = video
            best = moment
    return best
