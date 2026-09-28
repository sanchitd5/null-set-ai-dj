"""set_learner on synthetic click-track songs with a known set layout."""
from pathlib import Path
import numpy as np

from app.music_brain import set_learner as sl
from app.music_brain import techniques as tq

SR = sl.SR


def _clicks(seconds, seed, gap=(0.12, 0.45)):
    rng = np.random.default_rng(seed)
    y = np.zeros(int(seconds * SR))
    t = 0.0
    decay = np.exp(-np.arange(int(0.03 * SR)) / (0.006 * SR))
    while t < seconds - 0.05:
        i = int(t * SR)
        y[i:i + len(decay)] += 0.6 * decay * rng.choice([-1, 1], len(decay))
        t += rng.uniform(*gap)                 # aperiodic: one best offset per window
    return y


def _song(title, start, seed, seconds=80):
    stems = {n: _clicks(seconds, seed * 10 + k) for k, n in enumerate(sl.STEMS)}
    return stems, sl.SongData(title, start, 124.0, "8A", {n: sl.onset_env(y) for n, y in stems.items()})


def _place(dst, src, set_t, src_t, dur):
    a, b = int(set_t * SR), int(src_t * SR)
    n = min(int(dur * SR), len(dst) - a, len(src) - b)
    dst[a:a + n] += src[b:b + n]


def test_parse_tracklist_skips_ids_and_sorts():
    e = sl.parse_tracklist("intro text\n1:06:30 Aerodynamic - Daft Punk\n00:00 Fred again.. - Kyle\n12:01 ID - ID\n[3:05] B - C")
    assert [(x.start, x.title) for x in e] == [(0.0, "Fred again.. - Kyle"), (185.0, "B - C"), (3990.0, "Aerodynamic - Daft Punk")]
    m = sl.parse_tracklist("| 1:06:30 | Aerodynamic x Victory Lap Five | 2:37 |\n20:50 Quiereme x2\n1:55:18 end")
    assert [(x.start, x.title) for x in m] == [(1250.0, "Quiereme"), (3990.0, "Aerodynamic"), (3990.0, "Victory Lap Five")]


def test_ncc_finds_the_offset():
    x = np.random.default_rng(0).normal(size=500)
    c = sl.ncc(x[200:260], x)
    assert int(np.argmax(c)) == 200 and c[200] > 0.99


def test_transition_order_is_learned(tmp_path):
    a_st, A = _song("A - One", 0.0, 1)
    b_st, B = _song("B - Two", 36.0, 2)
    dur = 80.0
    mix = {n: np.zeros(int(dur * SR)) for n in sl.STEMS}
    for n in ("drums", "other", "vocals"):
        _place(mix[n], a_st[n], 0, 0, 44)             # A's top runs to 44
    _place(mix["bass"], a_st["bass"], 0, 0, 36)       # A owns the low end to 36
    for n in ("drums", "other"):
        _place(mix[n], b_st[n], 24, 0, 56)            # B's drums/top in at 24 (stem intro)
    _place(mix["bass"], b_st["bass"], 36, 12, 44)     # bass swap at 36
    rows = sl.stem_timeline(mix, [A, B], lambda t: 0.0)

    owner = {(r["t"], r["stem"]): r["owner"] for r in rows}
    assert owner[(8.0, "bass")].track == 0 and abs(owner[(8.0, "bass")].src_t - 8.0) < 0.1
    assert owner[(48.0, "bass")].track == 1 and abs(owner[(48.0, "bass")].src_t - 24.0) < 0.1

    obs = sl.transitions(rows, [A, B], "synthetic")
    kinds = {o.kind for o in obs}
    assert "bass_swap" in kinds and "stem_intro" in kinds, obs
    intro = next(o for o in obs if o.kind == "stem_intro")
    assert intro.detail["order"][-1] == "bass"

    store = sl.merge(obs, tmp_path / "learned.json")
    assert store["bass_swap"]["count"] == 1 and store["bass_swap"]["tempo_gap_max"] == 0.0
    again = sl.merge(obs, tmp_path / "learned.json")               # re-learning a set replaces, not doubles
    assert again["bass_swap"]["count"] == 1


