"""Make the two lines appear and disappear together.

`sync` fixes the *file*: one constant offset (plus an optional framerate
rescale) that moves an external subtitle onto the video's timing. This module
fixes the *pair*: two subtitlers working from the same dialogue rarely agree to
the centisecond, so even perfectly synced tracks show

    bottom  0:02:23.35 -> 0:02:25.97   给我张支票，什么意思，让我踢假球啊？
    top     0:02:23.65 -> 0:02:26.27   What is the meaning of this check?

and the top line arrives 300 ms late, which reads as a flicker. Neither track
is wrong; they were just authored independently. Note this happens with two
*embedded* tracks too, where `sync` never runs.

So: pair up cues that clearly cover the same line, and where the disagreement
is small, give the pair one start and one end. Cues with no counterpart, or
whose counterpart is far away, are left exactly as they were — a real timing
difference is information, not noise.
"""

from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass, replace

from .formats import Event

__all__ = ["snap", "grid_snap", "SnapResult", "GridResult", "OffsetEstimate"]

#: Never pull a cue past its own neighbours; leave this much of a gap.
MIN_GAP_CS = 1

#: A pair counts towards the relative-offset vote if it lands this close to
#: the median. Wider than authoring jitter, tighter than a real difference.
OFFSET_TOLERANCE_CS = 15

#: Fraction of matched pairs that must agree before a whole side is moved.
OFFSET_CONFIDENCE = 0.6

MIN_OFFSET_PAIRS = 8


@dataclass
class OffsetEstimate:
    """A constant timing difference between the two tracks as a whole."""

    delta_cs: int = 0
    confidence: float = 0.0
    pairs: int = 0
    applied: bool = False
    reason: str = ""

    def describe(self) -> str:
        ms = self.delta_cs * 10
        direction = "behind" if ms > 0 else "ahead of"
        return (f"ran {abs(ms)}ms {direction} the other "
                f"({round(self.confidence * 100)}% of {self.pairs} pairs agree)")


class SnapResult:
    def __init__(self, top: list[Event], bottom: list[Event],
                 starts: int = 0, ends: int = 0, pairs: int = 0,
                 offset: OffsetEstimate | None = None, moved: str = ""):
        self.top = top
        self.bottom = bottom
        self.starts = starts
        self.ends = ends
        self.pairs = pairs
        self.offset = offset or OffsetEstimate()
        self.moved = moved

    @property
    def changed(self) -> int:
        return self.starts + self.ends

    def describe(self) -> str:
        parts = []
        if self.offset.applied:
            parts.append(f"{self.moved} line {self.offset.describe()}; "
                         f"shifted that whole side")
        if not self.pairs:
            parts.append("no overlapping cue pairs to align")
        elif not self.changed:
            parts.append(f"{self.pairs} cue pairs already aligned")
        else:
            parts.append(f"aligned {self.starts} starts and {self.ends} ends "
                         f"across {self.pairs} cue pairs")
        return "; ".join(parts)


def _overlap(a: Event, b: Event) -> int:
    return min(a.end, b.end) - max(a.start, b.start)


def _match(top: list[Event], bottom: list[Event]) -> list[tuple[int, int]]:
    """One-to-one pairing of cues by greatest temporal overlap.

    Greedy on overlap size, which is enough here: the two tracks run in
    lockstep, so the ambiguous cases are only ever where one side splits a
    line the other keeps whole.
    """
    candidates: list[tuple[int, int, int]] = []
    j0 = 0
    for i, t in enumerate(top):
        while j0 < len(bottom) and bottom[j0].end <= t.start:
            j0 += 1
        j = j0
        while j < len(bottom) and bottom[j].start < t.end:
            ov = _overlap(t, bottom[j])
            if ov > 0:
                candidates.append((ov, i, j))
            j += 1

    candidates.sort(key=lambda c: c[0], reverse=True)
    used_t: set[int] = set()
    used_b: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for _, i, j in candidates:
        if i in used_t or j in used_b:
            continue
        used_t.add(i)
        used_b.add(j)
        pairs.append((i, j))
    return sorted(pairs)


