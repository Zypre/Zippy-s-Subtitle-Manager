"""Read SRT / ASS / SSA / WebVTT / MicroDVD into a common event model.

Everything is normalised to centiseconds, which is ASS's native resolution.
The format is sniffed from the *content*, never the extension: mkvextract
writes a track in its native format regardless of the filename we hand it, so
a file called `.ass` can easily contain SubRip data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

__all__ = ["Event", "load_events", "load", "parse_events", "sniff_format"]


@dataclass
class Event:
    """One subtitle cue, in ASS terms. Times are centiseconds."""

    start: int
    end: int
    text: str
    layer: int = 0
    name: str = ""
    margin_l: str = "0"
    margin_r: str = "0"
    margin_v: str = "0"
    effect: str = ""

    def shifted(self, delta_cs: int) -> "Event":
        return replace(
            self,
            start=max(0, self.start + delta_cs),
            end=max(0, self.end + delta_cs),
        )

    def to_dialogue(self, style: str) -> str:
        return (
            f"Dialogue: {self.layer},{cs_to_ass(self.start)},{cs_to_ass(self.end)},"
            f"{style},{self.name},{self.margin_l},{self.margin_r},{self.margin_v},"
            f"{self.effect},{self.text}"
        )

    @property
    def has_positional_override(self) -> bool:
        """True if the cue already places itself (\\an8 signs, \\pos, \\move)."""
        m = re.match(r"\{[^}]*\}", self.text)
        return bool(m and re.search(r"\\(an\d|a\d+|pos|move|org)", m.group(0)))

    @property
    def line_count(self) -> int:
        core = re.sub(r"^(\{[^}]*\})+", "", self.text)
        return core.count(r"\N") + 1


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------

def cs_to_ass(cs: int) -> str:
    cs = max(0, int(cs))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def cs_to_srt(cs: int) -> str:
    ms = max(0, int(cs)) * 10
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


_TIME_RE = re.compile(r"\s*(?:(\d+):)?(\d+):(\d+)[.,](\d+)")


def parse_time(t: str) -> int | None:
    """Parse `[hh:]mm:ss[.,]mmm` (or `.cc`) to centiseconds. None if unparseable."""
    m = _TIME_RE.match(t.strip())
    if not m:
        return None
    h = int(m.group(1) or 0)
    mi, s, frac = int(m.group(2)), int(m.group(3)), m.group(4)
    if len(frac) >= 3:            # milliseconds (SRT / VTT)
        sub = int(frac[:3]) // 10
    elif len(frac) == 2:          # centiseconds (ASS)
        sub = int(frac)
    else:
        sub = int(frac) * 10
    return h * 360000 + mi * 6000 + s * 100 + sub


# --------------------------------------------------------------------------
# per-format readers
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def _clean_inline(text: str) -> str:
    """Strip HTML-ish markup that SRT/VTT use and ASS does not understand."""
    return _TAG_RE.sub("", text)


def _srt_events(text: str) -> list[Event]:
    events: list[Event] = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 2:
            continue
        # Cue numbers are optional in the wild; find the timing line.
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        try:
            raw_start, raw_end = lines[idx].split("-->")[:2]
            start = parse_time(raw_start)
            end = parse_time(raw_end.split()[0])
        except (ValueError, IndexError):
            continue
        if start is None or end is None:
            continue
        body = _clean_inline(r"\N".join(lines[idx + 1:])).strip()
        if not body:
            continue
        events.append(Event(start=start, end=end, text=body))
    return events


def _vtt_events(text: str) -> list[Event]:
    text = re.sub(r"^WEBVTT.*?$", "", text, count=1, flags=re.MULTILINE)
    text = re.sub(r"^NOTE\b.*?(?:\r?\n\r?\n|\Z)", "", text, flags=re.DOTALL | re.MULTILINE)
    text = re.sub(r"^(?:STYLE|REGION)\b.*?(?:\r?\n\r?\n|\Z)", "", text,
                  flags=re.DOTALL | re.MULTILINE)
    return _srt_events(text)


def _ass_events(text: str) -> list[Event]:
    events: list[Event] = []
    fmt: list[str] | None = None
    in_events = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower() == "[events]":
            in_events = True
            continue
        if in_events and stripped.startswith("["):
            break
        if not in_events:
            continue
        if stripped.lower().startswith("format:"):
            fmt = [f.strip().lower() for f in stripped.split(":", 1)[1].split(",")]
            continue
        if not stripped.lower().startswith("dialogue:"):
            continue
        payload = stripped.split(":", 1)[1]
        n = (len(fmt) if fmt else 10)
        parts = [p.strip() for p in payload.split(",", n - 1)]
        if len(parts) < 10:
            continue
        keys = fmt or ["layer", "start", "end", "style", "name",
                       "marginl", "marginr", "marginv", "effect", "text"]
        row = dict(zip(keys, parts))
        start, end = parse_time(row.get("start", "")), parse_time(row.get("end", ""))
        if start is None or end is None:
            continue
        try:
            layer = int(row.get("layer", "0") or 0)
        except ValueError:
            layer = 0
        events.append(Event(
            start=start, end=end, text=row.get("text", ""), layer=layer,
            name=row.get("name", ""), margin_l=row.get("marginl", "0") or "0",
            margin_r=row.get("marginr", "0") or "0",
            margin_v=row.get("marginv", "0") or "0",
            effect=row.get("effect", ""),
        ))
    return events


_MICRODVD_RE = re.compile(r"^\s*\{(\d+)\}\{(\d+)\}(.*)$")


def _microdvd_events(text: str, fps: float = 23.976) -> list[Event]:
    events: list[Event] = []
    lines = text.splitlines()
    # A leading {1}{1}25.000 line declares the real frame rate.
    if lines:
        first = _MICRODVD_RE.match(lines[0])
        if first:
            try:
                declared = float(first.group(3).strip())
                if 10.0 < declared < 130.0:
                    fps = declared
                    lines = lines[1:]
            except ValueError:
                pass
    for line in lines:
        m = _MICRODVD_RE.match(line)
        if not m:
            continue
        start = int(round(int(m.group(1)) / fps * 100))
        end = int(round(int(m.group(2)) / fps * 100))
        body = m.group(3)
        body = re.sub(r"^\{[yYfFsScCpP]:[^}]*\}", "", body)  # drop style codes
        body = body.replace("|", r"\N").strip()
        if not body:
            continue
        events.append(Event(start=start, end=end, text=body))
    return events


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

def sniff_format(text: str, path_hint: str = "") -> str:
    """Return one of 'vtt', 'ass', 'microdvd', 'srt'."""
    low = text.lstrip("\ufeff \t\r\n").lower()
    if low.startswith("webvtt"):
        return "vtt"
    if "[script info]" in low or "[events]" in low or "[v4+ styles]" in low:
        return "ass"
    if _MICRODVD_RE.search(text.split("\n", 200)[0] if text else ""):
        return "microdvd"
    if re.search(r"^\s*\{\d+\}\{\d+\}", text, re.MULTILINE):
        return "microdvd"
    if "-->" in text:
        return "srt"
    ext = path_hint.lower().rsplit(".", 1)[-1] if "." in path_hint else ""
    return {"ass": "ass", "ssa": "ass", "vtt": "vtt", "sub": "microdvd"}.get(ext, "srt")


def parse_events(text: str, path_hint: str = "", fps: float = 23.976) -> list[Event]:
    kind = sniff_format(text, path_hint)
    if kind == "vtt":
        return _vtt_events(text)
    if kind == "ass":
        return _ass_events(text)
    if kind == "microdvd":
        return _microdvd_events(text, fps)
    return _srt_events(text)


def load(path, language: str | None = None,
         fps: float = 23.976) -> tuple[list[Event], str]:
    """Read a subtitle file whatever its encoding. Returns (events, encoding).

    Legacy encodings are the norm for Chinese subtitles -- GB18030 from the
    mainland, Big5 from Taiwan and Hong Kong -- and neither announces itself.
    See textio for how the guess is made.
    """
    from .textio import read_text
    text, encoding = read_text(path, language)
    return parse_events(text, str(path), fps), encoding


def load_events(path, fps: float = 23.976, language: str | None = None) -> list[Event]:
    return load(path, language, fps)[0]


def write_srt(events: list[Event], path) -> None:
    """Only used by --extract; the muxing path never round-trips through SRT."""
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as fh:
        for i, ev in enumerate(events, 1):
            body = re.sub(r"\{[^}]*\}", "", ev.text).replace(r"\N", "\n")
            fh.write(f"{i}\n{cs_to_srt(ev.start)} --> {cs_to_srt(ev.end)}\n{body}\n\n")
