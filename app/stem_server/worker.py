"""The stem server's GPU worker: one long-lived process, models loaded once, one song at a time.

The server (app/stem_server/server.py, class Worker) owns this process: it starts it on the first job,
watches its memory, kills it when it goes over the job memory limit, and lets it exit when idle so the
VRAM goes back to the card. Splits run on app.music_brain.audio.stem_engine (streaming, batched).

Protocol, JSON lines.
  stdin   {"job", "path", "out_dir", "model", "device", "batch", "half"}
  stdout  {"ready": true, "pid"}
          {"job", "progress": 0..1}
          {"job", "ok": true, "info": {...stem_engine.separate()...}}
          {"job", "ok": false, "error", "oom": bool, "gpu": {...}|null, "wait_expired": bool}
Exits on stdin EOF, or after STEM_WORKER_IDLE_S (default 120) with no job.

The card is shared (llama-server holds ~20 GB of the 24), so every job manages its own VRAM (run_job):
empty the cache, wait up to STEM_GPU_WAIT_S (default 60, polled every 2 s) for room, put the weights on the
card only for the job (STEM_KEEP_MODEL_ON_GPU=1 keeps them), size the batch to what is free, empty the
cache again. The allocator runs with expandable_segments so a freed cache really returns to the card.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")   # before torch is imported


def _is_oom(exc: BaseException) -> bool:
    """torch.OutOfMemoryError (also raised while loading the model onto a full card) or GpuOutOfMemory."""
    return "out of memory" in str(exc).lower() or type(exc).__name__ in ("OutOfMemoryError", "GpuOutOfMemory")


def _free_gpu(torch) -> None:
    import gc

    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _log(msg: str) -> None:
    print(f"[stem-worker] {msg}", file=sys.stderr, flush=True)


def run_job(torch, engines: dict, job: dict, progress: Callable[[float, str], None],
            make_engine: Optional[Callable[..., Any]] = None, sleep: Callable[[float], None] = time.sleep) -> dict:
    """One song. On the GPU: empty the cache, wait (STEM_GPU_WAIT_S, poll 2 s) until the weights + a batch-1
    split fit the VRAM usable, move the weights over, let the engine pick the batch from what is free, run,
    then move the weights back to host RAM and empty the cache again (STEM_KEEP_MODEL_ON_GPU=1 keeps them
    there). -> engine info + info["gpu"] (kind, free_mb / total_mb at start, wait_s, segment_s, batch).
    A failure carries the same dict as exc.gpu_stats."""
    from app.music_brain.audio.stem_engine import GpuOutOfMemory, StemEngine, wait_for_vram
    from app.music_brain.audio.stem_service import _detect_device

    make_engine = make_engine or StemEngine
    device = job.get("device") or _detect_device()
    cuda = device == "cuda"
    key = (job["model"], device, bool(job.get("half")))
    stats: dict = {"kind": "cpu" if device == "cpu" else "gpu", "device": device, "free_mb": None, "total_mb": None,
                   "wait_s": 0.0, "segment_s": None, "batch": None, "batch_start": None}
    eng = None
    try:
        _free_gpu(torch)
        eng = engines.get(key)
        if eng is None:
            for old in engines.values():                # one model resident at a time
                old.release()
            engines.clear()
            _free_gpu(torch)
            eng = make_engine(key[0], device, half=key[2], resident=not cuda)   # CUDA: weights move after the wait
            engines[key] = eng
        stats["segment_s"] = round(eng.seg / eng.samplerate, 2)
        if cuda:
            waited, free, total = wait_for_vram(torch, eng.min_need_mb(), float(os.environ.get("STEM_GPU_WAIT_S", "60")),
                                                sleep=sleep, log=lambda m: _log(f"{job.get('job')}: {m}"))
            stats.update(wait_s=round(waited, 1), free_mb=round(free), total_mb=round(total))
            eng.acquire()
        info = eng.separate(job["path"], job["out_dir"], progress=lambda p: progress(p, eng.device), batch=job.get("batch"))
        stats.update(batch=info.get("batch"), batch_start=info.get("batch_start"))
        _log(f"{job.get('job')}: {device}, {stats['free_mb']} of {stats['total_mb']} MB free at start, waited {stats['wait_s']}s, "
             f"segment {stats['segment_s']}s x batch {stats['batch_start']}" +
             (f" (backed off to {stats['batch']})" if stats["batch"] != stats["batch_start"] else ""))
        info["gpu"] = stats
        return info
    except Exception as exc:  # noqa: BLE001 -- re-raised with the stats attached
        exc.gpu_stats = stats
        if isinstance(exc, GpuOutOfMemory) or _is_oom(exc):
            _log(f"{job.get('job')}: {device} out of memory ({str(exc)[:120]})")
        raise
    finally:
        if eng is not None and cuda and os.environ.get("STEM_KEEP_MODEL_ON_GPU") != "1":
            try:
                eng.release()
            except Exception as exc:  # noqa: BLE001 -- a failed release must not mask the job's result
                _log(f"could not move the weights off the GPU: {exc}")
        _free_gpu(torch)


def main() -> int:
    idle_s = float(os.environ.get("STEM_WORKER_IDLE_S", "120"))
    import torch

    from app.music_brain.audio.stem_engine import GpuOutOfMemory
    from app.stem_server.demucs_runner import _cap_vram, verify_weights

    bad = verify_weights(Path(torch.hub.get_dir()) / "checkpoints")
    if bad:
        print(f"[stem-worker] model weights failed sha256, re-downloading: {', '.join(bad)}", file=sys.stderr, flush=True)
    if torch.cuda.is_available():
        _cap_vram(torch)                            # (no cudnn.benchmark: the batch varies with free VRAM,
                                                    #  and each new shape would be autotuned again)
    engines: dict = {}
    lines: "queue.Queue[str]" = queue.Queue()

    def read_stdin() -> None:
        for ln in sys.stdin:
            lines.put(ln)
        lines.put("")                               # EOF: the server went away

    threading.Thread(target=read_stdin, daemon=True).start()
    _emit({"ready": True, "pid": os.getpid()})
    while True:
        try:
            line = lines.get(timeout=idle_s)
        except queue.Empty:
            return 0                                # idle: release the VRAM
        if not line:
            return 0
        try:
            job = json.loads(line)
        except ValueError:
            continue
        jid = job.get("job")
        last = [0.0]

        def progress(p: float, device: str) -> None:
            now = time.monotonic()
            if p >= 1.0 or now - last[0] >= 0.5:
                last[0] = now
                _emit({"job": jid, "progress": round(p, 4), "device": device})

        try:
            _emit({"job": jid, "ok": True, "info": run_job(torch, engines, job, progress)})
        except Exception as exc:  # noqa: BLE001 -- reported to the server, the worker lives on
            oom = isinstance(exc, GpuOutOfMemory) or _is_oom(exc)
            msg = f"GPU out of memory: {exc}" if isinstance(exc, GpuOutOfMemory) else f"{type(exc).__name__}: {exc}"
            _emit({"job": jid, "ok": False, "oom": oom, "error": msg[:300], "gpu": getattr(exc, "gpu_stats", None),
                   "wait_expired": bool(getattr(exc, "wait_expired", False))})


if __name__ == "__main__":
    sys.exit(main())
