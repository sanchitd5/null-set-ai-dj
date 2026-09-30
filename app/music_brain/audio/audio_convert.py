"""Convert the existing WAV cache to FLAC in place (stems as 16-bit, keylock renders as 24-bit).

    python3 -m app.music_brain.audio.audio_convert                      # dry run: files, bytes, projected saving
    python3 -m app.music_brain.audio.audio_convert --apply [--only stems|keylock] [--limit N]
                                             [--jobs N] [--max-mem-gb G]

Per file: encode to a tmp FLAC, decode it back and compare with the WAV (same rate, channels and frame
count; samples bit-exact at 16-bit, max abs error under 1e-6 at 24-bit), atomically move it into place,
update the manifest / meta, and only then delete the WAV. On any mismatch the WAV stays and the file is
reported. Resumable and idempotent: a converted file has no WAV left, so a re-run skips it. Holds a lock
file so two converters never overlap, and skips keylock sets whose render is in flight (`<key>.tmp`).
--jobs N converts N folders at once in worker processes (one folder per worker, so a manifest
or meta.json has one writer); --max-mem-gb lowers N to fit. SIGINT / SIGTERM stop handing out
folders and let the running ones finish.
Ratios for the dry run are the measured ones in research/notes/audio-format-study.md.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import multiprocessing
import os
import signal
import sys
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import numpy as np

from app.music_brain.audio import audio_io

RATIO = {"PCM_16": 0.422, "PCM_24": 0.459}       # FLAC bytes / WAV bytes (study, MEASURED)
TOL_24 = 1e-6                                   # 24-bit quantisation step is 2^-23 ~ 1.2e-7
LOCK_NAME = ".audio_convert.lock"


def _default_cache() -> Path:
    from app.music_brain.config import CACHE_DIR
    return Path(CACHE_DIR)


def _subtype_for(wav: Path) -> str:
    import soundfile as sf
    st = sf.info(str(wav)).subtype
    return audio_io.RENDER_SUBTYPE if st in ("FLOAT", "DOUBLE", "PCM_24", "PCM_32") else audio_io.STEM_SUBTYPE


def verify(wav: Path, flac: Path, subtype: str) -> Optional[str]:
    """None when the FLAC decodes to the WAV's audio, else what differs."""
    import soundfile as sf
    a, b = sf.info(str(wav)), sf.info(str(flac))
    if (a.samplerate, a.channels, a.frames) != (b.samplerate, b.channels, b.frames):
        return f"shape {a.samplerate}/{a.channels}/{a.frames} != {b.samplerate}/{b.channels}/{b.frames}"
    if subtype == "PCM_16":
        x, _ = sf.read(str(wav), dtype="int16", always_2d=True)
        y, _ = sf.read(str(flac), dtype="int16", always_2d=True)
        return None if np.array_equal(x, y) else "16-bit samples differ"
    x, _ = sf.read(str(wav), dtype="float64", always_2d=True)
    y, _ = sf.read(str(flac), dtype="float64", always_2d=True)
    err = float(np.max(np.abs(np.clip(x, -1.0, 1.0) - y))) if x.size else 0.0
    return None if err < TOL_24 else f"24-bit max abs error {err:.3g}"


def convert_file(wav: Path, subtype: Optional[str] = None) -> Tuple[Path, int, int]:
    """wav -> sibling .flac, verified. Returns (flac, wav_bytes, flac_bytes); raises on mismatch (WAV kept)."""
    subtype = subtype or _subtype_for(wav)
    flac = wav.with_suffix(".flac")
    tmp = wav.with_name(f".{wav.stem}.convert{os.getpid()}.flac")
    try:
        audio_io.encode_file(wav, tmp, subtype)
        bad = verify(wav, tmp, subtype)
        if bad:
            raise ValueError(bad)
        os.replace(tmp, flac)
    finally:
        tmp.unlink(missing_ok=True)
    return flac, wav.stat().st_size, flac.stat().st_size


# ---- discovery -----------------------------------------------------------------------------------
def stem_dirs(cache: Path) -> Iterator[Path]:
    root = cache / "stems"
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir() and (d / audio_io.MANIFEST_NAME).exists():   # no manifest = not finished
                yield d


