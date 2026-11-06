"""Stem separation benchmark (numbers in docs/engineering/STEMS_PERF.md). Run from the repo root:

  python -m app.stem_server.bench SONG [SONG ...] --what cli,warm,engine,exact,half --models htdemucs,htdemucs_ft

  cli     the old stem-server path: stem_service.separate(runner=demucs_runner), one process per song
  warm    the old console path: app.music_brain.audio.stem_worker (model kept loaded), per song
  engine  app.music_brain.audio.stem_engine (streaming, batched), per song, fp32 and --batch values
  exact   engine vs demucs apply_model(shifts=0) on the same decoded audio: residual in dB
  half    engine fp16 autocast vs fp32: SDR per stem
Writes nothing outside a temp dir. Prints one JSON line per measurement.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np


class GpuSampler:
    """nvidia-smi utilisation / memory every 200 ms while a measurement runs."""

    def __init__(self):
        self.rows, self.p = [], None

    def __enter__(self):
        try:
            self.p = subprocess.Popen(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                                       "--format=csv,noheader,nounits", "-lms", "200"],
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            threading.Thread(target=self._read, daemon=True).start()
        except OSError:
            self.p = None
        return self

    def _read(self):
        for line in self.p.stdout:
            try:
                u, m = (float(v) for v in line.split(","))
                self.rows.append((u, m))
            except ValueError:
                pass

    def __exit__(self, *a):
        if self.p:
            self.p.terminate()
            self.p.wait()

    def summary(self) -> dict:
        if not self.rows:
            return {}
        u = [r[0] for r in self.rows]
        m = [r[1] for r in self.rows]
        return {"gpu_util_mean": round(sum(u) / len(u), 1), "gpu_mem_peak_mb": max(m), "gpu_mem_min_mb": min(m)}


def emit(**kw):
    print(json.dumps(kw), flush=True)


def duration(path: str) -> float:
    import soundfile as sf
    try:
        return sf.info(path).duration
    except Exception:
        return float("nan")


def bench_cli(song, model, tmp):
    from app.music_brain.audio import stem_service

    stem_service.STEMS_CACHE_DIR = Path(tmp) / "cli"
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    with GpuSampler() as g:
        t0 = time.monotonic()
        stem_service.separate(song, model=model, device="cuda", force=True, runner="app.stem_server.demucs_runner")
        dt = time.monotonic() - t0
    emit(what="cli", model=model, song=Path(song).name, dur=round(duration(song), 1), seconds=round(dt, 2),
         child_maxrss_mb=round(max(before, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) / 1024),
         **g.summary())


def bench_warm(songs, model, tmp):
    with GpuSampler() as g:
        t0 = time.monotonic()
        p = subprocess.Popen([sys.executable, "-m", "app.music_brain.audio.stem_worker", "--model", model],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        ready = json.loads(p.stdout.readline())
        t_ready = time.monotonic() - t0
        for i, s in enumerate(songs):
            t1 = time.monotonic()
            p.stdin.write(json.dumps({"id": str(i), "path": s, "out_dir": str(Path(tmp) / f"warm{i}")}) + "\n")
            p.stdin.flush()
            res = json.loads(p.stdout.readline())
            emit(what="warm", model=model, device=ready.get("device"), song=Path(s).name, dur=round(duration(s), 1),
                 seconds=round(time.monotonic() - t1, 2), worker_seconds=res.get("seconds"), ok=res.get("ok"),
                 startup_seconds=round(t_ready, 2) if i == 0 else None)
        p.stdin.close()
        p.wait()
    emit(what="warm-gpu", model=model, **g.summary())


def bench_engine(songs, model, tmp, batches, half=False, device="cuda", threads=None):
    """batch 0 = the engine's own pick from the VRAM free at that moment."""
    import torch

    from app.music_brain.audio.stem_engine import StemEngine

    cuda = device == "cuda"
    t0 = time.monotonic()
    eng = StemEngine(model, device, half=half, threads=(threads or os.cpu_count()) if not cuda else None)
    t_load = time.monotonic() - t0
    for b in batches:
        for i, s in enumerate(songs):
            free0 = torch.cuda.mem_get_info()[0] >> 20 if cuda else None
            if cuda:
                torch.cuda.reset_peak_memory_stats()
            with GpuSampler() as g:
                try:
                    info = eng.separate(s, Path(tmp) / f"eng{i}", batch=b or None)
                except Exception as exc:  # noqa: BLE001 -- a measurement, not a crash
                    emit(what="engine", model=model, half=half, batch=b, error=str(exc)[:200])
                    continue
            emit(what="engine", model=model, device=device, threads=torch.get_num_threads(), half=half, batch_asked=b, batch_start=info["batch_start"],
                 batch_end=info["batch"], song=Path(s).name, dur=round(duration(s), 1),
                 seconds=info["seconds"], decode_seconds=info["decode_seconds"], clipped=info["clipped_samples"],
                 load_seconds=round(t_load, 2), vram_free_before_mb=free0,
                 torch_peak_mb=round(torch.cuda.max_memory_allocated() / 2**20) if cuda else None,
                 torch_reserved_peak_mb=round(torch.cuda.max_memory_reserved() / 2**20) if cuda else None,
                 maxrss_mb=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024), **g.summary())


