"""Reading subtitle files whatever encoding they arrived in.

Subtitles are a graveyard of legacy encodings. A Chinese .srt from a mainland
release is usually GB18030; from Taiwan or Hong Kong, Big5. Neither announces
itself, and neither is UTF-8. Opening one with `errors='replace'` does not
fail — it quietly produces 要我踢假球？ as ÒªÎÒÌß¼ÙÇò£¿ and carries on.

So: sniff first, then decode, then hand the rest of the program plain str.
Everything this tool *writes* is UTF-8, so the mess stops at the door.

The tricky pair is GB18030 versus Big5. Structurally, GBK uses trail bytes in
0x80-0xA0 that Big5 forbids, so genuine GB text usually fails a Big5 decode
outright — that half is easy. The other direction is not: Big5 text almost
always decodes *without error* as GB18030, just into the wrong characters. So
candidates that decode cleanly are scored on whether the result looks like
language rather than noise, and the best score wins.

`charset_normalizer` or `chardet` are used instead when installed, since they
do this more thoroughly. Neither is required.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["read_text", "detect_encoding", "write_utf8", "reencode_file"]

#: Byte-order marks, checked before anything else because they are decisive.
BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xfe\xff", "utf-16-be"),
    (b"\xff\xfe", "utf-16-le"),
)

#: Tried in order. Multi-byte CJK codecs come first: they are strict, so a
#: clean decode is real evidence, whereas cp1252 and latin-1 accept any byte
#: at all and must never win on a technicality.
CANDIDATES: tuple[str, ...] = (
    "gb18030",      # mainland Chinese: GB2312 / GBK / GB18030
    "big5hkscs",    # Taiwan / Hong Kong
    "cp950",
    "shift_jis",
    "euc-jp",
    "euc-kr",
    "cp1251",       # Cyrillic
    "cp1250",       # Central European
    "cp1252",       # Western European
    "latin-1",      # accepts everything; the last resort by design
)

#: Which codecs to try first for a language we already asked for. This is the
#: strongest signal available and costs nothing: if the caller wants the
#: Chinese subtitle, EUC-KR is not a serious candidate no matter how cleanly
#: the bytes happen to decode.
LANGUAGE_PRIORITY: dict[str, tuple[str, ...]] = {
    "chi": ("gb18030", "big5hkscs", "cp950"),
    "jpn": ("shift_jis", "euc-jp"),
    "kor": ("euc-kr",),
    "rus": ("cp1251",),
    "ukr": ("cp1251",),
    "gre": ("cp1253",),
    "heb": ("cp1255",),
    "tha": ("cp874",),
    "ara": ("cp1256",),
}


def _plausibility(text: str) -> float:
    """How much this looks like human text rather than a bad decode.

    Deliberately does *not* reward recognisable Chinese characters. That was
    tried and it backfired: the only hanzi this program has a list of are
    variant forms, which are comparatively exotic, so garbage decodes scored
    higher on them than correct ones did (0.54 against 0.32 on a real sample).
    What survives are signals about the shape of the text as a whole.
    """
    if not text:
        return -1.0
    sample = text[:40000]
    good = bad = 0
    cjk = hangul = kana = 0

    for ch in sample:
        o = ord(ch)
        if ch in "\r\n\t" or 0x20 <= o < 0x7F:
            good += 1                                   # ASCII
        elif 0x4E00 <= o <= 0x9FFF:
            good += 1                                   # CJK ideographs
            cjk += 1
        elif 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF:
            good += 1                                   # CJK / fullwidth punctuation
        elif 0x3040 <= o <= 0x30FF:
            good += 1                                   # kana
            kana += 1
        elif 0xAC00 <= o <= 0xD7AF:
            good += 1                                   # hangul
            hangul += 1
        elif 0x0400 <= o <= 0x04FF:
            good += 1                                   # Cyrillic
        elif 0x00A0 <= o <= 0x024F:
            bad += 1                                    # Latin-1: classic mojibake
        elif o < 0x20:
            bad += 3                                    # stray control bytes
        elif 0xE000 <= o <= 0xF8FF or o >= 0x20000:
            bad += 5                                    # private use, rare planes
        else:
            bad += 1

    total = len(sample)
    score = (good - bad) / total

    # Korean and Chinese do not interleave. A heavy Hangul/ideograph mixture
    # means one codec is reading the other's bytes -- measured at 0.47 on a
    # wrong decode and 0.00 on every correct one. Kana with ideographs is
    # ordinary Japanese and is not penalised.
    script = cjk + hangul + kana
    if script and cjk and hangul:
        score -= 2.0 * min(cjk, hangul) / script
    return score


def detect_encoding(raw: bytes, language: str | None = None) -> tuple[str, float]:
    """Return (encoding, confidence 0-1). Never raises.

    `language` is the canonical code of the subtitle we are after, if known.
    Distinguishing Korean text from the same bytes read as Chinese is close to
    impossible from bytes alone, and completely unnecessary when the caller
    already asked for one of them.
    """
    if not raw:
        return "utf-8", 1.0

    for bom, encoding in BOMS:
        if raw.startswith(bom):
            return encoding, 1.0

    try:
        raw.decode("utf-8")
        return "utf-8", 1.0
    except UnicodeDecodeError:
        pass

    preferred = LANGUAGE_PRIORITY.get(language or "", ())
    order = list(preferred) + [c for c in CANDIDATES if c not in preferred]

    scored: list[tuple[float, int, str]] = []
    for rank, encoding in enumerate(order):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        score = _plausibility(text)
        if encoding in preferred:
            score += 0.25            # the caller told us what to expect
        scored.append((score, -rank, encoding))   # ties keep the listed order

    if not scored:
        return "latin-1", 0.0            # decodes anything; loses nothing
    scored.sort(reverse=True)
    best_score, _, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else -1.0
    margin = max(0.0, min(1.0, (best_score - runner_up) * 2))
    return best, round(0.5 + 0.5 * margin, 2)


def _external_guess(raw: bytes) -> tuple[str, float] | None:  # noqa: D401
    """Defer to a real detector if the user happens to have one installed."""
    try:
        from charset_normalizer import from_bytes  # type: ignore
        best = from_bytes(raw).best()
        if best is not None and best.encoding:
            return best.encoding, 0.95
    except Exception:
        pass
    try:
        import chardet  # type: ignore
        guess = chardet.detect(raw)
        if guess and guess.get("encoding") and guess.get("confidence", 0) > 0.7:
            return guess["encoding"], float(guess["confidence"])
    except Exception:
        pass
    return None


def read_text(path: str | Path, language: str | None = None) -> tuple[str, str]:
    """Read a subtitle file as str. Returns (text, encoding actually used)."""
    raw = Path(path).read_bytes()
    encoding, _ = detect_encoding(raw, language)
    try:
        text = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        text = raw.decode(encoding, errors="replace")
    return text.lstrip("\ufeff"), encoding


def write_utf8(path: str | Path, text: str, bom: bool = False) -> None:
    """Write text out verbatim.

    newline="" matters: the default translates every "\n" to os.linesep, which
    on Windows turns an SRT's existing "\r\n" into "\r\r\n". Line endings are
    the file's business, not ours.
    """
    with open(path, "w", encoding="utf-8-sig" if bom else "utf-8",
              newline="") as fh:
        fh.write(text)


def reencode_file(path: str | Path, backup_suffix: str = ".orig",
                  language: str | None = None) -> tuple[str, bool]:
    """Rewrite a subtitle file as UTF-8 in place. Returns (was_encoding, changed).

    The original is kept alongside as `<name>.srt.orig` unless it was already
    UTF-8, in which case nothing is touched at all. That includes UTF-8 with a
    byte-order mark: it is still UTF-8, players read it fine (Windows ones
    rather prefer it), and this program writes its own output that way.
    """
    path = Path(path)
    raw = path.read_bytes()
    encoding, _ = detect_encoding(raw, language)
    if encoding in ("utf-8", "utf-8-sig"):
        return encoding, False

    text = raw.decode(encoding, errors="replace").lstrip("\ufeff")
    if backup_suffix:
        backup = path.with_suffix(path.suffix + backup_suffix)
        if not backup.exists():
            backup.write_bytes(raw)
    write_utf8(path, text)
    return encoding, True