def test_vocal_lines_played_out_of_order_are_learned():
    c_st, C = _song("C - Three", 0.0, 3, seconds=60)
    mix = {n: np.zeros(int(40 * SR)) for n in sl.STEMS}
    # the DJ's new "lyric": line 20-26, line 4-10, line 20-26 again, line 40-46
    t = 0.0
    for src in (20, 4, 20, 40):
        _place(mix["vocals"], c_st["vocals"], t, src, 6)
        t += 6
    vrows = sl.stem_timeline(mix, [C], lambda t: 0.0, sl.VWIN_S, sl.VHOP_S, only=("vocals",), min_r=sl.VOCAL_MATCH_R, track=False)
    obs = sl.vocal_recuts(vrows, [C], "synthetic")
    kinds = {o.kind for o in obs}
    assert "vocal_resequence" in kinds, obs
    starts = [round(a) for a, _ in next(o for o in obs if o.kind == "vocal_resequence").detail["source_lines"]]
    assert starts[:2] == [20, 4]


def test_user_rules_survive_relearning_and_disable(tmp_path):
    p = tmp_path / "learned.json"
    o = sl.Observation("acapella_over", "setX", 60.0, "A", "B", 0.01, 1.0, {"vocal_from": "A"})
    sl.merge([o], p)
    sl.add_user_rule("acapella_over", "rap ~9 dB under the riff", path=p)
    store = sl.merge([o], p)                                        # re-learn the same set
    assert store["acapella_over"]["user_rules"][0]["text"] == "rap ~9 dB under the riff"
    r = next(x for x in tq.rank(tq.PairFeatures(128, 128, vocal_a_exit=0.5, stems_a=True, stems_b=True), learned=store)
             if x["name"] == "learned:acapella_over")
    assert "ok: user rule: rap ~9 dB under the riff" in r["reasons"]
    store = sl.add_user_rule("acapella_over", disable=True, path=p) and sl.load_learned(p)
    assert not any(x["name"] == "learned:acapella_over" for x in tq.rank(tq.PairFeatures(128, 128), learned=store))
    try:
        sl.add_user_rule("nope", "x", path=p)
        assert False
    except ValueError:
        pass


def test_rank_includes_learned_with_conditions():
    store = {"bass_swap": {"kind": "bass_swap", "what": "x", "stems": False, "live": True, "count": 1,
                           "tempo_gap_max": 0.02, "key_score_min": 0.9,
                           "observations": [{"set_id": "s", "at": 90, "track_a": "A", "track_b": "B",
                                             "tempo_gap": 0.02, "key_score": 0.9, "detail": {}}]}}
    near = next(x for x in tq.rank(tq.PairFeatures(128, 130, "8A", "9A"), learned=store) if x["name"] == "learned:bass_swap")
    far = next(x for x in tq.rank(tq.PairFeatures(128, 174, "8A", "9A"), learned=store) if x["name"] == "learned:bass_swap")
    assert near["fits"] and not far["fits"]
    assert near["source"].startswith("s 1:30 A -> B")


def test_plan_clips_pads_boundaries_and_merges_overlaps():
    assert sl.plan_clips([0, 300, 300, 400, 2000], 2050, pad=96) == [(204.0, 496.0), (1904.0, 2050.0)]
    assert sl.plan_clips([0, 0, 0], 350.0) == [(0.0, 350.0)]                  # short edit: whole file


def test_clip_offset_gives_set_time(tmp_path):
    a_st, A = _song("A - One", 1000.0, 1)
    clip = {n: np.zeros(int(40 * SR)) for n in sl.STEMS}
    for n in sl.STEMS:
        _place(clip[n], a_st[n], 0, 10, 40)           # clip starts at set 1000 s, A at its 10 s
    rows = sl.stem_timeline(clip, [A], lambda t: 0.0, t0=1000.0)
    r = next(r for r in rows if r["stem"] == "bass" and r["t"] == 1008.0)
    assert r["owner"].track == 0 and abs(r["owner"].src_t - 18.0) < 0.1


def test_parallel_keeps_order_and_survives_a_failure():
    logs = []

    def fn(x):
        if x == 2:
            raise RuntimeError("boom")
        return x * 10
    assert sl._parallel(fn, [1, 2, 3], 2, logs.append, str) == [10, None, 30]
    assert logs == ["failed 2: boom"]


def test_clip_audio_cuts_with_ffmpeg(tmp_path):
    import shutil
    import soundfile as sf
    if not shutil.which("ffmpeg"):
        return
    src = tmp_path / "set.wav"
    sf.write(src, np.random.default_rng(0).normal(0, 0.1, (44100 * 10, 2)), 44100)
    out = sl.clip_audio(src, 2.0, 5.0, tmp_path / "clips")
    y, sr = sf.read(out)
    assert sr == 44100 and abs(len(y) - 3 * 44100) < 50
    assert sl.clip_audio(src, 2.0, 5.0, tmp_path / "clips").read_bytes() == out.read_bytes()


