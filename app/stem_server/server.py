"""Stem server: FastAPI, bearer-token auth, one GPU job at a time, AAC windows of cached stems.

Routes (all but /health need `Authorization: Bearer $STEM_SERVER_TOKEN`):
  GET  /health
  HEAD/GET /v1/tracks/{id}                 404 unknown; 200 {"id", "separated": bool}
  POST /v1/tracks?id=<id>&ext=<ext>        raw audio body; sha256(body)[:16] must equal id
  POST /v1/separate {"id", "stems": 2|4}   -> {"job", "status", ...}
  GET  /v1/jobs/{job}                      -> {"job", "id", "status", "progress", "error", "device"}
  GET  /v1/tracks/{id}/stems/{name}.m4a?start=S&end=E   AAC of that window + X-Stem-* timing headers

Track files and stems share the console's cache (data/cache/uploads, data/cache/stems): a song the
console already split is served without a second split, and a song split here is the console's too.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import queue
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
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

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
        self.uploads = cache / "uploads"                # the console's track store (server.py UPLOAD_DIR)
        self.stems = cache / "stems"                    # the console's stem cache (config.STEMS_CACHE_DIR)
        self.windows = cache / "stem_windows"           # encoded AAC windows (derived, safe to delete)
        self.device = env.get("STEM_SERVER_DEVICE") or None   # None = cuda -> mps -> cpu
        # The console's account store (same cache): its per-device tokens work here too, so a phone
        # that signed in with Google once uses one token for both. Re-read when it changes on disk.
        self.auth_store = AuthStore(default_store_path(env))


class Job:
    def __init__(self, track_id: str, path: Path, stems: int):
        self.job = uuid.uuid4().hex[:12]
        self.id, self.path, self.stems = track_id, path, stems
        self.status, self.progress, self.error, self.device, self.note = "queued", 0.0, None, None, None
        self.created, self.started, self.finished = time.time(), None, None

    def to_dict(self) -> dict:
        return {"job": self.job, "id": self.id, "stems": self.stems, "status": self.status,
                "progress": self.progress, "error": self.error, "device": self.device, "note": self.note,
                "elapsed": round((self.finished or time.time()) - (self.started or self.created), 1)}


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
    def __init__(self, settings: Settings, separate_fn=separate_once):
        self.s = settings
        self.separate_fn = separate_fn
        self.jobs: Dict[str, Job] = {}
        self.q: "queue.Queue[Job]" = queue.Queue()
        self.lock = threading.Lock()          # jobs dict + hash memo
        self.encode_lock = threading.Lock()   # window encode + integrity repair
        self._hash: Dict[str, tuple] = {}     # track id -> (mtime, size, full sha256)
        for d in (self.s.uploads, self.s.windows):
            d.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._worker, daemon=True, name="stem-gpu").start()

    # ---- tracks ---------------------------------------------------------------------------------
    def track_path(self, track_id: str) -> Optional[Path]:
        for p in sorted(self.s.uploads.glob(f"{track_id}.*")):
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
            d = self.s.stems / f"{h}_{m}"
            st = stem_service.read_manifest(d)
            if st and all(st.get(n) and os.path.exists(st[n]) for n in FOUR):
                return st, d, m
        return None

    def ready_stems(self, track_id: str, p: Path) -> Optional[Dict[str, str]]:
        r = self.ready_set(track_id, p)
        return r[0] if r else None

    # ---- jobs -----------------------------------------------------------------------------------
    def submit(self, track_id: str, stems: int) -> Job:
        p = self.track_path(track_id)
        if p is None:
            raise HTTPException(404, "unknown track; upload it first")
        with self.lock:
            for j in self.jobs.values():
                if j.id == track_id and j.status in ("queued", "running"):
                    return j
        j = Job(track_id, p, stems)
        if self.ready_stems(track_id, p):
            j.status, j.progress, j.finished = "done", 1.0, time.time()
        with self.lock:
            self.jobs[j.job] = j
            self._trim_jobs()
        if j.status == "queued":
            self.q.put(j)
        return j

    def _trim_jobs(self, keep: int = 500) -> None:
        if len(self.jobs) > keep:
            for k in sorted(self.jobs, key=lambda k: self.jobs[k].created)[: len(self.jobs) - keep]:
                if self.jobs[k].status in ("done", "error"):
                    del self.jobs[k]

    def _worker(self) -> None:
        while True:                           # one GPU job at a time
            j = self.q.get()
            j.status, j.started = "running", time.time()
            try:
                if not self.ready_stems(j.id, j.path):
                    _, j.device, j.note = self.separate_fn(j.path, self.s.model, self.s.device, self.s.stems)
                if not self.ready_stems(j.id, j.path):
                    raise RuntimeError("separation finished without a complete 4-stem set")
                j.status, j.progress = "done", 1.0
            except Exception as exc:  # noqa: BLE001 -- reported through the job, the worker lives on
                j.status, j.error = "error", f"{type(exc).__name__}: {exc}"[:300]
                print(f"[stem-server] job {j.job} {j.id} failed: {j.error}", file=sys.stderr, flush=True)
            finally:
                j.finished = time.time()

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


class SeparateReq(BaseModel):
    id: str
    stems: int = 4


def create_app(settings: Optional[Settings] = None, separate_fn=separate_once) -> FastAPI:
    s = settings or Settings()
    srv = StemServer(s, separate_fn)
    app = FastAPI(title="null-set stem server", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.server = srv
    want = f"Bearer {s.token}".encode()
    limiter = app.state.limiter = FailureLimiter()

    def auth(request: Request) -> None:
        # per-IP failed-auth limiter (the console's): X-Forwarded-For honoured only from Caddy on loopback
        key = "ip:" + client_ip(LIMITS, request.scope, {k.lower(): v for k, v in request.headers.items()})
        wait = limiter.retry_after(key)
        if wait:
            raise HTTPException(429, "too many failed attempts; retry later", headers={"Retry-After": str(wait)})
        got = request.headers.get("authorization", "")
        if hmac.compare_digest(got.encode(), want):
            return                                         # STEM_SERVER_TOKEN (static)
        scheme, _, cred = got.partition(" ")
        if not (scheme.lower() == "bearer" and cred.strip() and s.auth_store.check_token(cred.strip())):
            limiter.fail(key, LIMITS)
            raise HTTPException(401, "bad or missing bearer token", headers={"WWW-Authenticate": "Bearer"})

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
        tmp = s.uploads / f"_up_{uuid.uuid4().hex}.{ext}"   # '_' prefix: the console never lists it
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
            os.replace(tmp, s.uploads / f"{id}.{ext}")
        finally:
            tmp.unlink(missing_ok=True)
        return {"id": id, "stored": True}

    @app.post("/v1/separate", dependencies=[Depends(auth)])
    def separate(req: SeparateReq):
        check_id(req.id)
        if req.stems not in (2, 4):
            raise HTTPException(400, "stems must be 2 or 4")
        return srv.submit(req.id, req.stems).to_dict()

    @app.get("/v1/jobs/{job}", dependencies=[Depends(auth)])
    def job(job: str):
        j = srv.jobs.get(job)
        if j is None:
            raise HTTPException(404, "unknown job")
        return j.to_dict()

    @app.get("/v1/tracks/{track_id}/stems/{name}.m4a", dependencies=[Depends(auth)])
    def stem_window(track_id: str, name: str, start: float, end: float):
        path, meta = srv.window(check_id(track_id), name, start, end)
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
        return FileResponse(path, media_type="audio/mp4", headers=headers)

    return app


def _make_app() -> FastAPI:   # uvicorn --factory app.stem_server.server:_make_app
    return create_app()
