"""Graph picks, shadow mode (graph_pick.py): one event per call, `off` writes none, a missing module is silent,
an error in the query never reaches the caller. The graph module is faked: no atlas needed."""
import sys
import types

import pytest

from app.ui.services import graph_pick as gp
from app.ui.services import session_log
from app.music_brain import atlas_bridge

MOD = "app.music_brain.atlas.graph_query"


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "graph.db"
    db.write_bytes(b"")
    monkeypatch.setenv("DJ_GRAPH_DB", str(db))
    monkeypatch.delenv("DJ_GRAPH_PICKS", raising=False)
    monkeypatch.setattr(atlas_bridge, "AVAILABLE", True)
    events = []
    monkeypatch.setattr(session_log, "log", lambda kind, **f: events.append((kind, f)))
    return events


def _fake(monkeypatch, fn):
    mod = types.ModuleType(MOD)
    mod.next_result = fn
    monkeypatch.setitem(sys.modules, MOD, mod)
    return mod


def _picks(n):
    return {"picks": [{"key": f"artist {i}|title {i}", "score": 1.0 - i / 10, "hops": 1} for i in range(1, n + 1)],
            "reason": None}


LLM = [{"artist": "Artist 2", "title": "Title 2"}, {"artist": "Artist 9", "title": "Title 9"}]


def test_mode_default_and_values(monkeypatch):
    monkeypatch.delenv("DJ_GRAPH_PICKS", raising=False)
    assert gp.mode() == "shadow"
    for v in ("off", "shadow", "on"):
        monkeypatch.setenv("DJ_GRAPH_PICKS", v)
        assert gp.mode() == v
    monkeypatch.setenv("DJ_GRAPH_PICKS", "bogus")
    assert gp.mode() == "shadow"


def test_shadow_writes_one_event(env, monkeypatch):
    seen = {}

    def fn(conn, seed, state, k, params, lang):
        seen.update(seed=seed, k=k, lang=lang)
        return _picks(7)

    _fake(monkeypatch, fn)
    out = gp.shadow("t001", {"played_keys": []}, LLM, lambda n: ("penalty", "x"), seed_key="artist 1|title 1")
    assert len(env) == 1 and env[0][0] == "graph_shadow"
    f = env[0][1]
    assert f["seed"] == "artist 1|title 1" and f["n_graph"] == 7 and len(f["graph"]) == 5
    assert f["llm"] == ["artist 2|title 2", "artist 9|title 9"] and f["overlap"] == 1
    assert out == f
    assert seen["seed"] == {"key": "artist 1|title 1", "track_id": "t001"} and seen["k"] == 10
    assert seen["lang"]({"x": 1}) == "penalty"


def test_unknown_seed_logged_with_reason(env, monkeypatch):
    _fake(monkeypatch, lambda *a: {"picks": [], "reason": "seed_unknown"})
    gp.shadow("t001", {}, LLM)
    assert env[0][1]["reason"] == "seed_unknown" and env[0][1]["overlap"] == 0


def test_off_writes_nothing(env, monkeypatch):
    monkeypatch.setenv("DJ_GRAPH_PICKS", "off")
    _fake(monkeypatch, lambda *a: _picks(3))
    assert gp.shadow("t001", {}, LLM) is None and env == []


def test_missing_module_is_silent(env, monkeypatch):
    monkeypatch.delitem(sys.modules, MOD, raising=False)
    monkeypatch.setattr(atlas_bridge, "AVAILABLE", False)
    assert gp.shadow("t001", {}, LLM) is None and env == []


def test_missing_graph_db_is_silent(env, monkeypatch, tmp_path):
    monkeypatch.setenv("DJ_GRAPH_DB", str(tmp_path / "nope.db"))
    _fake(monkeypatch, lambda *a: _picks(3))
    assert gp.shadow("t001", {}, LLM) is None and env == []


def test_query_error_never_reaches_the_caller(env, monkeypatch):
    def boom(*a):
        raise RuntimeError("bad graph")

    _fake(monkeypatch, boom)
    assert gp.shadow("t001", {}, LLM) is None and env == []


def test_state_from_request_keys():
    state = gp.state_from_request(
        object(), ["Artist 1 - Title 1 (Official Video)", "Artist 2 - Title 2"], ["Artist 3 - Title 3"],
        ["Artist 4 - Title 4"], ["Artist 5 - Title 5"], {}, None, 124.0)
    assert state["played_keys"] == ["artist 1|title 1", "artist 2|title 2", "artist 4|title 4", "artist 5|title 5"]
    assert state["rejected_keys"] == ["artist 3|title 3"]
    assert state["recent_artist_keys"] == ["artist 1", "artist 2"]
    assert state["seed_bpm"] == 124.0 and state["bpm_window_pct"] == 8.0


def test_state_skips_unparseable_names():
    state = gp.state_from_request(object(), ["just a title"], [], [], [], {})
    assert state["played_keys"] == [] and state["seed_bpm"] is None
