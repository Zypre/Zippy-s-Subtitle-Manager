"""Shortening release filenames, mostly so VLC can open them.

VLC on Windows fails on paths over 260 characters even with the
`LongPathsEnabled` registry value set to 1, and the reported workaround is to
shorten the name. Patching it would mean adding `longPathAware` to its
manifest, auditing its path handling and maintaining a Windows build of VLC
forever; a plugin cannot help either, because the failure is in the access
layer before plugins get a say. Renaming the file is the cheap fix.

Release names carry a lot that is not the title:

    [SavI0r] Tensei Kizoku no Isekai Boukenroku (The Aristocrat's Otherworldly
    Adventure) - S01E06 - A Date With the Strongest Captain of the Knights
    [BD][1080p][AV1][OPUS][Dual Audio].mkv          -> 182 characters

    S01E06 - A Date With the Strongest Captain of the Knights.mkv  -> 60

Every rename is written to an undo log next to the files, and sidecar
subtitles are renamed to match so they keep pairing with their video.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .discover import RELEASE_TOKENS, SUBTITLE_EXTS, find_candidates

__all__ = ["clean_name", "plan_renames", "apply_renames", "undo_renames",
           "RenamePlan", "UNDO_NAME"]

UNDO_NAME = "dual_subs_renames.json"

#: Windows' classic limit. The whole path has to fit, not just the name.
MAX_PATH = 260

#: SxxExx, 1x02, "- 06 -", "E06"...
EPISODE_PATTERNS = (
    re.compile(r"\bS(\d{1,2})\s*E(\d{1,3})(?:\s*-?\s*E(\d{1,3}))?\b", re.I),
    re.compile(r"\b(\d{1,2})x(\d{1,3})\b", re.I),
)

#: A bare episode number between separators, e.g. "Show - 06 - Title".
BARE_EPISODE = re.compile(r"\s-\s(\d{1,3})\s-\s")

#: Bracketed groups are release metadata essentially always.
BRACKETS = re.compile(r"[\[\{][^\]\}]*[\]\}]")

#: Parenthesised groups are kept only when they look like a year.
PARENS = re.compile(r"\(([^)]*)\)")

YEAR = re.compile(r"^(19|20)\d{2}$")


@dataclass
class RenamePlan:
    source: Path
    target: Path
    sidecars: list[tuple[Path, Path]] = field(default_factory=list)
    note: str = ""

    @property
    def changed(self) -> bool:
        return self.source != self.target

    @property
    def saved(self) -> int:
        return len(str(self.source)) - len(str(self.target))


def _normalise_separators(stem: str) -> str:
    """Turn scene-style dots into spaces.

    "Shaolin.Soccer.2001.BluRay.1080p" is one whitespace-delimited word, so
    nothing downstream can see the release tokens inside it. Only done when
    the name has no real spaces, so a title that genuinely contains a dot
    ("Mr. Nobody") is left alone.
    """
    if " " in stem or stem.count(".") < 2:
        return stem
    # Scene naming always ends Title.Year.Tags-GROUP, so the trailing
    # hyphenated segment is the group, not part of the title.
    stem = re.sub(r"-[A-Za-z0-9]{2,}$", "", stem)
    return re.sub(r"\.(?!\d\b)", " ", stem)


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text.replace("_", " "))
    text = re.sub(r"\s*-\s*-\s*", " - ", text)
    text = text.strip(" .-_")
    # Windows forbids these outright; a release name can contain them.
    return re.sub(r'[<>:"/\\|?*]', "", text).strip(" .-_")


def _strip_release_tokens(text: str) -> str:
    kept = []
    for word in text.split():
        bare = re.sub(r"[^0-9a-z]", "", word.lower())
        if bare and bare in RELEASE_TOKENS:
            continue
        if re.fullmatch(r"\d{3,4}[pi]", bare):
            continue
        kept.append(word)
    return " ".join(kept)


def find_episode(stem: str) -> tuple[str, int] | None:
    """Return (label, position-after-it) for the episode marker, if any."""
    for pattern in EPISODE_PATTERNS:
        m = pattern.search(stem)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
            label = f"S{season:02d}E{episode:02d}"
            if m.lastindex and m.lastindex >= 3 and m.group(3):
                label += f"-E{int(m.group(3)):02d}"
            return label, m.end()
    m = BARE_EPISODE.search(stem)
    if m:
        return f"E{int(m.group(1)):02d}", m.end() - 1
    return None


def clean_name(stem: str, style: str = "episode", keep_year: bool = True) -> str:
    """Reduce a release filename to something a player can open.

    style "episode": "S01E06 - Episode Title", dropping the series name, which
    the containing folder already carries. Falls back to "tags" if there is no
    episode marker, so films keep their title.

    style "tags": keep the whole name, drop only bracketed groups and release
    metadata. Safer, shortens less.
    """
    original = stem
    stem = _normalise_separators(stem)
    episode = find_episode(stem)

    if style == "episode" and episode:
        label, after = episode
        tail = stem[after:]
        tail = BRACKETS.sub(" ", tail)
        tail = PARENS.sub(lambda m: " ", tail)
        tail = _strip_release_tokens(_tidy(tail))
        tail = _tidy(tail)
        return _tidy(f"{label} - {tail}") if tail else label

    text = BRACKETS.sub(" ", stem)
    text = PARENS.sub(
        lambda m: f" ({m.group(1)}) " if keep_year and YEAR.match(m.group(1).strip())
        else " ", text)
    text = _strip_release_tokens(_tidy(text))
    text = _tidy(text)
    return text or _tidy(BRACKETS.sub(" ", original)) or original


def _fit(target: Path, limit: int) -> Path:
    """Trim the stem until the whole path fits, keeping the episode label."""
    if limit <= 0 or len(str(target)) <= limit:
        return target
    room = limit - len(str(target.parent)) - 1 - len(target.suffix)
    if room < 8:
        return target                 # the folder alone is too long; say so
    stem = target.stem
    if len(stem) <= room:
        return target
    cut = stem[:room].rstrip(" .-_")
    return target.with_name(cut + target.suffix)


def plan_renames(videos, style: str = "episode", limit: int = MAX_PATH,
                 rename_sidecars: bool = True) -> list[RenamePlan]:
    plans: list[RenamePlan] = []
    taken: set[str] = set()

    for video in videos:
        video = Path(video)
        new_stem = clean_name(video.stem, style)
        target = _fit(video.with_name(new_stem + video.suffix), limit)

        # Never collide, with an existing file or with another plan.
        base, n = target, 2
        while (str(target).lower() in taken or
               (target.exists() and target.resolve() != video.resolve())):
            target = base.with_name(f"{base.stem} ({n}){base.suffix}")
            n += 1
        taken.add(str(target).lower())

        plan = RenamePlan(source=video, target=target)
        if len(str(target)) > limit:
            plan.note = (f"still {len(str(target))} characters - the folder "
                         f"itself is too long to fix by renaming the file")

        if rename_sidecars and target != video:
            for cand in find_candidates(video):
                if cand.path.parent != video.parent:
                    continue
                suffix = cand.path.name[len(video.stem):] \
                    if cand.path.name.lower().startswith(video.stem.lower()) \
                    else cand.path.suffix
                plan.sidecars.append(
                    (cand.path, cand.path.with_name(target.stem + suffix)))
        plans.append(plan)
    return plans


def apply_renames(plans: list[RenamePlan], folder: Path | None = None
                  ) -> tuple[int, list[str]]:
    """Carry out the plans and write an undo log. Returns (count, problems)."""
    done: list[dict] = []
    problems: list[str] = []
    count = 0

    for plan in plans:
        if not plan.changed:
            continue
        pairs = [(plan.source, plan.target)] + plan.sidecars
        moved: list[tuple[Path, Path]] = []
        try:
            for src, dst in pairs:
                if not src.exists() or dst.exists():
                    continue
                src.rename(dst)
                moved.append((src, dst))
        except OSError as exc:
            for src, dst in reversed(moved):      # put this file back
                try:
                    dst.rename(src)
                except OSError:
                    pass
            problems.append(f"{plan.source.name}: {exc}")
            continue
        if moved:
            count += 1
            done += [{"from": str(s), "to": str(d)} for s, d in moved]

    if done:
        target = Path(folder or plans[0].source.parent) / UNDO_NAME
        existing = []
        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = []
        target.write_text(json.dumps(existing + done, indent=2,
                                     ensure_ascii=False), encoding="utf-8")
    return count, problems


def undo_renames(folder: Path) -> tuple[int, list[str]]:
    """Put everything back, newest first."""
    log = Path(folder) / UNDO_NAME
    if not log.exists():
        return 0, [f"no {UNDO_NAME} in {folder}"]
    try:
        entries = json.loads(log.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return 0, [f"could not read {UNDO_NAME}: {exc}"]

    count, problems = 0, []
    for entry in reversed(entries):
        src, dst = Path(entry["to"]), Path(entry["from"])
        if not src.exists():
            problems.append(f"missing: {src.name}")
            continue
        if dst.exists():
            problems.append(f"already there: {dst.name}")
            continue
        try:
            src.rename(dst)
            count += 1
        except OSError as exc:
            problems.append(f"{src.name}: {exc}")
    if count and not problems:
        log.unlink(missing_ok=True)
    return count, problems
