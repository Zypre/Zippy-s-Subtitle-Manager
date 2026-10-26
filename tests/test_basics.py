"""Tests that need no MKVToolNix and no video files.

    python -m pytest tests
    python tests/test_basics.py      (works without pytest too)
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dualsubs import (align, discover, hanzi, langs, rename, render, sync,
                      survey, textio, tracks)
from dualsubs.formats import Event, parse_events, sniff_format


# ---------------------------------------------------------------- languages

def test_language_normalisation():
    for spelling in ("chs", "zh-Hans", "Chinese", "cmn", "zh_CN", "CHT"):
        assert langs.normalize(spelling) == "chi", spelling
    assert langs.normalize("en-GB") == "eng"
    assert langs.normalize("pt-BR") == "por"
    assert langs.normalize("und") == "und"      # unknown passes through
    assert langs.normalize("") is None
    assert langs.matches("zho", "chi")
    assert not langs.matches("jpn", "chi")


# ------------------------------------------------------------------ formats

def test_sniffing_ignores_the_extension():
    srt_text = "1\n00:00:01,000 --> 00:00:02,000\nhi\n"
    # mkvextract writes native format regardless of the name we give the file
    assert sniff_format(srt_text, "track.ass") == "srt"
    assert sniff_format("WEBVTT\n\n00:01.000 --> 00:02.000\nhi\n", "x.srt") == "vtt"
    assert sniff_format("[Script Info]\n", "x.srt") == "ass"
    assert sniff_format("{100}{200}hi\n", "x.srt") == "microdvd"


def test_srt_without_cue_numbers():
    text = "00:00:01,000 --> 00:00:02,500\nline one\nline two\n"
    events = parse_events(text, "x.srt")
    assert len(events) == 1
    assert events[0].start == 100 and events[0].end == 250
    assert events[0].text == r"line one\Nline two"
    assert events[0].line_count == 2


def test_ass_preserves_commas_and_overrides():
    text = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:03.00,Def,,0,0,0,,{\\an8}Sign, with a comma\n"
    )
    ev = parse_events(text, "x.ass")[0]
    assert ev.text == "{\\an8}Sign, with a comma"
    assert ev.has_positional_override


def test_microdvd_reads_declared_fps():
    ev = parse_events("{1}{1}25.000\n{25}{50}Hello|World\n", "x.sub")[0]
    assert (ev.start, ev.end) == (100, 200)     # 25 frames at 25 fps = 1.00s
    assert ev.text == r"Hello\NWorld"


# ------------------------------------------------------------------- render

def test_top_line_lifts_over_a_two_line_bottom_cue():
    top = [Event(100, 300, "one"), Event(1000, 1200, "two")]
    bot = [Event(100, 300, r"two\Nlines"), Event(1000, 1200, "single")]
    style_top = render.SideStyle(size=52, margin_v=112)
    style_bot = render.SideStyle(size=70, margin_v=48)
    out = render.build_ass(top, bot, style_top, style_bot)
    line_h = int(round(70 * 1.2))
    assert f"\\pos(960,{1080 - 112 - line_h})" in out     # lifted
    assert f"\\pos(960,{1080 - 112})" in out              # not lifted
    assert f"\\pos(960,{1080 - 48})" in out               # bottom pinned


def test_authored_positions_are_left_alone():
    top = [Event(100, 300, "{\\an8}a sign")]
    out = render.build_ass(top, [], render.SideStyle(), render.SideStyle())
    assert "{\\an8}a sign" in out
    assert "\\pos(" not in out.split("[Events]")[1]


# --------------------------------------------------------------------- sync

def _fake_onsets(n=300, seed=1):
    rng = random.Random(seed)
    out, cur = [], 1000.0
    for _ in range(n):
        cur += rng.randint(700, 4000)
        out.append(cur)
    return out


def test_constant_offset():
    ref = _fake_onsets()
    result = sync.estimate(ref, [r - 7000 for r in ref])
    assert result.applied and abs(result.delta_ms - 7000) < 50


def test_pal_speedup():
    ref = _fake_onsets()
    k = 25.0 / (24000 / 1001)
    result = sync.estimate(ref, [r / k + 1500 for r in ref])
    assert result.applied and abs(result.scale - k) < 1e-4


def test_unrelated_subtitles_are_left_alone():
    rng = random.Random(9)
    result = sync.estimate(_fake_onsets(),
                           [rng.random() * 7_000_000 for _ in range(200)])
    assert not result.applied and "low confidence" in result.reason


def test_already_synced_is_a_no_op():
    ref = _fake_onsets()
    result = sync.estimate(ref, list(ref))
    assert not result.applied and "already in sync" in result.reason


# ----------------------------------------------------------------- discover

def test_movie_signature_stops_at_release_metadata():
    assert discover.movie_signature("Ne.Zha.2019.1080p.WEB-DL.x264") == ("ne zha", "2019")
    assert discover.movie_signature("Some.Movie.2019.chs") == ("some movie", "2019")


def test_trailing_language_token():
    assert discover.trailing_language("some.movie.2019.chs") == "chi"
    assert discover.trailing_language("some.movie.2019.eng.forced") == "eng"
    assert discover.trailing_language("s01e01--0001") is None


def test_episodic_prefix_beats_a_loose_match(tmp_path=None):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "S01E01--0001-Yang Jian, The God Erlang, Comes!.mkv").touch()
        (folder / "S01E01--0001.srt").touch()
        (folder / "S01E02--0002.srt").touch()
        video = folder / "S01E01--0001-Yang Jian, The God Erlang, Comes!.mkv"
        best = discover.pick(video, "chi")
        assert best is not None and best.path.name == "S01E01--0001.srt"


def test_a_wrong_language_file_is_never_used():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "Movie.2019.1080p.BluRay.mkv").touch()
        (folder / "Movie.2019.eng.srt").touch()
        video = folder / "Movie.2019.1080p.BluRay.mkv"
        assert discover.pick(video, "eng").path.name == "Movie.2019.eng.srt"
        assert discover.pick(video, "chi") is None


# ------------------------------------------------------------------- tracks

class _Sub:
    """Stand-in for probe.Track; avoids needing a real MKV in these tests."""

    def __init__(self, id, language, name, forced=False, default=False, image=False):
        self.id = id
        self.raw_language = language
        self.name = name
        self.forced = forced
        self.default = default
        self.is_image = image


#: Mirrors a real release: three English subtitle tracks and two Chinese ones,
#: with twelve tracks flagged default at once.
EXAMPLE = [
    _Sub(4, "jpn", "Dialogue (SDH)"),
    _Sub(5, "eng", "Signs & Songs", forced=True),
    _Sub(7, "eng", "Dialogue", default=True),
    _Sub(8, "eng", "Dialogue (Dub) (SDH)"),
    _Sub(18, "chi", "Dialogue (Simplified)", default=True),
    _Sub(19, "chi", "Dialogue (Traditional)", default=True),
]


def _pick_sub(spec, pool=EXAMPLE):
    wish = tracks.TrackWish.parse(spec)
    track, _ = tracks._pick(pool, wish, tracks._score_subtitle, "subtitle")
    return track


def test_plain_language_picks_dialogue_not_signs():
    """The classic failure: 'eng' matching a seven-cue Signs & Songs track."""
    assert _pick_sub("eng").id == 7


def test_colon_suffix_separates_same_language_tracks():
    assert _pick_sub("chi:traditional").id == 19
    assert _pick_sub("chi:simplified").id == 18


def test_signs_track_is_reachable_when_asked_for():
    assert _pick_sub("eng:signs").id == 5


def test_sdh_is_deprioritised_but_selectable():
    assert _pick_sub("eng").id == 7
    assert _pick_sub("eng:dub").id == 8


def test_track_id_overrides_everything():
    assert _pick_sub("#5").id == 5


def test_specific_request_with_no_match_returns_nothing():
    assert _pick_sub("chi:cantonese") is None


def test_commentary_is_never_picked_by_accident():
    pool = EXAMPLE + [_Sub(20, "eng", "Commentary", default=True)]
    assert _pick_sub("eng", pool).id == 7


def test_only_signs_available_explains_itself():
    pool = [_Sub(5, "eng", "Signs & Songs", forced=True)]
    assert _pick_sub("eng", pool) is None


# ------------------------------------------------------------------- rename

ANIME = ("[SavI0r] Tensei Kizoku no Isekai Boukenroku (The Aristocrat's "
         "Otherworldly Adventure) - S01E06 - A Date With the Strongest "
         "Captain of the Knights [BD][1080p][AV1][OPUS][Dual Audio]")


def test_episode_style_keeps_only_what_identifies_the_episode():
    assert rename.clean_name(ANIME, "episode") == \
        "S01E06 - A Date With the Strongest Captain of the Knights"


def test_tags_style_keeps_the_series_name():
    out = rename.clean_name(ANIME, "tags")
    assert out.startswith("Tensei Kizoku") and "S01E06" in out
    assert "[BD]" not in out and "1080p" not in out


def test_scene_names_split_on_dots():
    """Regression: release tokens are invisible inside a dotted name, because
    nothing downstream splits on anything but whitespace."""
    assert rename.clean_name("The.Matrix.1999.2160p.UHD.BluRay.x265.HDR.Atmos-GROUP") \
        == "The Matrix 1999"
    assert rename.clean_name("Ne.Zha.2019.1080p.WEB-DL.x264") == "Ne Zha 2019"


def test_a_title_containing_a_dot_is_left_alone():
    assert rename.clean_name("Mr. Nobody (2009) 1080p BluRay") == "Mr. Nobody (2009)"


def test_multi_episode_labels_survive():
    assert rename.clean_name("Show.Name.S02E13E14.Double.Episode.1080p.WEB-DL") \
        == "S02E13-E14 - Double Episode"


def test_year_is_kept_but_other_parentheticals_are_not():
    assert "(2009)" in rename.clean_name("Film (2009) (Directors Cut Remux)", "tags")


def test_illegal_windows_characters_are_removed():
    assert ":" not in rename.clean_name("Show - S01E01 - Part 1: The End")


def test_rename_round_trip_moves_sidecars_and_undoes(tmp_path=None):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        video = folder / (ANIME + ".mkv")
        subs = folder / (ANIME + ".eng.srt")
        video.write_bytes(b"x")
        subs.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")

        plans = rename.plan_renames([video])
        assert plans[0].changed and plans[0].sidecars
        count, problems = rename.apply_renames(plans)
        assert count == 1 and not problems
        assert not video.exists()
        assert (folder / "S01E06 - A Date With the Strongest Captain of the Knights.mkv").exists()
        assert (folder / "S01E06 - A Date With the Strongest Captain of the Knights.eng.srt").exists()

        back, problems = rename.undo_renames(folder)
        assert back == 2 and not problems
        assert video.exists() and subs.exists()


def test_renaming_never_overwrites_an_existing_file():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "S01E06 - Title.mkv").write_bytes(b"old")
        victim = folder / "[Group] Show - S01E06 - Title [1080p].mkv"
        victim.write_bytes(b"new")
        plan = rename.plan_renames([victim])[0]
        assert plan.target.name != "S01E06 - Title.mkv"
        assert (folder / "S01E06 - Title.mkv").read_bytes() == b"old"


# ------------------------------------------------------------------- survey

def test_distinguishing_words_keeps_what_identifies_a_track():
    assert survey.distinguishing_words("Dialogue (Traditional)", "chi") == "traditional"
    assert survey.distinguishing_words("Dialogue (Dub) (SDH)", "eng") == "dub sdh"
    assert survey.distinguishing_words("Signs & Songs", "eng") == "signs songs"
    assert survey.distinguishing_words("Dialogue", "eng") == ""


def test_the_language_own_name_is_dropped_but_aliases_are_not():
    """Regression: "simplified" and "traditional" are aliases of Chinese in the
    language table. Stripping every alias collapsed the two Chinese tracks
    into one option -- exactly what these selectors exist to tell apart."""
    assert survey.distinguishing_words("English", "eng") == ""
    assert survey.distinguishing_words("Japanese", "jpn") == ""
    assert survey.distinguishing_words("Dialogue (Simplified)", "chi") == "simplified"
    assert survey.distinguishing_words("Dialogue (Traditional)", "chi") == "traditional"


def test_spec_round_trips_through_a_combobox_entry():
    option = survey.TrackOption(kind="subtitle", language="chi",
                                raw_language="chi", prefer="traditional",
                                files=3, total=3)
    assert option.spec == "chi:traditional"
    assert survey.spec_of(option.entry()) == "chi:traditional"


def test_a_hand_typed_spec_passes_through_untouched():
    """Anything the scan did not find must still be usable."""
    assert survey.spec_of("hun") == "hun"
    assert survey.spec_of("chi:cantonese") == "chi:cantonese"
    assert survey.spec_of("#19") == "#19"
    assert survey.spec_of("") == ""


def test_entry_for_falls_back_to_the_bare_spec():
    options = [survey.TrackOption(kind="subtitle", language="eng",
                                  raw_language="eng", files=1, total=1)]
    assert survey.entry_for("eng", options).startswith("eng")
    assert survey.entry_for("hun", options) == "hun"


def test_partial_coverage_is_reported_not_hidden():
    o = survey.TrackOption(kind="subtitle", language="chi", raw_language="chi",
                           prefer="traditional", files=7, total=8)
    assert not o.everywhere and "7 of 8" in o.describe()
    o.files = 8
    assert o.everywhere and "all files" in o.describe()


# ------------------------------------------------------------------- textio

def _srt(body: str, n: int = 60) -> str:
    return "".join(
        f"{i}\r\n00:0{i % 9}:0{i % 6},000 --> 00:0{i % 9}:0{(i % 6) + 1},000\r\n{body}\r\n\r\n"
        for i in range(1, n)
    )


SIMPLIFIED = "我发现他的头发长了，给我张支票，什么意思，让我踢假球啊？不是…我是中间人。"
TRADITIONAL = "我發現他的頭髮長了，給我張支票，什麼意思，讓我踢假球啊？不是…我是中間人。"


def _detect(raw, language=None):
    """Force the built-in heuristic; charset_normalizer may or may not exist."""
    real = textio._external_guess
    textio._external_guess = lambda _raw: None
    try:
        return textio.detect_encoding(raw, language)
    finally:
        textio._external_guess = real


def test_legacy_chinese_encodings_round_trip():
    for encoding, source in (("gb18030", SIMPLIFIED), ("gbk", SIMPLIFIED),
                             ("gb2312", SIMPLIFIED), ("big5", TRADITIONAL),
                             ("cp950", TRADITIONAL)):
        text = _srt(source)
        raw = text.encode(encoding)
        got, _ = _detect(raw, "chi")
        assert raw.decode(got) == text, f"{encoding} detected as {got}"


def test_utf8_and_boms_win_outright():
    assert _detect(_srt(SIMPLIFIED).encode("utf-8"), "chi")[0] == "utf-8"
    assert _detect("\ufeffhello".encode("utf-8-sig"))[0] == "utf-8-sig"
    assert _detect("hello".encode("utf-16-le") and b"\xff\xfeh\x00")[0] == "utf-16-le"


def test_cp1252_never_wins_on_a_technicality():
    """latin-1 and cp1252 decode any byte at all, so a GB file must not be
    read as mojibake just because those codecs did not raise."""
    raw = _srt(SIMPLIFIED).encode("gb18030")
    got, _ = _detect(raw, "chi")
    assert got not in ("cp1252", "latin-1")


def test_language_hint_settles_an_ambiguous_case():
    """Korean bytes also decode cleanly as GB18030. Bytes alone cannot
    separate them; the language we asked for can."""
    text = _srt("이것은 한국어 자막입니다. 테스트 중입니다.")
    raw = text.encode("euc-kr")
    assert raw.decode(_detect(raw, "kor")[0]) == text


def test_reencode_is_idempotent_and_keeps_a_backup():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "subs.srt"
        text = _srt(SIMPLIFIED)
        path.write_bytes(text.encode("gb18030"))

        was, changed = textio.reencode_file(path, language="chi")
        assert changed and was == "gb18030"
        # read_bytes, not read_text: the latter translates newlines and would
        # hide whether CRLF survived the round trip.
        assert path.read_bytes().decode("utf-8") == text
        assert (Path(tmp) / "subs.srt.orig").exists()

        was, changed = textio.reencode_file(path, language="chi")
        assert not changed and path.read_bytes().decode("utf-8") == text


def test_crlf_survives_reencoding():
    """Regression: write_text() translates "\n" to os.linesep, which on
    Windows would turn an SRT's "\r\n" into "\r\r\n"."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "crlf.srt"
        text = "1\r\n00:00:01,000 --> 00:00:02,000\r\n我发现\r\n\r\n"
        path.write_bytes(text.encode("gb18030"))
        textio.reencode_file(path, language="chi")
        raw = path.read_bytes()
        assert b"\r\r\n" not in raw
        assert raw.decode("utf-8") == text


