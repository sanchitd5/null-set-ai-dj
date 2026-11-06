"""Stem server queue (app/stem_server/server.py): one GPU job at a time in priority order (deck / booked >
phone > backfill), one job per song (a higher ask raises it), the queue survives a restart, a device token
cannot jump the console's decks, and /v1/jobs reports position / progress / ETA. GPU worker ladder
(app/stem_server/gpu.py): GPU OOM -> CPU, a memory kill -> once more at batch 1, a crash -> fresh worker."""

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

TOKEN = "q" * 32


def _settings(tmp: Path) -> Settings:
    return Settings({"STEM_SERVER_TOKEN": TOKEN, "AIDJ_CACHE_DIR": str(tmp / "cache"),
                     "DJ_AUTH_FILE": str(tmp / "state" / "auth.json")})


def _track(s: Settings, n: int) -> str:
    body = f"song-{n}".encode() * 100
    tid = hashlib.sha256(body).hexdigest()[:16]
    s.uploads.mkdir(parents=True, exist_ok=True)
    (s.uploads / f"{tid}.mp3").write_bytes(body)
    return tid


class GatedSplit:
    """separate_fn that records the order and blocks until released."""

    def __init__(self):
        self.order, self.gate = [], threading.Event()

    def __call__(self, path, model, device, stems_dir):
        self.order.append(Path(path).stem)
        self.gate.wait(10)
        h = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        d = Path(stems_dir) / f"{h}_{model}"
        d.mkdir(parents=True, exist_ok=True)
        z = np.zeros((10, 2), dtype=np.int16)
        write_manifest(d, {n: str(write_flac(d / f"{n}.flac", z, 44100)) for n in ("vocals", "drums", "bass", "other")})
        return {}, "cpu", None