def keylock_dirs(cache: Path) -> Iterator[Path]:
    root = cache / "keylock"
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if (d.is_dir() and not d.name.endswith(".tmp") and (d / "meta.json").exists()
                    and not d.with_suffix(".tmp").exists()):             # render in flight
                yield d


def _stem_wavs(d: Path) -> List[Path]:
    m = audio_io.read_manifest(d) or {}
    out = []
    for p in map(Path, m.values()):
        if p.suffix.lower() == ".wav":
            out.append(p)
        elif p.with_suffix(".wav").exists():   # killed after the manifest moved: finish the delete
            out.append(p.with_suffix(".wav"))
    return out


def _keylock_wavs(d: Path) -> List[Path]:
    return sorted(p for p in d.glob("*.wav") if not p.name.startswith("."))


def plan(cache: Path, only: Optional[str] = None) -> List[Tuple[str, Path, List[Path]]]:
    out = []
    if only in (None, "stems"):
        out += [("stems", d, w) for d in stem_dirs(cache) if (w := _stem_wavs(d))]
    if only in (None, "keylock"):
        out += [("keylock", d, w) for d in keylock_dirs(cache) if (w := _keylock_wavs(d))]
    return out


# ---- run -----------------------------------------------------------------------------------------
def _finish_dir(kind: str, d: Path, done: dict) -> None:
    """Point the manifest / meta at the FLACs before any WAV goes away."""
    if kind == "stems":
        m = audio_io.read_manifest(d)          # WAVs still exist here, so swap in the new FLACs by path
        if not m:
            raise ValueError("manifest unreadable")
        audio_io.write_manifest(d, {n: str(done.get(Path(p), p)) for n, p in m.items()})
    else:
        p = d / "meta.json"
        meta = json.loads(p.read_text())
        wavs_left = [w for w in d.glob("*.wav") if w not in done]
        meta["format"] = "flac" if not wavs_left else "mixed"
        tmp = p.with_name(f".meta.json.tmp{os.getpid()}")
        tmp.write_text(json.dumps(meta))
        os.replace(tmp, p)


def _clean_tmp(d: Path) -> None:
    """Drop tmp files a killed earlier run or worker left behind: our `.x.convertPID.flac`, and the
    `.name.tmpPID` that audio_io's atomic writers use (FLAC, manifest, meta). Safe: only called on a
    finished folder by the lock holder, while no worker is converting it."""
    for t in set(d.glob(".*.convert*")) | set(d.glob(".*.tmp*")):
        t.unlink(missing_ok=True)


def _result(kind: str, d: Path) -> dict:
    return {"kind": kind, "dir": str(d), "files": 0, "converted": 0, "skipped": 0, "failed": [],
            "bytes_before": 0, "bytes_after": 0, "done": 0, "wavs": 0}


def convert_dir(kind: str, d: Path, wavs: List[Path]) -> dict:
    """Convert one cache folder start to finish: the unit of parallel work. Never raises; failures are
    in the result. Only this call writes d's manifest / meta.json while it runs."""
    r = _result(kind, d)
    r["wavs"] = len(wavs)
    if kind == "keylock" and d.with_suffix(".tmp").exists():
        r["skipped"] = len(wavs)
        return r
    try:
        _clean_tmp(d)
    except OSError:
        pass
    done = {}
    for w in wavs:
        r["files"] += 1
        if not w.exists():                     # evicted / converted meanwhile
            r["skipped"] += 1
            continue
        try:
            sub = audio_io.RENDER_SUBTYPE if kind == "keylock" else None
            flac, a, b = convert_file(w, sub)
            done[w] = flac
            r["bytes_before"] += a
            r["bytes_after"] += b
        except Exception as exc:               # WAV kept; reported
            r["failed"].append({"file": str(w), "error": f"{type(exc).__name__}: {exc}"})
    if done:
        try:
            _finish_dir(kind, d, done)
        except Exception as exc:               # manifest not updated: keep every WAV
            r["failed"].append({"file": str(d), "error": f"manifest: {type(exc).__name__}: {exc}"})
            return r
        for w in done:
            w.unlink(missing_ok=True)
            r["converted"] += 1
    r["done"] = len(done)
    return r


