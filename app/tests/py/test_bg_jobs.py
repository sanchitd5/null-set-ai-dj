"""Background audio jobs (app/ui/services/bg_jobs.py) and their HTTP surface in server.py."""
import threading
import time

from app.ui.services.bg_jobs import DONE, ERROR, EXPIRED, JobRunner


def _wait(job, timeout=3.0):
    t0 = time.monotonic()
    while job.active and time.monotonic() - t0 < timeout:
        time.sleep(0.01)
    return job


def test_job_returns_result_and_pending_body():
    r = JobRunner("t")
    gate = threading.Event()
    j = r.submit("k", lambda: (gate.wait(2), {"ok": True})[1])
    assert j.pending_body() == {"status": "pending", "job": j.id}
    gate.set()
    _wait(j)
    assert j.status == DONE and j.result == {"ok": True}
    assert r.get(j.id) is j


def test_identical_requests_share_one_job():
    r = JobRunner("t")
    calls = []
    gate = threading.Event()

    def work():
        calls.append(1)
        gate.wait(2)
        return 1

    a = r.submit("same", work)
    b = r.submit("same", work)
    assert a is b
    gate.set()
    _wait(a)
    c = r.submit("same", work)  # a fresh finished result is reused, not re-rendered
    assert c is a and calls == [1]
    other = r.submit("other", lambda: 2)
    assert other is not a


def test_one_job_at_a_time():
    r = JobRunner("t", workers=1)
    running, peak, lock = [0], [0], threading.Lock()

    def work():
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1

    jobs = [r.submit(f"k{i}", work) for i in range(4)]
    for j in jobs:
        _wait(j)
    assert peak[0] == 1 and all(j.status == DONE for j in jobs)


def test_bounded_queue_refuses_when_full():
    r = JobRunner("t", max_pending=2)
    gate = threading.Event()
    first = r.submit("run", lambda: gate.wait(2))
    time.sleep(0.05)  # first is RUNNING, not pending
    assert r.submit("a", lambda: 1) is not None
    assert r.submit("b", lambda: 1) is not None
    assert r.submit("c", lambda: 1) is None  # 2 pending already: caller answers "busy"
    gate.set()
    _wait(first)


def test_errors_are_reported_not_raised():
    r = JobRunner("t")
    j = _wait(r.submit("boom", lambda: 1 / 0))
    assert j.status == ERROR and "ZeroDivisionError" in j.error


def test_finished_jobs_expire_and_stale_queue_is_dropped():
    now = [1000.0]
    r = JobRunner("t", ttl_s=10.0, queue_ttl_s=5.0, clock=lambda: now[0])
    j = _wait(r.submit("k", lambda: 1))
    assert r.get(j.id) is j
    now[0] += 11.0
    assert r.get(j.id) is None  # forgotten after ttl_s
    # a job still queued after queue_ttl_s is dropped unrun
    gate, ran = threading.Event(), []
    blocker = r.submit("block", lambda: gate.wait(2))
    time.sleep(0.05)
    late = r.submit("late", lambda: ran.append(1))
    now[0] += 6.0
    gate.set()
    _wait(blocker)
    _wait(late)
    assert late.status == EXPIRED and ran == []


def test_forget_makes_the_next_submit_rerun():
    r = JobRunner("t")
    calls = []
    j = _wait(r.submit("k", lambda: calls.append(1)))
    r.forget(j.id)
    _wait(r.submit("k", lambda: calls.append(1)))
    assert calls == [1, 1]


def test_preplan_endpoint_starts_a_job_and_get_polls_it(monkeypatch):
    import pytest
    from fastapi import HTTPException

    from app.ui import server

    monkeypatch.setattr(server, "_cached_stems4", lambda tid: {"drums": "x"})
    gate = threading.Event()
    calls = []

    def fake_run(req, sa, sb):
        calls.append(req.a_id)
        gate.wait(2)
        return {"ok": True, "plan": {"a_in": 100.0}, "candidates": [], "ear": True}

    monkeypatch.setattr(server, "_run_preplan", fake_run)
    monkeypatch.setattr(server, "_ear_jobs", JobRunner("ear-test"))
    req = server.PreplanRequest(a_id="a", b_id="b", lo=90, hi=120, now=10, bpm_a=124)
    r1 = server.post_transition_preplan(req)
    r2 = server.post_transition_preplan(req)
    assert r1["status"] == "pending" and r2["job"] == r1["job"]  # joined, not re-run
    assert server.get_transition_preplan(r1["job"])["status"] == "pending"
    gate.set()
    _wait(server._ear_jobs.get(r1["job"]))
    done = server.get_transition_preplan(r1["job"], now=10)
    assert done["ok"] and done["plan"]["a_in"] == 100.0  # same shape as the old sync answer
    # the playhead moved past the planned start: no plan, not a stale one
    late = server.get_transition_preplan(r1["job"], now=95)
    assert late["ok"] is False and late["plan"] is None
    for bad in (lambda: server.get_transition_preplan("nope"),
                lambda: server.get_merge_audition(r1["job"]),  # kind is checked
                lambda: server.get_transition_preplan(r1["job"], now=float("nan"))):
        with pytest.raises(HTTPException):
            bad()
    assert calls == ["a"]


def test_merge_audition_endpoint_runs_as_a_job(monkeypatch):
    import types

    import pytest
    from fastapi import HTTPException

    from app.music_brain.render import merge
    from app.ui import server

    monkeypatch.setattr(server, "_cached_stems4", lambda tid: {"drums": "x"})
    monkeypatch.setattr(server, "_track_path", lambda tid: tid)
    monkeypatch.setattr(server, "analyze_track", lambda p: types.SimpleNamespace(bpm=124.0))
    calls = []

    def fake_audition(*a, **k):
        calls.append(k.get("key"))
        return [{"combo": {}, "label": "x", "ear": {"score": 7.0, "why": "ok"}}]

    monkeypatch.setattr(merge, "audition", fake_audition)
    monkeypatch.setattr(server, "_ear_jobs", JobRunner("ear-test"))
    combo = {"drums": "a", "bass": "b", "vocals": "a", "other": "b"}
    req = server.MergeAuditionRequest(a_id="a", b_id="b", a_time=60, b_time=10, combos=[combo])
    first = server.post_merge_audition(req)
    assert first["status"] == "pending"
    _wait(server._ear_jobs.get(first["job"]))
    done = server.get_merge_audition(first["job"])
    assert done == {"results": fake_audition()[:1], "ear": True}  # same shape as before
    assert server.post_merge_audition(req) == done  # fresh finished result: answered at once
    assert len(calls) == 2  # one real run (+ the direct call above)
    with pytest.raises(HTTPException):
        server.post_merge_audition(server.MergeAuditionRequest(
            a_id="a", b_id="b", a_time=60, b_time=10, combos=[{"drums": "c"}]))
    # an unheard answer (ear busy / off) is never reused: the next POST asks again
    monkeypatch.setattr(merge, "audition", lambda *a, **k: (calls.append("unheard"), [{"combo": {}, "label": "x", "ear": None}])[1])
    req2 = server.MergeAuditionRequest(a_id="a", b_id="b", a_time=30, b_time=10, combos=[combo])
    j = server.post_merge_audition(req2)["job"]
    _wait(server._ear_jobs.get(j))
    assert server.get_merge_audition(j)["ear"] is False
    again = server.post_merge_audition(req2)
    assert again["status"] == "pending" and again["job"] != j
    _wait(server._ear_jobs.get(again["job"]))
    assert calls.count("unheard") == 2
