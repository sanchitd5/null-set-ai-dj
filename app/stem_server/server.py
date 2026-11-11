"""Stem server: FastAPI, bearer-token auth, one GPU job at a time, AAC windows of cached stems.

Routes (all but /health need `Authorization: Bearer $STEM_SERVER_TOKEN`):
  GET  /health
  HEAD/GET /v1/tracks/{id}                 404 unknown; 200 {"id", "separated": bool}
  POST /v1/tracks?id=<id>&ext=<ext>        raw audio body; sha256(body)[:16] must equal id
  POST /v1/separate {"id", "stems": 2|4}   -> {"job", "status", ...}
  GET  /v1/jobs/{job}                      -> {"job", "id", "status", "progress", "error", "device"}
  GET  /v1/tracks/{id}/stems/{name}.m4a?start=S&end=E   AAC of that window + X-Stem-* timing headers
  GET  /v1/tracks/{id}/stems/{name}.m4a                 the WHOLE stem, encoded once and cached; HTTP Range (206)
                                                        resumes; the first request encodes all four stems at once

Track files and stems share the console's cache (data/cache/uploads, data/cache/stems): a song the
console already split is served without a second split, and a song split here is the console's too.
"""

from __future__ import annotations

import hashlib
import heapq
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.music_brain import song_store
from app.music_brain.audio import stem_service
from app.music_brain.config import CACHE_DIR, DEMUCS_MODEL
from app.stem_server import windows
from app.stem_server.demucs_runner import OOM_EXIT
from app.ui.services.auth import AuthStore, FailureLimiter, client_ip, default_store_path   # stdlib-only module: no console import chain

