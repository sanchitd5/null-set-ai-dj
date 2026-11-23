"""POST /api/song_menu/actions: builds dj-core's song_menu_actions context from server data and relays the answer."""
from fastapi import FastAPI
from app.tests.py.testclient_compat import TestClient

from app.music_brain import djcore_bridge
from app.ui.services import lyric_remix_api as lra
from app.ui.services import song_menu_api as sm

app = FastAPI()
app.include_router(sm.router)
client = TestClient(app)

AN = {"A1": {"bpm": 120.0, "key": {"camelot": "8A"}, "duration": 200.0, "music_span": {"start": 4, "end": 190}, "energy_curve": [1, 2]},
      "B1": {"bpm": 124.0, "key": {"camelot": "9A"}, "duration": 180.0}}
DOC = {"songs": {"A1": {"lines": [{"t": 1}] * 3}, "B1": {"lines": [{"t": 1}] * 2}, "C1": {"lines": []}},
       "plans": [{"id": "p1", "a": "A1", "b": "B1"}]}


def _setup(monkeypatch, calls):
    monkeypatch.setattr(lra, "load", lambda: DOC)
    sm.configure(analysis=lambda t: AN.get(t),
                 stem_readiness=lambda t: {"state": "in_flight", "ready_in_s": 12.0} if t == "B1" else {"state": "ready", "ready_in_s": 0.0})

    def fake(fn, args):
        calls.append((fn, args))
        return {"lyricRemix": {"state": "available"}, "vocalSwap": {"state": "not_possible"}}
    monkeypatch.setattr(djcore_bridge, "call", fake)


def test_bad_body_is_422():
    assert client.post("/api/song_menu/actions", json={"a": "bad id!", "bs": []}).status_code == 422
    assert client.post("/api/song_menu/actions", json={"a": "A1", "bs": ["x"] * 51}).status_code == 422


def test_builds_ctx_and_batches(monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    r = client.post("/api/song_menu/actions", json={"a": "A1", "bs": ["B1", "C1", "B1", "A1"], "pos_s": 30})
    assert r.status_code == 200
    items = r.json()["items"]
    assert set(items) == {"B1", "C1", "A1"}
    assert items["B1"]["lyricRemix"]["state"] == "available"
    assert items["A1"]["lyricRemix"]["state"] == "not_possible"          # the playing song is not its own candidate
    assert [c[0] for c in calls] == ["song_menu_actions"] * 2             # B1 and C1, de-duplicated, never A1
    ctx = calls[0][1]["ctx"]
    assert ctx["aId"] == "A1" and ctx["bId"] == "B1" and ctx["posS"] == 30
    assert ctx["barS"] == 2.0 and ctx["bpms"] == {"A1": 120.0, "B1": 124.0}
    assert ctx["keyScore"] == 0.9
    assert ctx["stems"] == {"a": {"state": "ready", "eta_s": 0.0}, "b": {"state": "in_flight", "eta_s": 12.0}}
    assert [p["id"] for p in ctx["plans"]] == ["p1"] and ctx["lines"] == {"a": 3, "b": 2}
    assert ctx["aAnalysis"] == {"duration": 200.0, "music_span": {"start": 4, "end": 190}}      # only what song bounds read
    assert "planState" not in ctx


def test_pair_without_plan_says_so(monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    DOC2 = {**DOC, "plans": []}
    monkeypatch.setattr(lra, "load", lambda: DOC2)
    client.post("/api/song_menu/actions", json={"a": "A1", "bs": ["B1", "C1"]})
    b1, c1 = calls[0][1]["ctx"], calls[1][1]["ctx"]
    assert b1["planState"] == "failed" and "no lyric remix" in b1["planReason"] and b1["canPlan"] is False
    assert "planState" not in c1 and c1["lines"]["b"] == 0               # no timed lyrics: dj-core words that itself


def test_unknown_songs_and_missing_analysis(monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    sm.configure(analysis=lambda t: None, stem_readiness=lambda t: {"state": "unknown"})
    client.post("/api/song_menu/actions", json={"a": "Z1", "bs": ["Y1"]})
    ctx = calls[0][1]["ctx"]
    assert ctx["keyScore"] is None and ctx["barS"] == 0 and ctx["bpms"] == {}
    assert ctx["stems"]["a"] == {"state": "none", "eta_s": None}


def test_dj_core_missing_means_not_possible(monkeypatch):
    _setup(monkeypatch, [])

    def boom(*a, **k):
        raise djcore_bridge.DjCoreUnavailable("no build")
    monkeypatch.setattr(djcore_bridge, "call", boom)
    r = client.post("/api/song_menu/actions", json={"a": "A1", "bs": ["B1"]}).json()["items"]["B1"]
    assert r["lyricRemix"]["state"] == "not_possible" and "dj-core is not available" in r["lyricRemix"]["reason"]