def _engine_array(eng, x):
    blocks = []
    eng.separate_array(x, blocks.append)
    return np.concatenate(blocks, axis=1)          # (S, n, 2)


def bench_exact(song, model, tmp):
    import torch
    from demucs.apply import apply_model

    from app.music_brain.audio.stem_engine import StemEngine, decode, mix_stats

    torch.backends.cudnn.allow_tf32 = False         # TF32 convs differ per batch shape: compare in true fp32
    torch.backends.cuda.matmul.allow_tf32 = False
    raw = Path(tmp) / "x.f32"
    n = decode(song, raw)
    x = np.fromfile(raw, dtype=np.float32).reshape(n, 2)
    eng = StemEngine(model, "cuda", batch=4)
    ours = _engine_array(eng, x)
    from demucs.pretrained import get_model
    m = get_model(model)
    mean, std = mix_stats(x)
    t = torch.from_numpy(((x.T - mean) / std).astype(np.float32))[None]
    with torch.no_grad():
        ref = apply_model(m, t, shifts=0, split=True, overlap=0.25, device="cuda")[0].numpy() * std + mean
    ref = ref.transpose(0, 2, 1)
    res = 10 * np.log10(np.sum((ours - ref) ** 2) / np.sum(ref ** 2))
    emit(what="exact", model=model, song=Path(song).name, residual_db=round(float(res), 1),
         max_abs_diff=float(np.max(np.abs(ours - ref))))


def bench_half(songs, model):
    from app.music_brain.audio.stem_engine import StemEngine, decode

    full, half = StemEngine(model, "cuda", batch=8), StemEngine(model, "cuda", half=True, batch=8)
    for s in songs:
        with tempfile.TemporaryDirectory() as d:
            raw = Path(d) / "x.f32"
            n = decode(s, raw)
            x = np.fromfile(raw, dtype=np.float32).reshape(n, 2)
        a = _engine_array(full, x)
        b = _engine_array(half, x)
        sdr = {name: round(float(10 * np.log10(np.sum(a[k] ** 2) / max(1e-20, np.sum((a[k] - b[k]) ** 2)))), 1)
               for k, name in enumerate(full.sources)}
        emit(what="half", model=model, song=Path(s).name, sdr_fp16_vs_fp32_db=sdr)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("songs", nargs="+")
    ap.add_argument("--what", default="cli,warm,engine,exact,half")
    ap.add_argument("--models", default="htdemucs,htdemucs_ft")
    ap.add_argument("--batch", default="1,4,8")
    ap.add_argument("--threads", default=str(os.cpu_count()), help="cpu mode: thread counts to try")
    a = ap.parse_args(argv)
    what = set(a.what.split(","))
    with tempfile.TemporaryDirectory(dir=os.environ.get("STEM_WORK_DIR") or None) as tmp:
        for model in a.models.split(","):
            if "cli" in what:
                for s in a.songs:
                    bench_cli(s, model, tmp)
            if "warm" in what:
                bench_warm(a.songs, model, tmp)
            if "engine" in what:
                bench_engine(a.songs, model, tmp, [int(b) for b in a.batch.split(",")])
            if "enginehalf" in what:
                bench_engine(a.songs, model, tmp, [int(b) for b in a.batch.split(",")], half=True)
            if "cpu" in what:
                for t in (int(v) for v in a.threads.split(",")):
                    bench_engine(a.songs[:1], model, tmp, [1], device="cpu", threads=t)
            if "exact" in what:
                bench_exact(a.songs[0], model, tmp)
            if "half" in what:
                bench_half(a.songs, model)


if __name__ == "__main__":
    main()
