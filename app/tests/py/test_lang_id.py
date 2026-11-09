"""Sung-language id: clips, aggregation, set profile, verdict, and the gate every pick source goes through.
Synthetic names and synthetic audio only."""
import numpy as np

from app.music_brain.analysis import lang_id
from app.ui.services import lang_service, pick_pool
from app.ui.services.autopilot_service import _filter_suggestions


def rec(lang, conf=0.9, instr=False):
    return {"v": 1, "lang": lang, "conf": conf, "instr": instr}


# ---------------------------------------------------------------- clips + aggregation

def test_pick_clips_one_per_third_and_capped():
    regs = [(10, 60), (130, 140), (200, 290), (300, 305)]      # last is too short to be a clip
    clips = lang_id.pick_clips(regs)
    assert len(clips) == 3
    assert all(b - a <= lang_id.CLIP_S + 1e-6 for a, b in clips)
    assert clips == sorted(clips)


def test_pick_clips_fills_a_gap_third_from_a_long_span():
    clips = lang_id.pick_clips([(0, 90)])                       # all singing sits in the first third
    assert len(clips) == 3
    assert len({c for c in clips}) == 3


def test_pick_clips_none_for_blips():
    assert lang_id.pick_clips([(1, 3), (10, 12)]) == []


def test_decide_instrumental_and_unsure_never_name_a_language():
    assert lang_id.decide([], 3.0)["instr"] is True
    assert lang_id.decide([{"xx": 0.2, "yy": 0.2}], 120.0)["lang"] is None
    r = lang_id.decide([{"xx": 0.9}, {"xx": 0.7, "yy": 0.2}], 120.0)
    assert (r["lang"], r["instr"]) == ("xx", False) and r["conf"] == 0.8


class _Eng:
    def __init__(self, probs):
        self.probs_seq = list(probs)
        self.n = 0

    def probs(self, audio):
        self.n += 1
        return self.probs_seq[(self.n - 1) % len(self.probs_seq)]


def test_identify_runs_the_engine_per_clip_on_generated_audio():
    sr = 16000
    y = (0.3 * np.sin(2 * np.pi * 220 * np.arange(sr * 100) / sr)).astype(np.float32)   # 100 s of a tone = "singing"
    eng = _Eng([{"zz": 0.95}])
    r = lang_id.identify(y, engine=eng, regions=[(0, 100)])
    assert r["lang"] == "zz" and eng.n == 3 and r["clips"] == 3


def test_identify_short_vocal_is_instrumental_without_the_engine():
    eng = _Eng([{"zz": 1.0}])
    r = lang_id.identify(np.zeros(16000 * 30, dtype=np.float32), engine=eng, regions=[(0, 4)])
    assert r["instr"] and eng.n == 0


# ---------------------------------------------------------------- profile + verdict

def test_profile_pure_mixed_and_empty():
    assert lang_id.profile_of([None, rec(None, instr=True)])["n"] == 0
    pure = lang_id.profile_of([rec("aa")] * 5)
    assert pure["pure"] and pure["top"] == "aa"
    mixed = lang_id.profile_of([rec("aa"), rec("bb"), rec("aa"), rec("bb")])
    assert not mixed["pure"] and set(mixed["share"]) == {"aa", "bb"}


def test_profile_weights_the_recent_songs():
    p = lang_id.profile_of([rec("aa")] * 6 + [rec("bb")] * 2)
    assert p["share"]["bb"] > 2 / 8


def test_verdict_rules():
    pure = lang_id.profile_of([rec("aa")] * 4)
    mixed = lang_id.profile_of([rec("aa"), rec("bb")] * 2)
    assert lang_id.verdict(rec("aa"), pure)[0] == "ok"
    v, why = lang_id.verdict(rec("cc"), pure)
    assert v == "block" and why.startswith("language jump: cc not in set (aa ")
    assert lang_id.verdict(rec("cc"), mixed)[0] == "penalty"
    assert lang_id.verdict(rec("aa"), mixed)[0] == "ok"
    assert lang_id.verdict(None, pure)[0] == "unknown"
    assert lang_id.verdict(rec(None, instr=True), pure)[0] == "ok"      # an instrumental never blocks
    assert lang_id.verdict(rec("cc"), lang_id.profile_of([]))[0] == "ok"   # no profile yet


def test_language_groups_are_one_language_for_the_set():
    pure = lang_id.profile_of([rec("hi")] * 4)
    assert lang_id.verdict(rec("pa"), pure)[0] == "ok"
    assert lang_id.verdict(rec("ur"), pure)[0] == "ok"
    assert lang_id.verdict(rec("en"), pure)[0] == "block"


# ---------------------------------------------------------------- pool