def test_layered_entries_share_a_slot():
    songs = [sl.SongData(t, 0.0, 120, None, {}) for t in ("A", "B", "C")] + [sl.SongData("D", 600.0, 120, None, {})]
    assert sl._candidates(300.0, songs) == [0, 1, 2]


def test_vocal_chops_are_learned():
    voc = _clicks(60, 44, gap=(0.04, 0.12))            # syllable-dense, like a real vocal stem
    C = sl.SongData("C - Chop", 0.0, 124.0, "8A", {"vocals": sl.onset_env(voc)})
    c_st = {"vocals": voc}
    mix = {n: np.zeros(int(20 * SR)) for n in sl.STEMS}
    t = 2.0
    for src in (30, 5, 44, 12, 30, 20):                 # 1.5 s fragments from all over the song
        _place(mix["vocals"], c_st["vocals"], t, src, 1.5)
        t += 1.5
    crows = sl.stem_timeline(mix, [C], lambda t: 0.0, sl.CWIN_S, sl.CHOP_HOP_S, only=("vocals",),
                             min_r=sl.CHOP_MATCH_R, track=False)
    obs = sl.vocal_chops(crows, [C], "s")
    assert len(obs) == 1 and obs[0].detail["jumps"] >= 3, obs
    starts = [round(a) for a, _ in obs[0].detail["fragments"]]
    assert starts[:3] == [30, 5, 44]


def test_learn_set_end_to_end_with_stubs(tmp_path, monkeypatch):
    """learn_set wiring: tracklist -> songs -> ffmpeg clip -> (stub) Demucs -> observations -> store."""
    import shutil
    import types
    import soundfile as sf
    if not shutil.which("ffmpeg"):
        return
    from app.music_brain import analyzer, lyrics, stem_service

    a_st, _ = _song("A - One", 0.0, 1)
    b_st, _ = _song("B - Two", 36.0, 2)
    mix = {n: np.zeros(int(80 * SR)) for n in sl.STEMS}
    for n in ("drums", "other", "vocals"):
        _place(mix[n], a_st[n], 0, 0, 44)
    _place(mix["bass"], a_st["bass"], 0, 0, 36)
    for n in ("drums", "other"):
        _place(mix[n], b_st[n], 24, 0, 56)
    _place(mix["bass"], b_st["bass"], 36, 12, 44)

    def write(stems, d):
        d.mkdir(parents=True, exist_ok=True)
        out = {}
        for n, y in stems.items():
            sf.write(d / f"{n}.wav", y, SR)
            out[n] = str(d / f"{n}.wav")
        return out
    set_path = tmp_path / "set.wav"
    sf.write(set_path, sum(mix.values()), SR)
    by_path = {str(tmp_path / "a.wav"): write(a_st, tmp_path / "sa"), str(tmp_path / "b.wav"): write(b_st, tmp_path / "sb")}
    for p in ("a.wav", "b.wav"):
        sf.write(tmp_path / p, np.zeros(SR), SR)

    def fake_separate(path, **kw):
        path = str(path)
        if path in by_path:
            return types.SimpleNamespace(stems=by_path[path])
        t0, t1 = (float(x) for x in Path(path).stem.split("-"))       # a clip of the set
        return types.SimpleNamespace(stems=write({n: y[int(t0 * SR):int(t1 * SR)] for n, y in mix.items()}, tmp_path / "sc"))

    monkeypatch.setattr(stem_service, "separate", fake_separate)
    monkeypatch.setattr(analyzer, "analyze", lambda p: types.SimpleNamespace(bpm=124.0, key=None))
    monkeypatch.setattr(lyrics, "fetch", lambda title, **kw: [])
    monkeypatch.setattr(sl, "SETS_DIR", tmp_path / "sets")
    monkeypatch.setattr(sl, "fetch_set", lambda src: (set_path, "e2e", ""))
    monkeypatch.setattr(sl, "find_or_fetch_song", lambda title, d, download=True, **kw: tmp_path / ("a.wav" if title.startswith("A") else "b.wav"))

    rep = sl.learn_set("x", tracklist="0:00 A - One\n0:36 B - Two", store_path=tmp_path / "learned.json", jobs=2, ai=False)
    kinds = {o["kind"] for o in rep["observations"]}
    assert {"bass_swap", "stem_intro"} <= kinds, rep["observations"]
    assert rep["clips"] == [(0.0, 80.0)] and not any(t["likely_wrong_song"] for t in rep["tracks"])
    assert sl.load_learned(tmp_path / "learned.json")["bass_swap"]["count"] >= 1