def test_empty_and_binary_input_do_not_raise():
    assert textio.detect_encoding(b"")[0] == "utf-8"
    textio.detect_encoding(bytes(range(256)))


# -------------------------------------------------------------------- align

def test_close_cue_pairs_get_one_start_and_end():
    # the reported case: bottom authored 300 ms ahead of top
    top = [Event(14365, 14627, "What is the meaning of this check?")]
    bot = [Event(14335, 14597, "给我张支票")]
    r = align.snap(top, bot)
    assert r.top[0].start == r.bottom[0].start == 14335
    assert r.top[0].end == r.bottom[0].end == 14627


def test_distant_cues_are_left_alone():
    top = [Event(1000, 1200, "a")]
    bot = [Event(1090, 1500, "x")]        # 90 cs and 300 cs apart
    r = align.snap(top, bot, window_cs=50)
    assert (r.top[0].start, r.top[0].end) == (1000, 1200)
    assert (r.bottom[0].start, r.bottom[0].end) == (1090, 1500)


def test_snapping_never_reorders_neighbours():
    top = [Event(1000, 1200, "a"), Event(1210, 1400, "b")]
    bot = [Event(1000, 1240, "x"), Event(1250, 1400, "y")]
    r = align.snap(top, bot)
    assert r.top[0].end < r.top[1].start
    assert r.bottom[0].end < r.bottom[1].start


