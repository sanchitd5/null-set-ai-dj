"""On-demand lyric remix plans (OD-4): the one-at-a-time job runner and atlas_bridge.plan_pair_on_demand (with a fake builder)."""
import gzip
import json
import threading
import time
import types

import pytest
from fastapi import FastAPI

from app.music_brain import atlas_bridge
from app.tests.py.testclient_compat import TestClient
from app.ui.services import lyric_remix_api as lra
from app.ui.services import lyric_remix_ondemand as od

app = FastAPI()
app.include_router(od.router)
client = TestClient(app)
DOC = {"songs": {"A1": {"lines": [{"t": 1}]}, "B1": {"lines": [{"t": 1}]}}, "plans": []}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    od._jobs.clear()
    monkeypatch.setattr(od.atlas_bridge, "AVAILABLE", True)
    monkeypatch.setattr(lra, "load", lambda: DOC)
    od.configure(cache_dir=lambda: tmp_path, now=time.time)
    yield
    od._jobs.clear()


def _wait(a, b, state, t=3.0):
    end = time.time() + t
    while time.time() < end:
        if od.status(a, b)["state"] == state:
            return True
        time.sleep(0.01)
    return False


def test_eligibility_names_the_song_without_lines(tmp_path):
    (tmp_path / "x").mkdir()
    assert od.eligibility(DOC, "A1", "B1") is None
    assert od.eligibility(DOC, "A1", "Z9") == "no lyrics transcribed yet for this song"
    (tmp_path / "lyric_remix" / "lines").mkdir(parents=True)
    (tmp_path / "lyric_remix" / "lines" / "Z9.json").write_text("{}")
    assert od.eligibility(DOC, "A1", "Z9") is None


def test_not_eligible_never_starts_a_job():
    r = client.post("/api/song_menu/plan", json={"a": "A1", "b": "Z9"}).json()
    assert r["state"] == "failed" and "no lyrics transcribed yet" in r["reason"] and not od._jobs


def test_runs_one_at_a_time_and_reports_done(monkeypatch):
    gate, running, seen = threading.Event(), [], []
    DOC["songs"].update({"C1": {"lines": [1]}, "D1": {"lines": [1]}})

    def planner(a, b, cache, chat, log=None, names=None, should_stop=None):
        running.append(1)
        seen.append(max(len(running), 0))
        gate.wait(3)
        running.pop()
        return {"ok": True, "plans": [{"id": "p"}]}
    od.configure(planner=planner)
    s1 = client.post("/api/song_menu/plan", json={"a": "A1", "b": "B1"}).json()
    s2 = client.post("/api/song_menu/plan", json={"a": "C1", "b": "D1"}).json()
    assert s1["state"] == "in_flight" and s2["state"] == "in_flight" and s2["eta_s"] > s1["eta_s"] - 1    # the second waits behind the first
    assert client.post("/api/song_menu/plan", json={"a": "A1", "b": "B1"}).json()["state"] == "in_flight"   # idempotent, no second job
    gate.set()
    assert _wait("A1", "B1", "done") and _wait("C1", "D1", "done")
    assert max(seen) == 1                                                  # never two plans at once
    assert client.get("/api/song_menu/plan", params={"a": "A1", "b": "B1"}).json()["state"] == "done"


def test_failure_is_kept_and_not_retried(monkeypatch):
    n = []
    od.configure(planner=lambda *a, **k: n.append(1) or {"ok": False, "reason": "no lyric remix of these two songs reads well enough"})
    client.post("/api/song_menu/plan", json={"a": "A1", "b": "B1"})
    assert _wait("A1", "B1", "failed")
    r = client.post("/api/song_menu/plan", json={"a": "A1", "b": "B1"}).json()
    assert r["state"] == "failed" and "reads well enough" in r["reason"] and len(n) == 1
    assert od.ctx_fields(DOC, "A1", "B1")["planState"] == "failed"


def test_planner_crash_is_a_plain_failure():
    def boom(*a, **k):
        raise RuntimeError("gpu on fire")
    od.configure(planner=boom)
    client.post("/api/song_menu/plan", json={"a": "A1", "b": "B1"})
    assert _wait("A1", "B1", "failed")
    assert "could not be planned" in od.status("A1", "B1")["reason"]


def test_queue_is_capped(monkeypatch):
    gate = threading.Event()
    for i in range(6):
        DOC["songs"][f"Q{i}"] = {"lines": [1]}
    od.configure(planner=lambda *a, **k: gate.wait(3) and {"ok": True, "plans": []})
    states = [client.post("/api/song_menu/plan", json={"a": f"Q{i}", "b": "B1"}).json()["state"] for i in range(5)]
    assert states[:od.MAX_QUEUE] == ["in_flight"] * od.MAX_QUEUE and states[od.MAX_QUEUE:] == ["failed", "failed"]
    gate.set()


def test_existing_plan_is_done_without_a_job(monkeypatch):
    monkeypatch.setattr(lra, "load", lambda: {**DOC, "plans": [{"id": "p", "a": "A1", "b": "B1"}]})
    assert od.start("A1", "B1")["state"] == "done" and not od._jobs


def test_same_id_is_422():
    assert client.post("/api/song_menu/plan", json={"a": "A1", "b": "A1"}).status_code == 422
    assert client.post("/api/song_menu/plan", json={"a": "bad id", "b": "A1"}).status_code == 422