def test_repeated_chorus_is_not_a_resequence():
    voc = _clicks(60, 5)
    voc[int(40 * SR):int(52 * SR)] = voc[int(20 * SR):int(32 * SR)]    # chorus at 20 and again at 40
    C = sl.SongData("C - Chorus", 0.0, 124.0, "8A", {"vocals": sl.onset_env(voc)})
    assert sl.same_material(C, 22, 42, 3.0) and not sl.same_material(C, 5, 42, 3.0)
    mix = {n: np.zeros(int(20 * SR)) for n in sl.STEMS}
    _place(mix["vocals"], voc, 0, 16, 18)                              # played straight through 16..34
    vrows = sl.stem_timeline(mix, [C], lambda t: 0.0, sl.VWIN_S, sl.VHOP_S, only=("vocals",),
                             min_r=sl.VOCAL_MATCH_R, track=False)
    assert not any(o.kind == "vocal_resequence" for o in sl.vocal_recuts(vrows, [C], "s"))


def test_vocal_hits_where_the_song_is_silent_are_dropped():
    s = sl.SongData("S", 0, 120, None, {}, vocal_db=np.array([-80.0] * 40 + [-20.0] * 40))   # sings from 10 s
    assert not sl.sung_at(s, 2.0, 3.0) and sl.sung_at(s, 12.0, 3.0)
    rows = [{"t": 0.0, "stem": "vocals", "db": -20, "owners": [sl.Hit(0, 0.9, 2.0, 1.0)], "owner": None},
            {"t": 1.0, "stem": "vocals", "db": -20, "owners": [sl.Hit(0, 0.9, 12.0, 1.0)], "owner": None}]
    kept = sl._only_sung(rows, [s], 3.0)
    assert kept[0]["owner"] is None and kept[1]["owner"].src_t == 12.0


def test_search_result_must_name_the_song():
    res = [{"title": "Sabrina - i am a party", "channel": "Sabrina", "id": "a", "duration": 200},
           {"title": "Fred again.. - i am a party (cover)", "channel": "x", "id": "b", "duration": 200},
           {"title": "i am a party - 1 hour mix", "channel": "Fred again..", "id": "c", "duration": 3600},
           {"title": "i am a party", "channel": "Fred again..", "id": "d", "duration": 212}]
    assert sl.pick_result(res, "Fred again..", "i am a party")["id"] == "d"
    assert sl.pick_result(res[:1], "Fred again..", "i am a party") is None
    assert sl.pick_result([{"title": "Bonita (Audio)", "channel": "J Balvin", "id": "e"}], "", "Bonita")["id"] == "e"


def test_the_set_itself_is_not_the_song():
    res = [{"title": "fred again.. - chanel x a new error x i am a party", "channel": "dump", "id": "rAJ", "duration": 347}]
    assert sl.pick_result(res, "Fred again..", "i am a party") is None
    assert sl.pick_result([{"title": "Cmon (LATIN MAFIA & Fred edit)", "channel": "Fred again..", "id": "z"}],
                          "", "Cmon (LATIN MAFIA & Fred edit)")["id"] == "z"


def test_layered_songs_are_not_handovers_and_half_time_folds():
    songs = [sl.SongData(t, s, 120, None, {}) for t, s in (("A", 0), ("B", 0), ("C", 300), ("D", 600), ("E", 600))]
    assert sl.handover_pairs(songs) == [(0, 2), (1, 2), (2, 3), (2, 4)]      # never A->B or D->E
    assert sl.tempo_gap(87.0, 174.0) == 0.0 and sl.tempo_gap(120.0, 126.0) == 0.05 and sl.tempo_gap(0, 120) is None