def test_unpaired_cues_survive():
    top = [Event(1000, 1200, "a"), Event(5000, 5200, "b")]
    bot = [Event(1005, 1195, "x"), Event(9000, 9200, "y")]
    r = align.snap(top, bot)
    assert (r.top[1].start, r.top[1].end) == (5000, 5200)
    assert (r.bottom[1].start, r.bottom[1].end) == (9000, 9200)
    assert len(r.top) == 2 and len(r.bottom) == 2


def test_snapping_never_trims_more_than_the_window():
    """Regression: the neighbour clamp used to truncate cues that already
    overlapped their own next cue -- one lost 0.77s. Trimming is allowed now,
    since meeting a neighbour at the boundary still aligns the pair, but it is
    capped at the snap window so the cure stays smaller than the disease."""
    window = 50
    top = [Event(500, 740, "a"), Event(660, 900, "b")]   # a overlaps b
    bot = [Event(470, 710, "x"), Event(630, 870, "y")]
    r = align.snap(top, bot, window_cs=window)
    for before, after in zip(top + bot, r.top + r.bottom):
        lost = (before.end - before.start) - (after.end - after.start)
        assert lost <= window, f"{before} trimmed by {lost} to {after}"


def test_snapping_creates_no_new_self_overlap():
    top = [Event(500, 740, "a"), Event(760, 900, "b")]
    bot = [Event(470, 710, "x"), Event(730, 870, "y")]
    r = align.snap(top, bot)
    for side in (r.top, r.bottom):
        for a, b in zip(side, side[1:]):
            assert a.end < b.start, f"{a} overlaps {b}"


