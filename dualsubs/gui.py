"""A window, for when the command line is more ceremony than the job deserves.

Pure tkinter, which ships with the Windows Python installer and bundles into a
PyInstaller build without extra work. If `tkinterdnd2` happens to be installed,
you can drop files straight onto the list; without it, dropping files onto the
.exe itself still works, because Windows passes them as arguments.

Everything the window shows is a field of Config, and every field is written
back to dual_subs.config.json when you run, save, or close. The command line
reads the same file, so the two stay in step.

Three rules shape the layout:

* Show what will happen before it happens. The queue lists, for every file,
  which track or file each line will come from, recomputed whenever a
  selector changes -- so "chi" quietly meaning Simplified on one release and
  Traditional on another is visible before a long remux, not after.
* Never freeze. Anything that touches a file runs on a worker thread, one job
  at a time, and a batch can be stopped between files.
* Only offer what applies. Options that do nothing in the current mode are
  greyed out rather than silently ignored.
"""

from __future__ import annotations

import copy
import os
import queue
import subprocess
import sys
import threading
import traceback
import webbrowser
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, colorchooser, filedialog, messagebox, ttk
import tkinter as tk
import tkinter.font as tkfont

from . import __version__, langs, probe, render, survey as survey_mod, worst
from .config import Config
from .pipeline import SideSpec, choose_source, collect_videos, process
from .probe import ProbeError
from .tools import ToolError, app_root, discover as discover_tools

TITLE = f"dual_subs {__version__}"
PROJECT_URL = "https://github.com/Zypre/Zippy-s-Subtitle-Manager"
PAD = 8
HINT = "#6b6b6b"

ALIGN_CHOICES = [
    ("match up cue pairs", "pairs"),
    ("pin one line to the other's timing (dubs)", "grid"),
    ("leave timing exactly as authored", "off"),
]
ANCHOR_CHOICES = [
    ("whichever matches the audio language", "auto"),
    ("the top line", "top"),
    ("the bottom line", "bottom"),
]
MODE_CHOICES = [
    ("sidecar", "Subtitle file beside the video",
     "Writes <video name>.ass next to the video in about a second. mpv and VLC "
     "load it automatically; the video itself is never touched."),
    ("mux", "New MKV with the dual track",
     "Copies the whole video into <name>.dual.mkv with the dual track added. "
     "Takes as long as copying the file; needed for TVs and phones that "
     "ignore loose subtitle files."),
    ("both", "Both", "The subtitle file and the new MKV."),
]
#: Sensible fallbacks when enumerating installed fonts is slow or unavailable.
FONT_FALLBACKS = ["Arial", "Segoe UI", "Tahoma", "Verdana",
                  "Microsoft YaHei", "SimHei", "SimSun", "KaiTi",
                  "Noto Sans CJK SC", "Source Han Sans SC"]

#: Shown in the appearance preview, keyed by language, until the queued
#: files have been read and the real worst moment can be shown instead.
SAMPLES = {
    "chi": "你要去哪儿？",
    "eng": "Where are you going?",
    "jpn": "どこへ行くの？",
    "kor": "어디 가?",
}
GENERIC_SAMPLE = "Sample subtitle line"

#: Per-side appearance fields that travel with a language when the two
#: lines are swapped. Margin is positional, so it stays where it is.
SWAPPABLE = ("font", "size", "spacing", "scale_x", "bold", "italic",
             "primary", "outline")

#: Every ttk.Combobox shares one popdown style; this picks an easy font.
LOG_FONT = ("Consolas", 9) if os.name == "nt" else ("DejaVu Sans Mono", 9)


class Tooltip:
    """Hover help. Kept short; the long story is in the README."""

    def __init__(self, widget, text: str | None = None, delay: int = 550,
                 provider=None) -> None:
        self.widget, self.text, self.delay = widget, text, delay
        #: Called with (x, y) to compute text on the fly, for tree cells.
        self.provider = provider
        self.tip: tk.Toplevel | None = None
        self.job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Motion>", self._moved, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        self._last_text = None

    def _moved(self, event) -> None:
        if self.provider is None:
            return
        text = self.provider(event.x, event.y)
        if text != self._last_text:
            self._hide()
            self._last_text = text
            if text:
                self._schedule(event)

    def _schedule(self, event=None) -> None:
        self._cancel()
        self.job = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None

    def _show(self) -> None:
        text = self._last_text if self.provider else self.text
        if not text:
            return
        x = self.widget.winfo_pointerx() + 14
        y = self.widget.winfo_pointery() + 18
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=text, justify="left", background="#ffffe6",
                 foreground="#222222", relief="solid", borderwidth=1,
                 wraplength=420, padx=6, pady=4).pack()

    def _hide(self, event=None) -> None:
        self._cancel()
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None
        if event is not None and event.type == tk.EventType.Leave:
            self._last_text = None


def tip(widget, text: str):
    Tooltip(widget, text)
    return widget


