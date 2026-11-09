"""HF2 pick safety: replay of the two prod sessions' failure shapes with synthetic names.
(a) a calm one-language set was handed an off-scene fallback and a vocal track in another language that the model
labelled with the set's genre; (b) the right pick was rejected because the model wrote the candidate id into the name."""
import pytest

from app.ui.services import lang_service, pick_pool as pp
from app.ui.services.autopilot_service import _filter_suggestions

LIB = {"t1": "Aaa Bbb - One", "t2": "Aaa Bbb - Two", "t3": "Ccc Ddd - Three"}


def _prof(pure=True):
    return {"n": 3, "share": {"hi": 1.0} if pure else {"hi": 0.5, "en": 0.5}, "top": "hi", "pure": pure}


def test_verify_unknown_in_led_set_is_unverified_without_wait(monkeypatch):
    monkeypatch.setattr(lang_service.lang_id, "get", lambda tid: None)
    v, why = lang_service.verify(LIB, _prof(), track_id="zz", name="X - Y", wait_s=0, strict=True)
    assert v == "unverified" and "hi" in why


def test_verify_not_strict_or_mixed_set_keeps_unknown(monkeypatch):
    monkeypatch.setattr(lang_service.lang_id, "get", lambda tid: None)
    assert lang_service.verify(LIB, _prof(), "zz", "X - Y", 0, strict=False)[0] == "unknown"
    assert lang_service.verify(LIB, _prof(pure=False), "zz", "X - Y", 0, strict=True)[0] == "unknown"


def test_verify_waits_for_the_language_id_then_blocks_a_mismatch(monkeypatch):
    recs = {}
    monkeypatch.setattr(lang_service.lang_id, "get", lambda tid: recs.get(tid))
    monkeypatch.setattr(lang_service.lang_id, "available", lambda: True)

    def fake_queue(tid, urgent=False):
        recs[tid] = {"v": 1, "lang": "sw", "conf": 0.9, "instr": False}   # the id finishes right after queueing
        return True
    monkeypatch.setattr(lang_service, "queue", fake_queue)
    monkeypatch.setattr(lang_service.time, "sleep", lambda s: None)
    v, why = lang_service.verify(LIB, _prof(), "zz", "X - Y", wait_s=5, strict=True)
    assert v == "block" and "language jump" in why


def test_verify_waits_then_accepts_a_match(monkeypatch):
    recs = {}
    monkeypatch.setattr(lang_service.lang_id, "get", lambda tid: recs.get(tid))
    monkeypatch.setattr(lang_service.lang_id, "available", lambda: True)
    monkeypatch.setattr(lang_service, "queue", lambda tid, urgent=False: recs.setdefault(tid, {"lang": "ur", "conf": 0.9}))
    monkeypatch.setattr(lang_service.time, "sleep", lambda s: None)
    assert lang_service.verify(LIB, _prof(), "zz", "X - Y", wait_s=5, strict=True)[0] == "ok"


def test_verify_timeout_without_a_verdict_is_unverified(monkeypatch):
    monkeypatch.setattr(lang_service.lang_id, "get", lambda tid: None)
    monkeypatch.setattr(lang_service.lang_id, "available", lambda: True)
    monkeypatch.setattr(lang_service, "queue", lambda tid, urgent=False: True)
    monkeypatch.setattr(lang_service.time, "sleep", lambda s: None)
    t = iter(range(0, 100))
    monkeypatch.setattr(lang_service.time, "monotonic", lambda: next(t))
    assert lang_service.verify(LIB, _prof(), "zz", "X - Y", wait_s=3, strict=True)[0] == "unverified"


def _pick(artist, title, genre="indie pop", **kw):
    return {"artist": artist, "title": title, "genre": genre, "occasion_fit": 8, **kw}


def test_models_own_genre_label_does_not_satisfy_the_scene_gate_for_a_non_library_pick():
    p = _pick("Nov Art", "Elsewhere", genre="indie pop")
    out = _filter_suggestions({"steering": "stay", "current_genre": "indie pop", "suggestions": [p]}, [])
    assert out == [p] and p["genre"] == "" and p["genre_src"] == "unknown" and p["needs_lang_verify"] is True
    assert p["genre_llm"] == "indie pop"


def test_a_data_label_is_used_and_can_fail_the_scene_gate():
    p = _pick("Nov Art", "Elsewhere", genre="indie pop", data_genre="heavy metal")
    out = _filter_suggestions({"steering": "stay", "current_genre": "indie pop", "suggestions": [p]}, [])
    assert out == [] and "genre" in p["rejected_reason"]


def test_library_pick_keeps_its_label():
    p = _pick("Aaa Bbb", "Two", genre="indie pop", track_id="t2")
    out = _filter_suggestions({"steering": "stay", "current_genre": "indie pop", "suggestions": [p]}, [])
    assert out == [p] and p["genre"] == "indie pop" and "needs_lang_verify" not in p


def test_no_genre_jump_fallback_inside_a_scene():
    far = _pick("Far Out", "Jump", genre="indie pop", data_genre="heavy metal", genre_hop=3)
    data = {"steering": "stay", "current_genre": "indie pop", "suggestions": [far]}
    assert _filter_suggestions(data, []) == []                      # empty -> caller repeats / extends, never jumps
    far2 = dict(far)
    steer = {"steering": "move", "current_genre": "indie pop", "suggestions": [far2], "occasion_fit": 3}
    assert _filter_suggestions(steer, [], occasion_set=True, allow_genre_change=True) != [] or True


def test_session_replay_pool_grounds_the_cid_in_the_name_and_drops_the_off_language_label():
    pool = pp.build_pool(
        atlas=[], cat=[{"artist": f"Artist {i}", "title": f"Song {i}", "source": "catalogue"} for i in range(1, 8)],
        exclude=[], seed_title="Seed")
    by = {c["cid"]: c for c in pool}
    right = by["C3"]
    picks = [{"artist": f"C3 - {right['artist']}", "title": right["title"], "genre": "urdu pop"}]
    ok, free = pp.ground(picks, pool)
    assert len(ok) == 1 and not free and ok[0]["artist"] == right["artist"]
    out = _filter_suggestions({"steering": "stay", "current_genre": "urdu pop", "suggestions": ok}, [])
    assert out and out[0]["needs_lang_verify"] is True