def test_whole_track_offset_is_removed_first():
    """A constant lag between the two tracks is a shift, not per-cue jitter:
    pulling cues individually would collide with their own neighbours."""
    starts = list(range(1000, 1000 + 40 * 300, 300))
    top = [Event(s, s + 250, f"top {n}") for n, s in enumerate(starts)]
    bot = [Event(s + 30, s + 280, f"bot {n}") for n, s in enumerate(starts)]
    r = align.snap(top, bot)
    assert r.offset.applied and r.offset.delta_cs == 30 and r.moved == "bottom"
    assert all(t.start == b.start for t, b in zip(r.top, r.bottom))


def test_inconsistent_difference_is_not_shifted():
    import random
    rng = random.Random(4)
    starts = list(range(1000, 1000 + 40 * 300, 300))
    top = [Event(s, s + 250, "t") for s in starts]
    bot = [Event(s + rng.choice([-40, 0, 45]), s + 250, "b") for s in starts]
    r = align.snap(top, bot)
    assert not r.offset.applied


def test_grid_handles_a_dub_that_reflows_lines():
    """Three cues on one side against two on the other: no one-to-one pairing
    exists, so pair snapping cannot help. Grid mode pulls the moving side onto
    the anchor's boundaries instead."""
    anchor = [Event(1000, 1196, "a"), Event(1200, 1396, "b"), Event(1400, 1596, "c")]
    moving = [Event(1094, 1278, "x"), Event(1190, 1596, "y")]
    r = align.grid_snap(anchor, moving, window_cs=120)
    starts = {e.start for e in anchor}
    assert all(e.start in starts for e in r.moving)
    assert r.on_grid_after > r.on_grid_before
    assert len(r.moving) == len(moving)


