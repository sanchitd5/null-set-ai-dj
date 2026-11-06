"""The stem server's GPU worker: one long-lived process, models loaded once, one song at a time.

The server (app/stem_server/server.py, class Worker) owns this process: it starts it on the first job,
watches its memory, kills it when it goes over the job memory limit, and lets it exit when idle so the
VRAM goes back to the card. Splits run on app.music_brain.audio.stem_engine (streaming, batched).

Protocol, JSON lines.
  stdin   {"job", "path", "out_dir", "model", "device", "batch", "half"}
  stdout  {"ready": true, "pid"}
          {"job", "progress": 0..1}
          {"job", "ok": true, "info": {...stem_engine.separate()...}}
          {"job", "ok": false, "error", "oom": bool}
Exits on stdin EOF, or after STEM_WORKER_IDLE_S (default 120) with no job.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from pathlib import Path


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    idle_s = float(os.environ.get("STEM_WORKER_IDLE_S", "120"))
    import torch

    from app.music_brain.audio.stem_engine import GpuOutOfMemory, StemEngine
    from app.stem_server.demucs_runner import _cap_vram, verify_weights

    bad = verify_weights(Path(torch.hub.get_dir()) / "checkpoints")
    if bad:
        print(f"[stem-worker] model weights failed sha256, re-downloading: {', '.join(bad)}", file=sys.stderr, flush=True)
    if torch.cuda.is_available():
        _cap_vram(torch)
        torch.backends.cudnn.benchmark = True       # fixed segment shape: autotuned kernels pay off
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
        try:
            from app.music_brain.audio.stem_service import _detect_device

            key = (job["model"], job.get("device") or _detect_device(), bool(job.get("half")))
            eng = engines.get(key)
            if eng is None:
                engines.clear()                     # one model resident at a time
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                eng = engines[key] = StemEngine(key[0], key[1], half=key[2])
            last = [0.0]

            def progress(p: float) -> None:
                now = time.monotonic()
                if p >= 1.0 or now - last[0] >= 0.5:
                    last[0] = now
                    _emit({"job": jid, "progress": round(p, 4), "device": eng.device})

            info = eng.separate(job["path"], job["out_dir"], progress=progress, batch=job.get("batch"))
            _emit({"job": jid, "ok": True, "info": info})
        except GpuOutOfMemory as exc:
            _emit({"job": jid, "ok": False, "oom": True, "error": f"GPU out of memory: {exc}"[:300]})
        except Exception as exc:  # noqa: BLE001 -- reported to the server, the worker lives on
            _emit({"job": jid, "ok": False, "oom": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
        finally:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()


if __name__ == "__main__":
    sys.exit(main())