def _wait(cond, t=10.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_priority_order_dedup_and_raise(tmp_path):
    s = _settings(tmp_path)
    split = GatedSplit()
    srv = StemServer(s, split)
    first, b1, p1, d1, b2 = (_track(s, i) for i in range(5))
    srv.submit(first, 4, "backfill")
    assert _wait(lambda: split.order == [first])               # running, holds the GPU
    jb1 = srv.submit(b1, 4, "backfill")
    jp1 = srv.submit(p1, 4, "phone")
    jd1 = srv.submit(d1, 4, "deck")
    jb2 = srv.submit(b2, 4, "backfill")
    assert [j.position for j in (jd1, jp1, jb1, jb2)] == [1, 2, 3, 4]
    again = srv.submit(b2, 4, "deck")                          # a deck asks for a parked backfill song
    assert again is jb2 and jb2.priority == "deck" and jb2.position == 2
    assert srv.submit(b1, 4, "backfill") is jb1                 # one job per song
    split.gate.set()
    assert _wait(lambda: all(j.status == "done" for j in (jb1, jp1, jd1, jb2)))
    assert split.order == [first, d1, b2, p1, b1]
    assert srv.submit(d1, 4, "deck").status == "done"           # split: answered from the cache


def test_queue_survives_a_restart(tmp_path):
    s = _settings(tmp_path)
    split = GatedSplit()
    srv = StemServer(s, split)
    a, b, c = (_track(s, i) for i in range(3))
    ja = srv.submit(a, 4, "phone")
    assert _wait(lambda: split.order == [a])
    jb, jc = srv.submit(b, 4, "backfill"), srv.submit(c, 4, "deck")
    rows = json.loads(s.state.read_text())["jobs"]
    assert [r["id"] for r in rows] == [a, c, b]                 # running, then queue order
    # "restart": a new server on the same cache, the old worker never finishes
    split2 = GatedSplit()
    split2.gate.set()
    srv2 = StemServer(s, split2)
    assert _wait(lambda: len(split2.order) == 3)
    assert split2.order[0] == c                                 # deck first; the interrupted one starts over
    assert sorted(split2.order) == sorted([a, b, c])
    assert _wait(lambda: srv2.jobs.get(jc.job) is not None and srv2.jobs[jc.job].status == "done")
    assert srv2.jobs[ja.job].status == "done" and srv2.jobs[jb.job].status == "done"   # same job ids
    assert _wait(lambda: json.loads(s.state.read_text())["jobs"] == [])
    split.gate.set()


def test_library_link_is_a_track_and_the_hash_still_binds_it(tmp_path):
    s = _settings(tmp_path)
    srv = StemServer(s, GatedSplit())
    lib = tmp_path / "library"
    lib.mkdir()
    body = b"library-song" * 50
    tid = hashlib.sha256(body).hexdigest()[:16]
    (lib / "Song.MP3").write_bytes(body)
    s.links.mkdir(parents=True)
    (s.links / f"{tid}.mp3").symlink_to(lib / "Song.MP3")
    assert srv.track_path(tid) == s.links / f"{tid}.mp3"
    assert srv.full_hash(tid, srv.track_path(tid)).startswith(tid)
    (lib / "Song.MP3").write_bytes(b"other bytes")              # the link now points at other audio
    srv._hash.clear()
    with pytest.raises(Exception):
        srv.full_hash(tid, srv.track_path(tid))


# ---- the GPU worker ladder (gpu.split) with a scripted worker ------------------------------------
class ScriptedWorker:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def run(self, msg, on_progress):
        self.calls.append((msg["device"], msg["batch"]))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        on_progress(0.5, msg["device"] or "cuda")
        return step


OK = {"ok": True, "info": {"stems": {}, "frames": 1, "seconds": 1.0}, "peak_rss": 0}


def test_ladder_gpu_oom_goes_to_cpu():
    w = ScriptedWorker([{"ok": False, "oom": True, "error": "GPU out of memory"}, OK])
    info, dev, note = gpu.split(w, "j", Path("x"), Path("o"), "htdemucs_ft", None, lambda *a: None)
    assert w.calls == [(None, None), ("cpu", None)] and dev == "cpu" and "split on CPU" in note


def test_ladder_memory_kill_retries_at_batch_one_then_gives_up():
    w = ScriptedWorker([gpu.WorkerDied("over limit", memory=True), OK])
    _, _, note = gpu.split(w, "j", Path("x"), Path("o"), "htdemucs_ft", "cuda", lambda *a: None)
    assert w.calls == [("cuda", None), ("cuda", 1)] and "batch 1" in note
    w = ScriptedWorker([gpu.WorkerDied("over", memory=True), gpu.WorkerDied("over again", memory=True)])
    with pytest.raises(RuntimeError, match="over again"):
        gpu.split(w, "j", Path("x"), Path("o"), "htdemucs_ft", "cuda", lambda *a: None)


def test_ladder_crash_gets_one_fresh_worker_and_errors_are_not_retried():
    w = ScriptedWorker([gpu.WorkerDied("segfault"), OK])
    gpu.split(w, "j", Path("x"), Path("o"), "htdemucs_ft", "cuda", lambda *a: None)
    assert len(w.calls) == 2
    w = ScriptedWorker([{"ok": False, "oom": False, "error": "ffmpeg could not decode"}])
    with pytest.raises(RuntimeError, match="decode"):
        gpu.split(w, "j", Path("x"), Path("o"), "htdemucs_ft", "cuda", lambda *a: None)
    assert len(w.calls) == 1


FAKE_WORKER = r'''
import json, os, sys, time
print(json.dumps({"ready": True, "pid": os.getpid()}), flush=True)
for line in sys.stdin:
    job = json.loads(line)
    if job["path"] == "hog":
        hog = b"x" * (96 << 20)                    # 96 MB touched: over the test's 48 MB limit
        time.sleep(5)
    if job["path"] == "die":
        os._exit(3)
    print(json.dumps({"job": job["job"], "progress": 0.5, "device": "cpu"}), flush=True)
    print(json.dumps({"job": job["job"], "ok": True, "info": {"stems": {}, "frames": 1}}), flush=True)
'''


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="RSS guard reads /proc (Linux)")
def test_rss_guard_kills_the_worker_not_the_server(tmp_path):
    import sys

    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER)
    w = gpu.GpuWorker(rss_limit=48 << 20, cmd=[sys.executable, str(script)])
    seen = []
    assert w.run({"job": "a", "path": "fine"}, lambda p, d=None: seen.append(p))["ok"] and seen == [0.5]
    with pytest.raises(gpu.WorkerDied) as e:
        w.run({"job": "b", "path": "hog"}, lambda *a: None)
    assert e.value.memory and not w.alive
    assert w.run({"job": "c", "path": "fine"}, lambda *a: None)["ok"]    # a fresh worker


def test_worker_death_is_reported(tmp_path):
    import sys

    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER)
    w = gpu.GpuWorker(cmd=[sys.executable, str(script)])
    with pytest.raises(gpu.WorkerDied) as e:
        w.run({"job": "a", "path": "die"}, lambda *a: None)
    assert not e.value.memory and "exit 3" in str(e.value)
