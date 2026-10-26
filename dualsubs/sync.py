"""Align an external subtitle file to the video's own timing.

We never look at the audio. Instead we take the cue *onsets* of an embedded
subtitle track in the same language (any codec — PGS works fine, we only want
its timestamps) and cross-correlate them against the external file's onsets.

Two failure modes are handled:
  * a constant offset (different release, intro trimmed, etc.)
  * a framerate mismatch (23.976 vs 25 fps PAL speed-up), which shows up as a
    linear scale rather than a shift.

If neither fits confidently we change nothing, which is the right call: a
wrongly-shifted subtitle is worse than an unshifted one.
"""

from __future__ import annotations

import bisect
import statistics
from collections import Counter
from dataclasses import dataclass

from .formats import Event

TOLERANCE_MS = 250      # how close two onsets must be to count as aligned
MAX_OFFSET_MS = 180_000  # cap on a plausible shift (3 minutes)
BIN_MS = 100            # histogram bin for the coarse vote
MIN_CONFIDENCE = 0.55   # fraction of external cues that must land on a reference cue
MIN_SHIFT_MS = 30       # below this, call it already in sync
MIN_CUES = 3

#: Framerate ratios worth testing. 1.0 first so it wins ties.
RATE_CANDIDATES: tuple[float, ...] = (
    1.0,
    25.0 / 24.0, 24.0 / 25.0,
    25.0 / (24000 / 1001), (24000 / 1001) / 25.0,
    24.0 / (24000 / 1001), (24000 / 1001) / 24.0,
    30.0 / (30000 / 1001), (30000 / 1001) / 30.0,
)


@dataclass
class SyncResult:
    scale: float = 1.0
    delta_ms: int = 0
    confidence: float = 0.0
    matched: int = 0
    total: int = 0
    applied: bool = False
    reason: str = ""

    @property
    def is_significant(self) -> bool:
        return abs(self.delta_ms) >= MIN_SHIFT_MS or abs(self.scale - 1.0) > 1e-6

    def describe(self) -> str:
        pct = round(self.confidence * 100)
        rate = "" if abs(self.scale - 1.0) < 1e-9 else f", rate x{self.scale:.5f}"
        return f"{self.delta_ms:+d}ms{rate} ({pct}% of {self.total} cues aligned)"


def _score(reference: list[float], targets: list[float], scale: float
           ) -> tuple[int, float, int]:
    """Best (delta_ms, confidence, matched) for one candidate rate ratio."""
    scaled = [t * scale for t in targets]
    votes: Counter[int] = Counter()
    for x in scaled:
        lo = bisect.bisect_left(reference, x - MAX_OFFSET_MS)
        hi = bisect.bisect_right(reference, x + MAX_OFFSET_MS)
        for e in reference[lo:hi]:
            votes[int(round((e - x) / BIN_MS))] += 1
    if not votes:
        return 0, 0.0, 0

    center = votes.most_common(1)[0][0] * BIN_MS
    diffs: list[float] = []
    for x in scaled:
        lo = bisect.bisect_left(reference, x + center - TOLERANCE_MS)
        hi = bisect.bisect_right(reference, x + center + TOLERANCE_MS)
        diffs += [e - x for e in reference[lo:hi]
                  if abs((e - x) - center) <= TOLERANCE_MS]
    delta = int(statistics.median(diffs)) if diffs else center

    matched = 0
    for x in scaled:
        xt = x + delta
        i = bisect.bisect_left(reference, xt)
        near = min(
            [abs(reference[k] - xt) for k in (i - 1, i) if 0 <= k < len(reference)]
            or [TOLERANCE_MS + 1]
        )
        if near <= TOLERANCE_MS:
            matched += 1
    return delta, matched / len(scaled), matched


def estimate(reference_ms: list[float], target_ms: list[float],
             allow_rate_fix: bool = True) -> SyncResult:
    """Work out how to move `target_ms` onto `reference_ms`."""
    reference = sorted(reference_ms)
    targets = sorted(target_ms)
    if len(reference) < MIN_CUES or len(targets) < MIN_CUES:
        return SyncResult(
            total=len(targets),
            reason=f"not enough cues (reference={len(reference)}, target={len(targets)})",
        )

    rates = RATE_CANDIDATES if allow_rate_fix else (1.0,)
    best = SyncResult(total=len(targets), reason="no plausible offset")
    for scale in rates:
        delta, conf, matched = _score(reference, targets, scale)
        # Require a clear win before accepting a rate change over a plain shift.
        threshold = best.confidence + (0.0 if scale == 1.0 else 0.05)
        if conf > threshold:
            best = SyncResult(scale=scale, delta_ms=delta, confidence=conf,
                              matched=matched, total=len(targets))

    if best.confidence < MIN_CONFIDENCE:
        best.reason = (
            f"best fit {best.delta_ms:+d}ms aligns only "
            f"{round(best.confidence * 100)}% of cues - low confidence"
        )
        return best
    if not best.is_significant:
        best.reason = f"already in sync ({best.describe()})"
        return best

    best.applied = True
    best.reason = f"shifted by {best.describe()}"
    return best


def apply(events: list[Event], result: SyncResult) -> list[Event]:
    """Return a retimed copy of `events`. Times are centiseconds."""
    if not result.applied:
        return events
    delta_cs = result.delta_ms / 10.0
    out: list[Event] = []
    for ev in events:
        start = max(0, int(round(ev.start * result.scale + delta_cs)))
        end = max(start, int(round(ev.end * result.scale + delta_cs)))
        out.append(type(ev)(**{**ev.__dict__, "start": start, "end": end}))
    return out


def onsets_ms(events: list[Event]) -> list[float]:
    return [ev.start * 10.0 for ev in events]
