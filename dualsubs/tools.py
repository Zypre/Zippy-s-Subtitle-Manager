"""Finding and running the external binaries.

Search order for mkvmerge/mkvextract:
  1. an explicit --mkvtoolnix path
  2. the config file
  3. a `tools/mkvtoolnix/` folder next to the executable  (the portable case)
  4. anything already on PATH
  5. the usual Windows install locations

Only mkvmerge and mkvextract are required. ffmpeg, if present, is used as a
fallback for extracting tracks mkvextract chokes on.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

WINDOWS = os.name == "nt"
EXE = ".exe" if WINDOWS else ""

#: Where a bundled copy lives, relative to the app root.
BUNDLED_SUBDIR = Path("tools") / "mkvtoolnix"

_WINDOWS_GUESSES = [
    r"C:\Program Files\MKVToolNix",
    r"C:\Program Files (x86)\MKVToolNix",
    r"C:\Program Files\MKVToolNix GUI",
    r"D:\Program Files\MKVToolNix",
]


class ToolError(RuntimeError):
    pass


def app_root() -> Path:
    """The folder the user actually launched: next to the .exe when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


@dataclass
class Toolset:
    mkvmerge: Path
    mkvextract: Path
    ffmpeg: Path | None = None
    #: Only needed for changing default/forced flags, so its absence is not
    #: fatal -- everything else works without it.
    mkvpropedit: Path | None = None

    @cached_property
    def version(self) -> str:
        # Through run(), so the windowed build does not flash a console.
        try:
            return run([str(self.mkvmerge), "--version"], timeout=20
                       ).stdout.strip().splitlines()[0]
        except Exception:
            return "unknown version"


def _candidate_dirs(explicit: str | os.PathLike | None,
                    from_config: str | None) -> list[Path]:
    dirs: list[Path] = []
    if explicit:
        dirs.append(Path(explicit))
    if from_config:
        dirs.append(Path(from_config))
    root = app_root()
    dirs.append(root / BUNDLED_SUBDIR)
    dirs.append(root / "mkvtoolnix")
    dirs.append(root)
    if WINDOWS:
        dirs += [Path(p) for p in _WINDOWS_GUESSES]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Programs" / "MKVToolNix")
    else:
        dirs += [Path("/usr/bin"), Path("/usr/local/bin"), Path("/opt/homebrew/bin")]
    return dirs


def _find_in(dirs: list[Path], name: str) -> Path | None:
    for d in dirs:
        try:
            candidate = d / f"{name}{EXE}"
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def discover(explicit: str | os.PathLike | None = None,
             from_config: str | None = None) -> Toolset:
    dirs = _candidate_dirs(explicit, from_config)

    merge = _find_in(dirs, "mkvmerge") or _which("mkvmerge")
    extract = _find_in(dirs, "mkvextract") or _which("mkvextract")

    missing = [n for n, p in (("mkvmerge", merge), ("mkvextract", extract)) if not p]
    if missing:
        raise ToolError(
            f"Could not find {' and '.join(missing)}.\n"
            f"Install MKVToolNix (https://mkvtoolnix.download/), or drop the\n"
            f"portable copy in:  {app_root() / BUNDLED_SUBDIR}\n"
            f"or point at it with --mkvtoolnix <folder>."
        )

    ff = _find_in(dirs, "ffmpeg") or _which("ffmpeg")
    propedit = _find_in(dirs, "mkvpropedit") or _which("mkvpropedit")
    return Toolset(mkvmerge=merge, mkvextract=extract, ffmpeg=ff,
                   mkvpropedit=propedit)


def _which(name: str) -> Path | None:
    found = shutil.which(name)
    return Path(found).resolve() if found else None


def _quiet() -> dict:
    """Keyword arguments that stop a child flashing a console on Windows."""
    kwargs: dict = {}
    if WINDOWS:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = si
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    """Run a child process without flashing a console window on Windows."""
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, **_quiet(),
    )


def run_cancellable(cmd: list[str], should_stop) -> subprocess.CompletedProcess | None:
    """Like run(), but polls `should_stop()` and kills the child when it says so.

    Returns None if it was stopped. For long reads in the background that a
    job touching the same file has to interrupt -- on Windows a file another
    process has open cannot be renamed.

    The waiting is done by communicate(), which keeps reading the child's
    output, never by polling. A pipe holds only so much (about 4 KB on
    Windows); a child whose output is not being read stops dead once it is
    full, and then waits forever for a reader that is itself waiting for the
    child to exit. mkvextract prints a line per track and a hundred progress
    updates, which is enough on a release with many subtitle tracks.
    """
    if should_stop():
        return None
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", **_quiet())
    while True:
        try:
            out, err = proc.communicate(timeout=0.1)
        except subprocess.TimeoutExpired:
            if should_stop():
                proc.kill()
                proc.communicate()
                return None
            continue
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
