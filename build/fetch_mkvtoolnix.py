#!/usr/bin/env python3
r"""Download the MKVToolNix portable build and keep only the CLI binaries.

    py -3 -m pip install py7zr
    py -3 build\fetch_mkvtoolnix.py            # into .\tools\mkvtoolnix
    py -3 build\fetch_mkvtoolnix.py --dest dist\tools\mkvtoolnix

Only mkvmerge.exe and mkvextract.exe are kept -- the GUI and its Qt DLLs are
several hundred MB and this program never touches them.

MKVToolNix is GPLv2. Keeping a copy for yourself is unrestricted; if you hand
the folder to anyone else, ship COPYING alongside it (this script keeps it) and
be ready to point them at the upstream source.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

BASE = "https://mkvtoolnix.download/windows/releases"
KEEP = {"mkvmerge.exe", "mkvextract.exe", "mkvpropedit.exe",
        "mkvinfo.exe", "COPYING"}


def _default_dest() -> Path:
    here = Path(__file__).resolve().parent
    for base in (here, here.parent, Path.cwd()):
        if (base / "dual_subs.py").is_file():
            return base / "tools" / "mkvtoolnix"
    return Path("tools") / "mkvtoolnix"


def build_url(version: str, arch: str) -> str:
    return f"{BASE}/{version}/mkvtoolnix-{arch}-{version}.7z"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="101.0", help="MKVToolNix release to fetch")
    ap.add_argument("--arch", default="64-bit", choices=["64-bit", "32-bit"])
    ap.add_argument("--dest", type=Path, default=None,
                    help="default: <project>\\tools\\mkvtoolnix")
    ap.add_argument("--archive", type=Path, default=None,
                    help="use an already-downloaded .7z instead of fetching")
    args = ap.parse_args()
    if args.dest is None:
        args.dest = _default_dest()

    try:
        import py7zr
    except ImportError:
        print("py7zr not installed. Run:  py -3 -m pip install py7zr")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        archive = args.archive
        if archive is None:
            url = build_url(args.version, args.arch)
            archive = tmp / "mkvtoolnix.7z"
            print(f"downloading {url}")
            try:
                urllib.request.urlretrieve(url, archive)
            except Exception as exc:
                print(f"\ndownload failed: {exc}\n"
                      f"Grab the portable .7z by hand from "
                      f"https://mkvtoolnix.download/downloads.html and re-run "
                      f"with --archive <file>")
                return 1

        print("extracting")
        with py7zr.SevenZipFile(archive, "r") as z:
            names = [n for n in z.getnames() if Path(n).name in KEEP]
            if not names:
                print("archive layout not recognised; extract mkvmerge.exe and "
                      "mkvextract.exe by hand instead")
                return 1
            z.extract(path=tmp / "out", targets=names)

        args.dest.mkdir(parents=True, exist_ok=True)
        for src in (tmp / "out").rglob("*"):
            if src.is_file() and src.name in KEEP:
                shutil.copy2(src, args.dest / src.name)
                print(f"  {src.name}")

    print(f"\ndone -> {args.dest.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
