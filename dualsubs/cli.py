"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, langs, probe
from .config import Config
from .pipeline import SideSpec, collect_videos, process
from .probe import ProbeError
from .tools import ToolError, discover as discover_tools

BANNER = f"dual_subs {__version__} - dual-language subtitle muxer"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dual_subs",
        description=(
            "Burn two subtitle languages into one MKV as a single styled ASS "
            "track: one line on top, one on the bottom. Each side is resolved "
            "independently from an embedded track or a matching subtitle file."
        ),
        epilog=(
            "examples:\n"
            "  dual_subs .                          process the current folder, ask for languages\n"
            '  dual_subs "D:\\Shows\\S01" -t eng -b chi\n'
            "  dual_subs episode.mkv -t eng -b chi --keep-ass\n"
            "  dual_subs episode.mkv --list          just show what tracks exist\n"
            "  dual_subs . -t eng -b '#4'            force bottom to embedded track 4\n"
            "  dual_subs . -t chi -b eng --align grid   dub reflows lines; pin both to one grid\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("targets", nargs="*", type=Path, default=None,
                   help="files and/or folders (default: current folder)")
    p.add_argument("-t", "--top", default=None,
                   help="top line: language code, #trackid, or a subtitle file")
    p.add_argument("-b", "--bottom", default=None,
                   help="bottom line: language code, #trackid, or a subtitle file")

    p.add_argument("-r", "--recursive", action="store_true",
                   help="descend into subfolders")
    p.add_argument("-o", "--output-dir", type=Path, default=None,
                   help="write results here instead of beside the source")
    p.add_argument("--overwrite", action="store_true",
                   help="replace an existing .dual.mkv")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve and report, but write nothing")
    p.add_argument("--list", action="store_true", dest="list_tracks",
                   help="list subtitle tracks and matching subtitle files, then exit")
    flags = p.add_argument_group("default tracks (mkvpropedit; no remux)")
    flags.add_argument("-A", "--set-audio", default=None, metavar="SPEC",
                       help="make this the default audio track: a language "
                            "code, #trackid, or lang:words-in-the-track-name")
    flags.add_argument("-S", "--set-subs", default=None, metavar="SPEC",
                       help="make this the default subtitle track, e.g. "
                            "'eng', 'chi:simplified', '#19'")
    flags.add_argument("--subs-off", action="store_true",
                       help="clear the default flag on every subtitle track")
    flags.add_argument("--set-dual", action="store_true",
                       help="make the dual track this run creates the default "
                            "subtitle (implies a mux)")

    names = p.add_argument_group("renaming (shorter paths, for VLC)")
    names.add_argument("--rename", action="store_true",
                       help="shorten release filenames; sidecar subtitles "
                            "follow, and an undo log is written")
    names.add_argument("--rename-style", choices=["episode", "tags"],
                       default="episode",
                       help="episode: 'S01E06 - Title', dropping the series "
                            "name the folder already carries. tags: keep the "
                            "name, drop only brackets and release metadata")
    names.add_argument("--max-path", type=int, default=260, metavar="N",
                       help="trim names until the whole path fits (default 260, "
                            "0 to disable)")
    names.add_argument("--undo-rename", action="store_true",
                       help="put the last rename back")

    p.add_argument("--reencode", action="store_true",
                   help="rewrite every subtitle file found as UTF-8, then exit "
                        "(keeps the original as <name>.srt.orig)")

    out = p.add_argument_group("output")
    out.add_argument("--sidecar", action="store_true",
                     help="write only a .ass beside the video - seconds, not minutes")
    out.add_argument("--both", action="store_true",
                     help="write the .ass AND remux")
    out.add_argument("--t2s", action="store_true",
                     help="convert traditional Chinese to simplified on "
                          "whichever side needs it")
    out.add_argument("--convert-top", choices=["none", "t2s", "auto"], default=None,
                     help="Chinese script conversion for the top line")
    out.add_argument("--convert-bottom", choices=["none", "t2s", "auto"], default=None,
                     help="Chinese script conversion for the bottom line "
                          "(t2s = always, auto = only if it looks traditional)")

    p.add_argument("--gui", action="store_true", help="open the window")
    p.add_argument("--cli", action="store_true",
                   help="stay in the console even with no arguments")
    p.add_argument("--keep-ass", action="store_true",
                   help="also save the merged .ass next to the output")
    p.add_argument("--no-stash", action="store_true",
                   help="leave originals in place instead of moving to _originals/")
    p.add_argument("--no-sync", action="store_true",
                   help="do not retime external subtitle files")
    p.add_argument("--no-rate-fix", action="store_true",
                   help="sync by constant offset only; never rescale for framerate")
    p.add_argument("--no-snap", action="store_true",
                   help="do not give matched cue pairs a common start and end")
    p.add_argument("--align", choices=["pairs", "grid", "off"], default=None,
                   dest="align_mode",
                   help="pairs: align matched cue pairs (default). grid: pull "
                        "one whole side onto the other's cue boundaries, for "
                        "tracks that split dialogue differently. off: neither")
    p.add_argument("--grid-window", type=int, default=None, metavar="MS",
                   help="how far a cue may be pulled in grid mode (default 1000)")
    p.add_argument("--anchor", choices=["top", "bottom", "auto"], default=None,
                   help="which side keeps its own timing in grid mode; auto "
                        "follows the video's default audio track")
    p.add_argument("--snap-window", type=int, default=None, metavar="MS",
                   help="how far apart two cue edges may be and still be "
                        "treated as the same moment (default 500)")

    p.add_argument("--mkvtoolnix", default=None, metavar="DIR",
                   help="folder holding mkvmerge/mkvextract")
    p.add_argument("--config", type=Path, default=None,
                   help="path to the settings file")
    p.add_argument("--write-config", action="store_true",
                   help="write the current settings back out and exit")
    p.add_argument("--no-pause", action="store_true",
                   help="do not wait for a keypress at the end")
    p.add_argument("-V", "--version", action="version", version=BANNER)
    return p


