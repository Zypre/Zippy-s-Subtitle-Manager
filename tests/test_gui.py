"""The window, built for real but never shown. Skips without tkinter or a display.

The settings file is redirected to a temporary folder first, so running these
never touches your own dual_subs.config.json.

    python -m pytest tests
    python tests/test_gui.py      (works without pytest too)
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dualsubs.config import Config

try:
    import tkinter as tk
    from dualsubs import gui
    _root = tk.Tk()
    _root.destroy()
    HAVE_TK = True
except Exception:          # no tkinter, or no display to open a window on
    HAVE_TK = False

try:
    import pytest
    pytestmark = pytest.mark.skipif(not HAVE_TK, reason="no tkinter or no display")
except ImportError:
    pytest = None


class _Window:
    """An App on a hidden root, with its settings file in a temp folder."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmp.name) / "dual_subs.config.json"
        self._default_path = Config.default_path
        Config.default_path = classmethod(lambda cls: self.config_path)
        self._argv = sys.argv
        sys.argv = ["dual_subs"]
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = gui.App(self.root)
        return self.app

    def __exit__(self, *exc):
        try:
            self.root.destroy()
        finally:
            Config.default_path = self._default_path
            sys.argv = self._argv
            self.tmp.cleanup()


# --------------------------------------------------------------------------

def test_the_window_builds_and_settings_round_trip():
    with _Window() as app:
        app.cfg.top.font = "Some Font"
        app.cfg.bottom.size = 61
        app.cfg.output_mode = "sidecar"
        app.cfg.align_mode, app.cfg.anchor = "grid", "bottom"
        app._load_settings()
        app._collect_settings()
        assert app.cfg.top.font == "Some Font" and app.cfg.bottom.size == 61
        assert app.cfg.output_mode == "sidecar"
        assert (app.cfg.align_mode, app.cfg.anchor) == ("grid", "bottom")
        assert app.save_settings(announce=False)
        assert Config.load(Config.default_path()).bottom.size == 61


def test_off_keeps_the_chosen_alignment_mode_for_later():
    with _Window() as app:
        app.cfg.align_mode = "grid"
        app.vars["align_mode"].set(gui._label(gui.ALIGN_CHOICES, "off"))
        app._collect_settings()
        assert app.cfg.snap_sides is False and app.cfg.align_mode == "grid"


def test_bad_numbers_keep_the_previous_value():
    with _Window() as app:
        before = app.cfg.top.size
        app.vars["top.size"].set("seventy")
        app.vars["bottom.margin_v"].set("99999")
        app._collect_settings()
        assert app.cfg.top.size == before
        assert app.cfg.bottom.margin_v == 1000          # clamped


def test_swap_moves_languages_and_looks_but_not_margins():
    with _Window() as app:
        app.vars["top_language"].set("chi")
        app.vars["bottom_language"].set("eng")
        app.vars["top.font"].set("Microsoft YaHei")
        app.vars["bottom.font"].set("Arial")
        app.vars["top.margin_v"].set("70")
        app.vars["bottom.margin_v"].set("10")
        app.swap_sides()
        assert app.vars["top_language"].get() == "eng"
        assert app.vars["bottom_language"].get() == "chi"
        assert app.vars["bottom.font"].get() == "Microsoft YaHei"
        assert (app.vars["top.margin_v"].get(), app.vars["bottom.margin_v"].get()) == ("70", "10")


def test_options_that_cannot_apply_are_greyed_out():
    with _Window() as app:
        app.vars["output_mode"].set("sidecar")
        app._update_states()
        assert app.stash_check.instate(["disabled"])
        assert app.dual_check.instate(["disabled"])
        app.vars["output_mode"].set("mux")
        app._update_states()
        assert not app.stash_check.instate(["disabled"])

        app.vars["align_mode"].set(gui._label(gui.ALIGN_CHOICES, "pairs"))
        app._update_states()
        snap_spin, grid_spin = app.timing_rows[0][1], app.timing_rows[1][1]
        assert not snap_spin.instate(["disabled"]) and grid_spin.instate(["disabled"])


def test_queue_groups_by_folder_and_removes_selection():
    with _Window() as app, tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        names = [f"[Group] A Long Series Name - S01E0{i} - Title {i} [1080p].mkv"
                 for i in (1, 2)]
        for name in names:
            (folder / name).write_bytes(b"")
        app._add([folder])
        assert len(app.videos) == 2
        group = app.tree.get_children()
        assert len(group) == 1 and group[0].startswith("dir:")
        shown = [app.tree.item(c, "text") for c in app.tree.get_children(group[0])]
        assert shown[0].startswith("… S01E01")        # the shared prefix is trimmed
        app.tree.selection_set(app.tree.get_children(group[0])[0])
        app.remove_selected()
        assert [v.name for v in app.videos] == [names[1]]


def test_colour_conversion_round_trips():
    assert gui._ass_to_hex("&H00FFFFFF") == "#ffffff"
    assert gui._ass_to_hex("&H0000FFFF") == "#ffff00"          # ASS is BGR
    assert gui._hex_to_ass("#ffff00", "&H80000000") == "&H8000FFFF"   # alpha kept
    assert gui._ass_to_hex("not a colour") is None


def test_distinct_parts_keeps_what_tells_files_apart():
    names = [f"[Grp] Some Very Long Series - S01E0{i} - {title} [BD][1080p].mkv"
             for i, title in ((1, "Arrival"), (2, "Departure"))]
    assert gui._distinct_parts(names) == ["… S01E01 - Arrival …",
                                          "… S01E02 - Departure …"]
    assert gui._distinct_parts(["a.mkv", "b.mkv"]) == ["a.mkv", "b.mkv"]
    assert gui._distinct_parts(["only.mkv"]) == ["only.mkv"]


def test_the_preview_finds_the_worst_moment_in_the_queue():
    import subprocess
    import time
    with _Window() as app, tempfile.TemporaryDirectory() as tmp:
        if app.tools is None:
            return                                # needs MKVToolNix
        folder = Path(tmp)

        def srt(name, lines):
            def ts(cs):
                return f"00:00:{cs // 100:02},{cs % 100 * 10:03}"
            (folder / name).write_text("\n".join(
                f"{i}\n{ts(s)} --> {ts(e)}\n{t}\n"
                for i, (s, e, t) in enumerate(lines, 1)), encoding="utf-8")

        srt("a.srt", [(100, 300, "短"), (400, 900, "这句话很长" * 8 + "。")])
        srt("b.srt", [(100, 300, "Short."), (450, 850, "A longer line. " * 3)])
        subprocess.run([str(app.tools.mkvmerge), "-q", "-o", str(folder / "Ep.mkv"),
                        "--language", "0:chi", str(folder / "a.srt"),
                        "--language", "0:eng", str(folder / "b.srt")], check=False)
        app.vars["top_language"].set("chi")
        app.vars["bottom_language"].set("eng")
        app._add([folder])
        end = time.time() + 20
        while time.time() < end and app.worst is None:
            app.root.update()
            time.sleep(0.05)
        assert app.worst is not None, app.preview_caption.get()
        assert app.worst.top.startswith("这句话很长") and app.worst.top_rows == 2
        assert app.worst.video == folder / "Ep.mkv" or app.worst.video.name == "Ep.mkv"
        assert "Worst moment" in app.preview_caption.get()


# --------------------------------------------------------------------------

if __name__ == "__main__":
    if not HAVE_TK:
        print("no tkinter or no display; skipping")
        raise SystemExit(0)
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL {name}  {exc}")
    print(f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
