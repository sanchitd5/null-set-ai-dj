"""Set-study progress file: writer (atomic, throttled, never raises, pruned), reader (state, ETA), API, CLI."""
import json
import os
import subprocess
import sys

import pytest

from app.music_brain import learn_progress as lp
from app.tests.py.testclient_compat import TestClient


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _mk(tmp_path, clock=None, **kw):
    return lp.Progress("https://youtu.be/abcdefghijk", root=tmp_path, clock=clock or Clock(), heartbeat_s=0, **kw)


def _doc(tmp_path, id_="abc"):
    return json.loads((tmp_path / f"{id_}.json").read_text())


def test_writer_shape_and_rename_from_standin(tmp_path):
    clk = Clock()
    p = _mk(tmp_path, clk)
    assert len(list(tmp_path.glob("pending-*.json"))) == 1
    p.set_id("abc", title="Big Set")
    assert not list(tmp_path.glob("pending-*.json"))
    p.stage("separate", total=4)
    clk.t += 2
    p.tick("separate", current="song.mp3")
    d = _doc(tmp_path)
    assert d["id"] == "abc" and d["title"] == "Big Set" and d["stage"] == "separate"
    assert d["counts"]["separate"] == {"done": 1, "total": 4} and d["tracks_done"] == 1
    assert d["pid"] == os.getpid() and d["current"] == "song.mp3"
    assert not list(tmp_path.glob(".*tmp"))                  # atomic: no temp left behind


def test_writes_are_throttled_but_stage_changes_and_end_are_forced(tmp_path):
    clk = Clock()
    p = _mk(tmp_path, clk)
    p.set_id("abc")
    p.stage("separate", total=9)
    p.tick("separate")
    p.tick("separate")                                       # same second: not written
    assert _doc(tmp_path)["counts"]["separate"]["done"] == 0
    clk.t += 1.5
    p.tick("separate")
    assert _doc(tmp_path)["counts"]["separate"]["done"] == 3  # memory kept all three
    p.stage("cut", total=1)                                  # forced without waiting
    assert _doc(tmp_path)["stage"] == "cut"
    p.finish()
    d = _doc(tmp_path)
    assert d["stage"] == "done" and d["finished_at"] == clk.t