# ---------------------------------------------------------------- atlas_bridge.plan_pair_on_demand with a fake builder

def _fake_builder(tmp_path, verdicts, langs=("en", "en"), cached=("A1", "B1")):
    out = tmp_path / "lyric_remix.json.gz"
    saved = {"judged": {}, "songs": {}, "calls": 0}
    asked = []

    def line(i):
        return {"i": i, "t": float(i), "e": i + 1.0, "beats": 4, "q": 0.9, "text": f"line {i}"}

    def load_song(t, cache_dir, write, log, cache_only=False):
        assert cache_only and not write                                    # never reads audio, never writes tables
        if t["id"] not in cached:
            return None
        return {"id": t["id"], "sig": "s", "bpm": 120.0, "lang": langs[0] if t["id"] == "A1" else langs[1], "lines": [line(i) for i in range(6)]}

    def cross(sa, sb):
        return [{"id": f"c{n}", "a": "A1", "b": "B1", "pieces": [("a", n), ("b", n)], "text": f"x{n}", "texts": ["a", "b"]} for n in range(3)]

    def judge(batch, chat, log, tag="", system=None):
        asked.append([c["id"] for c in batch])
        chat("sys", "user")
        v = verdicts.pop(0)
        return None if v is None else {n + 1: {"ok": ok, "score": 8 if ok else 3, "why": "w"} for n, ok in enumerate(v)}

    def save_state(cache_dir, state):
        for k, v in saved["judged"].items():
            state["judged"].setdefault(k, v)
        saved.update(judged=dict(state["judged"]), songs=dict(state["songs"]), calls=state["calls"])
        (tmp_path / "lyric_remix").mkdir(exist_ok=True)
        return state

    def read_doc(p):
        try:
            return json.loads(gzip.decompress(p.read_bytes()))
        except OSError:
            return None

    mod = types.SimpleNamespace(
        MIN_CLEAN_LINES=4, CROSS_GAP_MAX=0.08, CROSS_KEY_MIN=0.6, JUDGE_BATCH=2, DATA_V=1,
        _tracks=lambda c: [{"id": "A1"}, {"id": "B1"}, {"id": "Z9"}], load_song=load_song,
        _clean=lambda s: s["lines"], _pairs=lambda a, c: {}, cross_candidates=cross, _judge=judge,
        load_state=lambda c: {"judged": dict(saved["judged"]), "songs": dict(saved["songs"]), "calls": saved["calls"]},
        save_state=save_state, default_out=lambda: out, read_doc=read_doc, rules_hash=lambda: "r",
        write_doc=lambda p, d: p.write_bytes(gzip.compress(json.dumps(d).encode())),
        _plan=lambda c, j: {"id": c["id"], "a": c["a"], "b": c["b"], "score": j["score"] / 10, "why": j["why"]})
    return mod, out, saved, asked


def test_bridge_judges_and_stores_the_plan(tmp_path):
    mod, out, saved, asked = _fake_builder(tmp_path, [[True, False], [False]])
    chats = []
    r = atlas_bridge.plan_pair_on_demand("A1", "B1", tmp_path, lambda s, u: chats.append(1), mod=mod)
    assert r["ok"] and [p["id"] for p in r["plans"]] == ["c0"]
    assert asked == [["c0", "c1"]]                                       # a plan was accepted in the first batch: no second call
    doc = mod.read_doc(out)
    assert [p["id"] for p in doc["plans"]] == ["c0"] and set(doc["songs"]) == {"A1", "B1"} and doc["rejected"] == ["c1"]
    assert saved["judged"]["c0"]["ok"] and saved["calls"] == 1
    again = atlas_bridge.plan_pair_on_demand("A1", "B1", tmp_path, lambda s, u: chats.append(1), mod=mod)
    assert again["ok"] and len(asked) == 1                               # nothing is judged twice


def test_bridge_second_batch_then_nothing_good(tmp_path):
    mod, out, saved, asked = _fake_builder(tmp_path, [[False, False], [False]])
    r = atlas_bridge.plan_pair_on_demand("A1", "B1", tmp_path, lambda s, u: "", mod=mod)
    assert not r["ok"] and "reads well enough" in r["reason"] and len(asked) == 2 and not out.exists()


def test_bridge_llm_silent(tmp_path):
    mod, out, saved, asked = _fake_builder(tmp_path, [None, None])
    r = atlas_bridge.plan_pair_on_demand("A1", "B1", tmp_path, lambda s, u: "", mod=mod)
    assert not r["ok"] and "did not answer" in r["reason"]


def test_bridge_refusals_in_plain_words(tmp_path):
    mod, *_ = _fake_builder(tmp_path, [], cached=("A1",))
    assert atlas_bridge.plan_pair_on_demand("A1", "B1", tmp_path, None, mod=mod, names=lambda t: "Song " + t)["reason"] == "no lyrics transcribed yet for Song B1"
    mod, *_ = _fake_builder(tmp_path, [], langs=("en", "hi"))
    assert "same language" in atlas_bridge.plan_pair_on_demand("A1", "B1", tmp_path, None, mod=mod)["reason"]


def test_bridge_stops_at_the_deadline(tmp_path):
    mod, out, saved, asked = _fake_builder(tmp_path, [[False, False], [True]])
    r = atlas_bridge.plan_pair_on_demand("A1", "B1", tmp_path, lambda s, u: "", mod=mod, should_stop=lambda: bool(asked))
    assert not r["ok"] and len(asked) == 1
