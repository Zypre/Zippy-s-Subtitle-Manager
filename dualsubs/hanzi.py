"""Traditional -> Simplified Chinese conversion (繁体转简体).

Subtitle files from Taiwan and Hong Kong sources are written in traditional
characters (繁體字, fántǐzì — 繁 fán "complex", 體 tǐ "form/body", 字 zì
"character"). If you are learning from mainland material you want simplified
(简体字, jiǎntǐzì — 简 jiǎn "simple"). This converts between them.

Only T -> S is supported, and deliberately so: that direction is essentially
many-to-one (發 fā "emit" and 髮 fà "hair" both collapse to 发) so a lookup
table gets it right. The reverse is one-to-many and needs a ~1 MB phrase
dictionary plus real disambiguation to avoid writing 头發 for 头发.

Two stages, mirroring OpenCC's `tw2s` chain:
  1. regional variants  (裏 -> 裡, 着 -> 著 ...)
  2. traditional -> simplified proper

Phrases are applied before single characters, which is what keeps
一目瞭然 -> 一目了然 instead of the wrong 瞭 -> 了 everywhere.

If the `opencc` package happens to be installed it is used instead, since its
segmentation is better. Nothing depends on it.
"""

from __future__ import annotations

import re

from ._hanzi_data import (
    SIMPLIFIED_ONLY,
    TRADITIONAL_ONLY,
    TS_FROM,
    TS_PHRASES,
    TS_TO,
    VARIANT_FROM,
    VARIANT_PHRASES,
    VARIANT_TO,
)

__all__ = ["to_simplified", "detect_script", "convert_events", "available_backends"]

_VARIANT_TABLE = str.maketrans(VARIANT_FROM, VARIANT_TO)
_TS_TABLE = str.maketrans(TS_FROM, TS_TO)

_TRAD_SET = frozenset(TRADITIONAL_ONLY)
_SIMP_SET = frozenset(SIMPLIFIED_ONLY)

#: Longest-first alternation so 一目瞭然 beats 瞭然.
def _phrase_pattern(phrases: dict[str, str]) -> re.Pattern | None:
    if not phrases:
        return None
    keys = sorted(phrases, key=len, reverse=True)
    return re.compile("|".join(re.escape(k) for k in keys))


_VARIANT_RE = _phrase_pattern(VARIANT_PHRASES)
_TS_RE = _phrase_pattern(TS_PHRASES)

_opencc_converter = None
_opencc_tried = False


def _opencc():
    """The optional better backend, loaded once."""
    global _opencc_converter, _opencc_tried
    if not _opencc_tried:
        _opencc_tried = True
        try:
            import opencc  # type: ignore
            _opencc_converter = opencc.OpenCC("tw2s")
        except Exception:
            _opencc_converter = None
    return _opencc_converter


def available_backends() -> str:
    return "opencc" if _opencc() else "built-in table"


#: ASS override blocks: {\fn微軟正黑體\fs40}. A font name inside one has to
#: stay exactly as written, or the player cannot find the font.
_OVERRIDE_RE = re.compile(r"\{[^}]*\}")


def _apply(text: str, pattern: re.Pattern | None,
           phrases: dict[str, str], table: dict) -> str:
    """Phrases first, then single characters -- but only *between* phrases.

    A matched phrase is final. Running the character table over it as well
    would undo the protections OpenCC writes as identity entries: 乾坤 → 乾坤
    exists precisely so that 乾 (qián, heaven) is not turned into 干 (gān,
    dry), and 著名 → 著名 so that 著 (zhù) is not read as the particle 着.
    """
    if pattern is None:
        return text.translate(table)
    out: list[str] = []
    pos = 0
    for m in pattern.finditer(text):
        out.append(text[pos:m.start()].translate(table))
        out.append(phrases[m.group(0)])
        pos = m.end()
    out.append(text[pos:].translate(table))
    return "".join(out)


def _convert_plain(text: str, use_opencc: bool) -> str:
    if use_opencc:
        conv = _opencc()
        if conv is not None:
            return conv.convert(text)
    text = _apply(text, _VARIANT_RE, VARIANT_PHRASES, _VARIANT_TABLE)
    return _apply(text, _TS_RE, TS_PHRASES, _TS_TABLE)


def to_simplified(text: str, use_opencc: bool = True) -> str:
    """繁體 -> 简体. Non-Chinese text, and ASS override tags, pass through."""
    if not text:
        return text
    out: list[str] = []
    pos = 0
    for m in _OVERRIDE_RE.finditer(text):
        out.append(_convert_plain(text[pos:m.start()], use_opencc))
        out.append(m.group(0))
        pos = m.end()
    out.append(_convert_plain(text[pos:], use_opencc))
    return "".join(out)


def detect_script(text: str, sample: int = 20000) -> tuple[str, float]:
    """Return ('traditional' | 'simplified' | 'unknown', confidence 0-1).

    Counts only characters that exist on exactly one side. Text with too few
    of those — English subtitles, or Chinese written entirely in shared
    characters — comes back 'unknown' rather than being guessed at.
    """
    trad = simp = 0
    for ch in text[:sample]:
        if ch in _TRAD_SET:
            trad += 1
        elif ch in _SIMP_SET:
            simp += 1
    total = trad + simp
    if total < 5:
        return "unknown", 0.0
    if trad >= simp:
        return "traditional", trad / total
    return "simplified", simp / total


# --------------------------------------------------------------------------


def convert_events(events, mode: str) -> tuple[list, str]:
    """Convert a list of Events in place-ish. Returns (events, note).

    `mode` is 't2s' to always convert, or 'auto' to convert only when the text
    actually looks traditional.
    """
    from dataclasses import replace

    mode = (mode or "").strip().lower()
    if mode not in ("t2s", "auto"):
        return events, ""
    if not events:
        return events, ""

    joined = "\n".join(e.text for e in events)
    script, confidence = detect_script(joined)

    if mode == "auto":
        if script == "unknown":
            return events, ""          # not Chinese; nothing to report
        if script != "traditional":
            return events, "already simplified"
        if confidence < 0.6:
            return events, f"ambiguous script ({round(confidence * 100)}% traditional); left alone"

    out = [replace(e, text=to_simplified(e.text)) for e in events]
    changed = sum(1 for a, b in zip(events, out) if a.text != b.text)
    if not changed:
        return events, "already simplified"
    return out, (f"converted {changed}/{len(events)} cues to simplified "
                 f"via {available_backends()}")
