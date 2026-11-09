"""Graph picks ON (GRAPHDB G10): the graph answers the suggest call when enough of its picks survive the filters,
the LLM tops the list up when not, and everything else is unchanged. The graph module is faked (no atlas)."""
import json
import sys
import types

import pytest

import app.ui.services.autopilot_service as svc
from app.music_brain import atlas_bridge
from app.ui.services import graph_pick as gp
from app.ui.services import session_log

MOD = "app.music_brain.atlas.graph_query"
GATES = {"good": 3, "score": 0.2}
LLM_REPLY = json.dumps({"suggestions": [
    {"artist": "Llm Artist 1", "title": "Llm Song 1", "reason": "x"},
    {"artist": "Llm Artist 2", "title": "Llm Song 2", "reason": "x"},
    {"artist": "Llm Artist 3", "title": "Llm Song 3", "reason": "x"}], "current_genre": ""})


def _gpick(i, score=0.9, **kw):
    d = {"artist": f"Graph Artist {i}", "title": f"Graph Song {i}", "reason": "graph: measured 0.77", "genre": "",
         "era": "", "pool_source": "graph", "graph_key": f"graph artist {i}|graph song {i}", "graph_score": score,
         "graph_why": "graph: measured 0.77", "graph_reasons": []}
    d.update(kw)
    return d


@pytest.fixture
def env(monkeypatch):
    events, prompts = [], []
    monkeypatch.setattr(session_log, "log", lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(svc, "_verify_picks", lambda picks: (list(picks), []))
    monkeypatch.setattr(svc, "_note_pick", lambda s: None)

    def chat(system, user, **k):
        prompts.append(user)
        return LLM_REPLY
    monkeypatch.setattr(svc, "chat_raw", chat)
    fake = types.ModuleType(MOD)

    def decide(scores, min_good, min_score):
        good = sum(1 for s in scores if s >= min_score)
        return {"use_graph": good >= min_good, "good": good, "why": "ok" if good >= min_good else "too_few"}
    fake.decide = decide
    monkeypatch.setitem(sys.modules, MOD, fake)
    return events, prompts


def _suggest(**kw):
    return svc.suggest_next_tracks("Seed", "Art", 124.0, "8A", 200.0, 0.5, "", [], **kw)


def _decisions(events):
    return [f for k, f in events if k == "graph_pick"]


def test_enough_good_picks_skip_the_llm(env, monkeypatch):
    events, prompts = env
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: pytest.fail("LLM called"))
    meta = {}
    out = _suggest(graph_picks=[_gpick(i) for i in range(1, 7)], graph_min=GATES, meta=meta)
    assert [s["title"] for s in out] == ["Graph Song 1", "Graph Song 2", "Graph Song 3"]
    assert all(s["pool_source"] == "graph" and s["search_query"].startswith("ytmsearch:") for s in out)
    assert all(s["pick_reason"].endswith("graph: measured 0.77") for s in out)
    assert meta["source"] == "graph" and meta["steering"] == "stay"
    assert _decisions(events)[0]["decision"] == "graph"


def test_library_picks_keep_track_id_and_skip_verify(env, monkeypatch):
    seen = []
    monkeypatch.setattr(svc, "_verify_picks", lambda picks: (seen.append(list(picks)) or (list(picks), [])))
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: pytest.fail("LLM called"))
    out = _suggest(graph_picks=[_gpick(i, track_id=f"t{i}") for i in range(1, 5)], graph_min=GATES)
    assert [s["track_id"] for s in out] == ["t1", "t2", "t3"]
    assert seen == [[]]


def test_invented_yt_picks_do_not_count(env, monkeypatch):
    events, _ = env
    monkeypatch.setattr(svc, "_verify_picks", lambda picks: ([], list(picks)))
    monkeypatch.setattr(svc, "note_rejects", lambda picks, why: None)
    out = _suggest(graph_picks=[_gpick(i) for i in range(1, 5)], graph_min=GATES, n=3)
    d = _decisions(events)[0]
    assert d["decision"] == "llm_fallback" and d["why"] == "too_few" and d["good"] == 0
    assert all(s.get("pool_source") != "graph" for s in out)


def test_too_few_good_picks_llm_tops_up_graph_first(env):
    events, prompts = env
    picks = [_gpick(1), _gpick(2), _gpick(3, score=0.1), _gpick(4, score=0.05)]
    meta = {}
    out = _suggest(graph_picks=picks, graph_min=GATES, meta=meta, n=3)
    assert len(prompts) == 1
    assert [s["title"] for s in out] == ["Graph Song 1", "Graph Song 2", "Llm Song 1"]
    assert meta["source"] == "graph+llm"
    d = _decisions(events)[0]
    assert d["decision"] == "llm_topup" and d["why"] == "too_few" and d["good"] == 2


def test_topup_does_not_repeat_a_song_the_llm_also_named(env, monkeypatch):
    reply = json.dumps({"suggestions": [{"artist": "Graph Artist 1", "title": "Graph Song 1", "reason": "x"},
                                        {"artist": "Llm Artist 2", "title": "Llm Song 2", "reason": "x"}],
                        "current_genre": ""})
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: reply)
    out = _suggest(graph_picks=[_gpick(1)], graph_min=GATES, n=3)
    assert [s["title"] for s in out] == ["Graph Song 1", "Llm Song 2"]