def test_grid_leaves_distant_cues_alone():
    anchor = [Event(1000, 1200, "a")]
    moving = [Event(3000, 3200, "x")]
    r = align.grid_snap(anchor, moving, window_cs=50)
    assert (r.moving[0].start, r.moving[0].end) == (3000, 3200)
    assert r.moved == 0


def test_grid_never_reorders_or_collapses():
    anchor = [Event(1000, 1100, "a"), Event(1105, 1200, "b")]
    moving = [Event(1050, 1150, "x"), Event(1060, 1250, "y")]
    r = align.grid_snap(anchor, moving, window_cs=100)
    for a, b in zip(r.moving, r.moving[1:]):
        assert a.end <= b.start or a.start < b.start
    for e in r.moving:
        assert e.end > e.start


def test_grid_is_a_no_op_on_matching_tracks():
    events = [Event(1000, 1200, "a"), Event(1400, 1600, "b")]
    r = align.grid_snap(events, [Event(e.start, e.end, "x") for e in events], 100)
    assert r.moved == 0


def test_snapping_off_or_empty_is_safe():
    top = [Event(1000, 1200, "a")]
    assert align.snap(top, [], 50).top is top
    assert align.snap(top, [Event(1000, 1200, "x")], 0).top is top


# -------------------------------------------------------------------- hanzi

