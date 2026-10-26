"""Regression tests for edge cases found after 2.0.0. No MKVToolNix needed.

    python -m pytest tests
    python tests/test_edge_cases.py      (works without pytest too)
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dualsubs import discover, hanzi, rename, render, textio
from dualsubs.formats import Event
from dualsubs.pipeline import _sync_reference
from dualsubs.probe import MediaInfo, Track

SRT = "1\r\n00:00:01,000 --> 00:00:02,000\r\nhi\r\n"


def _touch(path: Path, text: str = SRT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- discovery

def test_an_episode_never_borrows_the_next_episodes_subtitles():
    # The names share most of their length, which the loose prefix match
    # used to accept: episode 1 quietly got episode 2's Chinese.
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        e1 = _touch(folder / "Long Series Name Here - S01E01 - Pilot.mkv")
        e2 = _touch(folder / "Long Series Name Here - S01E02 - Ep.mkv")
        sub = _touch(folder / "Long Series Name Here - S01E02 - Ep.chi.srt")
        assert discover.pick(e1, "chi") is None
        assert discover.pick(e2, "chi").path == sub.resolve()


def test_different_episode_markers_never_match_even_without_the_sibling():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        e1 = _touch(folder / "Show.S01E01.1080p.mkv")
        _touch(folder / "Show.S01E02.1080p.chi.srt")       # its video is elsewhere
        assert discover.pick(e1, "chi") is None


def test_a_closer_sibling_keeps_its_own_subtitle():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        part1 = _touch(folder / "Some Film Part 1.mkv")
        part2 = _touch(folder / "Some Film Part 2.mkv")
        sub = _touch(folder / "Some Film Part 2.chi.srt")
        assert discover.pick(part1, "chi") is None
        assert discover.pick(part2, "chi").path == sub.resolve()


def test_two_cuts_of_one_film_can_share_a_subtitle():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        a = _touch(folder / "Movie.2019.1080p.BluRay.mkv")
        b = _touch(folder / "Movie.2019.2160p.WEB.mkv")
        sub = _touch(folder / "Movie.2019.chi.srt")
        assert discover.pick(a, "chi").path == sub.resolve()
        assert discover.pick(b, "chi").path == sub.resolve()


def test_a_folder_named_after_the_episode_belongs_to_it():
    # Subs/<release name>/2_English.srt: the files say nothing but the language.
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        e1 = _touch(folder / "Show.S01E01.1080p.WEB.x264-GRP.mkv")
        _touch(folder / "Show.S01E02.1080p.WEB.x264-GRP.mkv")
        eng = _touch(folder / "Subs" / e1.stem / "2_English.srt")
        chi = _touch(folder / "Subs" / e1.stem / "3_Chinese.srt")
        _touch(folder / "Subs" / "Show.S01E02.1080p.WEB.x264-GRP" / "2_English.srt")
        assert discover.pick(e1, "eng").path == eng.resolve()
        assert discover.pick(e1, "chi").path == chi.resolve()
        found = {c.path for c in discover.find_candidates(e1)}
        assert found == {eng.resolve(), chi.resolve()}


def test_a_lone_films_subs_folder_is_its_own():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        film = _touch(folder / "Movie.2019.1080p.mkv")
        eng = _touch(folder / "Subs" / "2_English.srt")
        assert discover.pick(film, "eng").path == eng.resolve()


def test_forced_and_sdh_files_rank_below_the_full_dialogue():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        video = _touch(folder / "Movie.2019.1080p.mkv")
        # alphabetical order puts both of these first, which used to decide it
        _touch(folder / "Movie.2019.1080p.eng.forced.srt")
        _touch(folder / "Movie.2019.1080p.eng.cc.srt")
        full = _touch(folder / "Movie.2019.1080p.eng.srt")
        assert discover.pick(video, "eng").path == full.resolve()


def test_a_film_called_forced_is_not_penalised():
    assert discover._qualifier_adjustment("forced entry 2019", "forced entry 2019.eng") == 0
    assert discover._qualifier_adjustment("movie", "movie.eng.forced") < 0


def test_our_own_output_is_never_read_back_as_a_source():
    # A re-run with "overwrite" used to feed the previous dual .ass back in as
    # the Chinese side, giving three lines of text instead of two.
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        video = _touch(folder / "Film.mkv")
        own = _touch(folder / "Film.ass",
                     render.build_ass([], [], render.SideStyle(), render.SideStyle()))
        theirs = _touch(folder / "Film.srt")
        assert discover.is_generated(own) and not discover.is_generated(theirs)
        assert discover.pick(video, "chi").path == theirs.resolve()
        everything = discover.find_candidates(video, include_generated=True)
        assert own.resolve() in {c.path for c in everything}


def test_episode_markers():
    assert discover.episode_of("show.s01e06.title") == (1, 6)
    assert discover.episode_of("show 2x03 title") == (2, 3)
    assert discover.episode_of("Show - 06 - Title") == (None, 6)
    assert discover.episode_of("Movie.1920x1080.2019") is None
    assert discover.episode_of("movie.2019") is None


# ------------------------------------------------------------------ renaming

def test_renaming_leaves_a_neighbours_subtitle_alone():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        e1 = _touch(folder / "Long Series Name Here - S01E01 - Pilot.mkv")
        e2 = _touch(folder / "Long Series Name Here - S01E02 - Ep.mkv")
        _touch(folder / "Long Series Name Here - S01E02 - Ep.chi.srt")
        plans = rename.plan_renames([e1, e2])
        assert plans[0].sidecars == []
        assert [d.name for _, d in plans[1].sidecars] == ["S01E02 - Ep.chi.srt"]
        count, problems = rename.apply_renames(plans)
        assert count == 2 and not problems
        assert (folder / "S01E02 - Ep.chi.srt").exists()


def test_each_folder_gets_its_own_undo_log():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        a = _touch(root / "Season 1" / "[Grp] Show - S01E01 - One [1080p].mkv")
        b = _touch(root / "Season 2" / "[Grp] Show - S02E01 - Two [1080p].mkv")
        count, problems = rename.apply_renames(rename.plan_renames([a, b]))
        assert count == 2 and not problems
        for season in ("Season 1", "Season 2"):
            log = root / season / rename.UNDO_NAME
            assert log.exists(), season
            assert len(json.loads(log.read_text(encoding="utf-8"))) == 1
        back, problems = rename.undo_renames(root / "Season 2")
        assert back == 1 and not problems and b.exists() and not a.exists()


def test_a_folder_named_after_the_video_is_renamed_with_it():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        video = _touch(folder / "[Grp] Show - S01E01 - One [1080p].mkv")
        _touch(folder / "Subs" / video.stem / "2_English.srt")
        count, problems = rename.apply_renames(rename.plan_renames([video]))
        assert count == 1 and not problems
        assert (folder / "Subs" / "S01E01 - One" / "2_English.srt").exists()
        back, problems = rename.undo_renames(folder)
        assert back == 2 and not problems
        assert (folder / "Subs" / video.stem / "2_English.srt").exists()


def test_sidecars_stay_put_if_their_video_cannot_move():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        video = _touch(folder / "[Grp] Show - S01E01 - One [1080p].mkv")
        sub = _touch(folder / "[Grp] Show - S01E01 - One [1080p].chi.srt")
        plans = rename.plan_renames([video])
        _touch(plans[0].target, "someone else got here first")
        count, problems = rename.apply_renames(plans)
        assert count == 0 and problems
        assert video.exists() and sub.exists()


# ------------------------------------------------------------------- hanzi

def test_protected_phrases_keep_their_characters():
    # OpenCC writes these as identity entries precisely to protect them.
    convert = lambda s: hanzi.to_simplified(s, use_opencc=False)
    assert convert("他是著名的作家，著作很多。") == "他是著名的作家，著作很多。"
    assert convert("扭轉乾坤") == "扭转乾坤"         # qián, not gān
    assert convert("乾隆皇帝") == "乾隆皇帝"
    assert convert("餅乾很乾") == "饼干很干"          # ordinary 乾 still converts
    assert convert("他著急了") == "他着急了"          # the particle still converts
    assert convert("頭髮") == "头发"


def test_font_names_inside_override_tags_are_not_converted():
    text = r"{\fn微軟正黑體\fs40}這是測試{\i1}內容{\i0}"
    assert hanzi.to_simplified(text, use_opencc=False) == \
        r"{\fn微軟正黑體\fs40}这是测试{\i1}内容{\i0}"


# ------------------------------------------------------------------ render

def test_a_comma_in_a_font_name_cannot_shift_the_style_fields():
    line = render.SideStyle(font="Foo, Bar").to_style_line("X")
    assert len(line.split(":", 1)[1].split(",")) == 23


def test_a_long_bottom_line_that_wraps_lifts_the_top_line():
    layout = render.Layout()
    style = render.SideStyle(font="Arial", size=52)
    short = Event(0, 100, "What is the meaning of this check?")
    long_ = Event(0, 100, "So I said to him, look, if you really want me to throw "
                          "the match then you are going to have to pay a lot more")
    assert render.visual_lines(short, style, layout) == 1
    assert render.visual_lines(long_, style, layout) == 2
    assert render.visual_lines(Event(0, 100, r"one\Ntwo"), style, layout) == 2
    # tags take no space
    assert render.visual_lines(Event(0, 100, r"{\b1\fs20\c&H00FF00&}" * 30 + "hi"),
                               style, layout) == 1

    top = [Event(0, 100, "上")]
    lifted, _ = render._position(top, [long_], render.SideStyle(margin_v=70),
                                 style, layout)
    flat, _ = render._position(top, [short], render.SideStyle(margin_v=70),
                               style, layout)
    y = lambda ev: int(ev.text.split(",")[1].split(")")[0])
    assert y(lifted[0]) < y(flat[0])


def test_cjk_counts_as_full_width():
    layout = render.Layout()
    style = render.SideStyle(size=70)
    assert render.visual_lines(Event(0, 1, "中" * 26), style, layout) == 1
    assert render.visual_lines(Event(0, 1, "中" * 30), style, layout) == 2


# ------------------------------------------------------------------ textio

def test_utf8_with_a_bom_is_left_alone():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "subs.srt"
        raw = b"\xef\xbb\xbf" + SRT.encode("utf-8")
        path.write_bytes(raw)
        was, changed = textio.reencode_file(path)
        assert not changed and path.read_bytes() == raw
        assert not (Path(tmp) / "subs.srt.orig").exists()


# ---------------------------------------------------------------- pipeline

def _track(id_, lang, name="", forced=False):
    return Track(id=id_, sub_index=id_, codec="SubRip/SRT", codec_id="S_TEXT/UTF8",
                 language=lang, raw_language=lang, name=name, forced=forced)


def test_sync_never_uses_a_signs_track_as_its_reference():
    media = MediaInfo(path=Path("x.mkv"), title="", tracks=[
        _track(2, "eng", "Signs & Songs", forced=True),
        _track(3, "eng", "Dialogue"),
    ])
    track, _ = _sync_reference(media, "eng")
    assert track.id == 3
    track, _ = _sync_reference(media, "chi")       # other-language fallback too
    assert track.id == 3


# --------------------------------------------------------------------------

if __name__ == "__main__":
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