def test_never_raises_even_when_the_dir_is_unwritable(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    p = lp.Progress("s", root=blocker / "sub", heartbeat_s=0)     # mkdir under a file fails
    p.set_id("abc")
    p.stage("cut", total=2)
    p.tick("cut")
    p.warn("failed x")
    p.techniques([object()])                                 # not an observation: still no raise
    p.fail(ValueError("boom"))
    p.finish()


def test_failure_records_error_and_keyboard_interrupt(tmp_path):
    p = _mk(tmp_path)
    p.set_id("abc")
    p.fail(KeyboardInterrupt())
    d = _doc(tmp_path)
    assert d["stage"] == "error" and d["error"] == "KeyboardInterrupt" and d["finished_at"]


def test_keeps_only_the_newest_20(tmp_path):
    for i in range(25):
        f = tmp_path / f"old{i:02d}.json"
        f.write_text("{}")
        os.utime(f, (100 + i, 100 + i))
    p = _mk(tmp_path)
    p.set_id("abc")
    names = {f.stem for f in tmp_path.glob("*.json")}
    assert len(names) == lp.KEEP and "abc" in names and "old00" not in names and "old24" in names


def test_warnings_from_log_lines(tmp_path):
    seen = []
    clk = Clock()
    p = _mk(tmp_path, clk)
    p.set_id("abc")
    log = p.wrap_log(seen.append)
    log("separating x.mp3")
    log("failed to cut clip 3-9 s: boom")
    log("song A - B: NOT FOUND")
    assert seen[0] == "separating x.mp3" and len(seen) == 3
    clk.t += 2
    p.update()
    assert _doc(tmp_path)["warnings"] == ["failed to cut clip 3-9 s: boom", "song A - B: NOT FOUND"]


def test_announce_lines(tmp_path):
    said = []
    p = _mk(tmp_path, announce=said.append)
    p.set_id("abc")
    p.stage("cut", total=3)
    assert any("learn-status" in s for s in said) and any(s.endswith("abc.json") for s in said) and "stage cut (3)" in said


def test_state_running_stale_done_error():
    base = {"pid": 4242, "started_at": 0, "updated_at": 1000, "stage": "separate", "counts": {}}
    dead, live = (lambda pid: False), (lambda pid: True)
    assert lp.derive(base, now=1030, alive=live)["state"] == "running"        # fresh heartbeat
    assert lp.derive(base, now=1030, alive=dead)["state"] == "stale"          # pid gone: dead at once
    assert lp.derive(base | {"pid": None}, now=1030, alive=dead)["state"] == "running"   # no pid: heartbeat decides
    assert lp.derive(base, now=1000 + lp.STALE_S + 1, alive=dead)["state"] == "stale"
    assert lp.derive(base, now=1000 + lp.STALE_S + 1, alive=live)["state"] == "running"   # slow, not dead
    assert lp.derive(base | {"stage": "done", "finished_at": 1500}, now=9999, alive=dead)["state"] == "done"
    e = lp.derive(base | {"stage": "error", "finished_at": 1200, "error": "x"}, now=9999, alive=dead)
    assert e["state"] == "error" and e["elapsed_s"] == 1200


def test_pid_alive_real_and_fake():
    assert lp.pid_alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not lp.pid_alive(p.pid)
    assert not lp.pid_alive(None)


def test_eta_math():
    d = {"stage": "separate", "started_at": 0, "stage_started_at": 100, "counts": {"separate": {"done": 2, "total": 10}}}
    assert lp.eta_s(d, now=300) == pytest.approx(200 / 2 * 8)
    assert lp.eta_s(d | {"counts": {"separate": {"done": 0, "total": 10}}}, now=300) is None
    assert lp.eta_s(d | {"counts": {"separate": {"done": 10, "total": 10}}}, now=300) == 0
    assert lp.eta_s(d | {"stage": "merge"}, now=300) is None
    out = lp.derive(d | {"pid": 1, "updated_at": 290}, now=300, alive=lambda p: True)
    assert out["state"] == "running" and out["eta_s"] == 800 and out["elapsed_s"] == 300


def test_api_shape(tmp_path, monkeypatch):
    from app.ui import server

    monkeypatch.setattr(lp, "PROGRESS_DIR", tmp_path)
    monkeypatch.setattr(lp, "THROTTLE_S", 0.0)
    now = lp.time.time()
    p = lp.Progress("s", root=tmp_path, clock=lambda: now - 30, heartbeat_s=0)
    p.set_id("older")
    p.fail(ValueError("nope"))
    q = lp.Progress("s2", root=tmp_path, heartbeat_s=0)
    q.set_id("newer", title="T")
    q.stage("separate", total=5)
    q.tick("separate")
    (tmp_path / "junk.json").write_text("{not json")
    c = TestClient(server.app)
    r = c.get("/api/learn/progress")
    assert r.status_code == 200
    studies = r.json()["studies"]
    assert [s["id"] for s in studies] == ["newer", "older"]          # newest first, junk skipped
    assert studies[0]["state"] == "running" and studies[0]["tracks_done"] == 1 and "elapsed_s" in studies[0]
    assert studies[1]["state"] == "error" and studies[1]["eta_s"] is None
    one = c.get("/api/learn/progress/newer").json()
    assert one["title"] == "T" and one["state"] == "running"
    assert c.get("/api/learn/progress/nope").status_code == 404
    assert c.get("/api/learn/progress/..%2Fx").status_code in (404, 422)


def test_learn_set_writes_progress_end_to_end(tmp_path, monkeypatch):
    """A stubbed study (test_set_learner's harness) leaves a done file with per-stage counts and technique tallies."""
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg")
    from app.music_brain import set_learner as sl
    from app.tests.py.test_set_learner import _stub_study

    _stub_study(tmp_path, monkeypatch)
    said = []
    sl.learn_set("x", tracklist="0:00 A - One\n0:36 B - Two", store_path=tmp_path / "l.json", jobs=2, ai=False,
                 log=said.append)
    (d,) = lp.read_all()
    assert d["state"] == "done" and d["stage"] == "done" and d["finished_at"]
    assert d["tracks_total"] == 2 and d["tracks_done"] == 2
    assert d["counts"]["separate"] == {"done": 2, "total": 2} and d["counts"]["analyze"]["done"] == d["counts"]["analyze"]["total"] >= 1
    assert d["techniques_found"].get("bass_swap", 0) >= 1
    assert any(x.startswith("stage separate") for x in said) and any(x.startswith("progress file") for x in said)
    assert "clip" not in json.dumps(d["warnings"])


def test_a_failed_study_leaves_an_error_file(tmp_path):
    from app.music_brain import set_learner as sl

    with pytest.raises(FileNotFoundError):
        sl.learn_set(str(tmp_path / "missing.mp3"))
    (d,) = lp.read_all()
    assert d["state"] == "error" and "missing.mp3" in d["error"]
