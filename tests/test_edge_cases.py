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


def test_a_missing_track_is_explained_accurately():
    from dualsubs.pipeline import SideSpec, choose_source
    pgs = Track(id=7, sub_index=7, codec="HDMV PGS", codec_id="S_HDMV/PGS",
                language="kor", raw_language="kor", name="")
    media = MediaInfo(path=Path("/nonexistent/x.mkv"), title="", tracks=[
        _track(3, "chi", "Dialogue (Simplified)"), pgs])
    # A text track exists; it just is not the one asked for. This used to
    # say "the only Chinese track is image-based: SubRip/SRT".
    problem = choose_source(media, SideSpec.parse("chi:traditional")).problem
    assert '"traditional"' in problem and "Dialogue (Simplified)" in problem
    assert "image-based" not in problem
    problem = choose_source(media, SideSpec.parse("kor")).problem
    assert "image-based (HDMV PGS)" in problem
    problem = choose_source(media, SideSpec.parse("jpn")).problem
    assert "no Japanese subtitles found" in problem
    assert choose_source(media, SideSpec.parse("chi")).track.id == 3


# ------------------------------------------------------------ wrapping (2.2)

ZH = render.SideStyle(font="Microsoft YaHei", size=70)
EN = render.SideStyle(font="Arial", size=52)
LONG_ZH = ("所以我跟他说，如果你真的想让我踢假球，那你得多付钱，"
           "不然我是不会答应你的，因为这样对我们都不好。")
LONG_EN = ("So I said to him, look, if you really want me to throw the match "
           "then you are going to have to pay a lot more")


def test_an_over_wide_chinese_line_is_broken_at_punctuation():
    # Renderers wrap only at spaces by default, so this used to run off both
    # edges of the screen. Checked against libass 0.17.
    layout = render.Layout()
    rows = render.wrap_rows(LONG_ZH, ZH, layout)
    assert rows == ["所以我跟他说，如果你真的想让我踢假球，那你得多付钱，",
                    "不然我是不会答应你的，因为这样对我们都不好。"]
    assert all(render.text_width(r, ZH) <= 1900 for r in rows)


def test_breaks_never_start_a_row_on_punctuation_or_split_tags():
    layout = render.Layout()
    text = r"{\i1}" + "中" * 25 + "。」" + "文" * 20 + r"{\i0}"
    broken = render.break_long_words(text, ZH, layout)
    assert broken.startswith(r"{\i1}") and broken.endswith(r"{\i0}")
    for row in broken.split(r"\N")[1:]:
        assert row[0] not in render.NO_ROW_START
    assert render.break_long_words("中" * 26, ZH, layout) == "中" * 26   # it fits


def test_the_script_carries_the_breaks_but_signs_keep_their_layout():
    top = [Event(0, 100, LONG_ZH),
           Event(0, 100, r"{\an8\pos(960,50)}" + "招牌" * 30)]
    script = render.build_ass(top, [], ZH, EN)
    dialogue = [l for l in script.splitlines() if l.startswith("Dialogue:")]
    assert r"付钱，\N不然" in dialogue[0] or r"付钱，\N不然" in dialogue[1]
    sign = next(l for l in dialogue if "招牌" in l)
    assert r"\N" not in sign


def test_english_wraps_at_spaces_into_even_rows():
    rows = render.wrap_rows(LONG_EN, EN, render.Layout())
    assert len(rows) == 2 and " ".join(rows) == LONG_EN
    widths = [render.text_width(r, EN) for r in rows]
    assert max(widths) - min(widths) < 200          # evened out, not greedy


# ------------------------------------------------------- worst moment (2.2)

from dualsubs import worst


def test_the_worst_moment_is_the_tallest_stack_on_screen():
    top = [(0, 100, "短"), (200, 300, LONG_ZH), (400, 500, "这句很长但是没有那么长")]
    bottom = [(0, 100, "Short."), (210, 290, LONG_EN), (600, 700, LONG_EN)]
    moment = worst.worst_moment(top, bottom, ZH, EN, render.Layout())
    assert (moment.top, moment.bottom) == (LONG_ZH, LONG_EN)
    assert (moment.top_rows, moment.bottom_rows, moment.start) == (2, 2, 210)
    assert moment.when() == "0:00:02"


def test_a_lone_line_still_counts_and_width_breaks_ties():
    bottom = [(0, 100, "one\\Ntwo\\Nthree")]
    moment = worst.worst_moment([], bottom, ZH, EN, render.Layout())
    assert moment.rows == 3 and moment.top == ""
    tie = worst.worst_moment([(0, 10, "a"), (20, 30, "abcdef")], [], ZH, EN,
                             render.Layout())
    assert tie.top == "abcdef"