def prompt_language(which: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else " (e.g. eng, chi, jpn)"
    while True:
        raw = input(f"  {which} language{suffix}: ").strip()
        if not raw and default:
            return default
        if raw:
            return raw


def show_tracks(video: Path, tools, cfg: Config) -> None:
    from . import discover as disc
    print(f"\n{video.name}")
    try:
        media = probe.probe(video, tools)
    except ProbeError as exc:
        print(f"  ! {exc}")
        return
    if media.tracks:
        for t in media.tracks:
            print(f"  embedded  {t.describe()}")
    else:
        print("  embedded  (none)")
    cands = disc.find_candidates(video)
    for c in cands[:12]:
        tag = langs.display(c.language) if c.language else "no language token"
        print(f"  on disk   {c.path.name}  ({tag}, score {c.score})")
    if not cands:
        print("  on disk   (no matching subtitle files)")


def _flags_after_run(result, video: Path, tools, args) -> list[str]:
    """Settle default flags once a file has been processed.

    After a remux they go on the file just written: mkvmerge copies each
    source track's flags, so the dual track being marked default does not stop
    the original Chinese and English tracks from also being default, and the
    flags have to be settled after the fact. In subtitle-file mode nothing new
    was written, so they go on the original -- a header edit, not a remux.
    """
    from .tracks import settle_flags

    target = next((p for p in result.written if p.suffix.lower() == ".mkv"), None)
    lines = []
    if target is None:
        target = video
        if args.set_dual:
            lines.append("(--set-dual needs a remux: in subtitle-file mode "
                         "there is no dual track inside the video)")
    outcome = settle_flags(target, tools, audio=args.set_audio or "",
                           subtitles=args.set_subs or "",
                           subtitles_off=args.subs_off,
                           newest_subtitle=args.set_dual and target != video)
    return lines + outcome.lines


def set_defaults(videos: list[Path], tools, args) -> int:
    """Change default audio/subtitle flags without touching the media."""
    from .tracks import settle_flags

    if tools.mkvpropedit is None:
        print("\n[!] mkvpropedit was not found next to mkvmerge; it is part "
              "of MKVToolNix and is what changes flags without a remux.")
        return 2

    changed = untouched = failed = 0
    for i, video in enumerate(videos, 1):
        print(f"\n[{i}/{len(videos)}] {video.name}")
        outcome = settle_flags(video, tools, audio=args.set_audio or "",
                               subtitles=args.set_subs or "",
                               subtitles_off=args.subs_off, dry_run=args.dry_run)
        for line in outcome.lines:
            print(f"    {line}")
        if not outcome.ok:
            failed += 1
        elif outcome.edits:
            changed += 1
        else:
            untouched += 1

    verb = "would change" if args.dry_run else "changed"
    print(f"\n{changed} {verb}, {untouched} already correct, {failed} failed.")
    return 1 if failed else 0


def rename_files(videos: list[Path], args) -> int:
    """Shorten release filenames so long paths stop breaking players."""
    from .rename import apply_renames, plan_renames

    plans = plan_renames(videos, style=args.rename_style, limit=args.max_path)
    changing = [p for p in plans if p.changed]
    if not changing:
        print("\nnothing to rename; these names are already clean")
        return 0

    print()
    for plan in changing:
        print(f"  {plan.source.name}")
        print(f"    -> {plan.target.name}"
              f"   ({len(str(plan.source))} -> {len(str(plan.target))} chars)")
        for src, dst in plan.sidecars:
            print(f"    -> {dst.name}")
        if plan.note:
            print(f"    ! {plan.note}")

    if args.dry_run:
        print(f"\ndry run: {len(changing)} file(s) would be renamed")
        return 0

    count, problems = apply_renames(plans)
    for problem in problems:
        print(f"  ! {problem}")
    folders = sorted({p.source.parent for p in changing})
    print(f"\n{count} renamed. Undo with:  dual_subs "
          + " ".join(f'"{f}"' for f in folders) + " --undo-rename")
    return 1 if problems else 0


def undo_rename(targets) -> int:
    from .rename import UNDO_NAME, undo_renames

    total, problems = 0, []
    for target in targets:
        folder = Path(target)
        folder = folder if folder.is_dir() else folder.parent
        count, issues = undo_renames(folder)
        total += count
        problems += issues
    for problem in problems:
        print(f"  ! {problem}")
    print(f"\n{total} file(s) put back.")
    return 1 if problems and not total else 0


def show_options(videos: list[Path], tools) -> None:
    """What you can actually pass to -A, -S, -t and -b for this queue.

    Per-file listings are unreadable for a season, and a fixed menu of
    languages would offer tracks these files do not have while hiding ones
    they do. So the options come from the files themselves.
    """
    from . import survey as survey_mod

    print(f"\nReading tracks across {len(videos)} files...")
    result = survey_mod.survey(videos, tools)
    if not result.scanned:
        print("  (nothing readable)")
        return

    for heading, options, flag in (("Audio  (-A)", result.audio, "-A"),
                                   ("Subtitles  (-S, -t, -b)", result.subtitles, "-S")):
        print(f"\n{heading}")
        if not options:
            print("  (none)")
            continue
        width = max(len(o.spec) for o in options)
        for o in options:
            print(f"  {o.spec:<{width}}   {o.describe()}")
    for problem in result.failed[:5]:
        print(f"\n  ! {problem}")


def reencode(targets: list[Path], recursive: bool,
             top: str | None, bottom: str | None) -> int:
    """Normalise subtitle files on disk to UTF-8."""
    from . import discover
    from .textio import reencode_file

    hint = langs.normalize(bottom or top or "") or None
    seen: set[Path] = set()
    files: list[Path] = []
    for target in targets:
        target = Path(target)
        if target.is_file():
            candidates = [target]
        else:
            it = target.rglob("*") if recursive else target.glob("*")
            candidates = [p for p in it if p.is_file()]
        for p in candidates:
            if p.suffix.lower() in discover.SUBTITLE_EXTS and p.resolve() not in seen:
                seen.add(p.resolve())
                files.append(p)

    if not files:
        print("\n[!] no subtitle files found")
        return 1

    print(f"\n{len(files)} subtitle file(s)")
    changed = 0
    for path in sorted(files):
        try:
            was, did = reencode_file(path, language=hint)
        except OSError as exc:
            print(f"  !  {path.name}: {exc}")
            continue
        if did:
            changed += 1
            print(f"  -> {path.name}   {was} -> utf-8")
        else:
            print(f"  ok {path.name}   already utf-8")
    print(f"\n{changed} converted, {len(files) - changed} already UTF-8.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Judged on the arguments actually given: main([...]) called from code
    # with an empty sys.argv used to open the window and wait forever.
    interactive = not (sys.argv[1:] if argv is None else argv)

    if args.gui or (interactive and not args.cli):
        try:
            from .gui import run as run_gui
        except ImportError as exc:
            if args.gui:
                print(f"[!] the window needs tkinter, which is missing: {exc}")
                return 2
        else:
            return run_gui()

    print(BANNER)
    try:
        cfg = Config.load(args.config)
    except (OSError, ValueError) as exc:
        print(f"[!] could not read settings: {exc}")
        cfg = Config()

    if args.no_stash:
        cfg.stash_originals = False
    if args.no_sync:
        cfg.auto_sync = False
    if args.no_rate_fix:
        cfg.allow_rate_fix = False
    if args.no_snap:
        cfg.snap_sides = False
    if args.snap_window is not None:
        cfg.snap_window_ms = max(0, args.snap_window)
    if args.align_mode is not None:
        cfg.align_mode = args.align_mode
        cfg.snap_sides = args.align_mode != "off"
    if args.grid_window is not None:
        cfg.grid_window_ms = max(0, args.grid_window)
    if args.anchor is not None:
        cfg.anchor = args.anchor
    if args.sidecar:
        cfg.output_mode = "sidecar"
    if args.both:
        cfg.output_mode = "both"
    if args.t2s:
        cfg.top_convert = cfg.bottom_convert = "auto"
    if args.convert_top is not None:
        cfg.top_convert = "" if args.convert_top == "none" else args.convert_top
    if args.convert_bottom is not None:
        cfg.bottom_convert = "" if args.convert_bottom == "none" else args.convert_bottom

    if args.write_config:
        path = cfg.save(args.config)
        print(f"[ok] settings written to {path}")
        return 0

    if args.undo_rename:
        return _finish(undo_rename(args.targets or [Path.cwd()]),
                       interactive, args.no_pause)

    if args.reencode:
        # Pure text work: needs neither MKVToolNix nor a video file.
        return _finish(reencode(args.targets or [Path.cwd()], args.recursive,
                                args.top, args.bottom),
                       interactive, args.no_pause)

    try:
        tools = discover_tools(args.mkvtoolnix, cfg.mkvtoolnix_dir or None)
    except ToolError as exc:
        print(f"\n[!] {exc}")
        return _finish(2, interactive, args.no_pause)
    print(f"[ok] {tools.version}")
    print(f"     {tools.mkvmerge.parent}")

    targets = args.targets or [Path.cwd()]
    videos = collect_videos(targets, cfg.video_exts, args.recursive,
                            cfg.output_suffix)
    if not videos:
        where = ", ".join(str(t) for t in targets)
        print(f"\n[!] no {'/'.join(cfg.video_exts)} files found in: {where}")
        return _finish(1, interactive, args.no_pause)
    print(f"[ok] {len(videos)} file(s) to process")

    if args.list_tracks:
        for v in videos[:12]:
            show_tracks(v, tools, cfg)
        if len(videos) > 12:
            print(f"\n... and {len(videos) - 12} more")
        if len(videos) > 1:
            show_options(videos, tools)
        return _finish(0, interactive, args.no_pause)


    if args.rename:
        return _finish(rename_files(videos, args), interactive, args.no_pause)

    flag_only = (args.set_audio or args.set_subs or args.subs_off) \
        and not (args.top or args.bottom or args.set_dual)
    if flag_only:
        return _finish(set_defaults(videos, tools, args), interactive, args.no_pause)

    top_raw = args.top or cfg.top_language
    bot_raw = args.bottom or cfg.bottom_language
    if not top_raw or not bot_raw:
        show_tracks(videos[0], tools, cfg)
        print()
        top_raw = top_raw or prompt_language("Top")
        bot_raw = bot_raw or prompt_language("Bottom")

    top_spec, bot_spec = SideSpec.parse(top_raw), SideSpec.parse(bot_raw)
    if top_spec.label == bot_spec.label:
        print("\n[!] the two sides must be different")
        return _finish(1, interactive, args.no_pause)

    ok = failed = 0
    for i, video in enumerate(videos, 1):
        print(f"\n[{i}/{len(videos)}] {video.name}")
        result = process(video, top_spec, bot_spec, tools, cfg,
                         output_dir=args.output_dir, overwrite=args.overwrite,
                         dry_run=args.dry_run, keep_ass=args.keep_ass)
        if result.ok:
            ok += 1
            if not args.dry_run and (args.set_dual or args.set_audio
                                     or args.set_subs or args.subs_off):
                for line in _flags_after_run(result, video, tools, args):
                    print(f"    {line}")
            for path in result.written:
                verb = "would write" if args.dry_run else "->"
                print(f"    {verb} {path.name}")
        else:
            failed += 1
            print(f"    ! {result.message}")

    print(f"\ndone. {ok} succeeded, {failed} failed.")
    return _finish(0 if failed == 0 else 1, interactive, args.no_pause)


def _finish(code: int, interactive: bool, no_pause: bool) -> int:
    if interactive and not no_pause and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nPress Enter to close...")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
