#!/usr/bin/env python3
r"""Build a standalone dual_subs.exe with PyInstaller.

    py -3 build\build_exe.py        (or from anywhere inside the project)

Produces two single-file executables that need no Python on the target machine:

    dist\dual_subs.exe       console, for scripting and scheduled jobs
    dist\dual_subs_gui.exe   windowed; drop files onto its icon to queue them

Drop MKVToolNix next to them (see build\fetch_mkvtoolnix.py) and the whole
folder is portable.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

def find_project_root() -> Path:
    """Locate the folder holding dual_subs.py and the dualsubs package.

    Works whether this script sits in build\\ or has been moved up beside the
    project, and whatever the working directory happens to be. Assuming a fixed
    depth was a bug: it climbed out of the project entirely when the layout was
    flattened.
    """
    here = Path(__file__).resolve().parent
    for base in (here, here.parent, Path.cwd(), *Path.cwd().parents):
        if (base / "dual_subs.py").is_file() and (base / "dualsubs").is_dir():
            return base
    raise SystemExit(
        "Could not find the project. Expected dual_subs.py and the dualsubs\\ "
        f"folder in or above:\n  {here}\n  {Path.cwd()}\n"
        "Run this from inside the extracted dual_subs folder."
    )


ROOT = find_project_root()


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not installed. Run:  py -3 -m pip install pyinstaller")
        return 1

    rc = _build("dual_subs", console=True)
    if rc == 0:
        rc = _build("dual_subs_gui", console=False)
    if rc == 0:
        tools = ROOT / "dist" / "tools" / "mkvtoolnix"
        for name in ("dual_subs", "dual_subs_gui"):
            exe = ROOT / "dist" / (f"{name}.exe" if sys.platform == "win32" else name)
            if exe.exists():
                print(f"Built: {exe}  ({exe.stat().st_size / 1_048_576:.1f} MB)")
        if not tools.exists():
            print(f"Next: put mkvmerge.exe + mkvextract.exe in  {tools}")
    return rc


def _build(name: str, console: bool) -> int:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--console" if console else "--windowed",
        "--name", name,
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / "_work"),
        "--specpath", str(ROOT / "build"),
        # nothing heavy is imported, so trim the obvious dead weight
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc",
        "--exclude-module", "email",
        str(ROOT / "dual_subs.py"),
    ]
    print(" ".join(cmd))
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