def test_no_good_picks_is_the_plain_llm_path(env):
    events, prompts = env
    base = _suggest()
    base_prompts = list(prompts)
    prompts.clear()
    out = _suggest(graph_picks=[_gpick(1, score=0.0)], graph_min=GATES)
    assert out == base and prompts == base_prompts
    assert _decisions(events)[0]["decision"] == "llm_fallback"


def test_no_graph_picks_changes_nothing(env):
    events, prompts = env
    a = _suggest()
    b = _suggest(graph_picks=None, graph_min=None)
    c = _suggest(graph_picks=[])
    assert a == b == c and len(prompts) == 3 and _decisions(events) == []
    assert all(s["pool_source"] != "graph" for s in a if "pool_source" in s)


def test_filters_still_apply_to_graph_picks(env, monkeypatch):
    # a song already played is dropped by the repeat filter, so it does not count as good
    events, _ = env
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: pytest.fail("LLM called"))
    picks = [_gpick(i) for i in range(1, 5)]
    out = svc.suggest_next_tracks("Seed", "Art", 124.0, "8A", 200.0, 0.5, "", ["Graph Artist 1 - Graph Song 1"],
                                  graph_picks=picks, graph_min=GATES)
    assert [s["title"] for s in out] == ["Graph Song 2", "Graph Song 3", "Graph Song 4"]


def test_decide_without_the_mirror_falls_back(env, monkeypatch):
    monkeypatch.setitem(sys.modules, MOD, types.ModuleType(MOD))      # an older dj-atlas pin: no decide()
    d, good = gp.decide([_gpick(1)], GATES)
    assert d["use_graph"] is False and d["why"] == "error" and good == []


# ---- graph_pick.picks_for ----

@pytest.fixture
def graph(tmp_path, monkeypatch):
    db = tmp_path / "graph.db"
    db.write_bytes(b"")
    monkeypatch.setenv("DJ_GRAPH_DB", str(db))
    monkeypatch.setattr(atlas_bridge, "AVAILABLE", True)
    return db


def _query(monkeypatch, fn):
    mod = types.ModuleType(MOD)
    mod.next_result = fn
    monkeypatch.setitem(sys.modules, MOD, mod)


def test_picks_for_maps_to_suggestions(graph, monkeypatch):
    seen = {}

    def fn(conn, seed, state, k, params, lang):
        seen.update(seed=seed, k=k)
        return {"reason": None, "picks": [
            {"key": "a 1|t 1", "artist": "A 1", "title": "T 1", "track_id": "t1", "yt_id": None, "source": "library",
             "score": 0.8, "hops": 1, "reasons": [{"kind": "edge", "via": "measured", "value": 0.77},
                                                  {"kind": "novelty", "via": "yt", "value": 0.1}]},
            {"key": "a 2|t 2", "artist": "A 2", "title": "T 2", "track_id": None, "yt_id": "yid", "source": "yt",
             "score": 0.5, "hops": 2, "reasons": []},
            {"key": "x|", "artist": "X", "title": None, "score": 0.1, "hops": 1, "reasons": []}]}
    _query(monkeypatch, fn)
    picks, why = gp.picks_for("t0", {}, 3, None, seed_key="s|s", label_of=lambda n: {"genre": "house", "era": "2010s"})
    assert why is None and seen["k"] == 10 and seen["seed"] == {"key": "s|s", "track_id": "t0"}
    assert [p["title"] for p in picks] == ["T 1", "T 2"]
    a, b = picks
    assert a["pool_source"] == "graph" and a["track_id"] == "t1" and a["genre"] == "house" and a["era"] == "2010s"
    assert a["graph_why"] == "graph: measured 0.77" and a["graph_score"] == 0.8
    assert "track_id" not in b and b["yt_id"] == "yid" and b["graph_why"] == "graph"


def test_picks_for_failures_give_no_picks(graph, monkeypatch, tmp_path):
    _query(monkeypatch, lambda *a: {"reason": "seed_unknown", "picks": []})
    assert gp.picks_for("t0", {}, 3) == ([], "seed_unknown")

    def boom(*a):
        raise RuntimeError("bad graph")
    _query(monkeypatch, boom)
    assert gp.picks_for("t0", {}, 3) == ([], "error")
    monkeypatch.setenv("DJ_GRAPH_DB", str(tmp_path / "nope.db"))
    assert gp.picks_for("t0", {}, 3) == ([], "error")
    monkeypatch.setattr(atlas_bridge, "AVAILABLE", False)
    assert gp.picks_for("t0", {}, 3) == ([], "error")


def test_thresholds_default_and_env_override(monkeypatch):
    monkeypatch.delenv("DJ_GRAPH_MIN_GOOD", raising=False)
    monkeypatch.delenv("DJ_GRAPH_MIN_SCORE", raising=False)
    from app.music_brain.generated.dj_config import GRAPH_PARAMS
    assert gp.thresholds() == {"good": GRAPH_PARAMS["min_good"], "score": GRAPH_PARAMS["min_score"]}
    monkeypatch.setenv("DJ_GRAPH_MIN_GOOD", "2")
    monkeypatch.setenv("DJ_GRAPH_MIN_SCORE", "0.5")
    assert gp.thresholds() == {"good": 2, "score": 0.5}
    monkeypatch.setenv("DJ_GRAPH_MIN_GOOD", "bogus")
    assert gp.thresholds()["good"] == GRAPH_PARAMS["min_good"]