def estimate_offset(top: list[Event], bottom: list[Event],
                    pairs: list[tuple[int, int]],
                    max_cs: int) -> OffsetEstimate:
    """How far the bottom track as a whole sits behind the top track.

    Positive means the bottom line arrives later. Uses the median of matched
    cue starts, so a handful of differently-split lines cannot drag it.
    """
    if len(pairs) < MIN_OFFSET_PAIRS:
        return OffsetEstimate(pairs=len(pairs), reason="too few matched pairs")
    diffs = [bottom[j].start - top[i].start for i, j in pairs]
    median = int(round(statistics.median(diffs)))
    agree = sum(1 for d in diffs if abs(d - median) <= OFFSET_TOLERANCE_CS)
    confidence = agree / len(diffs)

    est = OffsetEstimate(delta_cs=median, confidence=confidence, pairs=len(diffs))
    if abs(median) > max_cs:
        est.reason = ("the two tracks differ by more than the snap window; "
                      "that is a sync problem, not authoring jitter")
    elif abs(median) < 2:
        est.reason = "tracks already share a timeline"
    elif confidence < OFFSET_CONFIDENCE:
        est.reason = (f"the difference is not consistent "
                      f"({round(confidence * 100)}% agree); left alone")
    else:
        est.applied = True
    return est


def _shift(events: list[Event], delta_cs: int) -> list[Event]:
    """A uniform move. Cannot create overlaps, since spacing is preserved."""
    return [replace(e, start=max(0, e.start + delta_cs),
                    end=max(0, e.end + delta_cs)) for e in events]


def snap(top_events: list[Event], bottom_events: list[Event],
         window_cs: int = 50, anchor: str = "top") -> SnapResult:
    """Give closely-matched cue pairs a common start and end.

    `window_cs` is how far apart two edges may be and still count as the same
    intended moment. 50 cs (half a second) is comfortably more than authoring
    jitter and comfortably less than a real timing difference.
    """
    if not top_events or not bottom_events or window_cs <= 0:
        return SnapResult(top_events, bottom_events)

    top = sorted(top_events, key=lambda e: (e.start, e.end))
    bot = sorted(bottom_events, key=lambda e: (e.start, e.end))
    pairs = _match(top, bot)
    if not pairs:
        return SnapResult(top, bot)

    # Stage 1: if one track sits a constant distance behind the other, move
    # that whole side onto the other. Per-cue snapping cannot do this job --
    # pulling individual cues across a busy stretch would overlap their own
    # neighbours, so roughly half of them get declined and the result looks
    # worse than leaving it alone.
    offset = estimate_offset(top, bot, pairs, window_cs)
    moved = ""
    if offset.applied:
        if anchor == "bottom":
            top = _shift(top, offset.delta_cs)
            moved = "top"
        else:
            bot = _shift(bot, -offset.delta_cs)
            moved = "bottom"
        pairs = _match(top, bot)

    # Stage 2: whatever jitter is left, cue by cue.
    new_top = list(top)
    new_bot = list(bot)
    starts = ends = 0

    for i, j in pairs:
        t, b = top[i], bot[j]

        # Both edges move to a common value: as early as allowed for the
        # start, as late as allowed for the end. Where a neighbour is in the
        # way we meet it at the boundary rather than giving up -- but only if
        # that costs less than the misalignment being fixed. Trimming a cue by
        # more than the snap window would be a worse artefact than the
        # flicker, so those pairs are left exactly as authored.
        start = None
        if abs(t.start - b.start) <= window_cs and t.start != b.start:
            floor = max(
                (top[i - 1].end + MIN_GAP_CS) if i else 0,
                (bot[j - 1].end + MIN_GAP_CS) if j else 0,
            )
            candidate = max(min(t.start, b.start), floor)
            trim = max(candidate - t.start, candidate - b.start)
            if trim <= window_cs and candidate < min(t.end, b.end):
                start = candidate

        end = None
        if abs(t.end - b.end) <= window_cs and t.end != b.end:
            ceiling = min(
                (top[i + 1].start - MIN_GAP_CS) if i + 1 < len(top) else 10**9,
                (bot[j + 1].start - MIN_GAP_CS) if j + 1 < len(bot) else 10**9,
            )
            candidate = min(max(t.end, b.end), ceiling)
            trim = max(t.end - candidate, b.end - candidate)
            if trim <= window_cs and candidate > max(t.start, b.start):
                end = candidate

        if start is None and end is None:
            continue
        if start is not None:
            starts += 1
        if end is not None:
            ends += 1
        new_top[i] = replace(t, start=start if start is not None else t.start,
                             end=end if end is not None else t.end)
        new_bot[j] = replace(b, start=start if start is not None else b.start,
                             end=end if end is not None else b.end)

    return SnapResult(new_top, new_bot, starts, ends, len(pairs), offset, moved)


