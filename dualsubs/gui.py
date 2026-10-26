"""A window, for when the command line is more ceremony than the job deserves.

Pure tkinter, which ships with the Windows Python installer and bundles into a
PyInstaller build without extra work. If `tkinterdnd2` happens to be installed,
you can drop files straight onto the list; without it, dropping files onto the
.exe itself still works, because Windows passes them as arguments.

Everything the window shows is a field of Config, and every field is written
back to dual_subs.config.json when you run, save, or close. The command line
reads the same file, so the two stay in step.
"""

from __future__ import annotations

import queue
import sys
import threading
import traceback
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, filedialog, messagebox, ttk
import tkinter as tk
import tkinter.font as tkfont

from . import langs, probe, survey as survey_mod
from .config import Config
from .pipeline import SideSpec, collect_videos, process
from .probe import ProbeError
from .tools import ToolError, discover as discover_tools

PAD = 8

#: Only used before anything has been scanned. Every selector is rebuilt from
#: the queued files as soon as they are read, because a fixed list offers
#: languages a release does not have and cannot offer the ones it does.
LANGUAGE_CHOICES: list[str] = []
ALIGN_CHOICES = [
    ("match up cue pairs", "pairs"),
    ("pin one side to the other's grid", "grid"),
    ("leave both alone", "off"),
]
ANCHOR_CHOICES = [
    ("follow the video's audio track", "auto"),
    ("keep the top line's timing", "top"),
    ("keep the bottom line's timing", "bottom"),
]
MODE_CHOICES = [
    ("subtitle file only (fast)", "sidecar"),
    ("remux into a new MKV", "mux"),
    ("both", "both"),
]
#: Sensible fallbacks when enumerating installed fonts is slow or unavailable.
FONT_FALLBACKS = ["Arial", "Segoe UI", "Tahoma", "Verdana",
                  "Microsoft YaHei", "SimHei", "SimSun", "KaiTi",
                  "Noto Sans CJK SC", "Source Han Sans SC"]