class App:
    def __init__(self, root: Tk) -> None:
        self.root = root
        try:
            self.cfg = Config.load()
            self.cfg_problem = ""
        except (OSError, ValueError) as exc:
            self.cfg = Config()
            self.cfg_problem = str(exc)
        self.tools = None
        self.videos: list[Path] = []
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.vars: dict[str, tk.Variable] = {}
        self.survey = survey_mod.Survey()
        self.scan_generation = 0
        self.scanning = False
        self.plan_generation = 0
        self.plan_job = None
        self.preview_job = None
        #: The running job's thread, its name, and the flag that stops it.
        self.worker: threading.Thread | None = None
        self.job_name = ""
        self.stop_event = threading.Event()
        #: Widgets that start a job or change the queue; disabled while busy.
        self.busy_widgets: list = []
        self.busy_menu_items: list[tuple[tk.Menu, str]] = []
        #: Comboboxes whose contents come from the queue, by option kind.
        self.track_boxes: dict[str, list] = {"audio": [], "subtitle": [],
                                             "subtitle_embedded": []}
        self.ratio_cache: dict[str, float] = {}
        #: Each queued video's subtitle text, read once in the background.
        self.texts: dict[Path, worst.FileText] = {}
        #: Subtitle files on disk, which are cheap to read; keyed by path.
        self.sub_texts: dict[Path, tuple] = {}
        self.text_thread: threading.Thread | None = None
        self.text_stop = threading.Event()
        self.text_progress = (0, 0)
        #: The moment the preview shows, and the bookkeeping to find it.
        self.worst: worst.Moment | None = None
        self.worst_generation = 0
        self.worst_job = None

        root.title(TITLE)
        root.minsize(760, 560)
        self._style()
        self._build()
        self._load_settings()
        self._restore_geometry()
        self._bind_keys()
        self._update_states()
        self._find_tools()
        self._accept_dropped_arguments()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.bind("<Destroy>", self._on_destroy, add="+")
        self.drain_job = root.after(80, self._drain)
        if self.cfg_problem:
            self.write(f"could not read the settings file, using defaults: "
                       f"{self.cfg_problem}\n", "error")

    # ------------------------------------------------------------- layout

    def _style(self) -> None:
        style = ttk.Style(self.root)
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        style.configure("Treeview", rowheight=line + 6)
        style.configure("Hint.TLabel", foreground=HINT)
        style.configure("Run.TButton", padding=(14, 4))

    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=3)
        root.rowconfigure(3, weight=2)
        self._build_menu()

        bar = ttk.Frame(root, padding=(PAD, PAD, PAD, 0))
        bar.grid(row=0, column=0, sticky="ew")
        for text, command, help_ in (
                ("Add files...", self.add_files, "Queue video files (Ctrl+O)."),
                ("Add folder...", self.add_folder,
                 "Queue every video in a folder (Ctrl+Shift+O).")):
            b = tip(ttk.Button(bar, text=text, command=command), help_)
            b.pack(side="left", padx=(0, 6))
            self.busy_widgets.append(b)
        self.vars["recursive"] = BooleanVar(value=False)
        tip(ttk.Checkbutton(bar, text="include subfolders",
                            variable=self.vars["recursive"]),
            "When adding a folder, also take the videos in every folder "
            "inside it.").pack(side="left", padx=(PAD, 0))

        tools = ttk.Menubutton(bar, text="Tools")
        tools["menu"] = self._tools_menu(tools)
        tools.pack(side="right")
        self.busy_widgets.append(tools)
        for text, command, help_ in (
                ("Clear", self.clear, "Empty the queue. Nothing on disk changes."),
                ("Remove", self.remove_selected,
                 "Take the selected files out of the queue (Delete). "
                 "Nothing on disk changes.")):
            b = tip(ttk.Button(bar, text=text, command=command), help_)
            b.pack(side="right", padx=(0, 6))
            self.busy_widgets.append(b)

        self._build_queue(root)

        book = ttk.Notebook(root, padding=(PAD, PAD, PAD, 0))
        book.grid(row=2, column=0, sticky="ew")
        book.add(self._tab_subtitles(book), text="Subtitles")
        book.add(self._tab_appearance(book), text="Appearance")
        book.add(self._tab_timing(book), text="Timing")
        book.add(self._tab_defaults(book), text="Default tracks")
        self.book = book

        logframe = ttk.Frame(root, padding=(PAD, PAD, PAD, 0))
        logframe.grid(row=3, column=0, sticky="nsew")
        logframe.columnconfigure(0, weight=1)
        logframe.rowconfigure(0, weight=1)
        self.log = tk.Text(logframe, height=7, wrap="word", state="disabled",
                           font=LOG_FONT, borderwidth=1, relief="sunken")
        self.log.grid(row=0, column=0, sticky="nsew")
        logscroll = ttk.Scrollbar(logframe, orient="vertical", command=self.log.yview)
        logscroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=logscroll.set)
        self.log.tag_configure("error", foreground="#b00020")
        self.log.tag_configure("ok", foreground="#0a7d28")
        self.log.tag_configure("dim", foreground="#777777")
        self.log.tag_configure("head", font=(LOG_FONT[0], LOG_FONT[1], "bold"))
        log_menu = tk.Menu(self.log, tearoff=False)
        log_menu.add_command(label="Copy", command=lambda: self.log.event_generate("<<Copy>>"))
        log_menu.add_command(label="Select all",
                             command=lambda: self.log.tag_add("sel", "1.0", "end"))
        log_menu.add_separator()
        log_menu.add_command(label="Clear log", command=self.clear_log)
        self.log.bind("<Button-3>", lambda e: log_menu.tk_popup(e.x_root, e.y_root))

        foot = ttk.Frame(root, padding=PAD)
        foot.grid(row=4, column=0, sticky="ew")
        foot.columnconfigure(0, weight=1)
        self.status = StringVar(value="Add videos to get started: drag them "
                                      "onto the program's icon, or use Add files.")
        ttk.Label(foot, textvariable=self.status, style="Hint.TLabel"
                  ).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(foot, mode="determinate", length=160)
        self.progress.grid(row=0, column=1, padx=(PAD, PAD))
        self.run_button = ttk.Button(foot, text="Run", style="Run.TButton",
                                     command=self.run_or_stop)
        self.run_button.grid(row=0, column=2)
        tip(self.run_button, "Process every queued file with these settings "
                             "(Ctrl+Enter). While running, this stops the "
                             "batch after the current file.")

    def _build_menu(self) -> None:
        bar = tk.Menu(self.root, tearoff=False)
        file_menu = tk.Menu(bar, tearoff=False)
        self._busy_command(file_menu, "Add files...", self.add_files, "Ctrl+O")
        self._busy_command(file_menu, "Add folder...", self.add_folder, "Ctrl+Shift+O")
        file_menu.add_separator()
        self._busy_command(file_menu, "Remove selected", self.remove_selected, "Del")
        self._busy_command(file_menu, "Clear queue", self.clear)
        file_menu.add_separator()
        file_menu.add_command(label="Save settings", command=self.save_settings,
                              accelerator="Ctrl+S")
        file_menu.add_command(label="Show settings file", command=self.show_settings_file)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self._on_close, accelerator="Ctrl+Q")
        bar.add_cascade(label="File", menu=file_menu)
        bar.add_cascade(label="Tools", menu=self._tools_menu(bar))
        help_menu = tk.Menu(bar, tearoff=False)
        help_menu.add_command(label="Read me", command=self.open_readme)
        help_menu.add_command(label="Project page", command=lambda: webbrowser.open(PROJECT_URL))
        help_menu.add_separator()
        help_menu.add_command(label=f"About dual_subs {__version__}", command=self.about)
        bar.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=bar)

    def _tools_menu(self, parent) -> tk.Menu:
        menu = tk.Menu(parent, tearoff=False)
        self._busy_command(menu, "Inspect selected files", self.inspect, "Ctrl+I")
        self._busy_command(menu, "Set default tracks now", self.set_flags)
        menu.add_separator()
        self._busy_command(menu, "Shorten file names...", self.shorten_names)
        self._busy_command(menu, "Undo last rename...", self.undo_rename)
        self._busy_command(menu, "Convert subtitle files to UTF-8...", self.reencode)
        menu.add_separator()
        self._busy_command(menu, "Locate MKVToolNix...", self._ask_for_tools)
        return menu

    def _busy_command(self, menu: tk.Menu, label: str, command, accel: str = "") -> None:
        menu.add_command(label=label, command=command, accelerator=accel)
        self.busy_menu_items.append((menu, label))

    def _build_queue(self, root) -> None:
        frame = ttk.Frame(root, padding=(PAD, 6, PAD, 0))
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame, columns=("top", "bottom", "result"),
                                 selectmode="extended", height=6)
        for col, text, width in (("#0", "File", 300), ("top", "Top line", 190),
                                 ("bottom", "Bottom line", 190),
                                 ("result", "Result", 170)):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=width, minwidth=80, stretch=True, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        hscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        hscroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=hscroll.set)
        self.tree.tag_configure("error", foreground="#b00020")
        self.tree.tag_configure("ok", foreground="#0a7d28")
        self.tree.tag_configure("warn", foreground="#a34f00")
        self.tree.tag_configure("folder", foreground="#444444")
        self._enable_drop(self.tree)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._on_tree_menu)
        Tooltip(self.tree, provider=self._cell_text, delay=400)

        self.tree_menu = tk.Menu(self.tree, tearoff=False)
        self.tree_menu.add_command(label="Inspect", command=self.inspect)
        self.tree_menu.add_command(label="Open containing folder",
                                   command=self.open_folder)
        self.tree_menu.add_separator()
        self.tree_menu.add_command(label="Remove from queue",
                                   command=self.remove_selected)

    # ------------------------------------------------------------ the tabs

    def _tab_subtitles(self, parent) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=PAD)
        tab.columnconfigure(1, weight=1)
        tab.columnconfigure(5, weight=1)

        self.vars["top_language"] = StringVar()
        self.vars["bottom_language"] = StringVar()
        spec_help = ("Pick from what the queued files contain, or type one: a "
                     "language code (chi), words to tell same-language tracks "
                     "apart (chi:traditional), an embedded track number (#4), "
                     "or a subtitle file's path.")
        for col, (side, label) in enumerate((("top", "Top line"),
                                             ("bottom", "Bottom line"))):
            c = col * 4
            ttk.Label(tab, text=label).grid(row=0, column=c, sticky="w")
            box = ttk.Combobox(tab, textvariable=self.vars[f"{side}_language"],
                               values=[], width=30)
            box.grid(row=0, column=c + 1, sticky="ew", padx=(6, 2))
            tip(box, spec_help)
            self.track_boxes["subtitle"].append(box)
            browse = ttk.Button(tab, text="...", width=3,
                                command=lambda s=side: self.choose_subtitle_file(s))
            browse.grid(row=0, column=c + 2, sticky="w")
            tip(browse, "Use one particular subtitle file for this line. Best "
                        "with a single video queued, since every file gets it.")
            if side == "top":
                swap = ttk.Button(tab, text="⇅ Swap", command=self.swap_sides)
                swap.grid(row=0, column=3, padx=PAD)
                tip(swap, "Put the bottom language on top and vice versa. Each "
                          "language keeps its font, size and colour.")
            self.vars[f"{side}_language"].trace_add("write", self._schedule_plan)
            self.vars[f"{side}_language"].trace_add("write", self._schedule_preview)
            self.vars[f"{side}_language"].trace_add("write", self._schedule_worst)

        out = ttk.LabelFrame(tab, text="Output", padding=(PAD, 4))
        out.grid(row=1, column=0, columnspan=7, sticky="ew", pady=(PAD, 0))
        self.vars["output_mode"] = StringVar()
        for i, (value, label, help_) in enumerate(MODE_CHOICES):
            tip(ttk.Radiobutton(out, text=label, value=value,
                                variable=self.vars["output_mode"],
                                command=self._update_states),
                help_).grid(row=0, column=i, sticky="w", padx=(0, PAD * 2))
        opts = ttk.Frame(out)
        opts.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.vars["overwrite"] = BooleanVar(value=False)
        self.vars["stash_originals"] = BooleanVar()
        tip(ttk.Checkbutton(opts, text="replace output from an earlier run",
                            variable=self.vars["overwrite"]),
            "Without this, a file whose output already exists is skipped. "
            "A subtitle file this program did not write is never replaced.").pack(side="left")
        self.stash_check = tip(
            ttk.Checkbutton(opts, text="move originals to _originals\\ after a remux",
                            variable=self.vars["stash_originals"]),
            "Once the new MKV is written, move the source video and any "
            "subtitle files it used into an _originals folder, so the folder "
            "holds only the finished versions.")
        self.stash_check.pack(side="left", padx=(PAD * 2, 0))

        self.vars["t2s"] = BooleanVar()
        tip(ttk.Checkbutton(
            tab, text="Convert traditional Chinese to simplified  (繁體 → 简体)",
            variable=self.vars["t2s"]),
            "Each line is checked and converted only if it really is "
            "traditional. English and already-simplified text are left as "
            "they are, so it is safe to leave on."
        ).grid(row=2, column=0, columnspan=7, sticky="w", pady=(PAD, 0))

        self.plan_hint = StringVar(value="Queue some videos and the list above "
                                         "shows which track or file each line "
                                         "will come from.")
        ttk.Label(tab, textvariable=self.plan_hint, style="Hint.TLabel",
                  wraplength=700, justify="left"
                  ).grid(row=3, column=0, columnspan=7, sticky="w", pady=(PAD, 0))
        return tab

    def _tab_appearance(self, parent) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=PAD)
        tab.columnconfigure(11, weight=1)
        fonts = self._font_list()

        headers = [("", ""), ("Font", ""),
                   ("Size", "Text height, in pixels of a 1080p frame."),
                   ("Margin", "Distance from the bottom of the frame, in pixels "
                              "of a 1080p frame. Scales with any resolution."),
                   ("Spacing", "Extra pixels between characters; negative "
                               "tightens. Not shown in the preview."),
                   ("Width %", "Horizontal stretch; under 100 squeezes long "
                               "lines. Not shown in the preview."),
                   ("Bold", ""), ("Italic", ""), ("Colour", ""),
                   ("Outline", "Thickness of the dark edge around the text, "
                               "which keeps it readable on bright scenes.")]
        for c, (text, help_) in enumerate(headers):
            label = ttk.Label(tab, text=text, style="Hint.TLabel")
            label.grid(row=0, column=c, sticky="w", padx=(0, 6))
            if help_:
                tip(label, help_)

        for r, side in enumerate(("top", "bottom"), start=1):
            ttk.Label(tab, text=side.capitalize()).grid(row=r, column=0, sticky="w", pady=2)
            self.vars[f"{side}.font"] = StringVar()
            ttk.Combobox(tab, textvariable=self.vars[f"{side}.font"],
                         values=fonts, width=20).grid(row=r, column=1, padx=(0, 6))
            for c, (key, low, high, step) in enumerate(
                    (("size", 6, 400, 1), ("margin_v", 0, 1000, 2),
                     ("spacing", -50, 50, 1), ("scale_x", 20, 400, 5)), start=2):
                self.vars[f"{side}.{key}"] = StringVar()
                ttk.Spinbox(tab, textvariable=self.vars[f"{side}.{key}"],
                            from_=low, to=high, width=5, increment=step
                            ).grid(row=r, column=c, padx=(0, 6))
            for c, key in ((6, "bold"), (7, "italic")):
                self.vars[f"{side}.{key}"] = BooleanVar()
                ttk.Checkbutton(tab, variable=self.vars[f"{side}.{key}"]
                                ).grid(row=r, column=c, sticky="w")
            self.vars[f"{side}.primary"] = StringVar()
            swatch = tk.Button(tab, width=3, relief="groove", borderwidth=2,
                               command=lambda s=side: self.choose_colour(s))
            swatch.grid(row=r, column=8, sticky="w", padx=(0, 6))
            tip(swatch, "Text colour.")
            setattr(self, f"{side}_swatch", swatch)
            self.vars[f"{side}.outline"] = StringVar()
            ttk.Spinbox(tab, textvariable=self.vars[f"{side}.outline"],
                        from_=0, to=10, width=4, increment=0.5
                        ).grid(row=r, column=9, padx=(0, 6))

        self.vars["layout.auto_lift"] = BooleanVar()
        self.vars["preview_worst"] = BooleanVar(value=True)
        row = ttk.Frame(tab)
        row.grid(row=3, column=0, columnspan=12, sticky="w", pady=(6, 4))
        tip(ttk.Checkbutton(row, text="lift the top line over a bottom line "
                                      "that takes two rows",
                            variable=self.vars["layout.auto_lift"]),
            "When the bottom subtitle wraps or has two lines, the top one is "
            "raised by a row for that moment so the two never overlap.").pack(side="left")
        tip(ttk.Checkbutton(row, text="preview the worst moment in the queued files",
                            variable=self.vars["preview_worst"]),
            "Show the moment where the most rows of text are on screen at "
            "once, taken from the files themselves. Each file is read once, "
            "in the background, when it is added; changing languages or "
            "fonts afterwards costs nothing.").pack(side="left", padx=(PAD * 2, 0))

        self.preview = tk.Canvas(tab, height=170, highlightthickness=0,
                                 background="#1e2a36")
        self.preview.grid(row=4, column=0, columnspan=12, sticky="ew")
        self.preview.bind("<Configure>", lambda e: self._schedule_preview())
        self.preview_caption = StringVar()
        ttk.Label(tab, textvariable=self.preview_caption, style="Hint.TLabel"
                  ).grid(row=5, column=0, columnspan=12, sticky="w", pady=(2, 0))
        #: Settings that change how many rows a line takes, so which moment
        #: is the worst -- as opposed to colour or margin, which only move it.
        wrapping = {"top.size", "top.scale_x", "top.spacing", "bottom.size",
                    "bottom.scale_x", "bottom.spacing", "preview_worst"}
        for name, var in self.vars.items():
            if name.startswith(("top.", "bottom.", "layout.", "preview_")):
                var.trace_add("write", self._schedule_preview)
            if name in wrapping:
                var.trace_add("write", self._schedule_worst)
        return tab

    def _tab_timing(self, parent) -> ttk.Frame:
        tab = ttk.Frame(parent, padding=PAD)
        tab.columnconfigure(2, weight=1)
        self.vars["auto_sync"] = BooleanVar()
        self.vars["allow_rate_fix"] = BooleanVar()
        self.vars["snap_window_ms"] = StringVar()
        self.vars["align_mode"] = StringVar()
        self.vars["anchor"] = StringVar()
        self.vars["grid_window_ms"] = StringVar()

        tip(ttk.Checkbutton(tab, text="Retime subtitle files from disk to match the video",
                            variable=self.vars["auto_sync"], command=self._update_states),
            "A subtitle file from another release can be a few seconds early or "
            "late. Its timing is compared with a track inside the video and "
            "shifted only when the match is convincing."
            ).grid(row=0, column=0, columnspan=3, sticky="w")
        self.rate_check = tip(
            ttk.Checkbutton(tab, text="...including a frame-rate fix "
                                      "(25 fps subtitles on a 23.976 fps video)",
                            variable=self.vars["allow_rate_fix"]),
            "European (PAL) releases run 4% fast, so their subtitles drift "
            "further out as the film goes on. This corrects that too.")
        self.rate_check.grid(row=1, column=0, columnspan=3, sticky="w", padx=(20, 0))

        ttk.Separator(tab, orient="horizontal").grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(8, 6))

        ttk.Label(tab, text="Keep the two lines in step").grid(row=3, column=0, sticky="w")
        align = ttk.Combobox(tab, textvariable=self.vars["align_mode"], state="readonly",
                             values=[c[0] for c in ALIGN_CHOICES], width=38)
        align.grid(row=3, column=1, columnspan=2, sticky="w", padx=(6, 0))
        align.bind("<<ComboboxSelected>>", lambda e: self._update_states())
        tip(align, "Match up cue pairs: two lines saying the same thing start "
                   "and end together instead of a fraction of a second apart.\n\n"
                   "Pin to the other's timing: for dubs, where the two "
                   "languages split sentences differently, so there are no "
                   "pairs to match.\n\nLeave as authored: change nothing.")

        self.timing_rows = []
        for r, (label, var, low, high, step, unit, help_) in enumerate((
                ("treat as simultaneous within", "snap_window_ms", 0, 2000, 50, "ms",
                 "Two cue edges this close are taken as the same moment. Half a "
                 "second covers subtitler jitter without merging different lines."),
                ("pull cue edges by up to", "grid_window_ms", 0, 4000, 100, "ms",
                 "How far a cue may be moved onto the other line's timing."),
        ), start=4):
            lab = ttk.Label(tab, text=label)
            lab.grid(row=r, column=0, sticky="w", padx=(20, 0), pady=(4, 0))
            spin = ttk.Spinbox(tab, textvariable=self.vars[var], from_=low, to=high,
                               increment=step, width=7)
            spin.grid(row=r, column=1, sticky="w", padx=(6, 6), pady=(4, 0))
            tip(spin, help_)
            ttk.Label(tab, text=unit).grid(row=r, column=2, sticky="w", pady=(4, 0))
            self.timing_rows.append((lab, spin))

        lab = ttk.Label(tab, text="line that keeps its own timing")
        lab.grid(row=6, column=0, sticky="w", padx=(20, 0), pady=(4, 0))
        anchor = ttk.Combobox(tab, textvariable=self.vars["anchor"], state="readonly",
                              values=[c[0] for c in ANCHOR_CHOICES], width=38)
        anchor.grid(row=6, column=1, columnspan=2, sticky="w", padx=(6, 0), pady=(4, 0))
        tip(anchor, "The other line is moved onto this one's timing, which "
                    "takes it slightly away from its own audio -- so keep the "
                    "language you are listening to.")
        self.anchor_row = (lab, anchor)

        ttk.Label(tab, style="Hint.TLabel", wraplength=640, justify="left",
                  text="Two subtitlers rarely agree to the centisecond, so the "
                       "same sentence can appear a fifth of a second apart and "
                       "seem to flicker. Hover over any setting for details."
                  ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(10, 0))
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
        tip(audio_box, "The audio a player starts with. Blank leaves it alone.")
        ttk.Label(tab, text="Default subtitles").grid(row=1, column=0, sticky="w",
                                                      pady=(6, 0))
        subs_box = ttk.Combobox(tab, textvariable=self.vars["set_subs"],
                                values=[], width=46)
        subs_box.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(6, 0))
        tip(subs_box, "The subtitle track a player starts with. A plain "
                      "language picks the main dialogue track, never "
                      "\"Signs & Songs\" or commentary. Blank leaves it alone.")
        self.track_boxes["audio"].append(audio_box)
        self.track_boxes["subtitle_embedded"].append(subs_box)

        self.dual_check = tip(
            ttk.Checkbutton(tab, text="make the new dual track the default subtitle",
                            variable=self.vars["set_dual"], command=self._update_states),
            "Only for a remux: in subtitle-file mode the dual track is not "
            "inside the video.")
        self.dual_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(tab, text="turn subtitles off by default",
                        variable=self.vars["subs_off"], command=self._update_states
                        ).grid(row=3, column=0, columnspan=2, sticky="w")
        self.subs_box = subs_box

        row = ttk.Frame(tab)
        row.grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        button = ttk.Button(row, text="Set flags now", command=self.set_flags)
        button.pack(side="left")
        self.busy_widgets.append(button)
        ttk.Label(row, style="Hint.TLabel",
                  text="  edits the file header only -- a moment, whatever the size"
                  ).pack(side="left")
        ttk.Label(tab, style="Hint.TLabel", wraplength=640, justify="left",
                  text="Run applies these too: to the new MKV after a remux, or "
                       "straight to the original file in subtitle-file mode. "
                       "Leave everything blank to leave the flags as they are."
                  ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        return tab

    def _font_list(self) -> list[str]:
        try:
            found = sorted({f for f in tkfont.families() if not f.startswith("@")})
        except tk.TclError:
            found = []
        return found or FONT_FALLBACKS

    # ----------------------------------------------------- enabling/disabling

    def _update_states(self, *_) -> None:
        """Grey out whatever cannot apply in the current mode."""
        mode = self.vars["output_mode"].get()
        remux = mode in ("mux", "both")
        align = _value(ALIGN_CHOICES, self.vars["align_mode"].get())
        _enable(self.stash_check, remux)
        _enable(self.dual_check, remux)
        _enable(self.rate_check, self.vars["auto_sync"].get())
        _enable(self.subs_box, not (self.vars["subs_off"].get()
                                    or (remux and self.vars["set_dual"].get())))
        for widgets, on in ((self.timing_rows[0], align != "off"),
                            (self.timing_rows[1], align == "grid"),
                            (self.anchor_row, align != "off")):
            for w in widgets:
                _enable(w, on)

        busy = self.worker is not None and self.worker.is_alive()
        for w in self.busy_widgets:
            _enable(w, not busy)
        for menu, label in self.busy_menu_items:
            try:
                menu.entryconfigure(label, state="disabled" if busy else "normal")
            except tk.TclError:
                pass
        self.run_button.configure(text="Stop" if busy else "Run")

    # ---------------------------------------------------------- settings io

    def _load_settings(self) -> None:
        c = self.cfg
        options = self.survey.subtitles
        self.vars["top_language"].set(survey_mod.entry_for(c.top_language or "eng", options))
        self.vars["bottom_language"].set(survey_mod.entry_for(c.bottom_language or "chi", options))
        mode = c.output_mode if c.output_mode in ("sidecar", "mux", "both") else "mux"
        self.vars["output_mode"].set(mode)
        self.vars["stash_originals"].set(c.stash_originals)
        self.vars["t2s"].set(bool(c.top_convert or c.bottom_convert))
        self.vars["auto_sync"].set(c.auto_sync)
        self.vars["allow_rate_fix"].set(c.allow_rate_fix)
        self.vars["snap_window_ms"].set(str(c.snap_window_ms))
        align = c.align_mode if c.snap_sides else "off"
        self.vars["align_mode"].set(_label(ALIGN_CHOICES, align))
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
            self.vars[f"{side}.italic"].set(style.italic)
            self.vars[f"{side}.primary"].set(style.primary)
            self.vars[f"{side}.outline"].set(_num_text(style.outline))
        gui = c.gui or {}
        self.vars["recursive"].set(bool(gui.get("recursive", False)))
        self.vars["preview_worst"].set(bool(gui.get("preview_worst", True)))
        self._paint_swatches()

    def _collect_settings(self) -> None:
        """Pull every widget back into the Config. Bad numbers keep the old value."""
        c = self.cfg
        c.top_language = survey_mod.spec_of(self.vars["top_language"].get())
        c.bottom_language = survey_mod.spec_of(self.vars["bottom_language"].get())
        c.output_mode = self.vars["output_mode"].get() or "mux"
        c.stash_originals = self.vars["stash_originals"].get()
        convert = "auto" if self.vars["t2s"].get() else ""
        c.top_convert = c.bottom_convert = convert
        c.auto_sync = self.vars["auto_sync"].get()
        c.allow_rate_fix = self.vars["allow_rate_fix"].get()
        c.snap_window_ms = _int(self.vars["snap_window_ms"], c.snap_window_ms, 0, 5000)
        align = _value(ALIGN_CHOICES, self.vars["align_mode"].get())
        c.snap_sides = align != "off"
        if align != "off":
            c.align_mode = align       # "off" is stored as snap_sides=False
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
            style.italic = self.vars[f"{side}.italic"].get()
            if _ass_to_hex(self.vars[f"{side}.primary"].get()):
                style.primary = self.vars[f"{side}.primary"].get()
            style.outline = _float(self.vars[f"{side}.outline"], style.outline, 0, 10)
        c.gui = dict(c.gui or {},
                     recursive=self.vars["recursive"].get(),
                     preview_worst=self.vars["preview_worst"].get(),
                     geometry=self.root.geometry())

    def save_settings(self, announce: bool = True) -> bool:
        self._collect_settings()
        try:
            path = self.cfg.save()
        except OSError as exc:
            self.write(f"could not save settings: {exc}\n", "error")
            return False
        self._load_settings()          # reflect any clamping back into the widgets
        if announce:
            self.status.set(f"settings saved to {path.name}")
        return True

    def _restore_geometry(self) -> None:
        geometry = (self.cfg.gui or {}).get("geometry", "")
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        try:
            size, _, _ = geometry.partition("+")
            w, h = (int(v) for v in size.split("x"))
        except ValueError:
            w, h = 1000, 780
        # Never open bigger than the screen, whatever was saved on another one.
        w, h = min(w, screen_w - 40), min(h, screen_h - 80)
        self.root.geometry(f"{max(w, 760)}x{max(h, 560)}")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel(
                    TITLE, f"{self.job_name.capitalize()} is still running. Quit anyway?\n\n"
                    "A file being written right now may be left incomplete."):
                return
        self.save_settings(announce=False)
        self._pause_text_scan()           # or mkvextract outlives the window
        self.root.destroy()

    def _on_destroy(self, event) -> None:
        """Cancel pending timers, which would otherwise fire into a window
        that no longer exists."""
        if event.widget is not self.root:
            return
        self.text_stop.set()
        for job in (self.drain_job, self.plan_job, self.worst_job, self.preview_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass

    def _bind_keys(self) -> None:
        r = self.root
        r.bind_all("<Control-o>", lambda e: self._when_idle(self.add_files))
        r.bind_all("<Control-O>", lambda e: self._when_idle(self.add_folder))
        r.bind_all("<Control-Shift-o>", lambda e: self._when_idle(self.add_folder))
        r.bind_all("<Control-s>", lambda e: self.save_settings())
        r.bind_all("<Control-q>", lambda e: self._on_close())
        r.bind_all("<Control-i>", lambda e: self._when_idle(self.inspect))
        r.bind_all("<Control-Return>", lambda e: self.run_or_stop())
        self.tree.bind("<Delete>", lambda e: self._when_idle(self.remove_selected))
        self.tree.bind("<Control-a>", lambda e: self.tree.selection_set(self.tree.get_children()))

    def _when_idle(self, action) -> None:
        if not (self.worker and self.worker.is_alive()):
            action()

    # -------------------------------------------------------------- input

    def _enable_drop(self, widget) -> None:
        try:
            from tkinterdnd2 import DND_FILES  # type: ignore
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    def _on_drop(self, event) -> None:
        if self.worker and self.worker.is_alive():
            self.status.set("wait for the current job to finish before adding files")
            return
        self._add([Path(p) for p in self.root.tk.splitlist(event.data)])

    def _accept_dropped_arguments(self) -> None:
        args = [Path(a) for a in sys.argv[1:] if not a.startswith("-")]
        if args:
            self._add(args)

    def add_files(self) -> None:
        chosen = filedialog.askopenfilenames(
            title="Choose videos", initialdir=self._last_dir(),
            filetypes=[("Matroska video", "*.mkv"), ("All files", "*.*")])
        if chosen:
            self._remember_dir(Path(chosen[0]).parent)
            self._add([Path(c) for c in chosen])

    def add_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Choose a folder",
                                         initialdir=self._last_dir())
        if chosen:
            self._remember_dir(Path(chosen))
            self._add([Path(chosen)])

    def _last_dir(self) -> str:
        folder = (self.cfg.gui or {}).get("last_dir", "")
        return folder if folder and Path(folder).is_dir() else ""

    def _remember_dir(self, folder: Path) -> None:
        self.cfg.gui = dict(self.cfg.gui or {}, last_dir=str(folder))

    def _add(self, targets: list[Path]) -> None:
        found = collect_videos(targets, self.cfg.video_exts,
                               self.vars["recursive"].get(), self.cfg.output_suffix)
        known = set(self.videos)
        fresh = [v for v in found if v not in known]
        self.videos.extend(fresh)
        if not found and targets:
            names = ", ".join(t.name or str(t) for t in targets)
            hint = ("" if self.vars["recursive"].get()
                    or not any(t.is_dir() for t in targets)
                    else " (tick \"include subfolders\" to look deeper)")
            self.write(f"no {'/'.join(self.cfg.video_exts)} files in {names}{hint}\n", "dim")
        self._refresh_tree()
        skipped = len(found) - len(fresh)
        self.status.set(f"{len(self.videos)} file(s) queued"
                        + (f", {skipped} already listed" if skipped else ""))
        if fresh:
            self._rescan()

    # --------------------------------------------------------------- queue

    def _refresh_tree(self) -> None:
        """Rebuild the queue, grouped by folder."""
        self.tree.delete(*self.tree.get_children())
        folders: dict[Path, str] = {}
        for video in self.videos:
            folder = video.parent
            if folder not in folders:
                iid = f"dir:{folder}"
                label = f"{folder.name or folder}    — {folder}"
                self.tree.insert("", "end", iid=iid, text=label, open=True,
                                 tags=("folder",))
                folders[folder] = iid
            self.tree.insert(folders[folder], "end", iid=str(video), text=video.name,
                             values=("", "", ""))
        for folder, iid in folders.items():
            children = self.tree.get_children(iid)
            shown = _distinct_parts([Path(c).name for c in children])
            for child, text in zip(children, shown):
                self.tree.item(child, text=text)
            self.tree.item(iid, values=("", "", f"{len(children)} file(s)"))
        queued = set(self.videos)
        self.texts = {v: t for v, t in self.texts.items() if v in queued}
        self._schedule_plan()
        self._schedule_worst()

    def _selected_videos(self) -> list[Path]:
        chosen: list[Path] = []
        for iid in self.tree.selection():
            if iid.startswith("dir:"):
                chosen += [Path(c) for c in self.tree.get_children(iid)]
            else:
                chosen.append(Path(iid))
        seen: set[Path] = set()
        return [v for v in chosen if not (v in seen or seen.add(v))]

    def remove_selected(self) -> None:
        doomed = set(self._selected_videos())
        if not doomed:
            self.status.set("select files in the queue first")
            return
        self.videos = [v for v in self.videos if v not in doomed]
        self._refresh_tree()
        self.status.set(f"{len(self.videos)} file(s) queued")
        self._rescan()

    def clear(self) -> None:
        self.videos.clear()
        self._refresh_tree()
        self.status.set("queue cleared")
        self._rescan()

    def _set_cell(self, video: Path, column: str, text: str, tag: str = "") -> None:
        iid = str(video)
        if not self.tree.exists(iid):
            return
        self.tree.set(iid, column, text)
        if column == "result" or tag:
            self.tree.item(iid, tags=(tag,) if tag else ())

    def _cell_text(self, x: int, y: int) -> str | None:
        """Full text of the hovered cell, since long ones are cut off."""
        iid = self.tree.identify_row(y)
        column = self.tree.identify_column(x)
        if not iid:
            return None
        if column == "#0":
            return iid[4:] if iid.startswith("dir:") else iid
        try:
            text = self.tree.set(iid, column)
        except tk.TclError:
            return None
        return text if len(text) > 24 else None

    def _on_double_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid and not iid.startswith("dir:"):
            self.tree.selection_set(iid)
            self._when_idle(self.inspect)

    def _on_tree_menu(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        busy = bool(self.worker and self.worker.is_alive())
        for label in ("Inspect", "Remove from queue"):
            self.tree_menu.entryconfigure(label, state="disabled" if busy else "normal")
        self.tree_menu.tk_popup(event.x_root, event.y_root)

    def open_folder(self) -> None:
        chosen = self._selected_videos()
        if chosen:
            _reveal(chosen[0])

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
            self.scanning = False
            self.survey = survey_mod.Survey()
            self._apply_survey()
            return
        self.scanning = True
        videos = list(self.videos)
        tools = self.tools

        def work() -> None:
            try:
                result = survey_mod.survey(
                    videos, tools,
                    progress=lambda done, total: self.messages.put(
                        ("scan_progress", (generation, done, total))),
                    should_stop=lambda: self.scan_generation != generation,
                )
            except Exception:
                self.messages.put(("log", (traceback.format_exc(), "error")))
                return
            self.messages.put(("scan_done", (generation, result)))

        threading.Thread(target=work, daemon=True).start()

    def _apply_survey(self) -> None:
        """Push the scanned options into every track selector."""
        entries = {
            "audio": [""] + self.survey.audio_entries(),
            "subtitle": [""] + self.survey.subtitle_entries(),
            "subtitle_embedded": [""] + self.survey.subtitle_entries(embedded_only=True),
        }
        for kind, boxes in self.track_boxes.items():
            for box in boxes:
                try:
                    box.configure(values=entries[kind])
                except tk.TclError:
                    pass
        # Re-expand whatever is selected so it shows its description, and
        # leave a hand-typed spec exactly as typed.
        options = {"audio": self.survey.audio, "subtitle": self.survey.subtitles}
        for name, kind in (("top_language", "subtitle"),
                           ("bottom_language", "subtitle"),
                           ("set_audio", "audio"),
                           ("set_subs", "subtitle")):
            var = self.vars[name]
            spec = survey_mod.spec_of(var.get())
            if spec:
                entry = survey_mod.entry_for(spec, options[kind])
                if entry != var.get():
                    var.set(entry)
        self._schedule_plan()

    # ------------------------------------------------- what will happen, per file

    def _schedule_plan(self, *_) -> None:
        if self.plan_job is not None:
            self.root.after_cancel(self.plan_job)
        self.plan_job = self.root.after(250, self._update_plan)

    def _update_plan(self) -> None:
        """Fill the Top line / Bottom line columns for every queued file.

        Pure decision-making on the scan's cached results plus a look at the
        files beside each video, done off the UI thread because a big folder
        means a lot of filename matching.
        """
        self.plan_job = None
        self.plan_generation += 1
        generation = self.plan_generation
        top = SideSpec.parse(survey_mod.spec_of(self.vars["top_language"].get()))
        bottom = SideSpec.parse(survey_mod.spec_of(self.vars["bottom_language"].get()))
        media = dict(self.survey.media)
        videos = list(self.videos)
        pending = "reading..." if self.scanning else "\u2717 could not read this file"
        if top.label == bottom.label and top.label != "?":
            self.status.set("the top and bottom lines are the same; pick two different ones")

        def work() -> None:
            rows = []
            for video in videos:
                if self.plan_generation != generation:
                    return
                info = media.get(video)
                if info is None:
                    rows.append((video, pending, ""))
                    continue
                try:
                    t = choose_source(info, top)
                    b = choose_source(info, bottom, {t.file} if t.file else set())
                    rows.append((video, _cell(t), _cell(b)))
                except Exception as exc:          # a bad spec must not kill the thread
                    rows.append((video, f"✗ {exc}", ""))
            self.messages.put(("plan", (generation, rows)))

        threading.Thread(target=work, daemon=True).start()

    def _plan_summary(self, rows) -> str:
        """One or two sentences on what Run would do, for the Subtitles tab."""
        if not rows:
            return ("Queue some videos and the list above shows which track or "
                    "file each line will come from.")
        if any(t == "reading..." for _, t, _ in rows):
            return "Reading the queued files..."
        bad = [(v, t if t.startswith("\u2717") else b)
               for v, t, b in rows if t.startswith("\u2717") or b.startswith("\u2717")]
        lines = []
        if bad:
            lines.append(f"{len(bad)} of {len(rows)} file(s) cannot be done as set "
                         f"and would fail: {bad[0][1][2:]}. Hover over a cell for "
                         f"the full text.")
        else:
            lines.append(f"Ready: both lines found in all {len(rows)} file(s).")
        for side in ("top", "bottom"):
            spec = SideSpec.parse(survey_mod.spec_of(self.vars[f"{side}_language"].get()))
            if not spec.language or spec.prefer:
                continue
            variants = [o.spec for o in self.survey.subtitles
                        if o.language == spec.language and o.prefer]
            if len(variants) > 1:
                lines.append(
                    f"\"{spec.language}\" on the {side} line matches more than one "
                    f"{langs.display(spec.language)} track ({', '.join(variants[:3])}); "
                    f"the list shows which each file uses. Pick one from the "
                    f"menu to be sure.")
        return "\n".join(lines)

    # ------------------------------------------------------------- output

    def write(self, text: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text, tag or "")
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _drain(self) -> None:
        """Pull messages from worker threads onto the UI thread."""
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self.write(payload[0], payload[1])
                elif kind == "progress":
                    self.progress["value"] = payload
                elif kind == "status":
                    self.status.set(payload)
                elif kind == "cell":
                    self._set_cell(*payload)
                elif kind == "plan":
                    generation, rows = payload
                    if generation == self.plan_generation:
                        for video, top, bottom in rows:
                            iid = str(video)
                            if not self.tree.exists(iid):
                                continue
                            self.tree.set(iid, "top", top)
                            self.tree.set(iid, "bottom", bottom)
                            if not self.tree.set(iid, "result"):
                                problem = "\u2717" in top[:1] + bottom[:1]
                                self.tree.item(iid, tags=("warn",) if problem else ())
                        self.plan_hint.set(self._plan_summary(rows))
                elif kind == "scan_progress":
                    generation, done, total = payload
                    if generation == self.scan_generation and done < total:
                        self.status.set(f"reading tracks... {done}/{total}")
                elif kind == "scan_done":
                    generation, result = payload
                    if generation == self.scan_generation:
                        self.scanning = False
                        self.survey = result
                        self._apply_survey()
                        problems = len(result.failed)
                        self.status.set(
                            f"{len(self.videos)} file(s) queued"
                            + (f", {problems} unreadable" if problems else ""))
                        for problem in result.failed[:5]:
                            self.write(f"  ! {problem}\n", "error")
                        self._ensure_text_scan()
                        self._schedule_worst()
                elif kind == "renamed":
                    mapping = payload
                    self.videos = [mapping.get(v, v) for v in self.videos]
                    # A rename keeps size and modification time, so the text
                    # already read is still good under the new name.
                    for old, new in mapping.items():
                        if old in self.texts:
                            self.texts[new] = self.texts.pop(old)
                    self._refresh_tree()
                    self._rescan()
                elif kind == "job_done":
                    self.worker = None
                    self.progress["value"] = 0
                    self._update_states()
                    self._ensure_text_scan()
                elif kind == "text_progress":
                    self.text_progress = payload
                    self._update_caption()
                elif kind == "text_done":
                    path, text = payload
                    if path in self.videos:
                        self.texts[path] = text
                        if text.problem:
                            self.write(f"  ! preview could not read {path.name}: "
                                       f"{text.problem}\n", "dim")
                    self._schedule_worst()
                elif kind == "text_scan_end":
                    self.text_thread = None
                    self.text_progress = (0, 0)
                    self._ensure_text_scan()      # anything queued meanwhile
                    self._update_caption()
                elif kind == "worst":
                    generation, moment = payload
                    if generation == self.worst_generation:
                        self.worst = moment
                        self._update_caption()
                        self._schedule_preview()
        except queue.Empty:
            pass
        self.drain_job = self.root.after(80, self._drain)

    def _post(self, text: str, tag: str | None = None) -> None:
        self.messages.put(("log", (text, tag)))

    # --------------------------------------------------------------- jobs

    def _start_job(self, name: str, target, *args) -> bool:
        """Run `target(*args)` on a worker thread. One job at a time."""
        if self.worker and self.worker.is_alive():
            self.status.set(f"{self.job_name} is still running")
            return False
        self._pause_text_scan()
        self.stop_event.clear()
        self.job_name = name

        def wrapper() -> None:
            try:
                target(*args)
            except Exception:
                self._post(traceback.format_exc(), "error")
                self.messages.put(("status", f"{name} failed; see the log"))
            finally:
                self.messages.put(("job_done", None))

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()
        self._update_states()
        return True

    # ------------------------------------------- reading text for the preview

    def _ensure_text_scan(self) -> None:
        """Read the text of any queued file that has not been read yet.

        One pass per file, for all its text tracks at once, so this happens
        once when a file is added and never again for that file -- unless it
        changes on disk. It gives way to every job: a job stops it (killing
        the read in progress), and it picks up where it left off afterwards.
        """
        if self.tools is None or (self.text_thread and self.text_thread.is_alive()):
            return
        if self.worker and self.worker.is_alive():
            return
        todo = [self.survey.media[v] for v in self.videos
                if v in self.survey.media and not self._text_is_current(v)]
        if not todo:
            return
        self.text_stop.clear()
        tools, stop = self.tools, self.text_stop

        def work() -> None:
            import time
            started, read, size = time.time(), 0, 0
            try:
                for i, media in enumerate(todo):
                    if stop.is_set():
                        break
                    self.messages.put(("text_progress", (i, len(todo))))
                    text = worst.read_file_text(media, tools, stop.is_set)
                    if text is None:              # stopped part way
                        break
                    read += 1
                    size += (text.stamp or (0, 0))[0]
                    self.messages.put(("text_done", (media.path, text)))
            except Exception:
                self._post(traceback.format_exc(), "error")
            finally:
                if read:
                    self._post(f"preview: read the subtitles of {read} file(s), "
                               f"{size / 1e9:.1f} GB, in {time.time() - started:.0f} s\n",
                               "dim")
                self.messages.put(("text_scan_end", None))

        self.text_thread = threading.Thread(target=work, daemon=True)
        self.text_thread.start()
        self._update_caption()

    def _text_is_current(self, video: Path) -> bool:
        text = self.texts.get(video)
        return text is not None and text.stamp == worst.file_stamp(video)

    def _pause_text_scan(self) -> None:
        """Stop reading so a job has the files to itself (Windows will not
        rename a file another process has open)."""
        thread = self.text_thread
        if thread and thread.is_alive():
            self.text_stop.set()
            thread.join(timeout=10)

    # ------------------------------------------------ the worst moment, found

    def _schedule_worst(self, *_) -> None:
        if self.worst_job is not None:
            self.root.after_cancel(self.worst_job)
        self.worst_job = self.root.after(300, self._update_worst)

    def _update_worst(self) -> None:
        """Find the worst moment across the queue, off the UI thread.

        Pure arithmetic on text already in memory, plus reading any subtitle
        files on disk the first time they are needed.
        """
        self.worst_job = None
        self.worst_generation += 1
        generation = self.worst_generation
        if not self.vars["preview_worst"].get():
            self.worst = None
            self._update_caption()
            self._schedule_preview()
            return
        top = SideSpec.parse(survey_mod.spec_of(self.vars["top_language"].get()))
        bottom = SideSpec.parse(survey_mod.spec_of(self.vars["bottom_language"].get()))
        styles = (self._side_style("top"), self._side_style("bottom"), self._layout())
        entries = [(v, self.survey.media.get(v), self.texts.get(v)) for v in self.videos]

        def sources():
            for video, media, text in entries:
                if media is None or text is None:
                    continue
                t = choose_source(media, top)
                b = choose_source(media, bottom, {t.file} if t.file else set())
                yield video, self._cues(t, text), self._cues(b, text)

        def work() -> None:
            try:
                moment = worst.worst_across(
                    sources(), *styles,
                    should_stop=lambda: self.worst_generation != generation)
            except Exception:
                self._post(traceback.format_exc(), "error")
                moment = None
            self.messages.put(("worst", (generation, moment)))

        threading.Thread(target=work, daemon=True).start()

    def _cues(self, choice, text) -> list:
        if choice.track is not None:
            return text.tracks.get(choice.track.id, [])
        if choice.file is not None:
            stamp = worst.file_stamp(choice.file)
            cached = self.sub_texts.get(choice.file)
            if cached is None or cached[0] != stamp:
                try:
                    cached = (stamp, worst.read_subtitle_file(choice.file))
                except (OSError, ValueError):
                    cached = (stamp, [])
                self.sub_texts[choice.file] = cached
            return cached[1]
        return []

    def _side_style(self, side: str) -> render.SideStyle:
        v = self.vars
        return render.SideStyle(
            font=v[f"{side}.font"].get().strip() or "Arial",
            size=_int(v[f"{side}.size"], 52, 6, 400),
            margin_v=_int(v[f"{side}.margin_v"], 50, 0, 1000),
            spacing=_int(v[f"{side}.spacing"], 0, -50, 50),
            scale_x=_int(v[f"{side}.scale_x"], 100, 20, 400),
            bold=v[f"{side}.bold"].get(),
            italic=v[f"{side}.italic"].get(),
            primary=v[f"{side}.primary"].get(),
            outline=_float(v[f"{side}.outline"], 2, 0, 10))

    def _layout(self) -> render.Layout:
        c = self.cfg.layout
        return render.Layout(play_res_x=c.play_res_x, play_res_y=c.play_res_y,
                             auto_lift=self.vars["layout.auto_lift"].get(),
                             line_height_factor=c.line_height_factor)

    def _update_caption(self) -> None:
        done, total = self.text_progress
        reading = (f"reading subtitles for the preview: {done + 1} of {total} "
                   f"file(s)" if total else "")
        moment = self.worst if self.vars["preview_worst"].get() else None
        if moment is not None:
            where = (self.tree.item(str(moment.video), "text")
                     if moment.video and self.tree.exists(str(moment.video))
                     else getattr(moment.video, "name", ""))
            text = (f"Worst moment{' so far' if reading else ''}: {where} at "
                    f"{moment.when()} — {moment.top_rows} row(s) on top, "
                    f"{moment.bottom_rows} below")
        elif not self.vars["preview_worst"].get():
            text = "Sample text."
        elif not self.videos:
            text = "Sample text. Queue videos to preview the worst moment in them."
        elif reading or self.scanning:
            text = "Sample text for now."
        elif self.texts:
            text = "Sample text: none of the queued files has text for these lines."
        else:
            text = "Sample text until the queued files have been read."
        self.preview_caption.set(text + (f"   ({reading})" if reading else ""))

    def run_or_stop(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.status.set("stopping after the current file...")
            self.run_button.state(["disabled"])
            return
        self.run()

    # -------------------------------------------------------------- tools

    def _find_tools(self) -> None:
        try:
            self.tools = discover_tools(None, self.cfg.mkvtoolnix_dir or None)
            self.write(f"{TITLE}  ·  {self.tools.version}\n", "dim")
            if self.tools.mkvpropedit is None:
                self.write("mkvpropedit was not found, so default tracks cannot "
                           "be changed. It ships with MKVToolNix.\n", "dim")
            if self.videos:
                self._rescan()
        except ToolError as exc:
            self.tools = None
            self.write(f"{exc}\n\n", "error")
            self.root.after(300, self._ask_for_tools)

    def _ask_for_tools(self) -> None:
        if not messagebox.askyesno(
                "MKVToolNix",
                "dual_subs reads and writes MKV files with MKVToolNix "
                "(mkvmerge and mkvextract).\n\n"
                + ("It could not be found. " if self.tools is None else "")
                + "Point to the folder that contains mkvmerge.exe?"):
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
        self._rescan()

    def _check_ready(self, need_tools: bool = True) -> bool:
        if need_tools and self.tools is None:
            self._ask_for_tools()
            if self.tools is None:
                return False
        if not self.videos:
            self.status.set("add some files first")
            return False
        return True

    # ----------------------------------------------------------- inspect

    def inspect(self) -> None:
        if not self._check_ready():
            return
        targets = self._selected_videos() or self.videos[:5]
        media = dict(self.survey.media)
        self._start_job("inspecting", self._inspect_work, targets, media)

    def _inspect_work(self, targets, cached) -> None:
        from . import discover as disc
        for video in targets:
            self._post(f"\n{video.name}\n", "head")
            info = cached.get(video)
            if info is None:
                try:
                    info = probe.probe(video, self.tools)
                except ProbeError as exc:
                    self._post(f"  {exc}\n", "error")
                    continue
            for t in info.audio:
                flag = " [default]" if t.default else ""
                name = f'  "{t.name}"' if t.name else ""
                self._post(f"  audio     track {t.id}: {t.raw_language}{name}{flag}\n", "dim")
            for t in info.tracks:
                self._post(f"  subtitle  {t.describe()}\n", "dim")
            if not info.tracks:
                self._post("  subtitle  (none embedded)\n", "dim")
            cands = disc.find_candidates(video)
            for c in cands[:8]:
                tag = langs.display(c.language) if c.language else "no language in name"
                where = c.path.parent.name + os.sep if c.path.parent != video.parent else ""
                self._post(f"  on disk   {where}{c.path.name}  ({tag})\n", "dim")
            if not cands:
                self._post("  on disk   (no matching subtitle files)\n", "dim")

    # ------------------------------------------------------------- flags

    def _flag_wishes(self) -> dict:
        return dict(audio=survey_mod.spec_of(self.vars["set_audio"].get()),
                    subtitles=survey_mod.spec_of(self.vars["set_subs"].get()),
                    subtitles_off=self.vars["subs_off"].get())

    def set_flags(self) -> None:
        """Apply default-track flags to the queued files, without remuxing."""
        from .tracks import wants_flags
        if not self._check_ready():
            return
        if self.tools.mkvpropedit is None:
            self.write("mkvpropedit not found; it ships with MKVToolNix\n", "error")
            return
        wishes = self._flag_wishes()
        if not wants_flags(**wishes):
            self.status.set("choose a default audio or subtitle track first "
                            "(Default tracks tab)")
            self.book.select(3)
            return
        self.progress["maximum"] = len(self.videos)
        self._start_job("setting flags", self._flags_work, list(self.videos), wishes)

    def _flags_work(self, videos, wishes) -> None:
        from .tracks import settle_flags
        changed = failed = 0
        for i, video in enumerate(videos, 1):
            if self.stop_event.is_set():
                break
            self._post(f"\n{video.name}\n", "head")
            outcome = settle_flags(video, self.tools, **wishes)
            for line in outcome.lines:
                self._post(f"  {line}\n", "error" if line.startswith("!") else "dim")
            if not outcome.ok:
                failed += 1
                self.messages.put(("cell", (video, "result", "✗ flags not set", "error")))
            elif outcome.edits:
                changed += 1
                self.messages.put(("cell", (video, "result", "✓ flags set", "ok")))
            else:
                self.messages.put(("cell", (video, "result", "flags already right", "")))
            self.messages.put(("progress", i))
        self.messages.put(("status", f"default tracks: {changed} changed, "
                                     f"{failed} failed"))

    # ----------------------------------------------------------- renaming

    def shorten_names(self) -> None:
        """Strip release tags from the queued files' names.

        Long paths are why: VLC on Windows cannot open a file past 260
        characters even with LongPathsEnabled set.
        """
        from .rename import plan_renames

        if not self._check_ready(need_tools=False):
            return
        plans = plan_renames(self.videos, style="episode")
        changing = [p for p in plans if p.changed]
        if not changing:
            self.write("\nthese names are already short and clean\n", "dim")
            return
        preview = "\n".join(
            f"{p.source.name}\n    → {p.target.name}" for p in changing[:4])
        if len(changing) > 4:
            preview += f"\n... and {len(changing) - 4} more (all listed in the log)"
        for p in changing:
            self.write(f"  {p.source.name}\n    → {p.target.name}\n", "dim")
        if not messagebox.askokcancel(
                "Shorten file names",
                f"Rename {len(changing)} file(s)?\n\n{preview}\n\n"
                "Subtitle files beside them are renamed to match, and "
                "Tools → Undo last rename puts everything back."):
            return
        self._start_job("renaming", self._rename_work, plans)

    def _rename_work(self, plans) -> None:
        from .rename import apply_renames
        changing = [p for p in plans if p.changed]
        count, problems = apply_renames(plans)
        for problem in problems:
            self._post(f"  ! {problem}\n", "error")
        mapping = {p.source: p.target for p in changing if p.target.exists()}
        for target in mapping.values():
            self._post(f"  ✓ {target.name}\n", "ok")
        self.messages.put(("renamed", mapping))
        self.messages.put(("status", f"{count} renamed -- Tools → Undo last "
                                     f"rename puts them back"))

    def undo_rename(self) -> None:
        import json
        from .rename import UNDO_NAME

        folders = sorted({v.parent for v in self.videos})
        if not folders:
            chosen = filedialog.askdirectory(title="Folder whose renames to undo",
                                             initialdir=self._last_dir())
            folders = [Path(chosen)] if chosen else []
        logs = [(f, f / UNDO_NAME) for f in folders if (f / UNDO_NAME).exists()]
        if not logs:
            messagebox.showinfo("Undo rename",
                                "There is nothing to undo: none of these folders "
                                f"has a rename log ({UNDO_NAME}).")
            return
        mapping: dict[Path, Path] = {}
        total = 0
        for _, log in logs:
            try:
                entries = json.loads(log.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                entries = []
            total += len(entries)
            mapping.update({Path(e["to"]): Path(e["from"]) for e in entries
                            if "to" in e and "from" in e})
        where = "\n".join(str(f) for f, _ in logs)
        if not messagebox.askokcancel(
                "Undo rename", f"Put back {total} renamed file(s) in:\n\n{where}"):
            return
        self._start_job("undoing renames", self._undo_work, [f for f, _ in logs], mapping)

    def _undo_work(self, folders, mapping) -> None:
        from .rename import undo_renames
        total = 0
        for folder in folders:
            count, problems = undo_renames(folder)
            total += count
            for problem in problems:
                self._post(f"  ! {problem}\n", "error")
        self.messages.put(("renamed", {k: v for k, v in mapping.items() if v.exists()}))
        self.messages.put(("status", f"{total} file(s) put back"))

    # ----------------------------------------------------------- encoding

    def reencode(self) -> None:
        """Normalise the subtitle files beside the queued videos to UTF-8."""
        if not self._check_ready(need_tools=False):
            return
        if not messagebox.askokcancel(
                "Convert subtitle files to UTF-8",
                "Rewrite the subtitle files beside the queued videos as UTF-8?\n\n"
                "Only files in an older encoding (GB18030, Big5, ...) change. "
                "Each keeps its original as <name>.orig.\n\n"
                "Not needed for this program, which reads those encodings "
                "itself -- it helps players that do not."):
            return
        hint = langs.normalize(survey_mod.spec_of(self.vars["bottom_language"].get())
                               .split(":")[0]) or None
        self._start_job("converting", self._reencode_work, list(self.videos), hint)

    def _reencode_work(self, videos, hint) -> None:
        from . import discover as disc
        from .textio import reencode_file
        seen: set[Path] = set()
        changed = 0
        for video in videos:
            for cand in disc.find_candidates(video):
                if cand.path in seen:
                    continue
                seen.add(cand.path)
                try:
                    was, did = reencode_file(cand.path, language=cand.language or hint)
                except OSError as exc:
                    self._post(f"  ! {cand.path.name}: {exc}\n", "error")
                    continue
                if did:
                    changed += 1
                    self._post(f"  {cand.path.name}   {was} → utf-8\n", "ok")
                else:
                    self._post(f"  {cand.path.name}   already utf-8\n", "dim")
        if not seen:
            self._post("no subtitle files found beside the queued videos\n", "dim")
        self.messages.put(("status", f"{changed} of {len(seen)} subtitle file(s) converted"))

    # --------------------------------------------------------------- run

    def run(self) -> None:
        from .tracks import wants_flags
        if not self._check_ready():
            return
        if not self.save_settings(announce=False):
            return
        top, bottom = self.cfg.top_language, self.cfg.bottom_language
        if not top or not bottom:
            messagebox.showinfo(TITLE, "Choose a language for both the top and "
                                       "the bottom line first.")
            self.book.select(0)
            return
        if SideSpec.parse(top).label == SideSpec.parse(bottom).label:
            messagebox.showinfo(TITLE, "The top and bottom lines are the same. "
                                       "Choose two different ones.")
            self.book.select(0)
            return

        cfg = copy.deepcopy(self.cfg)      # later edits must not reach this run
        remux = cfg.output_mode in ("mux", "both")
        wishes = self._flag_wishes()
        wishes["newest_subtitle"] = remux and self.vars["set_dual"].get()
        if not wants_flags(**wishes):
            wishes = None
        elif self.tools.mkvpropedit is None:
            self.write("mkvpropedit not found, so the Default tracks settings "
                       "will be skipped\n", "error")
            wishes = None

        videos = list(self.videos)
        for video in videos:
            self._set_cell(video, "result", "waiting", "")
        self.progress["maximum"] = len(videos)
        self.write(f"\n— {len(videos)} file(s), "
                   f"{dict((v, l) for v, l, _ in MODE_CHOICES)[cfg.output_mode].lower()}\n",
                   "head")
        self._start_job("the run", self._run_work, videos, top, bottom, cfg,
                        self.vars["overwrite"].get(), wishes)

    def _run_work(self, videos, top, bottom, cfg, overwrite, wishes) -> None:
        from .tracks import settle_flags
        top_spec, bot_spec = SideSpec.parse(top), SideSpec.parse(bottom)
        ok = failed = 0
        done = 0
        try:
            for i, video in enumerate(videos, 1):
                if self.stop_event.is_set():
                    break
                self.messages.put(("status", f"{i}/{len(videos)}  {video.name}"))
                self.messages.put(("cell", (video, "result", "working...", "")))
                self._post(f"\n[{i}/{len(videos)}] {video.name}\n", "head")
                try:
                    result = process(video, top_spec, bot_spec, self.tools, cfg,
                                     overwrite=overwrite,
                                     log=lambda line: self._post(line + "\n", "dim"))
                except Exception:
                    result = None
                    self._post(traceback.format_exc(), "error")
                if result is not None and result.ok:
                    ok += 1
                    for path in result.written:
                        self._post(f"    → {path.name}\n", "ok")
                    note = "✓ " + ", ".join(p.suffix for p in result.written)
                    if wishes:
                        target = next((p for p in result.written
                                       if p.suffix.lower() == ".mkv"), video)
                        outcome = settle_flags(target, self.tools, **wishes)
                        for line in outcome.lines:
                            self._post(f"    {line}\n",
                                       "error" if line.startswith("!") else "dim")
                        if not outcome.ok:
                            note += ", flags not set"
                    self.messages.put(("cell", (video, "result", note, "ok")))
                else:
                    failed += 1
                    message = result.message if result is not None else "crashed; see the log"
                    self._post(f"    {message}\n", "error")
                    self.messages.put(("cell", (video, "result", f"✗ {message}", "error")))
                done = i
                self.messages.put(("progress", i))
        finally:
            skipped = len(videos) - done
            if skipped:
                for video in videos[done:]:
                    self.messages.put(("cell", (video, "result", "stopped", "")))
            summary = f"done: {ok} succeeded, {failed} failed"
            if skipped:
                summary += f", {skipped} not started"
            self.messages.put(("status", summary))
            self._post(f"\n{summary}\n", "head")

    # ---------------------------------------------------- the smaller dialogs

    def choose_subtitle_file(self, side: str) -> None:
        from .discover import SUBTITLE_EXTS
        start = str(self.videos[0].parent) if self.videos else self._last_dir()
        chosen = filedialog.askopenfilename(
            title=f"Subtitle file for the {side} line", initialdir=start,
            filetypes=[("Subtitles", " ".join(f"*{e}" for e in SUBTITLE_EXTS)),
                       ("All files", "*.*")])
        if chosen:
            self.vars[f"{side}_language"].set(chosen)

    def swap_sides(self) -> None:
        top, bottom = self.vars["top_language"], self.vars["bottom_language"]
        a, b = top.get(), bottom.get()
        top.set(b)
        bottom.set(a)
        for key in SWAPPABLE:
            t, b_ = self.vars[f"top.{key}"], self.vars[f"bottom.{key}"]
            a, b = t.get(), b_.get()
            t.set(b)
            b_.set(a)
        self._paint_swatches()
        self.status.set("swapped the top and bottom lines")

    def choose_colour(self, side: str) -> None:
        var = self.vars[f"{side}.primary"]
        current = _ass_to_hex(var.get()) or "#ffffff"
        _, chosen = colorchooser.askcolor(color=current, parent=self.root,
                                          title=f"{side.capitalize()} line colour")
        if chosen:
            var.set(_hex_to_ass(chosen, var.get()))
            self._paint_swatches()

    def _paint_swatches(self) -> None:
        for side in ("top", "bottom"):
            colour = _ass_to_hex(self.vars[f"{side}.primary"].get()) or "#ffffff"
            swatch = getattr(self, f"{side}_swatch", None)
            if swatch is not None:
                swatch.configure(background=colour, activebackground=colour)

    def show_settings_file(self) -> None:
        self.save_settings(announce=False)
        _reveal(Config.default_path())

    def open_readme(self) -> None:
        local = app_root() / "README.md"
        if local.exists():
            _open(local)
        else:
            webbrowser.open(PROJECT_URL + "#readme")

    def about(self) -> None:
        tools = self.tools.version if self.tools else "MKVToolNix not found"
        messagebox.showinfo(
            f"About dual_subs {__version__}",
            f"dual_subs {__version__}\n"
            "Two subtitle languages on screen at once.\n\n"
            f"{tools}\n"
            f"Settings: {Config.default_path()}\n\n"
            f"{PROJECT_URL}")

    # ---------------------------------------------------------- the preview

    def _schedule_preview(self, *_) -> None:
        if self.preview_job is not None:
            self.root.after_cancel(self.preview_job)
        self.preview_job = self.root.after(60, self._draw_preview)

    def _draw_preview(self) -> None:
        """The frame, drawn with the current settings.

        Rows break exactly where the written script will break them, and the
        top line is lifted by exactly what the script will lift it by -- the
        same functions decide both. The view takes in as much of the frame as
        the stack needs, shrinking to fit when a worst case climbs high.

        Tk cannot stretch glyphs or space letters, so Width % and Spacing are
        not drawn; position, size, font, colour, wrapping and lift are.
        """
        self.preview_job = None
        canvas = self.preview
        canvas.delete("all")
        cw, ch = canvas.winfo_width(), canvas.winfo_height()
        if cw < 50:
            return
        layout = self._layout()
        fw, fh = layout.play_res_x, layout.play_res_y
        top, bottom = self._side_style("top"), self._side_style("bottom")

        moment = self.worst if self.vars["preview_worst"].get() else None
        if moment is not None:
            top_text, bottom_text = moment.top, moment.bottom
        else:
            top_text = _sample(self.vars["top_language"].get())
            bottom_text = _sample(self.vars["bottom_language"].get())
        top_rows = render.wrap_rows(top_text, top, layout) if top_text else []
        bottom_rows = render.wrap_rows(bottom_text, bottom, layout) if bottom_text else []

        # Placement, as render._position does it.
        y_bottom = fh - bottom.margin_v
        lift = 0
        if layout.auto_lift and len(bottom_rows) > 1:
            lift = (len(bottom_rows) - 1) * round(bottom.size * layout.line_height_factor)
        y_top = fh - top.margin_v - lift
        highest = min([fh]
                      + ([y_top - len(top_rows) * top.size] if top_rows else [])
                      + ([y_bottom - len(bottom_rows) * bottom.size] if bottom_rows else []))
        visible = min(fh, max(fh * 0.3, fh - highest + 40))
        scale = min(cw / fw, ch / visible)
        x0 = (cw - fw * scale) / 2

        def at(frame_y: float) -> float:
            return ch - (fh - frame_y) * scale

        canvas.create_rectangle(x0, max(0, at(0)), x0 + fw * scale, ch,
                                fill="#33465a", width=0)
        whole = visible >= fh or at(0) >= 0
        canvas.create_text(x0 + 6, max(0, at(0)) + 4, anchor="nw", fill="#9fb1c4",
                           font=("TkDefaultFont", 8),
                           text=f"{fw}×{fh} frame" if whole
                           else f"bottom of a {fw}×{fh} frame")

        for rows, style, y in ((bottom_rows, bottom, y_bottom), (top_rows, top, y_top)):
            if not rows:
                continue
            text = "\n".join(rows)
            font = self._preview_font(style, scale)
            colour = _ass_to_hex(style.primary) or "#ffffff"
            outline = max(1, round(style.outline * scale)) if style.outline > 0 else 0
            for dx in range(-outline, outline + 1):
                for dy in range(-outline, outline + 1):
                    if (dx or dy) and abs(dx) + abs(dy) <= outline + 1:
                        canvas.create_text(cw / 2 + dx, at(y) + dy, text=text,
                                           font=font, fill="black", anchor="s",
                                           justify="center")
            canvas.create_text(cw / 2, at(y), text=text, font=font, fill=colour,
                               anchor="s", justify="center")
        self._update_caption()

    def _preview_font(self, style: render.SideStyle, scale: float) -> tuple:
        """A Tk font whose line height matches the ASS size.

        ASS sizes a font by its full line height; Tk by its em. The ratio
        differs per typeface, so it is measured once per family.
        """
        family = style.font
        ratio = self.ratio_cache.get(family)
        if ratio is None:
            probe_font = tkfont.Font(family=family, size=-100)
            ratio = max(0.5, probe_font.metrics("linespace") / 100)
            self.ratio_cache[family] = ratio
        pixels = max(4, round(style.size * scale / ratio))
        flags = [s for s, on in (("bold", style.bold), ("italic", style.italic)) if on]
        return (family, -pixels, *flags)


# ------------------------------------------------------------------ helpers

def _distinct_parts(names: list[str]) -> list[str]:
    """Trim what every name in a folder shares, so the differences show.

    A season of release files is "[Group] Long Series Name - S01E01 - Title
    [BD][1080p]..." eight times over; in a column of normal width every row
    reads identically. Cutting the shared start and end, at a word boundary,
    leaves "... S01E01 - Title ...". The full name is in the hover text.
    """
    if len(names) < 2:
        return list(names)
    prefix = os.path.commonprefix(names)
    suffix = os.path.commonprefix([n[::-1] for n in names])[::-1]
    cut_start = cut_end = 0
    if len(prefix) >= 15:
        cut_start = max(prefix.rfind(c) for c in " -_.])") + 1
    if len(suffix) >= 15:
        first = min((i for i in (suffix.find(c) for c in " -_.[(") if i >= 0),
                    default=-1)
        if first >= 0:
            cut_end = len(suffix) - first
    out = []
    for name in names:
        end = len(name) - cut_end
        core = name[cut_start:end].strip()
        if not core or end <= cut_start:
            out.append(name)
            continue
        out.append(("\u2026 " if cut_start else "") + core + (" \u2026" if cut_end else ""))
    return out


def _cell(choice) -> str:
    return f"✗ {choice.problem}" if choice.problem else choice.short()


def _enable(widget, on: bool) -> None:
    try:
        if isinstance(widget, ttk.Widget):
            widget.state(["!disabled"] if on else ["disabled"])
        else:
            widget.configure(state="normal" if on else "disabled")
    except tk.TclError:
        pass


def _sample(spec_entry: str) -> str:
    spec = survey_mod.spec_of(spec_entry)
    language = langs.normalize(spec.split(":")[0]) if spec and not spec.startswith("#") else None
    return SAMPLES.get(language or "", GENERIC_SAMPLE)


def _int(var: tk.Variable, fallback: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(float(str(var.get()).strip()))))
    except (ValueError, TypeError, tk.TclError):
        return fallback


def _float(var: tk.Variable, fallback: float, low: float, high: float) -> float:
    try:
        value = max(low, min(high, float(str(var.get()).strip())))
    except (ValueError, TypeError, tk.TclError):
        return fallback
    return int(value) if value == int(value) else value


def _num_text(value) -> str:
    return str(int(value)) if float(value) == int(value) else str(value)


def _ass_to_hex(colour: str) -> str | None:
    """'&H00BBGGRR' (ASS) -> '#rrggbb'. None if it is not a colour."""
    text = (colour or "").strip().upper().lstrip("&").lstrip("H").rstrip("&")
    if len(text) not in (6, 8):
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    bb, gg, rr = text[-6:-4], text[-4:-2], text[-2:]
    return f"#{rr}{gg}{bb}".lower()


def _hex_to_ass(colour: str, previous: str = "") -> str:
    """'#rrggbb' -> '&HAABBGGRR', keeping the previous alpha."""
    rr, gg, bb = colour[1:3], colour[3:5], colour[5:7]
    old = (previous or "").strip().upper().lstrip("&").lstrip("H")
    alpha = old[:2] if len(old) == 8 else "00"
    return f"&H{alpha}{bb}{gg}{rr}".upper()


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


def _open(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass


def _reveal(path: Path) -> None:
    """Open the folder holding `path`, with the file selected where possible."""
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(path)])
            return
    except OSError:
        pass
    _open(path.parent)


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