def test_pool_drops_other_language_library_songs_and_keeps_unknown():
    langs = {"t1": rec("aa"), "t2": rec("cc")}
    prof = lang_id.profile_of([rec("aa")] * 3)
    chk = lambda r: lang_id.verdict(langs.get(r.get("track_id")), prof)       # noqa: E731
    atl = [{"artist": "A", "title": "One", "source": "library", "track_id": "t1"},
           {"artist": "B", "title": "Two", "source": "library", "track_id": "t2"},
           {"artist": "C", "title": "Three", "source": "library", "track_id": "t3"}]
    pool = pick_pool.build_pool(atlas=atl, cat=[], exclude=[], seed_title="Seed", lang_check=chk)
    assert [c["title"] for c in pool] == ["One", "Three"]


def test_pool_sends_off_language_to_the_back_in_a_mixed_set():
    prof = lang_id.profile_of([rec("aa"), rec("bb")] * 2)
    chk = lambda r: lang_id.verdict(rec("cc") if r["title"] == "Odd" else rec("aa"), prof)    # noqa: E731
    cat = [{"artist": "A", "title": "Odd", "source": "catalogue"}, {"artist": "B", "title": "Fine", "source": "catalogue"}]
    pool = pick_pool.build_pool(atlas=[], cat=cat, exclude=[], seed_title="Seed", lang_check=chk)
    assert [c["title"] for c in pool] == ["Fine", "Odd"]


# ---------------------------------------------------------------- REGRESSION: a one-language set, every source

# Shape of the owner's set on 2026-11-09 (two songs in one language, then a song in another language arrived
# from a path that had no language gate). Names are synthetic.
LIB = {"id1": "Aaa Bbb - Song One", "id2": "Aaa Ccc - Song Two", "id3": "Ddd Eee - Song Foreign", "id4": "Fff Ggg - Song Same"}


def _store(monkeypatch):
    recs = {"id1": rec("hi"), "id2": rec("pa"), "id3": rec("en"), "id4": rec("hi")}
    monkeypatch.setattr(lang_id, "get", lambda tid: recs.get(tid))


def test_mono_language_set_never_lands_on_another_language_from_any_source(monkeypatch):
    _store(monkeypatch)
    prof = lang_service.profile_for(LIB, ["Aaa Bbb - Song One"], playing_id="id2")
    assert prof["pure"] and prof["top"] == "hi"

    # 1. server suggest (final filter): by library track id and by plain name
    chk = lambda s: lang_service.check(LIB, prof, s.get("track_id"), f"{s['artist']} - {s['title']}")   # noqa: E731
    data = {"steering": "stay", "suggestions": [
        {"artist": "Ddd Eee", "title": "Song Foreign", "track_id": "id3"},
        {"artist": "Ddd Eee", "title": "Song Foreign"},
        {"artist": "Fff Ggg", "title": "Song Same"}]}
    kept = _filter_suggestions(data, [], lang_check=chk)
    assert [k["title"] for k in kept] == ["Song Same"]

    # 2. phone sources (up-next, auto-fill, queue refill, LEAD TO bridge) ask the check endpoint's function
    for kw in ({"track_id": "id3"}, {"name": "Ddd Eee - Song Foreign (Official Video)"}):
        verdict, why = lang_service.check(LIB, prof, **kw)
        assert verdict == "block" and "language jump: en not in set" in why
    assert lang_service.check(LIB, prof, track_id="id4")[0] == "ok"
    assert lang_service.check(LIB, prof, name="Not In Library - Whatever")[0] == "unknown"

    # 3. the pool
    atl = [{"artist": "Ddd Eee", "title": "Song Foreign", "source": "library", "track_id": "id3"},
           {"artist": "Fff Ggg", "title": "Song Same", "source": "library", "track_id": "id4"}]
    pool = pick_pool.build_pool(atlas=atl, cat=[], exclude=[], seed_title="Seed", lang_check=lambda r: chk(r))
    assert [c["title"] for c in pool] == ["Song Same"]


def test_a_hand_pick_extends_the_profile(monkeypatch):
    _store(monkeypatch)
    # the owner played the foreign song by hand: it is in the history now, so the set is mixed, not blocked
    prof = lang_service.profile_for(LIB, ["Aaa Bbb - Song One", "Ddd Eee - Song Foreign"], playing_id="id2")
    assert not prof["pure"] and "en" in prof["share"]
    assert lang_service.check(LIB, prof, track_id="id3")[0] == "ok"


def test_paused_worker_takes_no_queued_id():
    """lang_service.set_paused (the admin jobs page): _next() hands out nothing while paused, urgent included."""
    saved = (list(lang_service._urgent), list(lang_service._backlog), dict(lang_service._state))
    lang_service._urgent[:] = ["u1"]
    lang_service._backlog[:] = ["b1"]
    try:
        lang_service.set_paused(True)
        assert lang_service._next() == (None, False) and lang_service.status()["paused"] is True
        assert lang_service.status()["queued"] == 2
        lang_service.set_paused(False)
        assert lang_service._next() == ("u1", True) and lang_service._next() == ("b1", False)
    finally:
        lang_service._urgent[:] = saved[0]
        lang_service._backlog[:] = saved[1]
        lang_service._state.update(saved[2])