def test_the_worst_across_files_names_its_file():
    files = [(Path("e1.mkv"), [(0, 100, "短")], [(0, 100, "Short.")]),
             (Path("e2.mkv"), [(0, 100, LONG_ZH)], [(0, 100, "Short.")])]
    moment = worst.worst_across(files, ZH, EN, render.Layout())
    assert moment.video == Path("e2.mkv")


def test_a_chatty_child_process_cannot_hang_a_cancellable_run():
    # The background read waited for mkvextract without reading its output.
    # Once that output filled the pipe (about 4 KB on Windows: a line per
    # track plus a hundred progress updates), both sides waited forever.
    import time
    from dualsubs.tools import run_cancellable
    deadline = time.time() + 20
    code = "import sys; sys.stdout.write('x' * 1000000); sys.stderr.write('e' * 1000000)"
    result = run_cancellable([sys.executable, "-c", code], lambda: time.time() > deadline)
    assert result is not None, "hung until stopped"
    assert len(result.stdout) == len(result.stderr) == 1000000


def test_compact_keeps_dialogue_and_drops_placed_signs():
    events = [Event(50, 90, r"{\i1}Hello{\i0}\Nthere"),
              Event(0, 40, r"{\an8}SIGN"), Event(10, 20, r"{\b1}  ")]
    assert worst.compact(events) == [(50, 90, r"Hello\Nthere")]


# ------------------------------------------------- signs and bilingual tracks

from dualsubs.formats import parse_events

_ASS_HEAD = """[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,52,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,{main},10,10,40,1
Style: Status,Arial,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,7,60,10,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass(main_alignment=2, lines=()):
    body = "\n".join(f"Dialogue: 0,0:00:0{i}.00,0:00:0{i}.50,{style},,0,0,0,,{text}"
                     for i, (style, text) in enumerate(lines))
    return _ASS_HEAD.format(main=main_alignment) + body + "\n"


def test_a_position_in_a_later_override_block_still_places_a_sign():
    # This used to look only at the first block, so the sign was pulled down
    # into the dialogue -- the 4-row status window in the preview.
    assert Event(0, 1, r"{\fad(200,200)}{\an7\pos(100,100)}Status").has_positional_override
    assert Event(0, 1, r"{\i1}word{\i0} {\pos(5,5)}x").has_positional_override
    assert not Event(0, 1, r"{\fad(200,200)}{\i1}Hello").has_positional_override


def test_a_sign_placed_by_its_style_keeps_its_place():
    events = parse_events(_ass(lines=[("Default", "Hello.")] * 6
                               + [("Status", r"Strength\h10\NMagic\h12")]), "x.ass")
    status = events[-1]
    assert status.text.startswith(r"{\an7}") and status.has_positional_override
    assert not any(e.has_positional_override for e in events[:-1])


def test_a_top_aligned_dialogue_style_is_still_dialogue():
    # A group that puts all its dialogue at the top: that style carries most
    # lines, so it is the dialogue, not a sign.
    events = parse_events(_ass(main_alignment=8, lines=[("Default", "Hello.")] * 6), "x.ass")
    assert not any(e.has_positional_override for e in events)


def test_ssa_alignments_are_translated():
    from dualsubs.formats import _SSA_ALIGNMENT
    assert _SSA_ALIGNMENT[6] == 8 and _SSA_ALIGNMENT[10] == 5 and _SSA_ALIGNMENT[2] == 2


def test_japanese_is_recognised_but_a_stray_kana_is_not():
    assert hanzi.is_japanese("はい　嘘偽りのない私の本当の称号です")
    assert hanzi.is_japanese(r"{\fs40}これは日本語です")
    assert not hanzi.is_japanese("是的，这就是我真正的称号。")
    assert not hanzi.is_japanese("我の天，这也太厉害了吧")       # stylistic の
    assert not hanzi.is_japanese("Hello there")


def test_the_japanese_half_of_a_bilingual_track_is_left_out():
    events = [Event(0, 1, "你好，你是谁？"), Event(0, 1, "こんにちは、あなたは誰ですか？"),
              Event(2, 3, r"这句是双语。\N{\fs40}これは二言語です"),
              Event(4, 5, "好的。")]
    kept, removed = hanzi.drop_japanese(events)
    assert removed == 2
    assert [e.text for e in kept] == ["你好，你是谁？", "这句是双语。", "好的。"]


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
