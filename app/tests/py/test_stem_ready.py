"""STEMREADY: stems ready before a transition. Patient GPU ladder (gpu.split patience_s / cpu_ok / GpuBusy), a parked
backfill job, the readiness log + p stats of the stem server, the ready_in_s estimate, and the console's pick hook and
readiness contract (/api/stems/ready, /api/stems/transition)."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from app.music_brain.audio.audio_io import write_flac, write_manifest
from app.stem_server import gpu
from app.stem_server.server import Settings, StemServer

TOKEN = "t" * 32


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


class Scripted:
    def __init__(self, script, clock=None, spend=0.0):
        self.script, self.calls, self.clock, self.spend = list(script), [], clock, spend

    def run(self, msg, on_progress):
        self.calls.append((msg["device"], msg["batch"], msg.get("wait_s")))
        if self.clock:
            self.clock.now += self.spend                 # the worker's own wait for VRAM
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


OK_GPU = {"ok": True, "info": {"stems": {}, "frames": 1, "seconds": 9.0, "gpu": {"kind": "gpu", "wait_s": 0.0}}}
OK_CPU = {"ok": True, "info": {"stems": {}, "frames": 1, "seconds": 900.0, "gpu": {"kind": "cpu", "wait_s": 0.0}}}
EXPIRED = {"ok": False, "oom": True, "wait_expired": True, "error": "GPU out of memory: wait expired",
           "gpu": {"kind": "gpu", "free_mb": 10, "total_mb": 24576, "wait_s": 60.0, "others_mb": 4400}}
CUDA_ERR = {"ok": False, "oom": True, "error": "AcceleratorError: CUDA error: out of memory", "gpu": {"kind": "gpu", "wait_s": 0.0}}


def _split(w, clock, **kw):
    return gpu.split(w, "j", Path("x"), Path("o"), "htdemucs_ft", "cuda", lambda *a: None, sleep=clock.sleep, clock=clock, **kw)


def test_patient_job_keeps_waiting_for_the_card_and_runs_on_the_gpu():
    c = Clock()
    w = Scripted([EXPIRED, EXPIRED, OK_GPU], c, spend=60.0)       # each try waits 60 s, the card frees on the third
    info, dev, note = _split(w, c, patience_s=600)
    assert [x[0] for x in w.calls] == ["cuda"] * 3 and info["gpu"]["kind"] == "gpu"
    assert w.calls[0][2] == 600 and w.calls[1][2] == 540          # the worker is told how much patience is left
    assert info["gpu"]["wait_s"] == 120.0                         # the failed waits are summed into the job's wait


def test_patience_over_then_cpu_for_a_job_that_may_use_it():
    c = Clock()
    w = Scripted([EXPIRED, EXPIRED, OK_CPU], c, spend=60.0)
    info, dev, note = _split(w, c, patience_s=100)
    assert w.calls[-1][0] == "cpu" and info["gpu"]["fallback"] == "GPU wait expired"
    assert [x[0] for x in w.calls] == ["cuda", "cuda", "cpu"] and c.now >= 100   # it waited the patience out first


def test_backfill_never_goes_to_the_cpu_it_is_parked():
    c = Clock()
    w = Scripted([EXPIRED], c, spend=60.0)
    with pytest.raises(gpu.GpuBusy):
        _split(w, c, patience_s=60, cpu_ok=False)
    assert all(x[0] == "cuda" for x in w.calls)


def test_cuda_error_oom_is_retried_every_backoff_within_patience(monkeypatch):
    monkeypatch.setenv("STEM_OOM_BACKOFF_S", "10")
    c = Clock()
    w = Scripted([CUDA_ERR, CUDA_ERR, CUDA_ERR, OK_GPU], c)
    info, dev, note = _split(w, c, patience_s=120)
    assert len(w.calls) == 4 and c.now == 30.0 and info["gpu"]["kind"] == "gpu"   # 3 pauses of 10 s, no CPU


def test_without_patience_the_old_ladder_still_applies():
    c = Clock()
    w = Scripted([CUDA_ERR, CUDA_ERR, OK_CPU], c)
    info, dev, note = _split(w, c)
    assert [x[0] for x in w.calls] == ["cuda", "cuda", "cpu"]


# ---- server: parked backfill, readiness log, estimate -------------------------------------------
def _settings(tmp: Path, **env) -> Settings:
    return Settings({"STEM_SERVER_TOKEN": TOKEN, "AIDJ_CACHE_DIR": str(tmp / "cache"),
                     "DJ_AUTH_FILE": str(tmp / "state" / "auth.json"), "STEM_GPU_SAMPLE_S": "0", **env})


def _track(s: Settings, n: int) -> str:
    body = f"song-{n}".encode() * 100
    tid = hashlib.sha256(body).hexdigest()[:16]
    s.uploads.mkdir(parents=True, exist_ok=True)
    (s.uploads / f"{tid}.mp3").write_bytes(body)
    return tid


def _fake_stems(path, model, stems_dir):
    h = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    d = Path(stems_dir) / f"{h}_{model}"
    d.mkdir(parents=True, exist_ok=True)
    z = np.zeros((10, 2), dtype=np.int16)
    write_manifest(d, {n: str(write_flac(d / f"{n}.flac", z, 44100)) for n in ("vocals", "drums", "bass", "other")})


def _fake_split(path, model, device, stems_dir):
    _fake_stems(path, model, stems_dir)
    return {}, "cuda", None


def _wait(cond, t=10.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _log(s):
    return [json.loads(x) for x in s.ready_log.read_text().splitlines()] if s.ready_log.exists() else []


def test_backfill_job_that_finds_the_card_full_is_parked_and_a_deck_job_runs_first(tmp_path):
    s = _settings(tmp_path, STEM_BACKFILL_DEFER_S="0.3")
    full = {"on": True}
    gate = threading.Event()
    order = []

    def split(self, j):
        order.append((j.id, j.priority))
        if j.priority == "backfill" and full["on"]:
            raise gpu.GpuBusy("the card stayed full for 60s")
        _fake_stems(j.path, s.model, s.stems)
        stem_dir_done(j)

    def stem_dir_done(j):
        j.info = {"seconds": 9.0, "device": "cuda", "gpu": {"kind": "gpu", "wait_s": 0.0}}
        j.device = "cuda"

    srv = StemServer(s, lambda *a: ({}, "cuda", None))
    srv.separate_fn = None
    srv._split = lambda j: split(srv, j)
    bf, dk = _track(s, 1), _track(s, 2)
    jb = srv.submit(bf, 4, "backfill")
    assert _wait(lambda: any(r["outcome"] == "deferred" for r in _log(s)))
    assert jb.status == "queued" and jb.deferred == 1 and "parked" in (jb.note or "")
    jd = srv.submit(dk, 4, "deck")                                 # the worker is free for the deck job
    assert _wait(lambda: jd.status == "done")
    full["on"] = False
    assert _wait(lambda: jb.status == "done")                      # parked job comes back by itself
    assert order[0] == (bf, "backfill") and order[1] == (dk, "deck") and order[-1] == (bf, "backfill")
    outcomes = [(r["id"], r["outcome"]) for r in _log(s)]
    assert (bf, "deferred") in outcomes and (dk, "done") in outcomes and (bf, "done") in outcomes
    gate.set()


def test_ready_log_and_stats_give_p(tmp_path):
    s = _settings(tmp_path)
    srv = StemServer(s, _fake_split)
    a, b = _track(s, 1), _track(s, 2)
    ja = srv.submit(a, 4, "deck")
    assert _wait(lambda: ja.status == "done")
    srv.submit(a, 4, "deck")                                       # cache hit: logged as cached, ready_after 0
    jb = srv.submit(b, 4, "deck")
    assert _wait(lambda: jb.status == "done" and len(_log(s)) == 3)
    rows = _log(s)
    assert [r["outcome"] for r in rows] == ["done", "cached", "done"]
    assert rows[1]["ready_after_s"] == 0.0 and rows[0]["priority"] == "deck" and rows[0]["queue_wait_s"] >= 0
    st = srv.ready_stats()
    assert st["asks"] == 3 and st["cached"] == 1 and st["failed"] == 0
    assert st["p_ready_within_s"]["120"] == 1.0 and st["ready_after_s"]["max"] is not None


def test_backfill_asks_do_not_count_in_p(tmp_path):
    s = _settings(tmp_path)
    srv = StemServer(s, _fake_split)
    t = _track(s, 3)
    j = srv.submit(t, 4, "backfill")
    assert _wait(lambda: j.status == "done")
    assert srv.ready_stats()["asks"] == 0


def test_ready_in_estimate_orders_the_queue(tmp_path):
    s = _settings(tmp_path)
    gate = threading.Event()

    def slow(path, model, device, stems_dir):
        gate.wait(10)
        _fake_stems(path, model, stems_dir)
        return {}, "cuda", None

    srv = StemServer(s, slow)
    srv.split_s.extend([10.0, 10.0, 10.0])
    t = [_track(s, n) for n in (1, 2, 3)]
    j1 = srv.submit(t[0], 4, "deck")
    assert _wait(lambda: j1.status == "running")
    j2, j3 = srv.submit(t[1], 4, "deck"), srv.submit(t[2], 4, "backfill")
    srv.annotate()
    assert j1.ready_in_s is not None and j2.ready_in_s > j1.ready_in_s and j3.ready_in_s > j2.ready_in_s
    assert abs((j3.ready_in_s - j2.ready_in_s) - 10.0) < 0.5      # one more split ahead = the median GPU split
    assert j3.to_dict()["ready_in_s"] == j3.ready_in_s and j2.to_dict()["waiting_gpu"] is False
    gate.set()


def test_a_split_stuck_waiting_for_the_card_says_so_and_has_no_estimate(tmp_path):
    s = _settings(tmp_path)
    gate = threading.Event()
    srv = StemServer(s, lambda *a: (gate.wait(10), {}, "cuda", None)[1:])
    j = srv.submit(_track(s, 1), 4, "deck")
    assert _wait(lambda: j.status == "running")
    j.started = time.time() - 60                                    # a minute in, still 0 %: it is waiting for VRAM
    nxt = srv.submit(_track(s, 2), 4, "deck")
    srv.annotate()
    assert j.waiting_gpu and j.ready_in_s is None and nxt.ready_in_s is None
    gate.set()


# ---- console: pick hook + readiness contract ----------------------------------------------------
@pytest.fixture
def con(monkeypatch, tmp_path):
    from app.ui import server as srv

    done = set()
    monkeypatch.setattr(srv, "STEM_READY_LOG", tmp_path / "t.jsonl")
    monkeypatch.setattr(srv, "_cached_stems4", lambda tid: {"vocals": "v"} if tid in done else None)
    monkeypatch.setattr(srv, "_stem_queue", [])
    monkeypatch.setattr(srv, "_stem_backlog", [])
    monkeypatch.setattr(srv, "_stem_remote", {})
    monkeypatch.setattr(srv, "_stem_busy", None)
    monkeypatch.setattr(srv, "_stem_cv", threading.Condition())
    monkeypatch.setattr(srv, "_stem_pick_at", {})
    monkeypatch.setattr(srv, "_stem_job_note", {})
    asked = []
    monkeypatch.setattr(srv, "_queue_stems", lambda tid, urgent=True: asked.append((tid, urgent)) or True)
    for tid in ("p1", "p2", "p3"):
        monkeypatch.setitem(srv._tracks, tid, tmp_path / f"{tid}.mp3")
    return srv, done, asked


def test_the_top_pick_goes_to_the_stem_queue_urgently_at_once(con):
    srv, done, asked = con
    srv._prepare_pick_stems([{"track_id": "p1"}, {"track_id": "p2"}])
    assert asked == [("p1", True)]                                  # only the song the phone will book
    assert "p1" in srv._stem_pick_at


def test_a_pick_with_stems_is_not_split_again_and_a_yt_only_pick_is_skipped(con):
    srv, done, asked = con
    done.add("p1")
    srv._prepare_pick_stems([{"track_id": "p1"}])
    srv._prepare_pick_stems([{"yt_id": "abcdefghijk", "title": "x"}, {"track_id": "p2"}])
    assert asked == []


def test_lookahead_pick_is_urgent_only_while_no_deck_job_is_open(con):
    srv, done, asked = con
    srv._prepare_pick_stems([{"track_id": "p1"}], lookahead=True)
    srv._stem_remote["p2"] = {"priority": "deck", "status": "running"}
    srv._prepare_pick_stems([{"track_id": "p3"}], lookahead=True)
    assert asked == [("p1", True), ("p3", False)]


def test_readiness_contract_states(con):
    srv, done, asked = con
    assert srv.stem_readiness("p1")["state"] == "none"
    assert srv.stem_readiness("zz")["state"] == "unknown"
    srv._stem_remote["p1"] = {"priority": "deck", "status": "queued", "position": 2, "ready_in_s": 21.5, "waiting_gpu": False}
    r = srv.stem_readiness("p1")
    assert (r["state"], r["ready_in_s"], r["position"], r["priority"]) == ("in_flight", 21.5, 2, "deck")
    srv._stem_remote.clear()
    srv._stem_job_note["p1"] = {"status": "error", "error": "RuntimeError: boom"}
    assert srv.stem_readiness("p1")["state"] == "failed" and srv.stem_readiness("p1")["error"] == "RuntimeError: boom"
    done.add("p1")
    assert srv.stem_readiness("p1")["state"] == "ready" and srv.stem_readiness("p1")["ready_in_s"] == 0.0


def test_ask_queues_what_is_not_ready_and_retries_a_failed_one(con):
    srv, done, asked = con
    srv._stem_job_note["p2"] = {"status": "error", "error": "x"}
    res = srv.get_stems_ready(ids="p1,p2", ask=True)
    assert [t["id"] for t in res["tracks"]] == ["p1", "p2"] and asked == [("p1", True), ("p2", True)]
    assert "p2" not in srv._stem_job_note
    asked.clear()
    srv.get_stems_ready(ids="p1", ask=False)
    assert asked == []


def test_transition_log_and_p(con):
    srv, done, asked = con
    done.update({"p1", "p2"})
    r1 = srv.post_stems_transition(srv.StemTransition(a_id="p1", b_id="p2", kind="stem_handover"))
    r2 = srv.post_stems_transition(srv.StemTransition(a_id="p1", b_id="p3", kind="stem_handover"))
    assert r1["both_ready"] is True and r2["both_ready"] is False and r2["b"]["state"] == "none"
    st = srv.get_stems_ready_stats()
    assert st["transitions"] == 2 and st["p"] == 0.5 and st["by_kind"]["stem_handover"] == {"n": 2, "both_ready": 1}