def test_local_set_needs_no_youtube(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(sl, "SETS_DIR", tmp_path)
    (tmp_path / "rAJ9Es-61ZE.mp3").write_bytes(b"x")
    monkeypatch.setitem(sys.modules, "yt_dlp", None)                   # any import of yt_dlp would fail
    p, sid, desc = sl.fetch_set("https://www.youtube.com/watch?v=rAJ9Es-61ZE&si=x")
    assert (p.name, sid, desc) == ("rAJ9Es-61ZE.mp3", "rAJ9Es-61ZE", "")


def test_set_download_found_by_title_when_named_by_youtube(tmp_path):
    d = tmp_path / "songs"
    d.mkdir()
    (d / "A_New_Error.mp3").write_bytes(b"x")
    (d / "Technologic.mp3").write_bytes(b"x"); (d / "Technologic_Live.mp3").write_bytes(b"x")
    lib = tmp_path / "lib"
    assert sl.find_or_fetch_song("Moderat - A New Error", d, library=lib, download=False).name == "A_New_Error.mp3"
    assert sl.find_or_fetch_song("Daft Punk - Technologic", d, library=lib, download=False) is None   # ambiguous


# ---------------------------------------------------------------- review fixes
def test_artist_collab_x_is_one_song_mashup_x_is_layers():
    e = sl.parse_tracklist("0:00 Fred again.. x Jon Hopkins - Open Eye Signal\n"
                           "3:00 Skrillex - Rumble x Fred again.. - Kyle\n"
                           "6:00 Aerodynamic x Victory Lap Five\n"
                           "9:00 Fred again.. x Skrillex x Four Tet - Baby again")
    assert [(x.start, x.title) for x in e] == [
        (0.0, "Fred again.. x Jon Hopkins - Open Eye Signal"),
        (180.0, "Skrillex - Rumble"), (180.0, "Fred again.. - Kyle"),
        (360.0, "Aerodynamic"), (360.0, "Victory Lap Five"),
        (540.0, "Fred again.. x Skrillex x Four Tet - Baby again")]


def test_fetch_set_refuses_an_id_that_escapes_the_sets_dir(tmp_path, monkeypatch):
    import sys, types, pytest
    from app.music_brain import yt_guard
    downloads = []

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False): return {"id": "../../evil", "title": "t"}
        def download(self, urls): downloads.append(urls)
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))
    monkeypatch.setattr(yt_guard, "call", lambda fn: fn({}))
    monkeypatch.setattr(sl, "SETS_DIR", tmp_path / "sets")
    with pytest.raises(ValueError, match="unusable video id"):
        sl.fetch_set("https://example.com/x/%2e%2e%2f%2e%2e%2fevil")
    assert downloads == [] and not (tmp_path / "evil.info.json").exists()


def test_cached_set_with_corrupt_meta_still_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "SETS_DIR", tmp_path)
    (tmp_path / "rAJ9Es-61ZE.mp3").write_bytes(b"x")
    (tmp_path / "rAJ9Es-61ZE.info.json").write_text("{not json", encoding="utf-8")
    assert sl.fetch_set("https://youtu.be/rAJ9Es-61ZE")[1:] == ("rAJ9Es-61ZE", "")


def test_relearning_a_set_that_now_finds_nothing_clears_it(tmp_path):
    p = tmp_path / "learned.json"
    o = sl.Observation("bass_swap", "setX", 60.0, "A", "B", 0.01, 1.0, {})
    keep = sl.Observation("bass_swap", "setY", 90.0, "C", "D", 0.02, 0.9, {})
    sl.merge([o, keep], p)
    store = sl.merge([], p, set_ids=("setX",))      # setX re-studied: its move was a wrong download
    assert [x["set_id"] for x in store["bass_swap"]["observations"]] == ["setY"]
    assert store["bass_swap"]["count"] == 1 and store["bass_swap"]["tempo_gap_max"] == 0.02


def test_a_disable_alone_survives_relearning(tmp_path):
    p = tmp_path / "learned.json"
    sl.add_user_rule("vocal_chop", disable=True, path=p)   # never seen yet, disabled up front
    sl.merge([sl.Observation("vocal_chop", "s", 9.0, "E")], p)
    sl.merge([], p, set_ids=("s",))
    assert sl.load_learned(p)["vocal_chop"]["disabled"] is True


def test_a_slot_never_analysed_is_not_called_the_wrong_song():
    songs = [sl.SongData("A", 0.0, 120, None, {"drums": np.zeros(4)}),
             sl.SongData("B", 600.0, 120, None, {"drums": np.zeros(4)})]
    rows = [{"t": 10.0, "stem": "drums", "db": -10, "owners": [sl.Hit(0, 0.9, 10.0, 1.0)]}]
    v = sl.verify_songs(rows, songs)                 # B's clip failed: no window in its slot
    assert v[0]["likely_wrong_song"] is False and v[1]["likely_wrong_song"] is False
    rows.append({"t": 610.0, "stem": "drums", "db": -10, "owners": []})
    assert sl.verify_songs(rows, songs)[1]["likely_wrong_song"] is True
