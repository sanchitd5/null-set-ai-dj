"""Long-lived stem separation worker (one process, model loaded once).

Why (measured 2026-09-27, Apple M-series, MPS, 3:30 song):
  subprocess `python -m demucs -n htdemucs_ft` per song ....... 53 s
  htdemucs_ft (a bag of 4 models) in a warm process ........... 38-42 s
  htdemucs (single model) in a warm process .................... 8.5-9 s
So: one process, htdemucs, and a small pipeline around the GPU:
  - a reader thread decodes the NEXT song while the current one runs on the GPU
  - a writer thread writes the finished stems while the next one runs
The GPU part stays serial (one MPS device: two concurrent models only split it).

Protocol: JSON lines. stdin  {"id", "path", "out_dir"}
                      stdout {"id", "ok": true, "stems": {name: path}, "seconds"}
                          or {"id", "ok": false, "error"}
Run: python -m app.music_brain.audio.stem_worker [--model htdemucs] [--device mps]
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from pathlib import Path

from app.music_brain.audio.audio_io import STEM_SUBTYPE, write_flac


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="htdemucs")
    ap.add_argument("--device", default=None)
    args = ap.parse_args()

    import torch
    from demucs.api import Separator

    device = args.device or ("cuda" if torch.cuda.is_available()
                             else "mps" if torch.backends.mps.is_available() else "cpu")
    sep = Separator(model=args.model, device=device, progress=False)
    out_lock = threading.Lock()

    def emit(obj: dict) -> None:
        with out_lock:
            sys.stdout.write(json.dumps(obj) + "\n")
            sys.stdout.flush()

    emit({"ready": True, "model": args.model, "device": device, "samplerate": sep.samplerate})

    jobs: "queue.Queue[dict | None]" = queue.Queue()
    decoded: "queue.Queue[tuple]" = queue.Queue(maxsize=1)   # one song decoded ahead
    to_write: "queue.Queue[tuple | None]" = queue.Queue(maxsize=2)

    def reader() -> None:
        while True:
            job = jobs.get()
            if job is None:
                decoded.put((None, None, None))
                return
            try:
                wav = sep._load_audio(Path(job["path"]))              # decode + resample on this thread
                decoded.put((job, wav, None))
            except Exception as exc:
                decoded.put((job, None, exc))

    def writer() -> None:
        while True:
            item = to_write.get()
            if item is None:
                return
            job, stems, t0 = item
            try:
                out = Path(job["out_dir"])
                out.mkdir(parents=True, exist_ok=True)
                paths = {}
                for name, wav in stems.items():
                    p = write_flac(out / f"{name}.flac", wav.cpu().numpy().T, sep.samplerate, STEM_SUBTYPE)
                    paths[name] = str(p)
                emit({"id": job["id"], "ok": True, "stems": paths, "seconds": round(time.monotonic() - t0, 2)})
            except Exception as exc:
                emit({"id": job["id"], "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    threading.Thread(target=reader, daemon=True).start()
    wt = threading.Thread(target=writer, daemon=True)
    wt.start()

    def stdin_loop() -> None:
        for line in sys.stdin:
            line = line.strip()
            if line:
                try:
                    jobs.put(json.loads(line))
                except ValueError:
                    emit({"ok": False, "error": "bad job line"})
        jobs.put(None)

    threading.Thread(target=stdin_loop, daemon=True).start()
    while True:
        job, wav, err = decoded.get()
        if job is None:
            break
        t0 = time.monotonic()
        if err is not None:
            emit({"id": job["id"], "ok": False, "error": f"decode: {err}"})
            continue
        try:
            with torch.no_grad():
                _, stems = sep.separate_tensor(wav, sep.samplerate)
            to_write.put((job, stems, t0))
        except Exception as exc:
            emit({"id": job["id"], "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    to_write.put(None)
    wt.join()


if __name__ == "__main__":
    main()
