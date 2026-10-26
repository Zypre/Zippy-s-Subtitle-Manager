#!/usr/bin/env python3
"""Launcher: `python dual_subs.py [options]`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dualsubs.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
