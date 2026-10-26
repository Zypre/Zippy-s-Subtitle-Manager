#!/usr/bin/env python3
"""Double-click to open the window with no console behind it.

Windows runs .pyw files with pythonw.exe, which has no console. Videos or
folders passed as arguments (or dropped onto the file, where the Python
installer has registered that) arrive queued, the same as with the .exe.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dualsubs.gui import run

if __name__ == "__main__":
    raise SystemExit(run())