def _crashed(kind: str, d: Path, wavs: List[Path], why: str) -> dict:
    """Called in the parent once the worker holding d is dead: clear the tmp files it may have left."""
    r = _result(kind, d)
    r["files"], r["wavs"] = len(wavs), len(wavs)
    try:
        _clean_tmp(d)
    except OSError:
        pass
    r["failed"].append({"file": str(d), "error": f"worker crashed: {why}"})
    return r


# ---- parallel ------------------------------------------------------------------------------------
# Peak RSS of one worker converting one folder, MEASURED with a synthetic 8-minute keylock set (4 stems,
# float WAV -> 24-bit FLAC, the heavier kind); see the flac-parallel commit message for the numbers.
PEAK_GB_PER_DIR = 1.6     # measured 1.57 GB peak RSS (0.55 GB of it is imports)
DEFAULT_MAX_MEM_GB = 6.4  # 4 workers at the measured peak
_stop = threading.Event()


def request_stop() -> None:
    """Stop handing out folders; running ones finish. What SIGINT / SIGTERM do during run()."""
    _stop.set()


def effective_jobs(jobs: Optional[int] = None, max_mem_gb: Optional[float] = None,
                   cpus: Optional[int] = None) -> int:
    """Default min(4, cpus // 2); hard cap cpus - 1; then at most max_mem_gb / PEAK_GB_PER_DIR; >= 1."""
    cpus = cpus or os.cpu_count() or 1
    j = min(4, cpus // 2) if jobs is None else jobs
    j = min(j, cpus - 1)
    mem = DEFAULT_MAX_MEM_GB if max_mem_gb is None else max_mem_gb
    return max(1, min(j, int(mem // PEAK_GB_PER_DIR)))


def _worker_init() -> None:
    # Ctrl-C reaches the whole process group: workers ignore it and finish their folder; the parent
    # decides to stop handing out work. SIGTERM stays default: a broken pool terminate()s its
    # survivors with it, and ignoring it hangs shutdown forever. A folder killed mid-way is still
    # safe (WAVs go only after the manifest moved; the next run finishes or cleans up).
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _pool_run(items: list, jobs: int, worker, on_result) -> Tuple[list, list]:
    """Run items in a fresh spawn pool, at most `jobs` in flight. Returns (suspects, not_started):
    suspects were in flight when a worker died, so any of them may be the one that crashed."""
    ctx = multiprocessing.get_context("spawn")
    queue, suspects, broken = deque(items), [], False
    with ProcessPoolExecutor(jobs, mp_context=ctx, initializer=_worker_init) as ex:
        running = {}
        while True:
            while queue and len(running) < jobs and not broken and not _stop.is_set():
                item = queue.popleft()
                try:
                    running[ex.submit(worker, *item)] = item
                except BrokenProcessPool:
                    queue.appendleft(item)
                    broken = True
            if not running:
                break
            fin, _ = wait(running, return_when=FIRST_COMPLETED)
            for f in fin:
                item = running.pop(f)
                try:
                    on_result(item, f.result())
                except BrokenProcessPool:
                    suspects.append(item)
                    broken = True
                except Exception as exc:        # e.g. unpicklable result; folder state is the worker's
                    on_result(item, _crashed(*item, f"{type(exc).__name__}: {exc}"))
    return suspects, list(queue)


def _run_parallel(todo: list, jobs: int, worker, on_result) -> list:
    """Returns the folders never started (stop requested)."""
    queue = list(todo)
    while queue and not _stop.is_set():
        suspects, queue = _pool_run(queue, jobs, worker, on_result)
        for s in suspects:                     # a dead worker takes its pool down: retry each alone
            if _stop.is_set():
                on_result(s, _crashed(*s, "pool broken, not retried (stop requested)"))
                continue
            again, _ = _pool_run([s], 1, worker, on_result)
            if again:
                on_result(s, _crashed(*s, "process died converting this folder"))
    return queue


def run(cache: Optional[Path] = None, apply: bool = False, only: Optional[str] = None,
        limit: Optional[int] = None, log=None, jobs: Optional[int] = None,
        max_mem_gb: Optional[float] = None, _worker=convert_dir) -> dict:
    cache = Path(cache) if cache else _default_cache()
    log = log or (lambda s: print(s, file=sys.stderr, flush=True))
    todo = plan(cache, only)
    if limit is not None:
        todo = todo[:limit]
    n_jobs = effective_jobs(jobs, max_mem_gb)
    summary = {"apply": apply, "dirs": len(todo), "files": 0, "converted": 0, "skipped": 0,
               "failed": [], "bytes_before": 0, "bytes_after": 0, "seconds": 0.0, "jobs": n_jobs,
               "interrupted": False, "not_started": 0}
    t0 = time.monotonic()
    if not apply:
        for kind, d, wavs in todo:
            for w in wavs:
                n = w.stat().st_size
                summary["files"] += 1
                summary["bytes_before"] += n
                summary["bytes_after"] += int(n * RATIO[_subtype_for(w)])
        summary["projected"] = True
        summary["saved_bytes"] = summary["bytes_before"] - summary["bytes_after"]
        return summary

    k = 0

    def on_result(item, r):
        nonlocal k
        k += 1
        for f in ("files", "converted", "skipped", "bytes_before", "bytes_after"):
            summary[f] += r[f]
        summary["failed"] += r["failed"]
        kind, d, wavs = item
        log(f"[{k}/{len(todo)}] {kind} {d.name}: {r['done']}/{len(wavs)} converted")

    def on_signal(signum, _frame):
        if not _stop.is_set():
            log(f"signal {signum}: no new folders, finishing the running ones")
        _stop.set()

    cache.mkdir(parents=True, exist_ok=True)
    with open(cache / LOCK_NAME, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("another audio_convert is running")
        _stop.clear()
        old = {}
        if threading.current_thread() is threading.main_thread():
            for s in (signal.SIGINT, signal.SIGTERM):
                old[s] = signal.signal(s, on_signal)
        try:
            if n_jobs == 1:                    # today's sequential path, in-process
                left = list(todo)
                while left and not _stop.is_set():
                    item = left.pop(0)
                    on_result(item, _worker(*item))
            else:
                left = _run_parallel(todo, n_jobs, _worker, on_result)
        finally:
            for s, h in old.items():
                signal.signal(s, h)
        summary["interrupted"] = _stop.is_set()
        summary["not_started"] = len(left)
        _stop.clear()
    summary["projected"] = False
    summary["saved_bytes"] = summary["bytes_before"] - summary["bytes_after"]
    summary["seconds"] = round(time.monotonic() - t0, 2)
    return summary


def _positive_int(s: str) -> int:
    n = int(s)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def _positive_float(s: str) -> float:
    x = float(s)
    if not x > 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return x


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="report files, bytes and projected saving (default)")
    g.add_argument("--apply", action="store_true", help="convert, verify, then delete each WAV")
    ap.add_argument("--only", choices=("stems", "keylock"))
    ap.add_argument("--limit", type=int, default=None, help="at most N cache dirs")
    ap.add_argument("--cache-dir", type=Path, default=None, help="default: data/cache (config.CACHE_DIR)")
    ap.add_argument("--jobs", type=_positive_int, default=None,
                    help="folders converted at once (default min(4, cpus // 2), capped at cpus - 1)")
    ap.add_argument("--max-mem-gb", type=_positive_float, default=None,
                    help=f"memory budget; jobs <= budget / {PEAK_GB_PER_DIR} GB per folder "
                         f"(default {DEFAULT_MAX_MEM_GB})")
    a = ap.parse_args(argv)
    try:
        res = run(a.cache_dir, apply=a.apply, only=a.only, limit=a.limit, jobs=a.jobs,
                  max_mem_gb=a.max_mem_gb)
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(res, indent=2))
    return 1 if res["failed"] or res["interrupted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