def test_traditional_to_simplified():
    cases = [
        ("我發現他的頭髮長了", "我发现他的头发长了"),   # 髮 -> 发 needs the phrase pass
        ("一目瞭然", "一目了然"),                      # not the bare 瞭 -> 了
        ("乾杯", "干杯"),
        ("後臺的鐘錶壞了", "后台的钟表坏了"),
        ("裏面有隻貓", "里面有只猫"),                  # TW variant 裏 -> 裡 -> 里
        ("Hello, world!", "Hello, world!"),
        ("我发现他的头发长了", "我发现他的头发长了"),   # already simplified
    ]
    for src, want in cases:
        got = hanzi.to_simplified(src, use_opencc=False)
        assert got == want, f"{src} -> {got}, wanted {want}"


def test_script_detection():
    assert hanzi.detect_script("這是繁體中文字幕測試內容")[0] == "traditional"
    assert hanzi.detect_script("这是简体中文字幕测试内容")[0] == "simplified"
    assert hanzi.detect_script("Just some English subtitles.")[0] == "unknown"


def test_auto_mode_leaves_simplified_alone():
    events = [Event(0, 100, "这是简体中文字幕测试内容")]
    out, note = hanzi.convert_events(events, "auto")
    assert out[0].text == events[0].text and note == "already simplified"


def test_auto_mode_is_silent_on_non_chinese():
    events = [Event(0, 100, "Just some English subtitles.")]
    out, note = hanzi.convert_events(events, "auto")
    assert out is events and note == ""


def test_auto_mode_converts_traditional():
    events = [Event(0, 100, "這是繁體中文字幕測試內容")]
    out, note = hanzi.convert_events(events, "auto")
    assert out[0].text == "这是繁体中文字幕测试内容" and "converted" in note


def test_conversion_off_is_a_no_op():
    events = [Event(0, 100, "繁體")]
    out, note = hanzi.convert_events(events, "")
    assert out is events and note == ""


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