ID_RE = re.compile(r"^[0-9a-f]{16}$")
AUDIO_EXTS = {"mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "aiff", "aif", "webm"}
FOUR = list(stem_service.FOUR_STEM_NAMES)
TWO = {"vocals": ["vocals"], "no_vocals": ["drums", "bass", "other"]}
MIN_TOKEN_LEN = 32
# Same numbers as the console (AuthSettings): 10 failures in 10 min -> that IP locked for 15 min.
# Trusted proxy = Caddy on this host only.
LIMITS = SimpleNamespace(trusted_proxies=("127.0.0.1", "::1"), max_failures=10, failure_window_s=600, lockout_s=900)


class Settings:
    def __init__(self, env: Optional[dict] = None):
        env = os.environ if env is None else env
        self.token = env.get("STEM_SERVER_TOKEN", "")
        if len(self.token) < MIN_TOKEN_LEN:
            raise RuntimeError(f"STEM_SERVER_TOKEN must be set (at least {MIN_TOKEN_LEN} characters); refusing to start")
        self.model = env.get("STEM_SERVER_MODEL", DEMUCS_MODEL)
        self.max_upload = int(float(env.get("STEM_SERVER_MAX_UPLOAD_MB", "200")) * (1 << 20))
        cache = Path(env.get("AIDJ_CACHE_DIR") or CACHE_DIR).expanduser().resolve()
        self.cache = cache
        self.songs = cache / "songs"                    # the console's song folders (song_store): audio + stems per song
        self.uploads = cache / "uploads"                # old flat track store, read-only fallback (G26: TODO 2026-12-15 remove)
        self.stems = cache / "stems"                    # old flat stem cache, read-only fallback
        self.windows = cache / "stem_windows"           # encoded AAC windows (derived, safe to delete)
        # The console's library tracks live outside uploads/: it links them here as <id>.<ext> (a symlink
        # to the real file). The sha256 check on every first use still binds the id to the bytes.
        self.links = cache / "stem_links"
        self.state = Path(env.get("STEM_SERVER_STATE") or cache / "stem_server_jobs.json")   # queue across restarts
        self.device = env.get("STEM_SERVER_DEVICE") or None   # None = cuda -> mps -> cpu
        self.rss_limit = int(float(env.get("STEM_JOB_RSS_GB", "6")) * (1 << 30))
        self.scope_mem = env.get("STEM_JOB_MEMORY_MAX") or None   # e.g. "7G": systemd scope for the worker
        # fp16 autocast on CUDA: ~1.3x faster, stems within 56-65 dB SDR of fp32 (STEMS_PERF.md)
        self.half = env.get("STEM_SERVER_HALF", "1") == "1"
        # whole-song m4a cache (derived; ~6 MB per stem-minute x4): oldest go first above this
        self.whole_cap = int(float(env.get("STEM_WHOLE_CACHE_GB", "6")) * (1 << 30))
        # The console's account store (same cache): its per-device tokens work here too, so a phone
        # that signed in with Google once uses one token for both. Re-read when it changes on disk.
        self.auth_store = AuthStore(default_store_path(env))


# Queue order: a deck or a booked transition first, then a phone booking, then the library backfill.
PRIORITIES = {"deck": 0, "booked": 0, "phone": 1, "backfill": 2}


class Job:
    def __init__(self, track_id: str, path: Path, stems: int, priority: str = "phone", job: Optional[str] = None):
        self.job = job or uuid.uuid4().hex[:12]
        self.id, self.path, self.stems = track_id, path, stems
        self.priority = priority
        self.status, self.progress, self.error, self.device, self.note = "queued", 0.0, None, None, None
        self.model: Optional[str] = None
        self.created, self.started, self.finished = time.time(), None, None
        self.seq = 0                       # queue order inside one priority (FIFO)
        self.position: Optional[int] = None
        self.info: dict = {}

    @property
    def prio(self) -> int:
        return PRIORITIES.get(self.priority, 1)

    def eta(self) -> Optional[float]:
        if self.status != "running" or not self.started or self.progress < 0.03:
            return None
        el = time.time() - self.started
        return round(el / self.progress * (1 - self.progress), 1)

    def to_dict(self) -> dict:
        return {"job": self.job, "id": self.id, "stems": self.stems, "status": self.status,
                "priority": self.priority, "position": self.position if self.status == "queued" else None,
                "progress": round(self.progress, 3), "error": self.error, "device": self.device,
                "model": self.model, "note": self.note, "eta": self.eta(),
                "elapsed": round((self.finished or time.time()) - (self.started or self.created), 1),
                "seconds": self.info.get("seconds"),
                # GPU decisions of the finished split: kind gpu|cpu, free_mb / total_mb at start, wait_s,
                # segment_s, batch (null until the job is done, or for a stem set that was already cached)
                "gpu": self.info.get("gpu")}


def separate_once(path: Path, model: str, device: Optional[str], stems_dir: Path) -> tuple[Dict[str, str], str, Optional[str]]:
    """Full 4-stem split through the shared stem_service (one cache with the console).
    CUDA failure (OOM or other) -> one retry on the CPU. -> (stems, device used, note)."""
    device = device or stem_service._detect_device()
    runner = "app.stem_server.demucs_runner"
    try:
        r = stem_service.separate(path, model=model, device=device, runner=runner)
        return r.stems, device, None
    except subprocess.CalledProcessError as exc:
        if device != "cuda":
            raise
        why = "CUDA out of memory (GPU shared / STEM_SERVER_VRAM_GB)" if exc.returncode == OOM_EXIT \
            else f"CUDA split failed (exit {exc.returncode})"
        print(f"[stem-server] {why}; retrying on CPU", file=sys.stderr, flush=True)
        r = stem_service.separate(path, model=model, device="cpu", runner=runner, force=True)
        return r.stems, "cpu", f"{why}; split on CPU"


class StemServer:
    """separate_fn None: the streaming engine in the long-lived GPU worker (app/stem_server/gpu.py).
    A function (path, model, device, stems_dir) -> (stems, device, note) replaces it (tests, fallback)."""

    def __init__(self, settings: Settings, separate_fn=None):
        self.s = settings
        self.separate_fn = separate_fn
        self.jobs: Dict[str, Job] = {}
        self.heap: list = []                  # (prio, seq, job id); stale entries skipped on pop
        self.cv = threading.Condition()
        self._seq = 0
        self.lock = threading.RLock()         # jobs dict + hash memo + state file
        self.encode_lock = threading.Lock()   # window encode + integrity repair
        self._set_locks: Dict[str, threading.Lock] = {}   # whole-song encode, one per stem set
        self._hash: Dict[str, tuple] = {}     # track id -> (mtime, size, full sha256)
        self.gpu = None
        self.last_gpu: Optional[dict] = None  # the GPU decisions of the last split (also on each job)
        self.current: Optional[Job] = None
        self._restoring = False
        for d in (self.s.songs / "_incoming", self.s.windows):
            d.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._worker, daemon=True, name="stem-gpu").start()

    # ---- tracks ---------------------------------------------------------------------------------
    def stem_dir(self, h: str, model: str) -> Path:
        """Where a song's stem set lives: its folder songs/<id>/stems/<model>."""
        return self.s.songs / h[:16] / "stems" / model

    def stem_manifest(self, h: str, model: str) -> Optional[Dict[str, str]]:
        """The set in the song folder, else in the old flat cache."""
        return (stem_service.read_manifest(self.stem_dir(h, model))
                or stem_service.read_manifest(self.s.stems / f"{h}_{model}"))

    def track_path(self, track_id: str) -> Optional[Path]:
        if re.fullmatch(r"[0-9a-f]{16}", track_id):
            p = song_store._folder_audio(str(self.s.songs / track_id))
            if p is not None:
                return p
        for d in (self.s.uploads, self.s.links):
            for p in sorted(d.glob(f"{track_id}.*")):
                if p.is_file() and p.suffix.lstrip(".").lower() in AUDIO_EXTS:
                    return p
        return None

    def full_hash(self, track_id: str, p: Path) -> str:
        st = p.stat()
        with self.lock:
            memo = self._hash.get(track_id)
        if memo and memo[:2] == (st.st_mtime, st.st_size):
            return memo[2]
        h = stem_service.file_hash(p)
        if h[:16] != track_id:
            raise HTTPException(500, f"stored track {track_id} fails its sha256 (corrupt); upload it again")
        with self.lock:
            self._hash[track_id] = (st.st_mtime, st.st_size, h)
        return h

    def models(self) -> List[str]:
        """Stem sets this server serves, best first: its own model, then the console's fast
        backfill model (stem_service.cached_four_stems order), so a song the console already
        split is never split again here."""
        return list(dict.fromkeys([self.s.model, stem_service.DEMUCS_MODEL, stem_service.FAST_MODEL]))

    def ready_set(self, track_id: str, p: Path) -> Optional[tuple[Dict[str, str], Path, str]]:
        """(stems, set folder, model) of the best complete 4-stem set in the shared cache."""
        h = self.full_hash(track_id, p)
        for m in self.models():
            st = self.stem_manifest(h, m)
            if st and all(st.get(n) and os.path.exists(st[n]) for n in FOUR):
                return st, Path(next(iter(st.values()))).parent, m
        return None

    def ready_stems(self, track_id: str, p: Path) -> Optional[Dict[str, str]]:
        r = self.ready_set(track_id, p)
        return r[0] if r else None

    def own_ready(self, track_id: str, p: Path) -> bool:
        """The song has a complete set of THIS server's model (htdemucs_ft): nothing left to split.
        A cheaper set (the console's old htdemucs backfill) is still served meanwhile (ready_set)."""
        st = self.stem_manifest(self.full_hash(track_id, p), self.s.model)
        return bool(st and all(st.get(n) and os.path.exists(st[n]) for n in FOUR))

    # ---- jobs -----------------------------------------------------------------------------------
    def submit(self, track_id: str, stems: int, priority: str = "phone", job_id: Optional[str] = None) -> Job:
        """Queue a split, one job per song: a second request for a song already queued or running
        returns that job (raising its priority when the new one ranks higher)."""
        if priority not in PRIORITIES:
            raise HTTPException(400, f"priority must be one of {sorted(PRIORITIES)}")
        p = self.track_path(track_id)
        if p is None:
            raise HTTPException(404, "unknown track; upload it first")
        with self.cv:
            for j in self.jobs.values():
                if j.id == track_id and j.status in ("queued", "running"):
                    if j.status == "queued" and PRIORITIES[priority] < j.prio:
                        j.priority = priority
                        self._push(j)
                        self._save()
                    return j
        j = Job(track_id, p, stems, priority, job_id)
        if self.own_ready(track_id, p):
            j.status, j.progress, j.finished, j.model = "done", 1.0, time.time(), self.s.model
        with self.cv:
            self.jobs[j.job] = j
            self._trim_jobs()
            if j.status == "queued":
                self._push(j)
                self._save()
        return j

    def _push(self, j: Job) -> None:
        """Caller holds self.cv."""
        self._seq += 1
        j.seq = self._seq
        heapq.heappush(self.heap, (j.prio, j.seq, j.job))
        self._positions()
        self.cv.notify_all()

    def _positions(self) -> None:
        q = sorted((j for j in self.jobs.values() if j.status == "queued"), key=lambda j: (j.prio, j.seq))
        for i, j in enumerate(q):
            j.position = i + 1

    def _pop(self) -> Job:
        with self.cv:
            while True:
                while self.heap:
                    prio, seq, jid = heapq.heappop(self.heap)
                    j = self.jobs.get(jid)
                    if j is not None and j.status == "queued" and (j.prio, j.seq) == (prio, seq):
                        j.status, j.started, j.position = "running", time.time(), None
                        self.current = j
                        self._positions()
                        self._save()
                        return j
                self.cv.wait()

    def active(self) -> List[Job]:
        with self.cv:
            return sorted((j for j in self.jobs.values() if j.status in ("queued", "running")),
                          key=lambda j: (j.status != "running", j.prio, j.seq))

    def _trim_jobs(self, keep: int = 500) -> None:
        if len(self.jobs) > keep:
            for k in sorted(self.jobs, key=lambda k: self.jobs[k].created)[: len(self.jobs) - keep]:
                if self.jobs[k].status in ("done", "error"):
                    del self.jobs[k]

    # ---- crash safety: the queue survives a restart ---------------------------------------------
    def _save(self) -> None:
        """Queued + running jobs to the state file (tmp + replace). Caller holds self.cv."""
        if self._restoring:
            return                            # the file still lists what is not re-queued yet
        rows = [{"job": j.job, "id": j.id, "stems": j.stems, "priority": j.priority, "created": j.created}
                for j in sorted(self.jobs.values(), key=lambda j: (j.status != "running", j.prio, j.seq))
                if j.status in ("queued", "running")]
        tmp = self.s.state.with_name(f".{self.s.state.name}.tmp{os.getpid()}")
        try:
            self.s.state.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"version": 1, "jobs": rows}), encoding="utf-8")
            os.replace(tmp, self.s.state)
        except OSError as exc:
            print(f"[stem-server] queue state not saved: {exc}", file=sys.stderr, flush=True)
        finally:
            tmp.unlink(missing_ok=True)

    def _restore(self) -> None:
        """Re-queue what was queued or running when the server stopped (a running one starts over)."""
        try:
            rows = json.loads(self.s.state.read_text(encoding="utf-8")).get("jobs") or []
        except (OSError, ValueError, AttributeError):
            return
        n = 0
        self._restoring = True
        for r in rows:
            try:
                if not ID_RE.match(str(r.get("id"))):
                    continue
                self.submit(r["id"], int(r.get("stems") or 4), r.get("priority") or "phone", r.get("job"))
                n += 1
            except Exception as exc:  # noqa: BLE001 -- a track that went away: drop its job
                print(f"[stem-server] not re-queued {r.get('id')}: {exc}", file=sys.stderr, flush=True)
        with self.cv:
            self._restoring = False
            self._save()
        if n:
            print(f"[stem-server] re-queued {n} job(s) from {self.s.state.name}", file=sys.stderr, flush=True)

    # ---- the GPU job loop ----------------------------------------------------------------------
    def _worker(self) -> None:
        self._restore()                       # here, not in __init__: hashing the queued tracks takes a while
        while True:                           # one GPU job at a time
            j = self._pop()
            try:
                if not self.own_ready(j.id, j.path):
                    if self.separate_fn is not None:
                        _, j.device, j.note = self.separate_fn(j.path, self.s.model, self.s.device, self.s.stems)
                    else:
                        self._split(j)
                if not self.own_ready(j.id, j.path):
                    raise RuntimeError("separation finished without a complete 4-stem set")
                j.status, j.progress, j.model = "done", 1.0, self.s.model
                if j.priority != "backfill":      # a phone/deck wants it: encode now, off the GPU thread
                    threading.Thread(target=self.pre_encode, args=(j.id,), daemon=True, name="stem-encode").start()
            except Exception as exc:  # noqa: BLE001 -- reported through the job, the worker lives on
                j.status, j.error = "error", f"{type(exc).__name__}: {exc}"[:300]
                print(f"[stem-server] job {j.job} {j.id} failed: {j.error}", file=sys.stderr, flush=True)
            finally:
                j.finished = time.time()
                with self.cv:
                    self.current = None
                    self._save()

    def _split(self, j: Job) -> None:
        """Streaming engine in the GPU worker; stems verified (every stem as long as the decoded mix)
        before the manifest, the cache-hit marker, is written; then the song's non-ft sets are pruned."""
        import soundfile as sf

        from app.stem_server import gpu

        if self.gpu is None:
            self.gpu = gpu.GpuWorker(self.s.rss_limit, self.s.scope_mem)
        h = self.full_hash(j.id, j.path)
        out = self.stem_dir(h, self.s.model)
        j.model = self.s.model

        def progress(p: float, device: Optional[str] = None) -> None:
            j.progress = max(0.0, min(1.0, p))
            if device:
                j.device = device

        # device None: the worker picks cuda -> mps -> cpu (torch is never imported in this process)
        info, _, j.note = gpu.split(self.gpu, j.job, j.path, out, self.s.model, self.s.device, progress, self.s.half)
        j.info, j.device = info, info.get("device")
        self.last_gpu = info.get("gpu")
        stems = info["stems"]
        if sorted(stems) != sorted(FOUR):
            raise RuntimeError(f"split returned {sorted(stems)}")
        for name, path in stems.items():
            frames = sf.info(path).frames
            if frames != info["frames"]:
                raise RuntimeError(f"{name}.flac has {frames} frames, the mix {info['frames']}")
        stem_service.write_manifest(out, stems)
        if self.s.model == stem_service.DEMUCS_MODEL:
            stem_service._auto_prune(h, song_store.stems_root_for(self.s.cache))
        print(f"[stem-server] {j.id} {self.s.model} on {j.device}: {info['frames'] / 44100:.0f} s of audio in "
              f"{info['seconds']} s (batch {info.get('batch')}, peak RSS {info.get('peak_rss_mb')} MB"
              f"{', ' + j.note if j.note else ''})", file=sys.stderr, flush=True)

    # ---- windows --------------------------------------------------------------------------------
    def window(self, track_id: str, name: str, start: float, end: float) -> tuple[Path, dict]:
        p = self.track_path(track_id)
        if p is None:
            raise HTTPException(404, "unknown track")
        parts = [name] if name in FOUR else TWO.get(name)
        if parts is None:
            raise HTTPException(404, f"unknown stem {name}")
        if not (end > start >= 0) or end - start > windows.MAX_WINDOW_S:
            raise HTTPException(400, f"need 0 <= start < end, window <= {windows.MAX_WINDOW_S:.0f} s")
        ready = self.ready_set(track_id, p)
        if ready is None:
            raise HTTPException(409, "not separated yet; POST /v1/separate")
        stems, cdir, model = ready
        total = windows.total_frames(stems[parts[0]])
        s0 = min(round(start * windows.SAMPLE_RATE), total)
        s1 = min(round(end * windows.SAMPLE_RATE), total)
        if s1 <= s0:
            raise HTTPException(416, "window is past the end of the track")
        dest = self.s.windows / cdir.name / f"{name}_{s0}_{s1}.m4a"
        with self.encode_lock:
            meta = windows.cached_window(dest) if dest.exists() else None
            if meta is None:
                bad = windows.verify_stems(cdir, stems, parts)
                if bad:
                    # corrupt cache: drop the set (and its windows) and split again
                    print(f"[stem-server] {track_id}: {bad}; deleting stems and re-splitting", file=sys.stderr, flush=True)
                    shutil.rmtree(cdir, ignore_errors=True)
                    shutil.rmtree(dest.parent, ignore_errors=True)
                    self.submit(track_id, 4)
                    raise HTTPException(409, f"stem cache failed its check ({bad}); re-splitting, retry later")
                meta = windows.encode_m4a(windows.read_window([stems[n] for n in parts], s0, s1), dest)
                meta.update(start_frame=s0, total_frames=total, model=model)
                windows.store_window_meta(dest, meta)
        return dest, meta

    # ---- whole-song stems: encoded once, served with HTTP Range -----------------------------------
    def whole(self, track_id: str, name: str) -> tuple[Path, dict]:
        """The whole stem as ONE m4a (cached on disk). The first request encodes all four stems
        in parallel (ffmpeg streams, ~1 s of CPU per minute of audio each), so the phone's four
        downloads wait for one encode, not four in a row. Same sidecar/meta contract as windows."""
        p = self.track_path(track_id)
        if p is None:
            raise HTTPException(404, "unknown track")
        if name not in FOUR:
            raise HTTPException(404, f"unknown stem {name}")
        ready = self.ready_set(track_id, p)
        if ready is None:
            raise HTTPException(409, "not separated yet; POST /v1/separate")
        stems, cdir, model = ready
        dest = self.s.windows / cdir.name / f"{name}_whole.m4a"
        meta = windows.cached_window(dest) if dest.exists() else None
        if meta is not None:
            os.utime(dest, None)                  # LRU for the prune
            return dest, meta
        with self._set_lock(cdir.name):
            meta = windows.cached_window(dest) if dest.exists() else None
            if meta is None:
                bad = windows.verify_stems(cdir, stems, FOUR)
                if bad:
                    print(f"[stem-server] {track_id}: {bad}; deleting stems and re-splitting", file=sys.stderr, flush=True)
                    shutil.rmtree(cdir, ignore_errors=True)
                    shutil.rmtree(dest.parent, ignore_errors=True)
                    self.submit(track_id, 4)
                    raise HTTPException(409, f"stem cache failed its check ({bad}); re-splitting, retry later")
                self._encode_all(stems, cdir.name, model)
                meta = windows.cached_window(dest) if dest.exists() else None
                if meta is None:
                    raise HTTPException(500, "whole-song encode failed")
        self._prune_whole()
        return dest, meta

    def _set_lock(self, key: str) -> threading.Lock:
        with self.lock:
            return self._set_locks.setdefault(key, threading.Lock())

    def _encode_all(self, stems: Dict[str, str], key: str, model: str) -> None:
        """Encode the four whole stems of one set at once (missing ones only). Caller holds the set lock."""
        from concurrent.futures import ThreadPoolExecutor

        def one(n: str) -> None:
            dest = self.s.windows / key / f"{n}_whole.m4a"
            if dest.exists() and windows.cached_window(dest) is not None:
                return
            total = windows.total_frames(stems[n])
            t0 = time.time()
            meta = windows.encode_whole_m4a(stems[n], dest)
            meta.update(start_frame=0, total_frames=total, model=model)
            windows.store_window_meta(dest, meta)
            print(f"[stem-server] {key[:16]} {n}: whole m4a {dest.stat().st_size >> 10} KB in {time.time() - t0:.1f} s",
                  file=sys.stderr, flush=True)

        with ThreadPoolExecutor(max_workers=len(FOUR)) as ex:
            for f in [ex.submit(one, n) for n in FOUR]:
                f.result()

    def pre_encode(self, track_id: str) -> None:
        """Encode a freshly split song's whole stems in the background, so the phone's first request
        is a file read. Never raises."""
        try:
            self.whole(track_id, FOUR[0])
        except Exception as exc:  # noqa: BLE001 -- best effort; the first request encodes instead
            print(f"[stem-server] pre-encode {track_id}: {exc}", file=sys.stderr, flush=True)

    def _prune_whole(self) -> None:
        """Whole-song m4a files are derived: keep the newest ones under STEM_WHOLE_CACHE_GB."""
        try:
            files = [(f.stat().st_mtime, f.stat().st_size, f) for f in self.s.windows.glob("*/*_whole.m4a")]
        except OSError:
            return
        total = sum(sz for _, sz, _ in files)
        for _, sz, f in sorted(files):
            if total <= self.s.whole_cap:
                break
            f.unlink(missing_ok=True)
            f.with_suffix(".json").unlink(missing_ok=True)
            total -= sz