# --------------------------------------------------------------------------
# Grid alignment: for tracks that split dialogue differently
# --------------------------------------------------------------------------

def _nearest(grid: list[int], value: int, window_cs: int) -> int | None:
    """Closest grid point within the window, or None."""
    i = bisect.bisect_left(grid, value)
    best = None
    for k in (i - 1, i):
        if 0 <= k < len(grid) and abs(grid[k] - value) <= window_cs:
            if best is None or abs(grid[k] - value) < abs(best - value):
                best = grid[k]
    return best


class GridResult:
    def __init__(self, moving: list[Event], moved: int = 0, total: int = 0,
                 on_grid_before: int = 0, on_grid_after: int = 0):
        self.moving = moving
        self.moved = moved
        self.total = total
        self.on_grid_before = on_grid_before
        self.on_grid_after = on_grid_after

    def describe(self) -> str:
        if not self.moved:
            return "already on the same grid"
        return (f"retimed {self.moved} of {self.total} cues onto the other "
                f"track's boundaries ({self.on_grid_before} -> "
                f"{self.on_grid_after} share a start)")


def grid_snap(anchor: list[Event], moving: list[Event],
              window_cs: int = 100) -> GridResult:
    """Quantise one track's cue boundaries onto the other's.

    Pair snapping assumes the two tracks split dialogue the same way. Often
    they do not: an English dub reflows lines to fit the mouth, so three
    Chinese cues cover what the English says in two. There is no one-to-one
    pairing to snap, and the two sides drift apart by a second or more inside
    a fast exchange.

    Instead of matching cues, match *moments*. Every start on the moving side
    is pulled to the nearest start on the anchor side, every end to the
    nearest end. The moving track keeps its own segmentation — it simply stops
    changing at times the anchor does not.

    This deliberately moves one side away from its own audio, so the anchor
    should be whichever language you are actually listening to.
    """
    if not anchor or not moving or window_cs <= 0:
        return GridResult(moving, total=len(moving))

    anchor = sorted(anchor, key=lambda e: (e.start, e.end))
    moving = sorted(moving, key=lambda e: (e.start, e.end))
    starts = sorted({e.start for e in anchor})
    ends = sorted({e.end for e in anchor})
    start_set = set(starts)

    before = sum(1 for e in moving if e.start in start_set)
    out: list[Event] = []
    moved = 0
    prev_end = -1

    for i, ev in enumerate(moving):
        new_start = _nearest(starts, ev.start, window_cs)
        new_end = _nearest(ends, ev.end, window_cs)
        start = ev.start if new_start is None else new_start
        end = ev.end if new_end is None else new_end

        # Never reorder, never collide with the cue we just placed, and never
        # collapse a cue to nothing. Any of those, and the edge stays put.
        if start <= prev_end:
            start = ev.start
        if start <= prev_end:
            start = prev_end + MIN_GAP_CS
        if end <= start + MIN_GAP_CS:
            end = max(ev.end, start + MIN_GAP_CS)
        if i + 1 < len(moving) and end >= moving[i + 1].start + window_cs:
            end = ev.end                      # do not swallow the next cue

        if (start, end) != (ev.start, ev.end):
            moved += 1
        out.append(replace(ev, start=start, end=end))
        prev_end = end

    after = sum(1 for e in out if e.start in start_set)
    return GridResult(out, moved, len(moving), before, after)
