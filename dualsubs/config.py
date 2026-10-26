"""Persistent settings.

Lives in `dual_subs.config.json` next to the executable. If the old
`dual_subs.settings.bat` is sitting there instead, its values are imported once
so nothing is lost when you switch over.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .render import Layout, SideStyle
from .tools import app_root

CONFIG_NAME = "dual_subs.config.json"
LEGACY_NAME = "dual_subs.settings.bat"


@dataclass
class Config:
    mkvtoolnix_dir: str = ""
    top_language: str = ""
    bottom_language: str = ""
    output_suffix: str = ".dual"
    #: "mux" writes a new MKV; "sidecar" writes only the .ass next to the
    #: video (seconds instead of minutes); "both" does each.
    output_mode: str = "mux"
    #: Appended to the video's stem for a sidecar. Empty means an exact stem
    #: match, which is what makes mpv and VLC load it automatically.
    sidecar_suffix: str = ""
    #: "", "t2s" (always) or "auto" (only when the text looks traditional)
    top_convert: str = ""
    bottom_convert: str = ""
    stash_originals: bool = True
    stash_dir_name: str = "_originals"
    auto_sync: bool = True
    allow_rate_fix: bool = True
    #: Give closely-matched cue pairs one start and one end, so the two lines
    #: do not flicker in a fifth of a second apart.
    snap_sides: bool = True
    snap_window_ms: int = 500
    #: "pairs" aligns matched cue pairs and moves both sides toward each
    #: other. "grid" pulls one whole side onto the other's cue boundaries,
    #: which is what handles a dub that reflows lines. "off" disables both.
    align_mode: str = "pairs"
    grid_window_ms: int = 1000
    #: Which side stays put in grid mode: "top", "bottom", or "auto" (the
    #: side whose language matches the video's default audio track).
    anchor: str = "auto"
    video_exts: list[str] = field(default_factory=lambda: [".mkv"])
    track_name_template: str = "Dual [{top}+{bottom}]"
    top: SideStyle = field(default_factory=lambda: SideStyle(
        font="Arial", size=52, margin_v=112))
    bottom: SideStyle = field(default_factory=lambda: SideStyle(
        font="Microsoft YaHei", size=70, margin_v=48))
    layout: Layout = field(default_factory=Layout)
    #: Window-only state (size, last folder, ...). The command line ignores it.
    gui: dict = field(default_factory=dict)

    # ---------------------------------------------------------------- io

    @classmethod
    def default_path(cls) -> Path:
        return app_root() / CONFIG_NAME

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or cls.default_path()
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            return cls.from_dict(raw)
        legacy = path.parent / LEGACY_NAME
        if legacy.exists():
            cfg = cls.from_legacy_bat(legacy)
            cfg.save(path)
            return cfg
        cfg = cls()
        cfg.save(path)
        return cfg

    def save(self, path: Path | None = None) -> Path:
        path = path or self.default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return path

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in raw.items() if k in known}
        for key, klass in (("top", SideStyle), ("bottom", SideStyle),
                           ("layout", Layout)):
            if isinstance(kwargs.get(key), dict):
                allowed = {f.name for f in fields(klass)}
                kwargs[key] = klass(**{k: v for k, v in kwargs[key].items()
                                       if k in allowed})
        return cls(**kwargs)

    # ------------------------------------------------------------ legacy

    @classmethod
    def from_legacy_bat(cls, path: Path) -> "Config":
        """Import `set "KEY=value"` lines from the old settings .bat."""
        values: dict[str, str] = {}
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r'^\s*set\s+"([A-Z_]+)=(.*?)"\s*$', text, re.MULTILINE):
            values[m.group(1)] = m.group(2).strip()

        def num(key: str, default):
            try:
                return type(default)(values[key])
            except (KeyError, ValueError, TypeError):
                return default

        cfg = cls()
        cfg.mkvtoolnix_dir = values.get("MKVTOOLNIX_DIR", "")
        cfg.auto_sync = values.get("AUTO_SYNC", "1") == "1"
        cfg.top = SideStyle(
            font=values.get("TOP_FONT", "Arial"),
            size=num("TOP_FONTSIZE", 52),
            margin_v=num("TOP_MARGINV", 112),
            spacing=num("TOP_SPACING", 0),
            scale_x=num("TOP_SCALEX", 100),
        )
        cfg.bottom = SideStyle(
            font=values.get("BOT_FONT", "Microsoft YaHei"),
            size=num("BOT_FONTSIZE", 70),
            margin_v=num("BOT_MARGINV", 48),
            spacing=num("BOT_SPACING", 0),
            scale_x=num("BOT_SCALEX", 100),
        )
        return cfg
