"""GPU memory management of the stem worker: batch by free VRAM, wait before falling back, OOM backoff,
CPU fallback, empty_cache around every job, status fields. torch.cuda is faked (no GPU on the bench)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.music_brain.audio import stem_engine as se
from app.stem_server import gpu, worker

MB = 2**20


class FakeCuda:
    def __init__(self, free_mb, total_mb=24576, frees=None):
        self.free_mb, self.total_mb = free_mb, total_mb
        self.frees = list(frees or [])             # successive free values, one per mem_get_info call
        self.empty_calls = 0

    def mem_get_info(self):
        if self.frees:
            self.free_mb = self.frees.pop(0) if len(self.frees) > 1 else self.frees[0]
        return int(self.free_mb * MB), int(self.total_mb * MB)

    def empty_cache(self):
        self.empty_calls += 1

    def is_available(self):
        return True


def fake_torch(free_mb, **kw):
    return SimpleNamespace(cuda=FakeCuda(free_mb, **kw))


class FakeEngine:
    """Stands in for StemEngine: records acquire / release, can raise OOM at given batch sizes."""
    seg, samplerate = 343980, 44100
    instances: list = []

    def __init__(self, model, device, half=False, resident=True):
        self.device, self.resident_args = device, resident
        self.on_device, self.log, self.oom_batches = resident, [], []
        self.torch = None
        self.batch = None
        FakeEngine.instances.append(self)

    def min_need_mb(self):
        return se.SEG_MB + se.VRAM_RESERVE_MB + se.VRAM_MARGIN_MB + (0 if self.on_device else 1000)

    def acquire(self):
        self.log.append("acquire")
        self.on_device = True

    def release(self):
        self.log.append("release")
        self.on_device = False

    def separate(self, src, out_dir, progress=None, batch=None):
        self.log.append("separate")
        self.batch = batch or 4
        if self.oom_batches:
            raise se.GpuOutOfMemory("CUDA out of memory (fake)")
        return {"stems": {}, "frames": 1, "seconds": 1.0, "device": self.device, "batch": 2, "batch_start": self.batch}


def job(**kw):
    return {"job": "j1", "path": "x", "out_dir": "o", "model": "htdemucs_ft", "device": "cuda", **kw}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    FakeEngine.instances = []
    monkeypatch.delenv("STEM_KEEP_MODEL_ON_GPU", raising=False)
    monkeypatch.setenv("STEM_GPU_WAIT_S", "60")


# ---- segments (batch) by free memory ---------------------------------------------------------------
@pytest.mark.parametrize("free,batch", [(24000, 8), (3891, 6), (2400, 3), (1500, 1), (200, 1)])
def test_pick_batch_follows_the_free_vram(free, batch):
    eng = object.__new__(se.StemEngine)
    eng.fixed_batch, eng.device, eng.torch = None, "cuda", fake_torch(free)
    eng.torch.cuda.get_per_process_memory_fraction = lambda i: 1.0
    assert eng.pick_batch() == batch


def test_pick_batch_honours_the_vram_cap():
    eng = object.__new__(se.StemEngine)
    eng.fixed_batch, eng.device, eng.torch = None, "cuda", fake_torch(20000)
    eng.torch.cuda.get_per_process_memory_fraction = lambda i: 0.1            # 2457 MB of 24576
    eng.torch.cuda.memory_reserved = lambda i: 200 * MB
    assert eng.pick_batch() == 3


# ---- wait, don't fall back early -------------------------------------------------------------------
def test_wait_then_run():
    t = fake_torch(0, frees=[300, 300, 5000])
    now = [0.0]
    waited, first, total = se.wait_for_vram(t, 1200, 60, sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
    assert waited == 4.0 and first == 300 and total == 24576


def test_no_wait_when_it_fits():
    assert se.wait_for_vram(fake_torch(5000), 1200, 60)[0] == 0.0


def test_wait_expires_after_the_limit():
    now = [0.0]
    with pytest.raises(se.GpuOutOfMemory) as e:
        se.wait_for_vram(fake_torch(300), 1200, 60, sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
    assert e.value.wait_expired and now[0] >= 60


def test_job_waits_for_room_then_runs_and_reports(monkeypatch):
    t = fake_torch(0, frees=[100, 100, 6000])
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    info = worker.run_job(t, {}, job(), lambda p, d: None, make_engine=FakeEngine, sleep=lambda s: None)
    g = info["gpu"]
    assert g["kind"] == "gpu" and g["free_mb"] == 100 and g["total_mb"] == 24576 and g["wait_s"] >= 0
    assert g["segment_s"] == 7.8 and g["batch"] == 2 and g["batch_start"] == 4


def test_job_wait_expiry_raises_with_stats(monkeypatch):
    monkeypatch.setenv("STEM_GPU_WAIT_S", "0")
    with pytest.raises(se.GpuOutOfMemory) as e:
        worker.run_job(fake_torch(100), {}, job(), lambda p, d: None, make_engine=FakeEngine, sleep=lambda s: None)
    assert e.value.wait_expired and e.value.gpu_stats["free_mb"] is None and e.value.gpu_stats["kind"] == "gpu"
    assert FakeEngine.instances[0].log == ["release"]            # never acquired, nothing stuck on the card


# ---- hygiene ---------------------------------------------------------------------------------------
def test_empty_cache_before_and_after_and_weights_leave_the_card():
    t = fake_torch(8000)
    worker.run_job(t, {}, job(), lambda p, d: None, make_engine=FakeEngine)
    eng = FakeEngine.instances[0]
    assert eng.log == ["acquire", "separate", "release"] and not eng.on_device
    assert t.cuda.empty_calls >= 3                 # before, after the model swap, after the job


def test_empty_cache_after_an_oom_and_weights_still_released():
    t = fake_torch(8000)
    engines = {}
    FakeEngine.instances = []
    orig = FakeEngine.separate

    def boom(self, *a, **k):
        raise se.GpuOutOfMemory("CUDA out of memory (fake)")

    FakeEngine.separate = boom
    try:
        before = t.cuda.empty_calls
        with pytest.raises(se.GpuOutOfMemory):
            worker.run_job(t, engines, job(), lambda p, d: None, make_engine=FakeEngine)
    finally:
        FakeEngine.separate = orig
    assert t.cuda.empty_calls > before and FakeEngine.instances[0].log[-1] == "release"


def test_keep_model_on_gpu_option(monkeypatch):
    monkeypatch.setenv("STEM_KEEP_MODEL_ON_GPU", "1")
    worker.run_job(fake_torch(8000), {}, job(), lambda p, d: None, make_engine=FakeEngine)
    assert FakeEngine.instances[0].on_device


def test_cpu_job_skips_the_vram_logic():
    t = fake_torch(0)
    info = worker.run_job(t, {}, job(device="cpu"), lambda p, d: None, make_engine=FakeEngine)
    assert info["gpu"]["kind"] == "cpu" and info["gpu"]["wait_s"] == 0.0 and FakeEngine.instances[0].log == ["separate"]


def test_worker_sets_expandable_segments():
    import os

    assert "expandable_segments" in os.environ["PYTORCH_CUDA_ALLOC_CONF"]


# ---- engine: weights on / off the card, OOM -> smaller batch ---------------------------------------
def test_engine_release_and_acquire_move_the_weights():
    torch = pytest.importorskip("torch")
    pytest.importorskip("demucs.apply")
    from app.tests.py.test_stem_engine import Fake

    eng = se.StemEngine("fake", "cpu", loaded=Fake(), resident=False)
    assert not eng.on_device and eng.model_mb > 0
    assert eng.min_need_mb() == se.SEG_MB + se.VRAM_RESERVE_MB + se.VRAM_MARGIN_MB + eng.model_mb
    eng.acquire()
    assert eng.on_device and eng.min_need_mb() == se.SEG_MB + se.VRAM_RESERVE_MB + se.VRAM_MARGIN_MB
    eng.release()                                   # cpu device: nothing to release
    assert eng.on_device


def test_engine_oom_empties_the_cache_and_halves_the_batch():
    np = pytest.importorskip("numpy")
    t = fake_torch(8000)
    eng = object.__new__(se.StemEngine)
    eng.device, eng.torch, eng.batch = "cuda", t, 8
    seen = []

    def forward(xb):
        seen.append(len(xb))
        if len(xb) > 2:
            raise RuntimeError("CUDA error: out of memory (fake)")
        return xb

    eng._forward = forward
    out = eng._run_batch(np.zeros((8, 2, 5), dtype=np.float32))
    assert out.shape[0] == 8 and eng.batch == 2 and seen[:3] == [8, 4, 2]
    assert t.cuda.empty_calls == 2                  # once per halving
    eng.batch = 1
    eng._forward = lambda xb: (_ for _ in ()).throw(RuntimeError("out of memory"))
    with pytest.raises(se.GpuOutOfMemory):
        eng._run_batch(np.zeros((1, 2, 5), dtype=np.float32))
    assert t.cuda.empty_calls == 3


# ---- ladder: OOM -> next smaller -> CPU; status fields ---------------------------------------------
class Scripted:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def run(self, msg, on_progress):
        self.calls.append((msg["device"], msg["batch"]))
        return self.script.pop(0)


def _ok(kind="gpu", **g):
    return {"ok": True, "info": {"stems": {}, "frames": 1, "seconds": 1.0, "gpu": {"kind": kind, "wait_s": 0.0, "free_mb": None, "total_mb": None, **g}}}


OOM_FAIL = {"ok": False, "oom": True, "error": "GPU out of memory: x", "gpu": {"kind": "gpu", "free_mb": 900, "total_mb": 24576, "wait_s": 3.0}}


def test_oom_then_smaller_batch_succeeds_on_gpu():
    w = Scripted([OOM_FAIL, _ok(free_mb=1800, total_mb=24576)])
    info, dev, note = gpu.split(w, "j", Path("x"), Path("o"), "htdemucs_ft", "cuda", lambda *a: None, sleep=lambda s: None)
    assert w.calls == [("cuda", None), ("cuda", 1)] and info["gpu"]["kind"] == "gpu" and info["gpu"]["wait_s"] == 3.0


def test_smallest_fails_goes_to_cpu_and_status_says_why():
    w = Scripted([OOM_FAIL, OOM_FAIL, _ok("cpu")])
    info, dev, note = gpu.split(w, "j", Path("x"), Path("o"), "htdemucs_ft", "cuda", lambda *a: None, sleep=lambda s: None)
    assert dev == "cpu" and info["gpu"]["kind"] == "cpu" and info["gpu"]["fallback"] == "GPU out of memory"
    assert info["gpu"]["free_mb"] == 900 and info["gpu"]["wait_s"] == 6.0


def test_wait_expired_goes_straight_to_cpu_without_a_retry():
    expired = {**OOM_FAIL, "wait_expired": True}
    w = Scripted([expired, _ok("cpu")])
    slept = []
    info, dev, note = gpu.split(w, "j", Path("x"), Path("o"), "htdemucs_ft", "cuda", lambda *a: None, sleep=slept.append)
    assert w.calls == [("cuda", None), ("cpu", None)] and slept == [] and info["gpu"]["fallback"] == "GPU wait expired"


def test_job_dict_and_jobs_route_carry_the_gpu_fields():
    from app.stem_server.server import Job

    j = Job("t", Path("x"), 4)
    assert j.to_dict()["gpu"] is None
    j.info = {"gpu": {"kind": "gpu", "free_mb": 3891, "total_mb": 24576, "wait_s": 0.0, "segment_s": 7.8, "batch": 6}}
    assert j.to_dict()["gpu"]["batch"] == 6 and "status" in j.to_dict()
