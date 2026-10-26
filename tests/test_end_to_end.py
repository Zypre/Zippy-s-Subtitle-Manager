"""End-to-end runs against real Matroska files. Needs MKVToolNix; skips without.

The files are built by mkvmerge from .srt input alone -- a container holding
only subtitle tracks is enough to exercise probing, extraction, muxing and
flag editing, and takes milliseconds to make.

    python -m pytest tests
    python tests/test_end_to_end.py      (works without pytest too)
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dualsubs import cli, probe
from dualsubs.config import Config
from dualsubs.pipeline import SideSpec, process
from dualsubs.tools import ToolError, discover

try:
    TOOLS = discover()
except ToolError:
    TOOLS = None

try:
    import pytest
    pytestmark = pytest.mark.skipif(TOOLS is None, reason="MKVToolNix not installed")
except ImportError:
    pytest = None

ENG = ["Where are you going?", "To the market.", "Take an umbrella.",
       "It is not raining.", "It will be.", "How do you know?"]
CHS = ["你要去哪儿？", "去市场。", "带把伞吧。", "没下雨啊。", "会下的。", "你怎么知道？"]


def _srt(path: Path, lines: list[str], shift_cs: int = 0) -> Path:
    def ts(cs):
        ms = cs * 10
        return f"00:{ms // 60000:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
    blocks = [f"{i}\n{ts(100 + i * 300 + shift_cs)} --> {ts(300 + i * 300 + shift_cs)}\n{t}\n"
              for i, t in enumerate(lines, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def _mkv(folder: Path, name: str, tracks: list[tuple]) -> Path:
    """tracks: (srt path, language, track name, default)."""
    out = folder / name
    cmd = [str(TOOLS.mkvmerge), "-q", "-o", str(out)]
    for path, lang, title, default in tracks:
        cmd += ["--language", f"0:{lang}", "--track-name", f"0:{title}",
                "--default-track-flag", f"0:{'yes' if default else 'no'}", str(path)]
    subprocess.run(cmd, check=False, capture_output=True)
    assert out.exists(), cmd
    return out


def _season(folder: Path) -> Path:
    work = folder / "_src"
    work.mkdir()
    eng = _srt(work / "eng.srt", ENG)
    chs = _srt(work / "chs.srt", CHS, shift_cs=8)
    signs = _srt(work / "signs.srt", ["MARKET", "TEA"])
    video = _mkv(folder, "Show - S01E01 - Pilot.mkv",
                 [(signs, "eng", "Signs & Songs", True),
                  (eng, "eng", "Dialogue", True),
                  (chs, "chi", "Dialogue (Simplified)", True)])
    for p in work.iterdir():
        p.unlink()
    work.rmdir()
    return video


def _cfg(**kw) -> Config:
    cfg = Config()
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


# --------------------------------------------------------------------------

def test_sidecar_run_writes_both_lines_and_a_rerun_reads_the_sources_again():
    with tempfile.TemporaryDirectory() as tmp:
        video = _season(Path(tmp))
        cfg = _cfg(output_mode="sidecar")
        top, bottom = SideSpec.parse("chi"), SideSpec.parse("eng")

        first = process(video, top, bottom, TOOLS, cfg, log=lambda _: None)
        assert first.ok, first.message
        ass = video.with_suffix(".ass")
        text = ass.read_text(encoding="utf-8-sig")
        assert "你要去哪儿" in text and "Where are you going?" in text
        assert "MARKET" not in text            # the signs track was not picked

        second = process(video, top, bottom, TOOLS, cfg, overwrite=True,
                         log=lambda _: None)
        assert second.ok, second.message
        again = ass.read_text(encoding="utf-8-sig")
        # Reading our own output back would have doubled the cues.
        assert again.count("Dialogue:") == text.count("Dialogue:")


def test_a_foreign_ass_beside_the_video_is_never_overwritten():
    with tempfile.TemporaryDirectory() as tmp:
        video = _season(Path(tmp))
        theirs = video.with_suffix(".ass")
        theirs.write_text("[Script Info]\nTitle: the release's own subtitles\n",
                          encoding="utf-8")
        result = process(video, SideSpec.parse("chi"), SideSpec.parse("eng"),
                         TOOLS, _cfg(output_mode="sidecar"), overwrite=True,
                         log=lambda _: None)
        assert not result.ok and "never overwritten" in result.message
        assert "the release's own subtitles" in theirs.read_text(encoding="utf-8")


def test_an_unwritable_destination_fails_one_file_without_raising():
    with tempfile.TemporaryDirectory() as tmp:
        video = _season(Path(tmp))
        blocker = Path(tmp) / "not a folder"
        blocker.write_text("x")
        result = process(video, SideSpec.parse("chi"), SideSpec.parse("eng"),
                         TOOLS, _cfg(output_mode="sidecar"),
                         output_dir=blocker / "out", log=lambda _: None)
        assert not result.ok and result.message


def test_mux_then_dual_default_clears_every_other_default():
    with tempfile.TemporaryDirectory() as tmp:
        video = _season(Path(tmp))
        code = cli.main([str(video), "-t", "chi", "-b", "eng", "--set-dual",
                         "--no-pause", "--no-stash", "--config",
                         str(Path(tmp) / "cfg.json")])
        assert code == 0
        out = video.with_name(video.stem + ".dual.mkv")
        media = probe.probe(out, TOOLS)
        defaults = [t.id for t in media.tracks if t.default]
        assert defaults == [media.tracks[-1].id]
        assert media.tracks[-1].name.startswith("Dual")


def test_sidecar_mode_puts_default_flags_on_the_original():
    with tempfile.TemporaryDirectory() as tmp:
        video = _season(Path(tmp))
        code = cli.main([str(video), "-t", "chi", "-b", "eng", "--sidecar",
                         "-S", "chi", "--no-pause", "--config",
                         str(Path(tmp) / "cfg.json")])
        assert code == 0
        media = probe.probe(video, TOOLS)
        assert [t.name for t in media.tracks if t.default] == ["Dialogue (Simplified)"]


def test_every_text_track_comes_out_of_one_read():
    from dualsubs import worst
    with tempfile.TemporaryDirectory() as tmp:
        video = _season(Path(tmp))
        media = probe.probe(video, TOOLS)
        text = worst.read_file_text(media, TOOLS)
        assert not text.problem
        assert sorted(text.tracks) == sorted(t.id for t in media.tracks)
        chinese = next(t.id for t in media.tracks if t.raw_language == "chi")
        assert text.tracks[chinese][0][2] == CHS[0]
        assert text.stamp == worst.file_stamp(video)
        # Asked to stop before it starts, it gives up rather than half-reading.
        assert worst.read_file_text(media, TOOLS, should_stop=lambda: True) is None


def test_a_cancelled_child_process_is_killed():
    import time
    from dualsubs.tools import run_cancellable
    started = time.time()
    stop_at = started + 0.3
    result = run_cancellable([sys.executable, "-c", "import time; time.sleep(30)"],
                             lambda: time.time() > stop_at)
    assert result is None and time.time() - started < 5


# --------------------------------------------------------------------------

if __name__ == "__main__":
    if TOOLS is None:
        print("MKVToolNix not found; skipping")
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