def _range(header: Optional[str], size: int) -> Optional[tuple[int, int]]:
    """'bytes=a-b' | 'bytes=a-' | 'bytes=-n' -> (first, last) inclusive; None = no/ignored Range header.
    Raises 416 for a range outside the file."""
    if not header or not header.lower().startswith("bytes=") or "," in header:
        return None
    a, _, b = header[6:].strip().partition("-")
    try:
        if a == "":
            n = int(b)
            first, last = max(0, size - n), size - 1
        else:
            first, last = int(a), (int(b) if b else size - 1)
    except ValueError:
        return None
    last = min(last, size - 1)
    if first > last or first >= size:
        raise HTTPException(416, "range not satisfiable", headers={"Content-Range": f"bytes */{size}"})
    return first, last


def _file_slice(path: Path, first: int, last: int, chunk: int = 1 << 20):
    with open(path, "rb") as f:
        f.seek(first)
        left = last - first + 1
        while left > 0:
            b = f.read(min(chunk, left))
            if not b:
                break
            left -= len(b)
            yield b


class SeparateReq(BaseModel):
    id: str
    stems: int = 4
    priority: str = "phone"     # deck | booked | phone | backfill; above phone only with STEM_SERVER_TOKEN


def create_app(settings: Optional[Settings] = None, separate_fn=None) -> FastAPI:
    s = settings or Settings()
    srv = StemServer(s, separate_fn)
    app = FastAPI(title="null-set stem server", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.server = srv
    want = f"Bearer {s.token}".encode()
    limiter = app.state.limiter = FailureLimiter()

    def auth(request: Request) -> str:
        """-> "server" for STEM_SERVER_TOKEN (the console on this host), "device" for an account token."""
        # per-IP failed-auth limiter (the console's): X-Forwarded-For honoured only from Caddy on loopback
        key = "ip:" + client_ip(LIMITS, request.scope, {k.lower(): v for k, v in request.headers.items()})
        wait = limiter.retry_after(key)
        if wait:
            raise HTTPException(429, "too many failed attempts; retry later", headers={"Retry-After": str(wait)})
        got = request.headers.get("authorization", "")
        if hmac.compare_digest(got.encode(), want):
            return "server"                                # STEM_SERVER_TOKEN (static)
        scheme, _, cred = got.partition(" ")
        if not (scheme.lower() == "bearer" and cred.strip() and s.auth_store.check_token(cred.strip())):
            limiter.fail(key, LIMITS)
            raise HTTPException(401, "bad or missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        return "device"

    def check_id(track_id: str) -> str:
        if not ID_RE.match(track_id):
            raise HTTPException(400, "track id must be 16 lowercase hex chars (sha256(bytes)[:16])")
        return track_id

    @app.exception_handler(HTTPException)
    async def _err(_req, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.api_route("/v1/tracks/{track_id}", methods=["GET", "HEAD"], dependencies=[Depends(auth)])
    def track(track_id: str):
        p = srv.track_path(check_id(track_id))
        if p is None:
            raise HTTPException(404, "unknown track")
        ready = srv.ready_set(track_id, p)
        return {"id": track_id, "separated": ready is not None, "model": ready[2] if ready else s.model}

    @app.post("/v1/tracks", dependencies=[Depends(auth)])
    async def upload(request: Request, id: str, ext: str = "mp3"):
        check_id(id)
        ext = ext.lower().lstrip(".")
        if ext not in AUDIO_EXTS:
            raise HTTPException(400, f"ext must be one of {sorted(AUDIO_EXTS)}")
        if int(request.headers.get("content-length") or 0) > s.max_upload:
            raise HTTPException(413, f"upload over {s.max_upload >> 20} MB")
        existing = srv.track_path(id)
        if existing is not None:
            return {"id": id, "stored": False}
        tmp = s.songs / "_incoming" / f"_up_{uuid.uuid4().hex}.{ext}"   # '_incoming': never listed as a song
        tmp.parent.mkdir(parents=True, exist_ok=True)
        h, n = hashlib.sha256(), 0
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    n += len(chunk)
                    if n > s.max_upload:
                        raise HTTPException(413, f"upload over {s.max_upload >> 20} MB")
                    h.update(chunk)
                    f.write(chunk)
            if n == 0:
                raise HTTPException(400, "empty body")
            if h.hexdigest()[:16] != id:
                raise HTTPException(422, "sha256 of the body does not match id")
            dest = s.songs / id / f"audio.{ext}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        return {"id": id, "stored": True}

    @app.post("/v1/separate")
    def separate(req: SeparateReq, who: str = Depends(auth)):
        check_id(req.id)
        if req.stems not in (2, 4):
            raise HTTPException(400, "stems must be 2 or 4")
        if req.priority not in PRIORITIES:
            raise HTTPException(400, f"priority must be one of {sorted(PRIORITIES)}")
        prio = req.priority
        if who != "server" and PRIORITIES[prio] < PRIORITIES["phone"]:
            prio = "phone"                                 # a deck / booked slot is the console's to claim
        return srv.submit(req.id, req.stems, prio).to_dict()

    @app.get("/v1/jobs", dependencies=[Depends(auth)])
    def jobs():
        """The queue in run order (running first) and the GPU worker."""
        act = srv.active()
        w = srv.gpu
        return {"jobs": [j.to_dict() for j in act], "queued": sum(j.status == "queued" for j in act),
                "model": s.model, "device": s.device or "auto", "gpu": srv.last_gpu,
                "worker": {"alive": bool(w and w.alive), "pid": w.pid if w else None}}

    @app.get("/v1/jobs/{job}", dependencies=[Depends(auth)])
    def job(job: str):
        j = srv.jobs.get(job)
        if j is None:
            raise HTTPException(404, "unknown job")
        return j.to_dict()

    analysis_lock = threading.Lock()   # one analysis at a time (CPU; the GPU queue is separate)

    @app.get("/v1/tracks/{track_id}/analysis", dependencies=[Depends(auth)])
    def analysis(track_id: str):
        """The console's analysis v6 of an uploaded track (same function and disk cache as
        GET /api/tracks/{id}/analysis): BPM, beatgrid, key, phrases, sections, drops, energy, vocals."""
        p = srv.track_path(check_id(track_id))
        if p is None:
            raise HTTPException(404, "unknown track; upload it first")
        srv.full_hash(track_id, p)                      # a corrupt stored file is refused, not analysed
        from app.music_brain.analysis.analyzer import analyze

        with analysis_lock:
            return analyze(p).to_dict()

    @app.get("/v1/tracks/{track_id}/stems/{name}.m4a", dependencies=[Depends(auth)])
    def stem_window(request: Request, track_id: str, name: str, start: Optional[float] = None, end: Optional[float] = None):
        """start+end: AAC of that window. Neither: the WHOLE stem as one cached m4a, with HTTP Range
        (Accept-Ranges: bytes, 206) so an interrupted download resumes where it stopped."""
        if (start is None) != (end is None):
            raise HTTPException(400, "give both start and end, or neither (whole song)")
        whole = start is None
        path, meta = srv.whole(check_id(track_id), name) if whole else srv.window(check_id(track_id), name, start, end)
        headers = {
            "X-Stem-Sample-Rate": str(meta["sample_rate"]),
            "X-Stem-Start-Frame": str(meta["start_frame"]),
            "X-Stem-Frames": str(meta["frames"]),
            "X-Stem-Total-Frames": str(meta["total_frames"]),
            "X-Stem-Encoder-Delay": str(meta["encoder_delay"]),
            "X-Stem-Encoder-Padding": str(meta["encoder_padding"]),
            "X-Stem-Model": str(meta["model"]),
            "X-Stem-Sha256": meta["sha256"],
            "Cache-Control": "private, max-age=31536000, immutable",
        }
        if not whole:
            return FileResponse(path, media_type="audio/mp4", headers=headers)
        size = path.stat().st_size
        headers["Accept-Ranges"] = "bytes"
        rng = _range(request.headers.get("range"), size)
        if rng is None:
            headers["Content-Length"] = str(size)
            return StreamingResponse(_file_slice(path, 0, size - 1), media_type="audio/mp4", headers=headers)
        first, last = rng
        headers.update({"Content-Range": f"bytes {first}-{last}/{size}", "Content-Length": str(last - first + 1)})
        return StreamingResponse(_file_slice(path, first, last), status_code=206, media_type="audio/mp4", headers=headers)

    return app


def _make_app() -> FastAPI:   # uvicorn --factory app.stem_server.server:_make_app
    return create_app()