class App:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.cfg = Config.load()
        self.tools = None
        self.videos: list[Path] = []
        self.worker: threading.Thread | None = None
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.vars: dict[str, tk.Variable] = {}
        self.survey = survey_mod.Survey()
        self.scan_generation = 0
        #: Comboboxes whose contents come from the queue, by option kind.
        self.track_boxes: dict[str, list] = {"audio": [], "subtitle": []}

        root.title("dual_subs")
        root.minsize(820, 680)
        self._build()
        self._load_settings()
        self._find_tools()
        self._accept_dropped_arguments()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(80, self._drain)

    # ------------------------------------------------------------- layout

    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=3)
        root.rowconfigure(3, weight=2)

        bar = ttk.Frame(root, padding=(PAD, PAD, PAD, 0))
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Button(bar, text="Add files...", command=self.add_files).pack(side="left")
        ttk.Button(bar, text="Add folder...", command=self.add_folder).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Remove", command=self.remove_selected).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="left", padx=(6, 0))
        self.vars["recursive"] = BooleanVar(value=False)
        ttk.Checkbutton(bar, text="include subfolders",
                        variable=self.vars["recursive"]).pack(side="left", padx=(PAD * 2, 0))
        ttk.Button(bar, text="Inspect", command=self.inspect).pack(side="right")
        ttk.Button(bar, text="Subtitles to UTF-8", command=self.reencode
                   ).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="Shorten names", command=self.shorten_names
                   ).pack(side="right", padx=(0, 6))

        frame = ttk.Frame(root, padding=(PAD, 4, PAD, 0))
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(frame, selectmode="extended", activestyle="none")
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.listbox.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=scroll.set)
        self._enable_drop(self.listbox)

        book = ttk.Notebook(root, padding=(PAD, PAD, PAD, 0))
        book.grid(row=2, column=0, sticky="ew")
        book.add(self._tab_subtitles(book), text="Subtitles")
        book.add(self._tab_appearance(book), text="Appearance")
        book.add(self._tab_timing(book), text="Timing")
        book.add(self._tab_defaults(book), text="Default tracks")

        logframe = ttk.Frame(root, padding=(PAD, PAD, PAD, 0))
        logframe.grid(row=3, column=0, sticky="nsew")
        logframe.columnconfigure(0, weight=1)
        logframe.rowconfigure(0, weight=1)
        self.log = tk.Text(logframe, height=10, wrap="none", state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        logscroll = ttk.Scrollbar(logframe, orient="vertical", command=self.log.yview)
        logscroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=logscroll.set)
        self.log.tag_configure("error", foreground="#b00020")
        self.log.tag_configure("ok", foreground="#0a7d28")
        self.log.tag_configure("dim", foreground="#777777")

        foot = ttk.Frame(root, padding=PAD)
        foot.grid(row=4, column=0, sticky="ew")
        foot.columnconfigure(0, weight=1)
        self.status = StringVar(value="")
        ttk.Label(foot, textvariable=self.status, foreground="#555555"
                  ).grid(row=0, column=0, sticky="w")
        ttk.Button(foot, text="Save settings", command=self.save_settings
                   ).grid(row=0, column=1, padx=(0, PAD))
        self.progress = ttk.Progressbar(foot, mode="determinate", length=160)
        self.progress.grid(row=0, column=2, padx=(0, PAD))
        self.run_button = ttk.Button(foot, text="Run", command=self.run)
        self.run_button.grid(row=0, column=3)

    # ------------------------------------------------------------ the tabs

    def _tab_subtitles(self, parent) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=PAD)
        for c in (1, 3):
            tab.columnconfigure(c, weight=1)

        self.vars["top_language"] = StringVar()
        self.vars["bottom_language"] = StringVar()
        ttk.Label(tab, text="Top line").grid(row=0, column=0, sticky="w")
        top_box = ttk.Combobox(tab, textvariable=self.vars["top_language"],
                               values=[], width=34)
        top_box.grid(row=0, column=1, sticky="ew", padx=(6, PAD * 2))
        ttk.Label(tab, text="Bottom line").grid(row=0, column=2, sticky="w")
        bottom_box = ttk.Combobox(tab, textvariable=self.vars["bottom_language"],
                                  values=[], width=34)
        bottom_box.grid(row=0, column=3, sticky="ew", padx=(6, 0))
        self.track_boxes["subtitle"] += [top_box, bottom_box]

        self.vars["output_mode"] = StringVar()
        ttk.Label(tab, text="Output").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(tab, textvariable=self.vars["output_mode"], state="readonly",
                     values=[c[0] for c in MODE_CHOICES], width=28
                     ).grid(row=1, column=1, sticky="w", padx=(6, PAD * 2), pady=(8, 0))

        flags = ttk.Frame(tab)
        flags.grid(row=1, column=2, columnspan=2, sticky="w", pady=(8, 0))
        self.vars["overwrite"] = BooleanVar(value=False)
        self.vars["stash_originals"] = BooleanVar()
        ttk.Checkbutton(flags, text="overwrite existing output",
                        variable=self.vars["overwrite"]).pack(side="left")
        ttk.Checkbutton(flags, text="stash originals",
                        variable=self.vars["stash_originals"]).pack(side="left", padx=(PAD, 0))

        self.vars["t2s"] = BooleanVar()
        ttk.Checkbutton(
            tab, text="Convert traditional Chinese to simplified  (\u7e41\u9ad4 \u2192 \u7b80\u4f53)",
            variable=self.vars["t2s"]).grid(row=2, column=0, columnspan=4,
                                            sticky="w", pady=(10, 0))
        ttk.Label(tab, foreground="#777777",
                  text="Checked, each side is examined and converted only if it is "
                       "actually traditional.\nEnglish and already-simplified text "
                       "are left untouched."
                  ).grid(row=3, column=0, columnspan=4, sticky="w", padx=(20, 0))
        return tab

    def _tab_appearance(self, parent) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=PAD)
        fonts = self._font_list()

        headers = ["", "Font", "Size", "Margin", "Spacing", "Scale X", "Bold"]
        for c, text in enumerate(headers):
            ttk.Label(tab, text=text, foreground="#555555").grid(
                row=0, column=c, sticky="w", padx=(0, 6))

        for r, side in enumerate(("top", "bottom"), start=1):
            ttk.Label(tab, text=side.capitalize()).grid(row=r, column=0, sticky="w", pady=3)
            self.vars[f"{side}.font"] = StringVar()
            ttk.Combobox(tab, textvariable=self.vars[f"{side}.font"],
                         values=fonts, width=24).grid(row=r, column=1, padx=(0, 6))
            for c, key in enumerate(("size", "margin_v", "spacing", "scale_x"), start=2):
                self.vars[f"{side}.{key}"] = StringVar()
                ttk.Spinbox(tab, textvariable=self.vars[f"{side}.{key}"],
                            from_=-200, to=400, width=6, increment=1
                            ).grid(row=r, column=c, padx=(0, 6))
            self.vars[f"{side}.bold"] = BooleanVar()
            ttk.Checkbutton(tab, variable=self.vars[f"{side}.bold"]
                            ).grid(row=r, column=6, sticky="w")

        ttk.Separator(tab, orient="horizontal").grid(
            row=3, column=0, columnspan=7, sticky="ew", pady=(10, 6))

        self.vars["layout.auto_lift"] = BooleanVar()
        ttk.Checkbutton(tab, text="lift the top line when the bottom cue wraps to two lines",
                        variable=self.vars["layout.auto_lift"]
                        ).grid(row=4, column=0, columnspan=7, sticky="w")
        ttk.Label(tab, foreground="#777777",
                  text="Margin is pixels up from the bottom of a 1920x1080 canvas, so it "
                       "scales with any video.\nSpacing is extra pixels between characters; "
                       "negative tightens."
                  ).grid(row=5, column=0, columnspan=7, sticky="w", pady=(6, 0))
        return tab

    def _tab_timing(self, parent) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=PAD)
        self.vars["auto_sync"] = BooleanVar()
        self.vars["allow_rate_fix"] = BooleanVar()
        self.vars["snap_sides"] = BooleanVar()
        self.vars["snap_window_ms"] = StringVar()

        ttk.Checkbutton(tab, text="Retime external subtitle files onto the video",
                        variable=self.vars["auto_sync"]).grid(row=0, column=0,
                                                              columnspan=3, sticky="w")
        ttk.Checkbutton(tab, text="...including a framerate rescale "
                                  "(25fps subs on 23.976fps video)",
                        variable=self.vars["allow_rate_fix"]
                        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=(20, 0))

        ttk.Separator(tab, orient="horizontal").grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(10, 6))

        self.vars["align_mode"] = StringVar()
        self.vars["anchor"] = StringVar()
        self.vars["grid_window_ms"] = StringVar()

        ttk.Label(tab, text="Keep the two lines together").grid(
            row=3, column=0, sticky="w")
        ttk.Combobox(tab, textvariable=self.vars["align_mode"], state="readonly",
                     values=[c[0] for c in ALIGN_CHOICES], width=32
                     ).grid(row=3, column=1, columnspan=2, sticky="w", padx=(6, 0))

        ttk.Label(tab, text="within").grid(row=4, column=0, sticky="w", padx=(20, 0))
        ttk.Spinbox(tab, textvariable=self.vars["snap_window_ms"], from_=0, to=2000,
                    increment=50, width=7).grid(row=4, column=1, sticky="w", padx=(6, 6))
        ttk.Label(tab, text="ms  (matched pairs)").grid(row=4, column=2, sticky="w")

        ttk.Label(tab, text="or").grid(row=5, column=0, sticky="w", padx=(20, 0))
        ttk.Spinbox(tab, textvariable=self.vars["grid_window_ms"], from_=0, to=4000,
                    increment=100, width=7).grid(row=5, column=1, sticky="w", padx=(6, 6))
        ttk.Label(tab, text="ms  (grid mode)").grid(row=5, column=2, sticky="w")

        ttk.Label(tab, text="Anchor").grid(row=6, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(tab, textvariable=self.vars["anchor"], state="readonly",
                     values=[c[0] for c in ANCHOR_CHOICES], width=32
                     ).grid(row=6, column=1, columnspan=2, sticky="w",
                            padx=(6, 0), pady=(6, 0))

        ttk.Label(tab, foreground="#777777",
                  text="Two subtitlers rarely agree to the centisecond, so the same line can "
                       "start a fifth of a\nsecond apart and appear to flicker. Matching up "
                       "pairs fixes that.\n\nWhen a dub reflows lines, though, the two tracks "
                       "split dialogue differently and no\npairing exists -- three lines "
                       "against two. Grid mode instead pins one side to the\nother's cue "
                       "boundaries, so it only ever changes when the anchor does. That moves\n"
                       "it away from its own audio, so anchor whichever language you are "
                       "listening to."
                  ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))
        return tab

    def _tab_defaults(self, parent) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=PAD)
        tab.columnconfigure(1, weight=1)

        self.vars["set_audio"] = StringVar()
        self.vars["set_subs"] = StringVar()
        self.vars["set_dual"] = BooleanVar(value=False)
        self.vars["subs_off"] = BooleanVar(value=False)

        ttk.Label(tab, text="Default audio").grid(row=0, column=0, sticky="w")
        audio_box = ttk.Combobox(tab, textvariable=self.vars["set_audio"],
                                 values=[], width=46)
        audio_box.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ttk.Label(tab, text="Default subtitles").grid(row=1, column=0, sticky="w",
                                                      pady=(6, 0))
        subs_box = ttk.Combobox(tab, textvariable=self.vars["set_subs"],
                                values=[], width=46)
        subs_box.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(6, 0))
        self.track_boxes["audio"].append(audio_box)
        self.track_boxes["subtitle"].append(subs_box)

        ttk.Checkbutton(tab, text="make the dual track the default subtitle",
                        variable=self.vars["set_dual"]
                        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(tab, text="turn subtitles off by default",
                        variable=self.vars["subs_off"]
                        ).grid(row=3, column=0, columnspan=2, sticky="w")

        ttk.Button(tab, text="Set flags now (no remux)", command=self.set_flags
                   ).grid(row=4, column=0, sticky="w", pady=(10, 0))
        ttk.Label(tab, foreground="#777777",
                  text="These flags live in the file header, so setting them takes a moment\n"
                       "whatever the file size -- nothing is re-encoded or rewritten.\n\n"
                       'A plain language code picks the main dialogue track and skips\n'
                       '"Signs & Songs" and commentary. Add words after a colon to choose\n'
                       'between tracks of the same language: chi:traditional, eng:dub.\n\n'
                       "Leave both blank and tick nothing to leave flags alone when running."
                  ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        return tab

    def set_flags(self) -> None:
        """Apply default-track flags to the queued files, without remuxing."""
        from .tracks import FlagError, TrackWish, apply_flags, choose

        if not self._check_ready():
            return
        if self.tools.mkvpropedit is None:
            self.write("mkvpropedit not found; it ships with MKVToolNix\n", "error")
            return
        audio = survey_mod.spec_of(self.vars["set_audio"].get())
        subs = survey_mod.spec_of(self.vars["set_subs"].get())
        off = self.vars["subs_off"].get()
        if not audio and not subs and not off:
            self.status.set("choose a default audio or subtitle track first")
            return

        changed = 0
        for video in self.videos:
            self.write(f"\n{video.name}\n")
            try:
                media = probe.probe(video, self.tools)
            except ProbeError as exc:
                self.write(f"  ! {exc}\n", "error")
                continue
            sel = choose(media,
                         TrackWish.parse(audio) if audio else None,
                         TrackWish.parse(subs) if subs else None,
                         subtitles_off=off)
            for r in sel.reasons:
                self.write(f"  {r}\n", "dim")
            for w in sel.warnings:
                self.write(f"  ! {w}\n", "error")
            try:
                edits = apply_flags(media, sel, self.tools)
            except FlagError as exc:
                self.write(f"  ! mkvpropedit: {exc}\n", "error")
                continue
            if edits:
                changed += 1
                self.write(f"  set {len(edits)} flag(s)\n", "ok")
            else:
                self.write("  already set\n", "dim")
        self.status.set(f"{changed} file(s) changed")

    def _font_list(self) -> list[str]:
        try:
            found = sorted({f for f in tkfont.families() if not f.startswith("@")})
        except tk.TclError:
            found = []
        return found or FONT_FALLBACKS

    # ---------------------------------------------------------- settings io

    def _load_settings(self) -> None:
        c = self.cfg
        self.vars["top_language"].set(c.top_language or "eng")
        self.vars["bottom_language"].set(c.bottom_language or "chi")
        self.vars["output_mode"].set(_label(MODE_CHOICES, c.output_mode))
        self.vars["stash_originals"].set(c.stash_originals)
        self.vars["t2s"].set(bool(c.top_convert or c.bottom_convert))
        self.vars["auto_sync"].set(c.auto_sync)
        self.vars["allow_rate_fix"].set(c.allow_rate_fix)
        self.vars["snap_sides"].set(c.snap_sides)
        self.vars["snap_window_ms"].set(str(c.snap_window_ms))
        self.vars["align_mode"].set(_label(ALIGN_CHOICES, c.align_mode))
        self.vars["anchor"].set(_label(ANCHOR_CHOICES, c.anchor))
        self.vars["grid_window_ms"].set(str(c.grid_window_ms))
        self.vars["layout.auto_lift"].set(c.layout.auto_lift)
        for side, style in (("top", c.top), ("bottom", c.bottom)):
            self.vars[f"{side}.font"].set(style.font)
            self.vars[f"{side}.size"].set(str(style.size))
            self.vars[f"{side}.margin_v"].set(str(style.margin_v))
            self.vars[f"{side}.spacing"].set(str(style.spacing))
            self.vars[f"{side}.scale_x"].set(str(style.scale_x))
            self.vars[f"{side}.bold"].set(style.bold)

    def _collect_settings(self) -> None:
        """Pull every widget back into the Config. Bad numbers keep the old value."""
        c = self.cfg
        c.top_language = survey_mod.spec_of(self.vars["top_language"].get())
        c.bottom_language = survey_mod.spec_of(self.vars["bottom_language"].get())
        c.output_mode = _value(MODE_CHOICES, self.vars["output_mode"].get())
        c.stash_originals = self.vars["stash_originals"].get()
        convert = "auto" if self.vars["t2s"].get() else ""
        c.top_convert = c.bottom_convert = convert
        c.auto_sync = self.vars["auto_sync"].get()
        c.allow_rate_fix = self.vars["allow_rate_fix"].get()
        c.snap_sides = self.vars["snap_sides"].get()
        c.snap_window_ms = _int(self.vars["snap_window_ms"], c.snap_window_ms, 0, 5000)
        c.align_mode = _value(ALIGN_CHOICES, self.vars["align_mode"].get())
        c.snap_sides = c.align_mode != "off"
        c.anchor = _value(ANCHOR_CHOICES, self.vars["anchor"].get())
        c.grid_window_ms = _int(self.vars["grid_window_ms"], c.grid_window_ms, 0, 5000)
        c.layout.auto_lift = self.vars["layout.auto_lift"].get()
        for side, style in (("top", c.top), ("bottom", c.bottom)):
            font = self.vars[f"{side}.font"].get().strip()
            if font:
                style.font = font
            style.size = _int(self.vars[f"{side}.size"], style.size, 6, 400)
            style.margin_v = _int(self.vars[f"{side}.margin_v"], style.margin_v, 0, 1000)
            style.spacing = _int(self.vars[f"{side}.spacing"], style.spacing, -50, 50)
            style.scale_x = _int(self.vars[f"{side}.scale_x"], style.scale_x, 20, 400)
            style.bold = self.vars[f"{side}.bold"].get()

    def save_settings(self, announce: bool = True) -> None:
        self._collect_settings()
        try:
            path = self.cfg.save()
        except OSError as exc:
            self.write(f"could not save settings: {exc}\n", "error")
            return
        self._load_settings()          # reflect any clamping back into the widgets
        if announce:
            self.status.set(f"settings saved to {path.name}")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel("dual_subs", "A run is in progress. Quit anyway?"):
                return
        self.save_settings(announce=False)
        self.root.destroy()

    # -------------------------------------------------------------- input

    def _enable_drop(self, widget) -> None:
        try:
            from tkinterdnd2 import DND_FILES  # type: ignore
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    def _on_drop(self, event) -> None:
        raw, paths, buf, depth = event.data, [], "", 0
        for ch in raw:                       # tk quotes paths containing spaces
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == " " and depth == 0:
                if buf:
                    paths.append(buf)
                buf = ""
            else:
                buf += ch
        if buf:
            paths.append(buf)
        self._add([Path(p) for p in paths])

    def _accept_dropped_arguments(self) -> None:
        args = [Path(a) for a in sys.argv[1:] if not a.startswith("-")]
        if args:
            self._add(args)

    def add_files(self) -> None:
        chosen = filedialog.askopenfilenames(
            title="Choose videos",
            filetypes=[("Matroska", "*.mkv"), ("All files", "*.*")])
        self._add([Path(c) for c in chosen])

    def add_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Choose a folder")
        if chosen:
            self._add([Path(chosen)])

    def _add(self, targets: list[Path]) -> None:
        found = collect_videos(targets, self.cfg.video_exts,
                               self.vars["recursive"].get(), self.cfg.output_suffix)
        added = 0
        for v in found:
            if v not in self.videos:
                self.videos.append(v)
                self.listbox.insert("end", str(v))
                added += 1
        if not found and targets:
            self.write(f"nothing matching {'/'.join(self.cfg.video_exts)} in "
                       f"{', '.join(t.name for t in targets)}\n", "dim")
        skipped = len(found) - added
        self.status.set(f"{len(self.videos)} file(s) queued"
                        + (f", {skipped} already listed" if skipped else ""))
        if added:
            self._rescan()

    # ---------------------------------------------------- scanning the queue

    def _rescan(self) -> None:
        """Re-read the queue's tracks in the background.

        Each file costs one mkvmerge call, so a season takes a moment and a
        whole library takes longer. It runs off the UI thread and reports as
        it goes; changing the queue bumps a generation counter, which the
        running scan checks so it abandons work nobody is waiting for.
        """
        self.scan_generation += 1
        generation = self.scan_generation
        if not self.videos or self.tools is None:
            self.survey = survey_mod.Survey()
            self._apply_survey()
            return
        videos = list(self.videos)

        def work() -> None:
            result = survey_mod.survey(
                videos, self.tools,
                progress=lambda done, total: self.messages.put(
                    ("scan_progress", (generation, done, total))),
                should_stop=lambda: self.scan_generation != generation,
            )
            self.messages.put(("scan_done", (generation, result)))

        threading.Thread(target=work, daemon=True).start()

    def _apply_survey(self) -> None:
        """Push the scanned options into every track selector."""
        entries = {
            "audio": [""] + self.survey.audio_entries(),
            "subtitle": [""] + self.survey.subtitle_entries(),
        }
        options = {"audio": self.survey.audio, "subtitle": self.survey.subtitles}
        for kind, boxes in self.track_boxes.items():
            for box in boxes:
                try:
                    box.configure(values=entries[kind])
                except tk.TclError:
                    pass
        # Re-expand whatever is selected so it shows its description, and
        # leave a hand-typed spec exactly as typed.
        for name, kind in (("top_language", "subtitle"),
                           ("bottom_language", "subtitle"),
                           ("set_audio", "audio"),
                           ("set_subs", "subtitle")):
            var = self.vars.get(name)
            if var is None:
                continue
            spec = survey_mod.spec_of(var.get())
            if spec:
                var.set(survey_mod.entry_for(spec, options[kind]))

    def remove_selected(self) -> None:
        for i in sorted(self.listbox.curselection(), reverse=True):
            self.listbox.delete(i)
            del self.videos[i]
        self.status.set(f"{len(self.videos)} file(s) queued")
        self._rescan()

    def clear(self) -> None:
        self.listbox.delete(0, "end")
        self.videos.clear()
        self.status.set("")
        self._rescan()

    # ------------------------------------------------------------- output

    def write(self, text: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text, tag or "")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain(self) -> None:
        """Pull messages from the worker thread onto the UI thread."""
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self.write(payload[0], payload[1])
                elif kind == "progress":
                    self.progress["value"] = payload
                elif kind == "status":
                    self.status.set(payload)
                elif kind == "scan_progress":
                    generation, done, total = payload
                    if generation == self.scan_generation and done < total:
                        self.status.set(f"reading tracks... {done}/{total}")
                elif kind == "scan_done":
                    generation, result = payload
                    if generation == self.scan_generation:
                        self.survey = result
                        self._apply_survey()
                        note = result.summary()
                        self.status.set(f"{len(self.videos)} file(s) queued"
                                        + (f" — {note}" if note else ""))
                        for problem in result.failed[:5]:
                            self.write(f"  ! {problem}\n", "error")
                elif kind == "done":
                    self.run_button.state(["!disabled"])
                    self.progress["value"] = 0
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _post(self, text: str, tag: str | None = None) -> None:
        self.messages.put(("log", (text, tag)))

    # -------------------------------------------------------------- tools

    def _find_tools(self) -> None:
        try:
            self.tools = discover_tools(None, self.cfg.mkvtoolnix_dir or None)
            self.write(f"{self.tools.version}\n{self.tools.mkvmerge.parent}\n\n", "dim")
            if self.videos:
                self._rescan()
        except ToolError as exc:
            self.tools = None
            self.write(f"{exc}\n\n", "error")
            self.root.after(300, self._ask_for_tools)

    def _ask_for_tools(self) -> None:
        if not messagebox.askyesno(
                "MKVToolNix not found",
                "mkvmerge and mkvextract could not be found.\n\n"
                "Point me at the folder containing them?"):
            return
        chosen = filedialog.askdirectory(title="Folder containing mkvmerge.exe")
        if not chosen:
            return
        try:
            self.tools = discover_tools(chosen, None)
        except ToolError as exc:
            self.write(f"{exc}\n", "error")
            return
        self.cfg.mkvtoolnix_dir = chosen
        self.save_settings(announce=False)
        self.write(f"{self.tools.version}\nsaved to the settings file\n\n", "dim")

    # ----------------------------------------------------------- inspect

    def inspect(self) -> None:
        if not self._check_ready():
            return
        selection = self.listbox.curselection()
        targets = ([self.videos[i] for i in selection] if selection
                   else self.videos[:5])
        from . import discover as disc
        for video in targets:
            self.write(f"\n{video.name}\n")
            try:
                media = probe.probe(video, self.tools)
            except ProbeError as exc:
                self.write(f"  {exc}\n", "error")
                continue
            for t in media.tracks:
                self.write(f"  embedded  {t.describe()}\n", "dim")
            if not media.tracks:
                self.write("  embedded  (none)\n", "dim")
            for c in disc.find_candidates(video)[:8]:
                tag = langs.display(c.language) if c.language else "no language token"
                self.write(f"  on disk   {c.path.name}  ({tag})\n", "dim")

    def shorten_names(self) -> None:
        """Strip release tags from the queued files' names.

        Long paths are why: VLC on Windows cannot open a file past 260
        characters even with LongPathsEnabled set.
        """
        from .rename import apply_renames, plan_renames

        if not self.videos:
            self.status.set("add some files first")
            return
        plans = plan_renames(self.videos, style="episode")
        changing = [p for p in plans if p.changed]
        if not changing:
            self.write("\nthese names are already clean\n", "dim")
            return

        preview = "\n".join(
            f"{p.source.name}\n    -> {p.target.name}" for p in changing[:4])
        if len(changing) > 4:
            preview += f"\n... and {len(changing) - 4} more"
        if not messagebox.askokcancel(
                "Shorten names",
                f"Rename {len(changing)} file(s)?\n\n{preview}\n\n"
                "Sidecar subtitles follow, and this can be undone."):
            return

        count, problems = apply_renames(plans)
        for problem in problems:
            self.write(f"  ! {problem}\n", "error")
        for plan in changing:
            self.write(f"  {plan.target.name}\n", "ok")

        # The queue now points at names that no longer exist.
        renamed = {p.source: p.target for p in changing}
        self.videos = [renamed.get(v, v) for v in self.videos]
        self.listbox.delete(0, "end")
        for v in self.videos:
            self.listbox.insert("end", str(v))
        self._rescan()
        self.status.set(f"{count} renamed — undo from the command line with "
                        f"--undo-rename")

    def reencode(self) -> None:
        """Normalise the subtitle files beside the queued videos to UTF-8."""
        from . import discover as disc
        from .textio import reencode_file

        if not self.videos:
            self.status.set("add some files first")
            return
        hint = langs.normalize(self.cfg.bottom_language or
                               self.cfg.top_language or "") or None
        seen: set[Path] = set()
        for video in self.videos:
            for cand in disc.find_candidates(video):
                if cand.path in seen:
                    continue
                seen.add(cand.path)
                try:
                    was, changed = reencode_file(cand.path, language=hint)
                except OSError as exc:
                    self.write(f"  ! {cand.path.name}: {exc}\n", "error")
                    continue
                if changed:
                    self.write(f"  {cand.path.name}   {was} -> utf-8\n", "ok")
                else:
                    self.write(f"  {cand.path.name}   already utf-8\n", "dim")
        if not seen:
            self.write("no subtitle files found beside the queued videos\n", "dim")
        self.status.set(f"checked {len(seen)} subtitle file(s)")

    # --------------------------------------------------------------- run

    def _check_ready(self) -> bool:
        if self.tools is None:
            self._ask_for_tools()
            return self.tools is not None
        if not self.videos:
            self.status.set("add some files first")
            return False
        return True

    def run(self) -> None:
        if not self._check_ready():
            return
        self.save_settings(announce=False)
        top, bottom = self.cfg.top_language, self.cfg.bottom_language
        if not top or not bottom:
            messagebox.showinfo("dual_subs", "Both lines need a language.")
            return
        if top == bottom:
            messagebox.showinfo("dual_subs", "The two lines must differ.")
            return

        self.run_button.state(["disabled"])
        self.progress["maximum"] = len(self.videos)
        self.worker = threading.Thread(
            target=self._work,
            args=(list(self.videos), top, bottom, self.vars["overwrite"].get()),
            daemon=True)
        self.worker.start()

    def _work(self, videos, top, bottom, overwrite) -> None:
        top_spec, bot_spec = SideSpec.parse(top), SideSpec.parse(bottom)
        ok = failed = 0
        try:
            for i, video in enumerate(videos, 1):
                self.messages.put(("status", f"{i}/{len(videos)}  {video.name}"))
                self._post(f"\n[{i}/{len(videos)}] {video.name}\n")
                try:
                    result = process(video, top_spec, bot_spec, self.tools,
                                     self.cfg, overwrite=overwrite,
                                     log=lambda line: self._post(line + "\n", "dim"))
                except Exception:
                    failed += 1
                    self._post(traceback.format_exc(), "error")
                    continue
                if result.ok:
                    ok += 1
                    for path in result.written:
                        self._post(f"    -> {path.name}\n", "ok")
                else:
                    failed += 1
                    self._post(f"    {result.message}\n", "error")
                self.messages.put(("progress", i))
        finally:
            self.messages.put(("status", f"done - {ok} succeeded, {failed} failed"))
            self.messages.put(("done", None))


# ------------------------------------------------------------------ helpers

def _int(var: tk.Variable, fallback: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(str(var.get()).strip())))
    except (ValueError, TypeError):
        return fallback


def _label(choices, value):
    for label, val in choices:
        if val == (value or ""):
            return label
    return choices[0][0]


def _value(choices, label):
    for text, val in choices:
        if text == label:
            return val
    return choices[0][1]


def run() -> int:
    try:
        from tkinterdnd2 import TkinterDnD  # type: ignore
        root = TkinterDnD.Tk()
    except Exception:
        root = Tk()
    try:
        root.call("tk", "scaling", 1.3)
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0
