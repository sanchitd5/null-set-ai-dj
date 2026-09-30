"""FastAPI backend exposing the Music Brain REST endpoints for the
interactive web workbench (spec 1.6 / 3.8): track upload + analysis, stem
separation, transition matching, preview rendering, and audio streaming.

Run with:
    uvicorn ui.server:app --reload
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.music_brain.analysis.analyzer import analyze as analyze_track
from app.music_brain.config import (
    CACHE_DIR,
    RECORDINGS_CACHE_DIR,
    ROOT_DIR,
    SAMPLES_CACHE_DIR,
    SET_LOGS_CACHE_DIR,
)
from app.music_brain.matching.knowledge_parser import KnowledgeParser
from app.music_brain.matching.recipe_matcher import RecipeMatcher
from app.music_brain.render.transition_renderer import render_full_mix, render_preview
from app.music_brain.learning.set_log import export_set_log_markdown, validate_set_log
from app.ui.services import engine as _engine
from app.ui.services.bg_jobs import DONE as _JOB_DONE, ERROR as _JOB_ERROR, EXPIRED as _JOB_EXPIRED, JobRunner
from app.ui.services.library_service import scan_library


def _host():
    """The installed engine's host: the port to YouTube, stems, audio reads and the logs."""
    return _engine.current().host


def separate_stems(path, **kw):
    return _host().separate(path, **kw)


def _load_audio(path, **kw):
    return _host().load_audio(path, **kw)

UPLOAD_DIR = CACHE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="AI Music Brain", version="0.1.0")


@app.on_event("startup")
def _boot_llm() -> None:
    """Load the DJ's LLM (MLX on Apple Silicon, Ollama fallback) in the
    background at startup, so the first suggestion does not pay model load."""
    from app.ui.services import model_runtime

    model_runtime.start_background()


@app.on_event("startup")
def _migrate_analyses() -> None:
    """Library analyses from before v5 get their tempo refined in the
    background, one song at a time; nothing is re-analysed from scratch."""
    from app.music_brain.analysis.analyzer import queue_upgrade

    queue_upgrade(sorted(_tracks.values()))


@app.on_event("startup")
def _cap_keylock_cache() -> None:
    from app.music_brain.audio import keylock_cache

    keylock_cache.run_async("startup")


@app.get("/api/llm/status")
def get_llm_status():
    from app.ui.services import model_runtime
    from app.ui.services.llm_gate import gate

    return {**model_runtime.status(), "gate": gate.snapshot()}

_knowledge = KnowledgeParser()
_matcher = RecipeMatcher(_knowledge)

# In-memory registry: track_id -> absolute file path. Rebuilt on restart
# from UPLOAD_DIR's contents (see _load_registry_from_disk below).
_tracks: Dict[str, Path] = {}
# Display names (track_id -> original filename stem). Persisted to a sidecar so
# a server restart doesn't collapse every track to its content hash (which then
# leaks into the autopilot LLM prompt as "Unknown - 065028eaec446431").
TRACK_NAMES_FILE = UPLOAD_DIR / "_names.json"
_track_names: Dict[str, str] = {}


_AUDIO_SUFFIXES = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aiff", ".aif", ".opus"}


def _load_registry_from_disk() -> None:
    # Audio only: aborted yt-dlp runs used to leave .webp thumbnails and .part
    # files here, which then showed up (and crashed analysis) as "tracks".
    for path in UPLOAD_DIR.glob("*"):
        if path.is_file() and not path.name.startswith("_") and path.suffix.lower() in _AUDIO_SUFFIXES:
            _tracks[path.stem] = path
    if TRACK_NAMES_FILE.exists():
        try:
            loaded = json.loads(TRACK_NAMES_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                _track_names.update({k: v for k, v in loaded.items() if isinstance(v, str)})
        except (json.JSONDecodeError, OSError):
            pass


def _remember_track_name(track_id: str, name: str) -> None:
    name = name.strip()
    if not name or name == track_id:
        return
    _track_names[track_id] = name
    try:
        TRACK_NAMES_FILE.write_text(json.dumps(_track_names, indent=2), encoding="utf-8")
    except OSError:
        pass


_load_registry_from_disk()


def _track_path(track_id: str) -> Path:
    path = _tracks.get(track_id)
    if path is None or not path.exists():
        from app.ui.services import dedup_songs

        canonical = dedup_songs.resolve_alias(track_id, CACHE_DIR)   # a quarantined duplicate resolves to its kept copy
        path = _tracks.get(canonical) if canonical != track_id else None
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail=f"Unknown track_id: {track_id}")
    return path


# --- Sampler one-shots & mix recordings (content-addressed blob storage) -------
# Same content-hash convention as /api/tracks: sha256(bytes)[:16] is the id, so
# re-uploading the identical blob is idempotent. Unlike tracks these are never
# analyzed — they're just stored and streamed back.

SAMPLE_LABELS_FILE = SAMPLES_CACHE_DIR / "_labels.json"

_samples: Dict[str, Path] = {}
_sample_labels: Dict[str, str] = {}
_recordings: Dict[str, Path] = {}
_set_logs: Dict[str, Path] = {}


def _load_blob_registry(directory: Path, registry: Dict[str, Path]) -> None:
    for path in directory.glob("*"):
        if path.is_file() and not path.name.startswith("_"):
            registry[path.stem] = path


def _load_sample_labels() -> None:
    if SAMPLE_LABELS_FILE.exists():
        try:
            _sample_labels.update(json.loads(SAMPLE_LABELS_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass


def _save_sample_labels() -> None:
    SAMPLE_LABELS_FILE.write_text(json.dumps(_sample_labels, indent=2), encoding="utf-8")


_load_blob_registry(SAMPLES_CACHE_DIR, _samples)
_load_blob_registry(RECORDINGS_CACHE_DIR, _recordings)
_load_blob_registry(SET_LOGS_CACHE_DIR, _set_logs)
_load_sample_labels()


def _media_type_for(path: Path) -> str:
    # mimetypes maps .webm to video/webm; MediaRecorder blobs here are audio-only.
    if path.suffix.lower() in (".webm", ".opus"):
        return "audio/webm"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


_BLOB_SUFFIXES = _AUDIO_SUFFIXES | {".webm"}
SAMPLE_MAX_BYTES = 25 * 1024 * 1024  # one-shots and chops, not whole songs


async def _store_blob(file: UploadFile, directory: Path, registry: Dict[str, Path],
                      default_suffix: str, max_bytes: Optional[int] = None) -> tuple[str, Path]:
    # The suffix picks the served Content-Type, so only audio suffixes are
    # accepted: an ".html" upload served back same-origin would be stored XSS.
    suffix = (Path(file.filename or "").suffix or default_suffix).lower()
    if suffix not in _BLOB_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"Unsupported audio file type: {suffix}")
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty upload")
    if max_bytes is not None and len(contents) > max_bytes:
        raise HTTPException(status_code=413,
                            detail=f"File too large ({len(contents) // (1024 * 1024)} MB, max {max_bytes // (1024 * 1024)} MB)")
    blob_id = hashlib.sha256(contents).hexdigest()[:16]
    dest = directory / f"{blob_id}{suffix}"
    dest.write_bytes(contents)
    registry[blob_id] = dest
    return blob_id, dest


class MatchRequest(BaseModel):
    track_a_id: str
    track_b_id: str
    top_n: int = 3
    no_cuts: bool = False   # the autopilot: never rank Hard Cut / Quick Cut ("hard cuts are a big no")
    punjabi_profile: str = "off"  # scene_profile mode off | on | auto; absent -> today's matching


class PreviewRequest(BaseModel):
    track_a_id: str
    track_b_id: str
    recipe: Optional[str] = None
    a_time: Optional[float] = None
    b_time: Optional[float] = None
    preview_seconds: float = 20.0


class RenderRequest(BaseModel):
    track_a_id: str
    track_b_id: str
    recipe: Optional[str] = None
    a_time: Optional[float] = None
    b_time: Optional[float] = None


class DownloadRequest(BaseModel):
    url: str


@app.post("/api/download")
async def download_from_url(req: DownloadRequest):
    """Download a YouTube or YouTube Music URL and register as a track."""
    from app.ui.services.download_service import detect_source

    download_to_dir = _host().download_to_dir
    source = detect_source(req.url)
    if source == "unknown":
        raise HTTPException(
            status_code=400,
            detail="Unsupported URL. Paste a YouTube or YouTube Music link.",
        )

    # Download into a private temp dir so thumbnails / .part files from a failed
    # or filtered run never land in UPLOAD_DIR; only the final audio is moved over.
    existing = _reuse_existing(req.url)
    if existing:                          # same song already in the library: no second upload
        return {"source": source, "tracks": existing, "reused": True}
    tmp_dir = UPLOAD_DIR / f"_dl_{uuid.uuid4().hex}"
    try:
        try:
            # Off the event loop: a YouTube fetch can take a minute and
            # would otherwise stall every other request (audio, analysis, UI).
            from starlette.concurrency import run_in_threadpool

            paths = await run_in_threadpool(download_to_dir, req.url, tmp_dir)
        except Exception as exc:
            from app.music_brain import yt_guard

            if isinstance(exc, yt_guard.Cooling):        # bot check: paused, heals by itself
                raise HTTPException(status_code=503, detail=str(exc),
                                    headers={"Retry-After": str(max(1, int(exc.until - time.time())))})
            raise HTTPException(status_code=500, detail=str(exc))

        results = _register_downloaded(paths)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return {"source": source, "tracks": results}


def _reuse_existing(url: str) -> Optional[List[dict]]:
    """A search URL for "Artist - Title" that the library already holds (same recording, remix markers
    included, aliases resolved): the existing track, so no second upload of it is downloaded."""
    from app.ui.services import dedup_songs

    wanted = dedup_songs.wanted_from_url(url)
    if not wanted:
        return None
    tid = dedup_songs.find_existing(
        wanted, _track_names, exists=lambda i: i in _tracks and _tracks[i].exists(), cache=CACHE_DIR)
    if not tid:
        return None
    return [{"track_id": tid, "filename": _tracks[tid].name, "display_name": _track_names.get(tid) or wanted}]


def _register_downloaded(paths: List[Path], names: Optional[List[str]] = None, move: bool = True,
                         queue: bool = True) -> List[dict]:
    """Move downloaded audio into UPLOAD_DIR under its content-hash id. names: display names
    (default the file stems); move=False copies (pair_atlas import-set keeps the study's file);
    queue=False leaves the separation to the console (import-set: stems cached by content)."""
    results = []
    for i, path in enumerate(paths):
        original_name = names[i] if names and i < len(names) and names[i] else path.stem
        data = path.read_bytes()
        track_id = hashlib.sha256(data).hexdigest()[:16]
        dest = UPLOAD_DIR / f"{track_id}{path.suffix}"
        if not dest.exists():
            if move:
                shutil.move(str(path), dest)
            else:
                shutil.copy2(str(path), dest)
        _tracks[track_id] = dest
        _remember_track_name(track_id, original_name)
        if queue:
            _queue_stems(track_id)        # separate now, before a deck needs it
        results.append({"track_id": track_id, "filename": dest.name, "display_name": original_name})
    return results


class DownloadJobRequest(BaseModel):
    url: str
    label: str = ""


@app.get("/api/search/youtube")
def get_youtube_search(q: str, limit: int = 8):
    """Song search for LEAD TO (no download); filtered like downloads."""
    search_songs = _host().search_songs

    if not (2 <= len(q.strip()) <= 120):
        raise HTTPException(status_code=400, detail="query must be 2-120 characters")
    from app.music_brain import yt_guard

    try:
        return {"results": search_songs(q, max(1, min(limit, 12)))}
    except yt_guard.Cooling as exc:            # bot check: YouTube paused, heals by itself
        raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": str(max(1, int(exc.until - time.time())))}) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"search failed: {exc}") from exc


@app.get("/api/youtube/status")
def get_youtube_status():
    """{cooling, until, strikes, last_error}: app.music_brain.yt_guard's bot-check backoff."""
    from app.music_brain import yt_guard

    return yt_guard.status()


@app.post("/api/download/jobs")
def post_download_job(req: DownloadJobRequest):
    """Start a background download (pre-download / prefetch); poll for progress."""
    from app.ui.services import download_jobs
    from app.ui.services.download_service import detect_source

    if detect_source(req.url) == "unknown":
        raise HTTPException(status_code=400, detail="Unsupported URL.")
    job_id = download_jobs.start_job(
        req.url, req.label[:120], UPLOAD_DIR,
        download_fn=_host().download_to_dir,
        register_fn=_register_downloaded,
        reuse_fn=_reuse_existing,
        analyze_fn=lambda tid: analyze_track(_track_path(tid)),
    )
    return {"job_id": job_id}


@app.get("/api/download/jobs")
def get_download_jobs():
    from app.ui.services import download_jobs

    return {"jobs": download_jobs.list_jobs()[:20]}


@app.get("/api/download/jobs/{job_id}")
def get_download_job(job_id: str):
    from app.ui.services import download_jobs

    job = download_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    return job


@app.post("/api/tracks")
async def upload_track(file: UploadFile):
    """Uploads a track, returns its track_id for use in every other endpoint."""
    suffix = Path(file.filename or "track").suffix or ".mp3"
    contents = await file.read()
    track_id = hashlib.sha256(contents).hexdigest()[:16]
    dest = UPLOAD_DIR / f"{track_id}{suffix}"
    dest.write_bytes(contents)
    _tracks[track_id] = dest
    if file.filename:
        _remember_track_name(track_id, Path(file.filename).stem)
    _queue_stems(track_id)                # separate now, before a deck needs it
    return {"track_id": track_id, "filename": file.filename}


def _name_from_tags(track_id: str, path: Path) -> Optional[str]:
    """A track stored under its hash with no remembered name: take it from the
    file's own tags ("Artist - Title", or the title when it already reads
    that way) and remember it."""
    try:
        import mutagen

        f = mutagen.File(str(path), easy=True)
        tags = (f.tags or {}) if f else {}
    except Exception:
        return None
    title = " ".join((tags.get("title") or [""])[0].split())
    artist = " ".join((tags.get("artist") or [""])[0].split())
    if not title:
        return None
    name = title if (" - " in title or " – " in title or not artist) else f"{artist} - {title}"
    _remember_track_name(track_id, name[:200])
    return name[:200]


@app.get("/api/tracks")
def list_tracks():
    from app.ui.services.download_service import _is_live, _is_mix

    def _entry(tid, p):
        name = _track_names.get(tid) or _name_from_tags(tid, p) or p.stem
        # not_a_song: live/event recordings and mixes already in the library
        # (downloaded before those filters existed); the autopilot skips them.
        return {"track_id": tid, "path": str(p), "display_name": name,
                "not_a_song": bool(_is_live(name) or _is_mix(name))}

    return {"tracks": [_entry(tid, p) for tid, p in _tracks.items()]}


def _register_library_tracks() -> list[dict[str, object]]:
    """Refresh configured local-library tracks in the existing ID registry.

    Library paths are discovered only by ``library_service`` from the server's
    DJ_LIBRARY_DIRS allowlist.  The browser never supplies a filesystem path.
    """

    tracks = scan_library()
    for track in tracks:
        tid = str(track["track_id"])
        if tid not in _tracks:
            _tracks[tid] = Path(str(track["path"]))
            _queue_stems(tid, urgent=False)   # new library track: backfill its stems
    return tracks


@app.get("/api/library")
def list_library():
    """List allowlisted local tracks and make them available to deck endpoints."""

    return {"tracks": _register_library_tracks()}


@app.post("/api/library/scan")
def scan_configured_library():
    """Refresh the configured local music folders without accepting client paths."""

    tracks = _register_library_tracks()
    return {"tracks": tracks, "count": len(tracks)}


@app.get("/api/tracks/{track_id}/analysis")
def get_analysis(track_id: str):
    path = _track_path(track_id)
    return analyze_track(path).to_dict()


@app.post("/api/tracks/{track_id}/separate")
def post_separate(track_id: str, stems: int = 4):
    path = _track_path(track_id)
    two_stems = "vocals" if stems == 2 else None
    result = separate_stems(path, two_stems=two_stems)
    return result.to_dict()


@app.get("/api/recipes")
def get_recipes():
    return {"recipes": [r.to_dict() for r in _knowledge.get_all()]}


def _cached_vocal_regions(track_id: str) -> Optional[list]:
    """Vocal regions only when a Demucs vocal stem is already cached:
    matching must never start a separation."""
    from app.music_brain.audio import stem_service
    from app.music_brain.config import DEMUCS_MODEL
    from app.music_brain.render.mashup import MASHUP_DEMUCS_MODEL

    if track_id in _vocal_regions:
        return _vocal_regions[track_id]
    audio_hash = stem_service.file_hash(_track_path(track_id))
    # ft 4-stem first: once it exists the fast 2-stem copy is pruned (stem_service.prune_non_ft)
    for model, two in ((DEMUCS_MODEL, None), (MASHUP_DEMUCS_MODEL, "vocals"), (DEMUCS_MODEL, "vocals")):
        stems = stem_service._load_from_cache(stem_service._cache_dir_for(audio_hash, model, two))
        if not stems or not stems.get("vocals"):
            continue
        if (model, two) == (MASHUP_DEMUCS_MODEL, "vocals"):
            return _vocal_regions_for(track_id)  # hits the same stem cache
        try:
            from app.music_brain.analysis.analyzer import vocal_presence_map

            regions = [list(r) for r in vocal_presence_map(Path(stems["vocals"]))]
        except Exception as exc:
            print(f"[match] vocal map unavailable for {track_id}: {exc}", flush=True)
            return None
        _vocal_regions[track_id] = regions
        return regions
    return None


# Live stems: ONE background worker separates songs into 4 stems (drums, bass,
# vocals, other) in the order the decks ask, so the playing song and the next
# one get stems while they play (~40 s per song on htdemucs_ft here).
STEM_NAMES = ("drums", "bass", "vocals", "other")
_stem_queue: "list[str]" = []      # urgent: decks, fresh uploads / downloads (FIFO)
_stem_backlog: "list[str]" = []    # library backfill: only while the LLM is idle
_stem_busy: Optional[str] = None
_stem_cv = threading.Condition()
_stem_done = {"separated": 0, "failed": 0}


_stem_cache: Dict[str, Dict[str, str]] = {}


def _cached_stems4(track_id: str) -> Optional[Dict[str, str]]:
    from app.music_brain.audio import stem_service

    hit = _stem_cache.get(track_id)
    if hit and all(os.path.exists(p) for p in hit.values()):
        return hit
    _stem_cache.pop(track_id, None)       # its folder was pruned for an ft set: resolve again
    stems = stem_service.cached_four_stems(_track_path(track_id))
    if stems:
        _stem_cache[track_id] = stems
    return stems


def _llm_busy() -> bool:
    from app.ui.services.llm_gate import gate

    snap = gate.snapshot()
    return bool(snap["in_flight"] or snap["queued"] or snap.get("live"))


def _stem_wait_note() -> str:
    """Why the background stem backfill is waiting, in the owner's words: the STEMS wait
    for the AI, the AI is not paused ("llm shows llm paused" was read the other way round)."""
    from app.ui.services.llm_gate import gate

    snap = gate.snapshot()
    doing = "live ear" if snap.get("live") else snap["in_flight"] or (snap["queued"][0] if snap["queued"] else "")
    return f"stems wait for the AI ({doing})" if doing else ""


STEM_IN_FLIGHT = 2        # one separating, one decoding ahead (stem_worker pipeline)
MAX_BACKFILL_S = 12 * 60  # longer files are albums / mixes: separated only if a deck asks


def _next_stem_job() -> Optional[str]:
    """Urgent first; the backfill only while the LLM is idle. Caller holds _stem_cv."""
    if _stem_queue:
        return _stem_queue.pop(0)
    if _stem_backlog and not _llm_busy():
        return _stem_backlog.pop(0)
    return None


def _stem_worker() -> None:
    """Feeds the persistent separation process (app/music_brain/audio/stem_worker.py):
    up to STEM_IN_FLIGHT songs at once so the next one decodes while the current
    one runs on the GPU. Falls back to one-shot Demucs if the process won't start."""
    import queue as _q

    global _stem_busy
    from app.music_brain.audio import stem_service

    proc, in_flight = None, {}
    while True:
        if proc is None or not proc.alive:
            try:
                proc = stem_service.StemWorker()
            except Exception as exc:
                print(f"[stems] worker unavailable ({exc}); one-shot Demucs", flush=True)
                proc = None
        with _stem_cv:
            while len(in_flight) < (STEM_IN_FLIGHT if proc else 1):
                tid = _next_stem_job()
                if tid is None:
                    break
                if tid not in _tracks or _cached_stems4(tid) is not None:
                    continue
                if proc:
                    proc.submit(tid, _track_path(tid))
                    in_flight[tid] = time.monotonic()
                else:
                    in_flight[tid] = None
            _stem_busy = next(iter(in_flight), None)
            if not in_flight:
                _stem_cv.wait(timeout=2.0)
                continue
        if proc is None:                                    # fallback: synchronous one-shot
            tid = next(iter(in_flight))
            try:
                separate_stems(_track_path(tid))
                _stem_done["separated"] += 1
                _cached_vocal_regions(tid)
            except Exception as exc:
                _stem_done["failed"] += 1
                print(f"[stems] {tid}: {exc}", flush=True)
            in_flight.pop(tid, None)
            continue
        try:
            res = proc.results.get(timeout=5.0)
        except _q.Empty:
            continue
        tid = res.get("id")
        if tid not in in_flight:
            if res.get("error") == "worker exited":
                for t in in_flight:
                    _queue_stems(t)                         # redo on a fresh worker
                in_flight.clear()
                proc = None
            continue
        in_flight.pop(tid)
        if res.get("ok"):
            try:
                _stem_cache[tid] = stem_service.StemWorker.finish(res)
                _stem_done["separated"] += 1
                _stem_done["last_seconds"] = res.get("seconds")
                _cached_vocal_regions(tid)
            except Exception as exc:
                _stem_done["failed"] += 1
                print(f"[stems] {tid}: {exc}", flush=True)
        else:
            _stem_done["failed"] += 1
            print(f"[stems] {tid}: {res.get('error')}", flush=True)
        with _stem_cv:
            _stem_busy = next(iter(in_flight), None)


def _start_stem_worker() -> None:
    if not getattr(_stem_worker, "started", False):
        _stem_worker.started = True
        threading.Thread(target=_stem_worker, daemon=True).start()


def _queue_stems(track_id: str, urgent: bool = True) -> bool:
    return _host().queue_stems(track_id, urgent)


def _queue_stems_impl(track_id: str, urgent: bool = True) -> bool:
    """Queue a 4-stem separation; True while queued or running. Urgent jobs
    (a deck, a new track) go ahead of the library backfill."""
    with _stem_cv:
        if _stem_busy == track_id or track_id in _stem_queue:
            return True
        if track_id in _stem_backlog:
            if not urgent:
                return True
            _stem_backlog.remove(track_id)
        (_stem_queue if urgent else _stem_backlog).append(track_id)
        _start_stem_worker()
        _stem_cv.notify_all()
        return True


def _dequeue_stems_impl(track_id: str) -> bool:
    """Drop a separation that is queued but not started. True when one was removed."""
    with _stem_cv:
        removed = False
        for q in (_stem_queue, _stem_backlog):
            if track_id in q:
                q.remove(track_id)
                removed = True
        return removed


class PrerenderItem(BaseModel):
    track_id: str = Field(max_length=64)
    bpms: List[float] = Field(default_factory=list, max_length=4)


class PrerenderRequest(BaseModel):
    items: List[PrerenderItem] = Field(default_factory=list, max_length=8)


def _prerender_loop() -> None:
    from app.ui.services import prerender

    while True:
        try:
            prerender.current().step()
        except Exception as exc:                 # never kills the worker
            print(f"[prerender] {type(exc).__name__}: {exc}", flush=True)
        time.sleep(1.0)


def _start_prerender_worker() -> None:
    if getattr(_host(), "threaded", True) and not getattr(_prerender_loop, "started", False):
        _prerender_loop.started = True
        threading.Thread(target=_prerender_loop, daemon=True, name="prerender").start()


@app.post("/api/prerender")
def post_prerender(req: PrerenderRequest):
    """The ranked next-song candidates (best first) and the tempi they may have to play at. The server
    makes their stems and key-locked tempo sets ahead of the booking, one heavy job at a time, and
    drops the queued work of any candidate that is no longer listed. Answers each one's readiness."""
    from app.ui.services import prerender

    items = [{"track_id": i.track_id, "bpms": i.bpms} for i in req.items if i.track_id in _tracks]
    sched = prerender.current()
    status = sched.set_items(items)
    sched.step()
    _start_prerender_worker()
    return {"items": status, "stats": sched.stats()}


@app.get("/api/prerender")
def get_prerender():
    from app.ui.services import prerender

    sched = prerender.current()
    sched.step()
    return {"items": sched.status(), "stats": sched.stats()}


@app.on_event("startup")
def _backfill_stems() -> None:
    """Every library track gets its stems separated ahead of time, in the
    background, so a deck load never waits on Demucs."""
    def scan():
        import soundfile as _sf

        from app.ui.services.download_service import _is_live, _is_mix

        missing = []
        for tid in list(_tracks):
            try:
                name = _track_names.get(tid) or ""
                if _is_mix(name) or _is_live(name):
                    continue                             # not a song: the autopilot never plays it
                try:
                    if _sf.info(str(_tracks[tid])).duration > MAX_BACKFILL_S:
                        continue                         # an album / mix file, not a song
                except Exception:
                    pass
                if _cached_stems4(tid) is None:
                    missing.append(tid)
            except Exception:
                continue
        for tid in missing:
            _queue_stems(tid, urgent=False)
        print(f"[stems] library backfill: {len(missing)} of {len(_tracks)} tracks to separate", flush=True)

    threading.Thread(target=scan, daemon=True).start()


_vocal_entry_cache: Dict[str, dict] = {}


@app.get("/api/tracks/{track_id}/vocal_entry")
def get_vocal_entry(track_id: str):
    """Where this song's vocal phrase starts for a mashup (after a repeated
    opening hook), whether it's rap, and how long it keeps going (bars)."""
    import librosa

    from app.music_brain.matching import techniques as tq

    _track_path(track_id)
    if track_id in _vocal_entry_cache:
        return _vocal_entry_cache[track_id]
    st = _cached_stems4(track_id)
    regions = _cached_vocal_regions(track_id) or []
    if not st or not regions:
        return {"entry": None, "reason": "no stems / vocals yet"}
    a = analyze_track(_track_path(track_id)).to_dict()
    bar = 240.0 / a["bpm"]
    yv, srv = _load_audio(st["vocals"], sr=22050, mono=True)
    rep = tq.repetitive_bars(yv, srv, a["downbeat_times"])

    def share(lo, hi):
        return sum(max(0.0, min(e, hi) - max(s, lo)) for s, e in regions) / max(1e-6, hi - lo)

    first = next((p for p in a["phrase_boundaries_8bar"] if share(p, p + 16 * bar) >= 0.5), None)
    if first is None:
        res = {"entry": None, "reason": "no 16-bar vocal phrase"}
    else:
        entry = tq.skip_repetitive_intro(rep, a["downbeat_times"], a["phrase_boundaries_8bar"], first)
        if share(entry, entry + 16 * bar) < 0.5:
            entry = first
        y, sr = _load_audio(st["vocals"], sr=16000, mono=True, offset=entry, duration=min(30.0, 16 * bar))
        style = _host().vocal_style(y, sr)
        res = {"entry": entry, "rap": style["rap"], "style": style, "bpm": a["bpm"],
               "vocal16": round(share(entry, entry + 16 * bar), 2), "vocal32": round(share(entry, entry + 32 * bar), 2)}
    _vocal_entry_cache[track_id] = res
    return res


def _cached_energy_fit(path, bpm: float, cur: int) -> int:
    """1 when the song's cached level is within energy.MAX_STEP of `cur`,
    -1 when outside, 0 when unknown (no level asked, or not measured yet)."""
    if not cur:
        return 0
    try:
        from app.music_brain.analysis import energy as en

        if not en._cache(Path(path)).exists():
            return 0
        return 1 if abs(en.level(path, bpm)["level"] - int(cur)) <= en.MAX_STEP else -1
    except Exception:
        return 0


@app.get("/api/library/lockable")
def get_library_lockable(bpm: float, key: str = "", exclude: str = "", limit: int = 6, max_gap: float = 0.08,
                         genre: str = "", era: str = "", punjabi_profile: str = "off", energy: int = 0,
                         a_id: str = "", anchor: str = ""):
    """Library songs whose analysed tempo locks to `bpm` (half / double time
    count) within max_gap, best key match first. The autopilot's fallback
    before it would force a tempo jump.

    `genre` = the playing song's genre. When given, a library song must be
    known to sit in the same scene (genre.genre_near) - tempo and key alone
    let Aqua "Barbie Girl" hand over to Bicep "Glue" (user). A song whose
    genre isn't known yet is left out too: better a genre-right tempo jump
    than a genre-wrong lock.

    `era` = the playing song's release decade: a library song more than one
    decade away is left out (Barbie Girl 1997 -> Glue 2017). Unknown era is
    allowed; genre already gates the unlabelled ones.

    Under an active Punjabi profile a song whose genre is not known yet is
    kept but ranked after the known ones: genre labels live in memory and only
    for songs the model named this server run, so after a restart the
    deadline fallback found nothing and the set sat in HOLD LOOP (session
    2026-09-30_102327). The console's vibe and energy gates still check it.

    `energy` = the playing song's measured level (1-10, 0 = unknown). Songs
    whose already-measured level sits within energy.MAX_STEP go first: in
    102327 every candidate died on "energy drop 8 -> 5". Nothing is measured
    here; unmeasured songs rank in between.

    The reference genre is `anchor` (the set's scene while the console recovers from an
    off-scene mistake), else the playing song's STORED label (`a_id`, genre_labels.json),
    else `genre` (the console's / model's label). Session 2026-09-30_191133: the console
    sent genre="" for a FOLLOW SET song, nothing was filtered, and Big Boss Vette (hip hop)
    handed over to Four Tet (electronic).

    `outside`: songs that lock but sit outside the reference scene, ranked same family,
    then unknown genre, then cross-family (genre.SCENE_RANK), each with its `scene_rel`.
    The console books one only when nothing in `tracks` passed and logs it as such."""
    from app.music_brain.matching import techniques as tq
    from app.music_brain.analysis.genre import MAX_ERA_GAP, SCENE_RANK, era_gap, genre_near, scene_relation
    from app.ui.services.download_service import _is_live, _is_mix
    from app.ui.services.track_identity import clean_identity

    skip = set(filter(None, exclude.split(",")))
    genre = str(genre or "").strip()[:80]
    anchor = str(anchor or "").strip()[:80]
    stored = (_track_vibe(str(a_id)[:64]).get("genre") or "") if a_id else ""
    genre = anchor or stored or genre
    # Punjabi scene profile: one scene (punjabi / bhangra / desi, bollywood near) and
    # a wider era gate while it is active; "off" (the default) is today's filter.
    from app.music_brain.analysis import scene_profile as _sp
    sp_active = _sp.selection_active(punjabi_profile, genre)
    era = str(era or "").strip()[:40]
    if a_id and not era:
        era = _track_vibe(str(a_id)[:64]).get("era") or ""
    out, outside = [], []
    for tid, path in list(_tracks.items()):
        if tid in skip:
            continue
        name = _track_names.get(tid) or path.stem
        if _is_mix(name) or _is_live(name):
            continue
        lib_key = _genre_key(clean_identity(name)[1])
        lib_genre = _suggested_genres.get(lib_key, "")
        near = (_sp.scene_near(genre, lib_genre, True) if sp_active
                else genre_near(genre, lib_genre)) if genre else True
        inside = near is True or (sp_active and not lib_genre)
        lib_era = _suggested_eras.get(lib_key, "")
        if sp_active:
            if _sp.era_jump(era, lib_era, True):
                continue
        else:
            egap = era_gap(era, lib_era)
            if egap is not None and egap > MAX_ERA_GAP:
                continue
        try:
            a = analyze_track(path)
        except Exception:
            continue
        if not a.bpm or a.duration < 90 or a.duration > 12 * 60:
            continue
        gap = min(abs(bpm / (a.bpm * m) - 1) for m in (1, 2, 0.5))
        if gap > max_gap:
            continue
        k = a.key.camelot if a.key else None
        ks = tq.camelot_score(key, k) if key and k else 0.5
        row = {"track_id": tid, "name": name, "bpm": a.bpm, "key": k, "gap": round(gap, 4),
               "key_score": ks, "duration": a.duration, "stems": _stem_cache.get(tid) is not None,
               "genre": lib_genre, "era": lib_era, "genre_known": near is True,
               "energy_fit": _cached_energy_fit(path, a.bpm, energy),
               "scene_rel": scene_relation(genre, lib_genre) if genre else "unknown"}
        (out if inside else outside).append(row)
    rank = lambda x: (-x["energy_fit"], -x["key_score"], x["gap"])
    out.sort(key=lambda x: (not x["genre_known"], *rank(x)))
    outside.sort(key=lambda x: (SCENE_RANK.get(x["scene_rel"], 3), *rank(x)))
    lim = max(1, min(limit, 20))
    return {"tracks": out[:lim], "outside": outside[:lim], "ref_genre": genre}


@app.get("/api/stems/status")
def get_stems_status():
    with _stem_cv:
        return {"busy": _stem_busy, "urgent": list(_stem_queue), "backlog": len(_stem_backlog),
                "llm_busy": _llm_busy(), "wait_note": _stem_wait_note(), **_stem_done}


@app.get("/api/tracks/{track_id}/vocals")
def get_track_vocals(track_id: str, separate: bool = False):
    """Vocal activity regions [[start, end], ...] for the hold loop / DJ mind.
    Cached stem -> regions now; separate=1 -> queue the 4-stem separation and
    answer pending; poll again."""
    _track_path(track_id)  # 404 for unknown ids
    regions = _cached_vocal_regions(track_id)
    if regions is not None:
        return {"regions": regions, "pending": False}
    pending = _queue_stems(track_id) if separate else False
    return {"regions": None, "pending": pending}


@app.get("/api/tracks/{track_id}/stems")
def get_track_stems(track_id: str, separate: bool = False, bpm: Optional[float] = None):
    """4 live stems for a deck: {stems: {name: url}} when separated, else
    pending (separate=1 queues it). bpm=X: the same stems key-locked at X BPM
    (rendered once and cached; pending while rendering)."""
    _track_path(track_id)
    stems = _cached_stems4(track_id)
    if stems and bpm:
        from app.music_brain.audio import keylock, stem_service

        native = analyze_track(_track_path(track_id)).bpm
        if abs(bpm / native - 1) > 0.005:
            if not keylock.available():
                raise HTTPException(status_code=503, detail="Rubber Band not installed")
            r = keylock.ensure_tempo(stem_service.file_hash(_track_path(track_id)), stems, native, bpm)
            if r["state"].startswith("error"):
                raise HTTPException(status_code=422, detail=r["state"])
            gate_open = _host().tempo_gate(r["key"])       # registers the ask (the sim models the render time)
            if r["state"] != "done" or not gate_open:
                return {"stems": None, "pending": True, "bpm": r["bpm"]}
            return {"stems": {n: f"/api/riff/{r['key']}/{n}" for n in STEM_NAMES}, "pending": False,
                    "bpm": r["bpm"], "ratio": r["ratio"], "native_bpm": native}
    if stems:
        return {"stems": {n: f"/api/tracks/{track_id}/stems/{n}" for n in STEM_NAMES}, "pending": False}
    return {"stems": None, "pending": _queue_stems(track_id) if separate else False}


_voiced_cache: Dict[tuple, dict] = {}


_pair_cache: Dict[tuple, tuple] = {}
_pair_lock = threading.Lock()
PAIR_CACHE_S = 600.0


def _pair_features_cached(a_id: str, b_id: str, keylock: bool = False):
    """_pair_features loads all of A's stems (seconds): once per pair per 10 min,
    rebuilt when either side's stems appear."""
    key = (a_id, b_id, keylock, bool(_cached_stems4(a_id)), bool(_cached_stems4(b_id)))
    with _pair_lock:
        hit = _pair_cache.get(key)
    if hit and time.time() - hit[0] < PAIR_CACHE_S:
        return hit[1]
    f = _pair_features(a_id, b_id, keylock)
    with _pair_lock:                 # FastAPI runs sync handlers on a thread pool
        _pair_cache[key] = (time.time(), f)
        if len(_pair_cache) > 64:
            _pair_cache.pop(min(_pair_cache, key=lambda k: _pair_cache[k][0]))
    return f


def _pair_features(a_id: str, b_id: str, keylock: bool = False):
    """PairFeatures for the technique library, from cached stems (never separates)."""
    import librosa
    import numpy as np

    from app.music_brain.matching import techniques as tq

    ta, tb = analyze_track(_track_path(a_id)), analyze_track(_track_path(b_id))
    sa, sb = _cached_stems4(a_id), _cached_stems4(b_id)

    def share(regions, lo, hi):
        if not regions or hi <= lo:
            return 0.0
        return sum(max(0.0, min(e, hi) - max(s, lo)) for s, e in regions) / (hi - lo)

    va, vb = _cached_vocal_regions(a_id) or [], _cached_vocal_regions(b_id) or []
    lo, hi = max(0.0, ta.duration - 90), ta.duration - 10          # A's exit window (last ~1.5 min)
    grooves, breaks = [], []
    if sa:
        audio = {n: _load_audio(sa[n], sr=11025, mono=True)[0] for n in tq.STEMS}
        smap = _host().stem_map(audio, 11025, ta.phrase_boundaries_8bar)
        grooves, breaks = tq.full_groove_runs(smap), tq.breakdowns(smap)
    b_rap, rap_at = None, []
    if sb and vb:
        key = (b_id, "style")
        if key not in _voiced_cache:
            # style per 20 s of sung/rapped audio, up to 8 chunks across the song
            chunks = [(s, min(e, s + 20)) for s, e in vb if e - s >= 12]
            step = max(1, len(chunks) // 8)
            rows = []
            for s, e in chunks[::step][:8]:
                y, sr = _load_audio(sb["vocals"], sr=16000, mono=True, offset=s, duration=e - s)
                rows.append({"at": round(s, 1), **_host().vocal_style(y, sr)})
            _voiced_cache[key] = rows
        rows = _voiced_cache[key]
        rap_at = [r["at"] for r in rows if r["rap"]]
        b_rap = bool(rap_at)
    return tq.PairFeatures(
        bpm_a=ta.bpm, bpm_b=tb.bpm, key_a=ta.key.camelot if ta.key else None, key_b=tb.key.camelot if tb.key else None,
        stems_a=bool(sa), stems_b=bool(sb), famous_a=bool(_fame.get(a_id, {}).get("famous")),
        vocal_a_exit=share(va, lo, hi), vocal_b_entry=max([share(vb, t, t + 30) for t in rap_at] or [share(vb, 0, min(tb.duration, 60))]), b_rap=b_rap, b_rap_at=rap_at,
        a_grooves=grooves, a_breakdowns=breaks, keylock=keylock, exit_window=(lo, hi),
        a_hook_drops=_safe_hook_drops(a_id),
    )


def _safe_hook_drops(track_id: str) -> list:
    return _host().hook_drops(track_id)


def _safe_hook_drops_impl(track_id: str) -> list:
    try:
        return _hook_drops(track_id)
    except Exception:          # lyrics are a bonus: never break technique ranking
        return []


_hook_drop_cache: Dict[str, tuple] = {}
_hook_drop_miss: Dict[str, float] = {}
HOOK_MISS_TTL_S = 600.0


def _hook_drops(track_id: str, top_n: int = 3, ai_call: bool = False) -> list:
    """hook_drop.plan() for a library track: synced lyrics (cached), aligned to the
    cached vocal stem when there is one, the local model's emotional-line picks
    (ai_call=False: only already-cached picks, never waits on the model).
    [] on any miss; never separates."""
    from app.music_brain.analysis import hook_drop, lyrics
    from app.music_brain.learning import set_ai
    from app.music_brain.learning.set_learner import load_learned

    path = _track_path(track_id)
    name = _track_names.get(track_id) or _name_from_tags(track_id, path) or path.stem
    miss = _hook_drop_miss.get(track_id)
    if miss and time.time() - miss < HOOK_MISS_TTL_S:
        return []                 # no lyrics a moment ago: don't re-ask LRCLIB or reload stems
    if track_id not in _hook_drop_cache:
        a = analyze_track(path)
        # lyrics first (cached on disk; one network call at most): the vocal stem is
        # only decoded when there are lines to align
        if not (lyrics.fetch(name, duration=a.duration) or lyrics.load_plain(name)):
            _hook_drop_miss[track_id] = time.time()
            return []
        stems = _cached_stems4(track_id)
        y = None
        if stems and stems.get("vocals"):
            import librosa

            y, _ = _load_audio(stems["vocals"], sr=11025, mono=True)
        lines = lyrics.for_file(name, y, 11025, duration=a.duration)
        if not lines:
            _hook_drop_miss[track_id] = time.time()
            return []
        _hook_drop_cache[track_id] = (lines, a)
    lines, a = _hook_drop_cache[track_id]
    picks = set_ai.emotional_lines(name, lines, call=ai_call)
    return hook_drop.plan(lines, a.bpm, a.phrase_boundaries_8bar, a.energy_times, a.energy_curve,
                          learned=load_learned(), top_n=top_n, ai_lines=picks)


@app.get("/api/tracks/{track_id}/hook-drops")
def get_hook_drops(track_id: str, top_n: int = 3, ai: bool = True):
    """Where to go acapella on the track's emotional hook and bring the drop back in:
    [{text, cut_at, drop_at, hold_s, score, ai, why[]}] best first (app.music_brain.analysis.hook_drop).
    ai=True asks the local model which lines carry the emotion (cached per song)."""
    drops = _hook_drops(track_id, max(1, min(top_n, 10)), ai_call=ai)
    _song_step("hook_drop_plan", track_id, decision=f"{len(drops)} hook drop(s)",
               why=(drops[0].get("why") if drops and isinstance(drops[0], dict) else "no synced lyrics / no hook"),
               result=[{k: d.get(k) for k in ("text", "cut_at", "drop_at", "score", "ai")} for d in drops[:3]
                       if isinstance(d, dict)])
    return {"track_id": track_id, "hook_drops": drops}


def _song_step(kind: str, track_id: Optional[str], **fields) -> None:
    """One AI step into the per-song log (app/ui/services/song_log.py). Never raises."""
    try:
        from app.ui.services import song_log
        _host().song_step(kind, track_id, **fields)
    except Exception:
        pass


SESSION_EVENT_KINDS = ("track", "ear_flush", "note", "glitch", "move", "deck_load", "deck_unload", "veto")


class SessionEvent(BaseModel):
    kind: str
    data: Dict = {}


@app.post("/api/session/event")
def post_session_event(ev: SessionEvent):
    """The console's side of this session's log (track changes, ear flushes)."""
    from app.ui.services import session_log

    # deck_load / deck_unload: a song loaded on the staging deck and why it backed off (191133 had no
    # trace of Hanumankind loading); veto: BAD PAIR (the console sent it, the whitelist refused it)
    if ev.kind not in SESSION_EVENT_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {', '.join(SESSION_EVENT_KINDS)}")
    # the event's own fields may reuse the log's names (a glitch report has its own "kind"):
    # those are kept as "<name>_" instead of clashing
    fields = {(f"{k}_" if k in ("kind", "t", "at") else k): v for k, v in list(ev.data.items())[:20] if isinstance(k, str)}
    session_log.log(ev.kind, **fields)
    from app.ui.services import song_log
    _host().session_event(ev.kind, fields)   # transitions / glitches also land on the song
    return {"ok": True, "session": session_log.SESSION_ID}


@app.get("/api/session/log")
def get_session_log(session: Optional[str] = None, limit: int = 500):
    """This session's events (track changes, every LLM / ear call with its timing),
    a summary, and the list of past sessions. ?session=<id> for an earlier one."""
    from app.ui.services import session_log

    try:
        events = session_log.read(session, max(1, min(limit, 5000)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"session": session or session_log.SESSION_ID, "sessions": session_log.sessions()[:30],
            "summary": session_log.summary(events), "events": events}


class StepBatch(BaseModel):
    steps: list = Field(default_factory=list)  # bad items are skipped, not a 422 for the batch


def _song_resolvers() -> None:
    """How song_log finds a track's file, analysis, stems, name and energy (for waveforms)."""
    from app.ui.services import song_log

    def energy(path: Path, bpm: float) -> dict:
        from app.music_brain.analysis import energy as en
        return en.level(path, bpm)

    def track_path(tid: str) -> Optional[Path]:
        try:
            return _track_path(tid)
        except HTTPException:
            return None

    song_log.configure(path=track_path, analysis=lambda p: analyze_track(p).to_dict(),
                       stems=_cached_stems4, name=lambda tid: _track_names.get(tid), energy=energy)


_song_resolvers()


@app.post("/api/session/steps")
def post_session_steps(batch: StepBatch):
    """A batch of AI steps from the console (app/ui/static/step-log.js), filed per song."""
    from app.ui.services import song_log

    if len(batch.steps) > song_log.MAX_BATCH:
        raise HTTPException(status_code=413, detail=f"at most {song_log.MAX_BATCH} steps per batch")
    return {"ok": True, "accepted": song_log.ingest(batch.steps)}


@app.get("/api/session/songs")
def get_session_songs(session: Optional[str] = None):
    """Songs of a session (this run unless ?session=) with step counts per phase / kind."""
    from app.ui.services import session_log, song_log

    try:
        return {"session": session or session_log.SESSION_ID, "songs": song_log.songs(session)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/session/songs/{session}/{nn}")
def get_session_song(session: str, nn: int, limit: int = 2000):
    """One song's meta + every AI step, in time order."""
    from app.ui.services import song_log

    try:
        s = song_log.song(session, nn, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if s is None:
        raise HTTPException(status_code=404, detail="no such song in that session")
    return s


@app.get("/api/session/songs/{session}/{nn}/waveform.png")
def get_session_song_png(session: str, nn: int, refresh: bool = False):
    """The song's rendered waveform with AI steps; rendered in the background on first ask (202)."""
    from fastapi.responses import JSONResponse
    from app.ui.services import song_log

    try:
        d = song_log.song_dir(session, nn)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if d is None:
        raise HTTPException(status_code=404, detail="no such song in that session")
    png = d / "waveform.png"
    if png.exists() and not refresh:
        return FileResponse(png, media_type="image/png")
    song_log.render_async(d)
    return JSONResponse({"pending": True}, status_code=202)


class PreplanRequest(BaseModel):
    a_id: str
    b_id: str
    lo: float = Field(ge=0, le=36000, allow_inf_nan=False)    # A's song seconds: earliest handover line
    hi: float = Field(ge=0, le=36000, allow_inf_nan=False)    # latest handover line
    now: float = Field(ge=0, le=36000, allow_inf_nan=False)   # A's current song position
    bpm_a: float = Field(gt=0, le=400, allow_inf_nan=False)   # A as it plays (pitch fader): informational;
                                                                # the plan is in song time on A's analysed BPM


# The silent ear's audio work (render clips from stems + rate them, up to tens of
# seconds) runs as background jobs, not inside the request thread: the POST starts
# or joins a job keyed by its inputs and answers {"status": "pending", "job": id} at
# once (or the result, when that job already finished); GET .../{job} answers the
# same pending body until the result lands. ONE ear job at a time for both kinds
# (they share the one model and the CPU), identical requests share one job.
# ttl 120 s = preplan.UNHEARD_TTL_S: an unheard answer is not reused longer than before
# (heard ones sit in the preplan / audition disk caches and come back in ms).
_ear_jobs = JobRunner("ear", workers=1, ttl_s=120.0, queue_ttl_s=120.0, max_pending=8)


def _job_answer(job, kind: str, fresh=None):
    """A job's HTTP answer: the unchanged final result when done, else the pending body."""
    if job is None or not job.key.startswith(kind + ":"):
        raise HTTPException(status_code=404, detail="unknown or expired job")
    if job.status == _JOB_DONE:
        return fresh(job.result) if fresh else job.result
    if job.status == _JOB_ERROR:
        raise HTTPException(status_code=500, detail=f"{kind} failed: {job.error}")
    if job.status == _JOB_EXPIRED:
        raise HTTPException(status_code=404, detail="job expired before it ran")
    return job.pending_body()


def _preplan_fresh(now: Optional[float]):
    """A finished plan whose B start is already too close to A's playhead is no plan."""
    from app.music_brain.render.preplan import MIN_LEAD_S

    def check(res: dict) -> dict:
        p = res.get("plan") if isinstance(res, dict) else None
        if now is not None and p and float(p.get("a_in") or 0.0) < now + MIN_LEAD_S:
            return {"ok": False, "plan": None, "candidates": [], "ear": False,
                    "why": "the planned start is already too close to A's playhead"}
        return res
    return check


def _run_preplan(req: PreplanRequest, sa: dict, sb: dict) -> dict:
    from app.music_brain.render import preplan
    from app.music_brain.matching import techniques as tq
    from app.ui.services import session_log

    ta, tb = analyze_track(_track_path(req.a_id)), analyze_track(_track_path(req.b_id))
    ka, kb = ta.key.camelot if ta.key else None, tb.key.camelot if tb.key else None
    t0 = time.time()
    # Song-time maths on A's ANALYSED tempo: a_in / handover sit on A's phrase grid in
    # song seconds, and the clip plays A unstretched, so B is stretched to ta.bpm. The
    # live pitch fader moves both decks' audio together (B follows A) and never the grid.
    res = preplan.preplan(ta.to_dict(), tb.to_dict(), sa, sb, ta.bpm or 120.0, tb.bpm or 120.0,
                          req.lo, req.hi, req.now, tq.camelot_score(ka, kb) if ka and kb else None,
                          b_rap=any(r.get("rap") for r in (_voiced_cache.get((req.b_id, "style")) or [])), cache_key=f"{req.a_id}:{req.b_id}")
    p = res.get("plan") or {}
    session_log.log("ear_preplan", elapsed=round(time.time() - t0, 2), ok=res["ok"], heard=res.get("ear"),
                    candidates=len(res.get("candidates") or []), a_in=p.get("a_in"), b_start=p.get("b_start"),
                    bars=p.get("bars"), label=p.get("label"), direction=p.get("direction"))
    _song_step("preplan", req.b_id, phase="planning", decision=p.get("label") if res.get("ok") else "no plan",
               why=res.get("why") or p.get("direction"), inputs={"a_id": req.a_id, "lo": req.lo, "hi": req.hi},
               result={"a_in": p.get("a_in"), "b_start": p.get("b_start"), "bars": p.get("bars"),
                       "ear": bool(res.get("ear")), "candidates": len(res.get("candidates") or [])})
    return res


@app.post("/api/transition/preplan")
def post_transition_preplan(req: PreplanRequest):
    """The silent ear pre-plans the whole transition (app.music_brain.render.preplan):
    when B starts inside A, from which of B's lines, for how long both play and
    which deck owns each stem; rendered offline and heard before the master plays it.
    Starts or joins a background job: {"status": "pending", "job": id} until
    GET /api/transition/preplan/{job} returns the plan (same shape as before)."""
    sa, sb = _cached_stems4(req.a_id), _cached_stems4(req.b_id)
    if not sa or not sb:
        raise HTTPException(status_code=409, detail="both songs need their 4 stems cached")
    # The window and tempo define the plan (preplan's own cache key); `now` only moves
    # the earliest usable start, checked against the finished plan below.
    key = f"preplan:{req.a_id}:{req.b_id}:{round(req.lo)}:{round(req.hi)}:{round(req.bpm_a, 1)}"
    job = _ear_jobs.submit(key, lambda: _run_preplan(req, sa, sb))
    if job is None:
        raise HTTPException(status_code=503, detail="the silent ear is busy: too many queued jobs")
    fresh = _preplan_fresh(req.now)
    if job.status == _JOB_DONE and fresh(job.result) is not job.result:
        _ear_jobs.forget(job.id)          # stale for this playhead: plan again from here
        job = _ear_jobs.submit(key, lambda: _run_preplan(req, sa, sb))
        if job is None:
            raise HTTPException(status_code=503, detail="the silent ear is busy: too many queued jobs")
    return _job_answer(job, "preplan", fresh)


@app.get("/api/transition/preplan/{job_id}")
def get_transition_preplan(job_id: str, now: Optional[float] = None):
    """A preplan job: {"status": "pending", "job": id} while it runs, then the plan.
    `now` (A's current song position) drops a plan whose start is already too close."""
    if now is not None and not (0.0 <= now <= 36000.0):      # also rejects NaN
        raise HTTPException(status_code=400, detail="now must be A's song seconds (0-36000)")
    return _job_answer(_ear_jobs.get(job_id), "preplan", _preplan_fresh(now))


class MergeAuditionRequest(BaseModel):
    a_id: str
    b_id: str
    a_time: float = Field(ge=0, le=36000, allow_inf_nan=False)   # A's song seconds where the merge starts
    b_time: float = Field(ge=0, le=36000, allow_inf_nan=False)   # B's song seconds at that moment
    combos: List[Dict[str, str]]     # up to 3, from stem-moves mergeRank (console)


@app.post("/api/merge/audition")
def post_merge_audition(req: MergeAuditionRequest):
    """The silent ear on candidate song merges (app.music_brain.render.merge): each combo is
    rendered offline from the cached stems (B key-locked to A's tempo) and the local
    omni model rates it. Advisory and cached; {"results": [...], "ear": bool}.
    Starts or joins a background job: {"status": "pending", "job": id} until
    GET /api/merge/audition/{job} returns that result."""
    from app.music_brain.render import merge

    sa, sb = _cached_stems4(req.a_id), _cached_stems4(req.b_id)
    if not sa or not sb:
        raise HTTPException(status_code=409, detail="both songs need their 4 stems cached")
    ok = [c for c in req.combos[:3] if set(c) == set(merge.ROLES) and set(c.values()) <= {"a", "b"}]
    if not ok:
        raise HTTPException(status_code=400, detail="combos must map drums/bass/vocals/other to 'a' or 'b'")
    a_time, b_time = max(0.0, req.a_time), max(0.0, req.b_time)

    def run() -> dict:
        from app.ui.services import session_log

        ta, tb = analyze_track(_track_path(req.a_id)), analyze_track(_track_path(req.b_id))
        t0 = time.time()
        res = merge.audition(sa, sb, ok, a_time, b_time, ta.bpm or 120.0, tb.bpm or 120.0,
                             key=f"{req.a_id}:{req.b_id}")
        session_log.log("ear_merge", elapsed=round(time.time() - t0, 2), combos=len(ok),
                        heard=sum(1 for r in res if r["ear"]), best=max((r["ear"]["score"] for r in res if r["ear"]), default=None))
        _song_step("merge_audition", req.b_id, phase="planning", decision=f"{len(ok)} combo(s) auditioned",
                   inputs={"a_id": req.a_id, "a_time": a_time, "b_time": b_time, "combos": ok},
                   result=[(r.get("ear") or {}).get("score") for r in res])
        return {"results": res, "ear": any(r["ear"] for r in res)}

    key = "audition:" + json.dumps([req.a_id, req.b_id, round(a_time, 2), round(b_time, 2), ok], sort_keys=True)
    job = _ear_jobs.submit(key, run)
    if job is not None and job.status == _JOB_DONE and not (job.result or {}).get("ear"):
        _ear_jobs.forget(job.id)          # unheard answers were never reused: ask again
        job = _ear_jobs.submit(key, run)
    if job is None:
        raise HTTPException(status_code=503, detail="the silent ear is busy: too many queued jobs")
    return _job_answer(job, "audition")


@app.get("/api/merge/audition/{job_id}")
def get_merge_audition(job_id: str):
    """A merge audition job: {"status": "pending", "job": id} while it runs, then the result."""
    return _job_answer(_ear_jobs.get(job_id), "audition")


@app.get("/api/learned/pick")
def get_learned_pick(a: str, b: str, keylock: bool = False, profile: str = ""):
    """The move learned from studied sets to play for A -> B, as a console recipe
    ({kind, recipe, seen, source, reasons, rules}), or {"pick": null}. The console
    only switches to it when that recipe is already allowed for the pair.
    profile: the Punjabi scene profile level the console resolved for this pair
    ("full" | "handover"; anything else = none, today's pick). Under "full" the pick
    also reads the Punjabi-tagged sets (techniques.learned_pick)."""
    from app.music_brain.analysis import scene_profile as sp
    from app.music_brain.matching import techniques as tq

    lvl = profile if profile in (sp.LEVEL_FULL, sp.LEVEL_HANDOVER) else None
    f = _pair_features_cached(a, b, keylock)
    pick = tq.learned_pick(tq.rank(f, scene=sp.learned_scene(lvl)), key_score=tq.camelot_score(f.key_a, f.key_b),
                           level=lvl, tempo_gap=f.tempo_gap if lvl else None)
    p = pick or {}
    why = "; ".join(map(str, [x for x in (p.get("clash"), p.get("degraded")) if x] + list(p.get("reasons") or [])))
    _song_step("learned_pick", b, phase="planning", decision=p.get("recipe") or "none",
               why=why[:300] or None, inputs={"a_id": a, "keylock": keylock, **({"profile": lvl} if lvl else {})})
    return {"pick": pick}


@app.get("/api/learned/moves")
def get_learned_moves():
    """The in-song learned moves (vocal loop / re-cut / chops, loop extend): per kind whether the
    console may play it (sighted, not disabled), the user's rules and the sightings' parameters."""
    from app.music_brain.matching import techniques as tq

    return {"moves": tq.learned_moves()}


@app.get("/api/learn/progress")
def get_learn_progress():
    """Set studies (agent_bridge learn-set), newest first: each with state running|done|error|stale,
    elapsed_s and, while running, a rough eta_s. Read-only; nothing here starts a study."""
    from app.music_brain.learning import learn_progress as lp

    return {"studies": lp.read_all()}


@app.get("/api/learn/progress/{set_id}")
def get_learn_progress_one(set_id: str):
    from app.music_brain.learning import learn_progress as lp

    d = lp.read_one(set_id)
    if d is None:
        raise HTTPException(status_code=404, detail="no such study")
    return d


@app.get("/api/techniques")
def get_techniques(a: str, b: str, keylock: bool = False):
    """Which learned techniques fit A -> B, each with its reasons (app.music_brain.matching.techniques)."""
    from app.music_brain.matching import techniques as tq

    f = _pair_features(a, b, keylock)
    return {"features": {"tempo_gap": round(f.tempo_gap, 4), "key_score": f.key, "b_style": _voiced_cache.get((b, "style")), "b_rap_at": f.b_rap_at,
                         "grooves": f.a_grooves, "breakdowns": f.a_breakdowns, "stems": [f.stems_a, f.stems_b]},
            "techniques": tq.rank(f)}


class RiffRequest(BaseModel):
    a_id: str
    b_id: str
    not_before: float = 0.0          # A's current position + lead: the groove must start after it


@app.post("/api/riff/plan")
def post_riff_plan(req: RiffRequest):
    """Riff over rap, live: pick A's groove + breakdown and B's rap entry, and
    start the key-locked render of A's stems at B's tempo (cached). Always
    answers; ok=False carries the reasons (the normal transition runs)."""
    import librosa
    import numpy as np

    from app.music_brain.audio import keylock
    from app.music_brain.audio import stem_service
    from app.music_brain.matching import techniques as tq
    from app.music_brain.analysis import waveform_params as wp

    if not keylock.available():
        return {"ok": False, "reasons": ["Rubber Band not installed (brew install rubberband)"]}
    f = _pair_features(req.a_id, req.b_id, keylock=True)
    fit = next(x for x in tq.rank(f) if x["name"] == "riff_over_rap")
    if not fit["fits"]:
        return {"ok": False, "reasons": [r for r in fit["reasons"] if r.startswith("no:")]}
    a = analyze_track(_track_path(req.a_id)).to_dict()
    b = analyze_track(_track_path(req.b_id)).to_dict()
    sb = _cached_stems4(req.b_id)

    def b_bass_db(lo, hi):
        y, sr = _load_audio(sb["bass"], sr=11025, mono=True, offset=lo, duration=hi - lo)
        return float(10 * np.log10(np.mean(y ** 2) + 1e-12))

    plan = keylock.choose(a, b, f.a_grooves, f.a_breakdowns, f.b_rap_at, b_bass_db, req.not_before)
    if not plan["ok"]:
        return plan
    # B's entry skips a repeated opening hook (rhythm repeats bar after bar)
    yv, srv = _load_audio(sb["vocals"], sr=22050, mono=True)
    rep = tq.repetitive_bars(yv, srv, b["downbeat_times"])
    entry = tq.skip_repetitive_intro(rep, b["downbeat_times"], b["phrase_boundaries_8bar"], plan["b_entry"])
    plan["b_skipped_hook_s"] = round(entry - plan["b_entry"], 2)
    plan["b_entry"] = plan["b_start"] = entry
    # The mashup runs 32 bars when B keeps rapping through it (the vibe holds), else 16
    vr = _cached_vocal_regions(req.b_id) or []
    win_lo = plan["b_entry"]
    win_hi = win_lo + keylock.MASHUP_BARS_LONG * plan["bar_s"]
    cov = sum(max(0.0, min(e, win_hi) - max(s, win_lo)) for s, e in vr) / (win_hi - win_lo)
    long_ok = cov >= keylock.MASHUP_LONG_VOCAL and b["duration"] >= win_hi + keylock.BLEND_BARS * plan["bar_s"] + 20
    plan["timeline"] = keylock.timeline(keylock.MASHUP_BARS_LONG if long_ok else keylock.MASHUP_BARS_SHORT)
    plan["mashup_vibe"] = {"b_rap_coverage": round(cov, 2), "long": long_ok}
    # the rap moves (hold on, A's drop out) sit on B's own measured lines; the mashup length above already was
    plan["timeline"], line_src = keylock.measured_lines(plan["timeline"], wp.profile(sb["vocals"]), plan["b_entry"], plan["bar_s"])
    plan["param_sources"] = {"mashup_bars": "measured" if vr else "fallback", **line_src}
    wp.note("riff_lines", plan["param_sources"], fit=plan["timeline"].get("fit"))
    dl = keylock.drop_clear(plan["timeline"])      # "never vocal mix a drop line": A's drop window plays clean
    if dl:
        return {"ok": False, "reasons": [dl]}
    key, state = keylock.ensure(stem_service.file_hash(_track_path(req.a_id)), _cached_stems4(req.a_id), plan)
    # B's levels where it drops (16 bars from the rap), for the balance
    lo, hi = plan["b_entry"], plan["b_entry"] + 16 * plan["bar_s"]
    lv = {n: _load_audio(sb[n], sr=11025, mono=True, offset=lo, duration=hi - lo)[0] for n in keylock.STEMS}
    plan["b_levels"] = {"b_mix_db": keylock.rms_db(sum(lv.values())), "b_vocals_db": keylock.rms_db(lv["vocals"]),
                        "b_bass_db": keylock.rms_db(lv["bass"])}
    plan.update(key=key, state=state, why=fit["reasons"],
                stems={n: f"/api/riff/{key}/{n}" for n in keylock.STEMS},
                detune_avoided_st=round(keylock.pitch_shift_semitones(plan["ratio"]), 2))
    return plan


@app.get("/api/riff/{key}")
def get_riff_state(key: str):
    from app.music_brain.audio import keylock

    return {"state": keylock.state(key), "meta": keylock.meta(key)}


@app.post("/api/riff/{key}/balance")
def post_riff_balance(key: str, b_levels: dict):
    """Gains for a rendered riff (A level-matched, B's rap and bass under the riff)."""
    from app.music_brain.audio import keylock

    m = keylock.backfill_voice_band(key)
    if not m or "a_mix_db" not in m:
        raise HTTPException(status_code=404, detail="riff not rendered")
    try:
        return keylock.balance(m, {k: float(b_levels[k]) for k in ("b_mix_db", "b_vocals_db", "b_bass_db")})
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"b_levels: {exc}") from exc


@app.get("/api/riff/{key}/{name}")
def get_riff_stem(key: str, name: str):
    from app.music_brain.audio import keylock

    p = _host().keylock_stem_path(key, name)
    if not p:
        raise HTTPException(status_code=404, detail="key-locked stem not rendered")
    from app.music_brain.audio.audio_io import media_type
    return FileResponse(p, media_type=media_type(p))


@app.get("/api/tracks/{track_id}/stems/{name}")
def get_track_stem_audio(track_id: str, name: str):
    if name not in STEM_NAMES:
        raise HTTPException(status_code=404, detail="unknown stem")
    stems = _cached_stems4(track_id)
    if not stems:
        raise HTTPException(status_code=404, detail="stems not separated yet")
    from app.music_brain.audio.audio_io import media_type
    return FileResponse(stems[name], media_type=media_type(stems[name]))


_fame: Dict[str, dict] = {}
FAME_PATH = CACHE_DIR / "fame.json"
FAMOUS_VIEWS = 20_000_000   # the room knows it (leavemealone 36M, played 7 min in the USB002 set)


@app.get("/api/tracks/{track_id}/fame")
def get_track_fame(track_id: str):
    """How well known the song is: YouTube views of its best matching upload."""

    _track_path(track_id)
    if not _fame and FAME_PATH.exists():
        try:
            _fame.update(json.loads(FAME_PATH.read_text()))
        except ValueError:
            pass
    if track_id in _fame:
        return _fame[track_id]
    name = _track_names.get(track_id) or ""
    views = _host().song_views(name) if len(name) >= 3 else None
    res = {"views": views, "famous": bool(views and views >= FAMOUS_VIEWS), "name": name}
    if views is not None:                    # only cache real answers
        _fame[track_id] = res
        FAME_PATH.write_text(json.dumps(_fame))
    return res


@app.post("/api/match")
def post_match(req: MatchRequest):
    import dataclasses

    tracks = []
    for track_id in (req.track_a_id, req.track_b_id):
        track = analyze_track(_track_path(track_id))
        try:
            regions = _cached_vocal_regions(track_id)
        except Exception as exc:  # best-effort: never break matching
            print(f"[match] vocal lookup failed for {track_id}: {exc}", flush=True)
            regions = None
        if regions:
            track = dataclasses.replace(track, vocal_active_regions=[tuple(r) for r in regions])
        tracks.append(track)
    track_a, track_b = tracks
    vibe_kw = _pair_vibe(req.track_a_id, req.track_b_id)
    # Punjabi scene profile level for this pair (scene_profile.level); "off" -> None
    from app.music_brain.analysis import scene_profile as _sp
    profile = _sp.level(req.punjabi_profile, vibe_kw.get("genre_a"), vibe_kw.get("genre_b"))
    candidates = _matcher.match(
        track_a, track_b, top_n=req.top_n, no_cuts=req.no_cuts, profile=profile, **vibe_kw,
    )
    # Measured vibe continuity (loudness / brightness / onset density / energy).
    # Best-effort: a vibe failure must never break matching.
    vibe = None
    try:
        from app.music_brain.analysis.vibe import analyze_vibe, vibe_distance
        vibe = vibe_distance(
            analyze_vibe(_track_path(req.track_a_id)),
            analyze_vibe(_track_path(req.track_b_id)),
        )
    except Exception:
        vibe = None
    # Measured energy 1-10 of both (app.music_brain.analysis.energy): the console refuses a
    # next song more than 2 levels away (1 when relaxed).
    try:
        from app.music_brain.analysis import energy as en

        la = en.level(_track_path(req.track_a_id), track_a.bpm)
        lb = en.level(_track_path(req.track_b_id), track_b.bpm)
        vibe = (vibe or {}) | {"energy_a": la["level"], "energy_b": lb["level"],
                               "energy_raw_a": la["raw"], "energy_raw_b": lb["raw"]}
    except Exception as exc:          # best-effort, but say why
        print(f"WARNING [match] energy level unavailable: {type(exc).__name__}: {exc}", flush=True)
    out = [c.to_dict() for c in candidates]
    _song_step("match", req.track_b_id, decision=(out[0].get("recipe") if out else "no candidates"),
               why=(out[0].get("explanation") if out else None), inputs={"a_id": req.track_a_id},
               result={"candidates": [{k: c.get(k) for k in ("recipe", "score", "a_time", "b_time")} for c in out[:5]],
                       "vibe": vibe})
    return {"candidates": out, "vibe": vibe}


class MashupRequest(BaseModel):
    host_id: str
    guest_id: str
    bars: int = 8
    host_mutable: bool = False      # host deck has live stems: its vocal can be muted


def _vocals_stem(track_id: str) -> str:
    return _host().vocals_stem(track_id)


def _vocals_stem_impl(track_id: str) -> str:
    """The ft 4-stem vocals when the song has a complete htdemucs_ft set (non-ft folders are
    pruned once it does), else a fast 2-stem run."""
    from app.music_brain.audio.stem_service import ft_vocals
    from app.music_brain.render.mashup import MASHUP_DEMUCS_MODEL

    ft = ft_vocals(_track_path(track_id))
    if ft:
        return ft
    result = separate_stems(_track_path(track_id), two_stems="vocals", model=MASHUP_DEMUCS_MODEL)
    path = result.stems.get("vocals")
    if not path:
        raise RuntimeError("vocal stem missing after separation")
    return path


_vocal_regions: Dict[str, list] = {}


def _vocal_regions_for(track_id: str) -> Optional[list]:
    """Cached Demucs vocal-activity regions for a track, or None if separation fails."""
    if track_id in _vocal_regions:
        return _vocal_regions[track_id]
    try:
        from app.music_brain.analysis.analyzer import vocal_presence_map

        regions = [list(r) for r in vocal_presence_map(Path(_vocals_stem(track_id)))]
    except Exception as exc:
        print(f"[blend] vocal map unavailable for {track_id}: {exc}", flush=True)
        return None
    _vocal_regions[track_id] = regions
    return regions


class BlendRequest(BaseModel):
    a_id: str
    b_id: str
    window_lo: float
    window_hi: float
    a_bpm_effective: Optional[float] = None
    bars: int = 16
    entry_mode: str = "match"       # "drop": enter on B's first long drop (peak moves)
    a_entry: Optional[float] = None  # where the playing song came in (its drop must play first)


@app.post("/api/blend/plan")
def post_blend_plan(req: BlendRequest):
    """Beat-to-beat blend: vocal-free exit phrase in A, vocal-free entry phrase
    in B, and the playback rate that locks B's tempo to A's."""
    from app.music_brain.render.blend import ALLOWED_BARS, ENTRY_MODES, plan_blend

    if req.bars not in ALLOWED_BARS:
        raise HTTPException(status_code=400, detail=f"bars must be one of {list(ALLOWED_BARS)}")
    if req.entry_mode not in ENTRY_MODES:
        raise HTTPException(status_code=400, detail=f"entry_mode must be one of {list(ENTRY_MODES)}")
    if not (0 <= req.window_lo <= req.window_hi <= 3600):
        raise HTTPException(status_code=400, detail="bad play window")
    a = analyze_track(_track_path(req.a_id))
    b = analyze_track(_track_path(req.b_id))
    return plan_blend(
        a, b, req.window_lo, req.window_hi,
        a_bpm_effective=req.a_bpm_effective,
        a_vocals=_vocal_regions_for(req.a_id),
        b_vocals=_vocal_regions_for(req.b_id),
        bars=req.bars,
        entry_mode=req.entry_mode,
        a_entry=req.a_entry,
    )


@app.post("/api/mashup/plan")
def post_mashup_plan(req: MashupRequest):
    """Plan guest-vocal-over-host-beat ("A x B"). Separates vocals (cached) only
    after the key/tempo checks pass, so incompatible pairs return quickly."""
    from app.music_brain.render.mashup import ALLOWED_BARS, plan_mashup

    if req.bars not in ALLOWED_BARS:
        raise HTTPException(status_code=400, detail=f"bars must be one of {list(ALLOWED_BARS)}")
    if req.host_id == req.guest_id:
        raise HTTPException(status_code=400, detail="host and guest must differ")
    host = analyze_track(_track_path(req.host_id))
    guest = analyze_track(_track_path(req.guest_id))
    try:
        plan = plan_mashup(
            host, guest,
            host_vocals_path=lambda: _vocals_stem(req.host_id),
            guest_vocals_path=lambda: _vocals_stem(req.guest_id),
            bars=req.bars,
            host_mutable=req.host_mutable,
            genres=(_track_vibe(req.host_id)["genre"], _track_vibe(req.guest_id)["genre"]),   # scene gate
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"mashup plan error: {exc}") from exc
    if plan.get("ok"):
        plan["guest_vocal_url"] = (
            f"/api/audio/stems/{req.guest_id}/vocals"
            f"?start={plan['guest_start']}&dur={plan['guest_duration']}"
        )
    return plan


class LayerRequest(BaseModel):
    a_id: str
    b_id: str
    window_lo: float
    window_hi: float
    a_bpm_effective: Optional[float] = None
    a_entry: Optional[float] = None
    max_hold_bars: int = 32
    unwind_bars: int = 8
    third_ids: List[str] = []      # next-next songs / earlier songs: vocal stem as a third layer


def _vocals_cached(track_id: str) -> bool:
    """True when the track's vocal stem is already separated (never runs Demucs)."""
    if track_id in _vocal_regions:
        return True
    from app.music_brain.render.mashup import MASHUP_DEMUCS_MODEL
    from app.music_brain.audio.stem_service import _cache_dir_for, _load_from_cache, file_hash

    from app.music_brain.audio.stem_service import complete_ft

    try:
        h = file_hash(Path(_track_path(track_id)))
        return (complete_ft(h) is not None
                or _load_from_cache(_cache_dir_for(h, MASHUP_DEMUCS_MODEL, "vocals")) is not None)
    except Exception:
        return False


def _layer_third(req: LayerRequest, a, b, layer: dict) -> Optional[dict]:
    """Vocal stem of a third song over the layer: cached stems only, key and
    tempo fit BOTH playing songs, on a B phrase with no vocal from A or B."""
    from app.music_brain.render.blend import tempo_lock
    from app.music_brain.render.layer import key_fits, pitch_fits, vocal_clash
    from app.music_brain.render.mashup import MAX_RATE_DEVIATION, plan_mashup

    a_eff = req.a_bpm_effective or a.bpm
    a_key = a.key.camelot if a.key else ""
    a_vocals = _vocal_regions.get(req.a_id) or []
    lock = tempo_lock(a_eff, b.bpm)
    if lock is None:
        return None
    b_bar = 240.0 / (b.bpm * lock[1])
    a_per_b = (240.0 / a.bpm) / b_bar
    bars = 16 if layer["hold_bars"] >= 32 else 8
    seen = {req.a_id, req.b_id}
    for gid in req.third_ids[:8]:
        if gid in seen:
            continue
        seen.add(gid)
        if not _vocals_cached(gid):
            continue
        try:
            guest = analyze_track(_track_path(gid))
        except Exception:
            continue
        g_key = guest.key.camelot if guest.key else ""
        if not key_fits(a_key, g_key) or not pitch_fits(a_eff, guest.bpm, MAX_RATE_DEVIATION):
            continue
        try:
            plan = plan_mashup(b, guest, host_vocals_path=lambda: _vocals_stem(req.b_id),
                               guest_vocals_path=lambda gid=gid: _vocals_stem(gid), bars=bars,
                               genres=(_track_vibe(req.b_id)["genre"], _track_vibe(gid)["genre"]))   # scene gate
        except Exception as exc:
            print(f"[layer] third layer {gid} skipped: {exc}", flush=True)
            continue
        if not plan.get("ok"):
            continue
        lo = layer["entry"] + 8 * b_bar                       # B settled as texture first
        hi = layer["b_end"] - plan["host_duration"]
        for h in plan["host_entries"]:
            if not lo - 0.01 <= h <= hi + 0.01:
                continue
            # A's vocal (on A's clock) must not sit under the guest vocal
            a_start = layer["start"] + (h - layer["entry"]) * a_per_b
            if vocal_clash(a_vocals, [(h, h + plan["host_duration"])], a_start, h,
                           plan["host_duration"] * a_per_b, a_per_b) > 0.0:
                continue
            plan["guest_vocal_url"] = (f"/api/audio/stems/{gid}/vocals"
                                       f"?start={plan['guest_start']}&dur={plan['guest_duration']}")
            return {"guest_id": gid, "host_entry": round(h, 3), "plan": plan}
    return None


@app.post("/api/layer/plan")
def post_layer_plan(req: LayerRequest):
    """LAYER transition: B under A as a texture for 16-64 bars, bass to B on a
    phrase line, A unwound over 8-16 bars; optional third vocal-stem layer."""
    from app.music_brain.render.layer import LAYER_HOLD_BARS, LAYER_UNWIND_BARS, plan_layer

    if req.max_hold_bars not in LAYER_HOLD_BARS:
        raise HTTPException(status_code=400, detail=f"max_hold_bars must be one of {list(LAYER_HOLD_BARS)}")
    if req.unwind_bars not in LAYER_UNWIND_BARS:
        raise HTTPException(status_code=400, detail=f"unwind_bars must be one of {list(LAYER_UNWIND_BARS)}")
    if not (0 <= req.window_lo <= req.window_hi <= 3600):
        raise HTTPException(status_code=400, detail="bad play window")
    if req.a_id == req.b_id:
        raise HTTPException(status_code=400, detail="a and b must differ")
    a = analyze_track(_track_path(req.a_id))
    b = analyze_track(_track_path(req.b_id))
    layer = plan_layer(
        a, b, req.window_lo, req.window_hi,
        a_bpm_effective=req.a_bpm_effective,
        a_vocals=_vocal_regions_for(req.a_id),
        b_vocals=_vocal_regions_for(req.b_id),
        max_hold_bars=req.max_hold_bars,
        unwind_bars=req.unwind_bars,
        a_entry=req.a_entry,
    )
    if layer.get("ok"):
        layer["groove"] = True
        try:
            layer["third"] = _layer_third(req, a, b, layer)
        except Exception as exc:  # the third layer is optional
            print(f"[layer] third layer failed: {exc}", flush=True)
            layer["third"] = None
    return layer


class BridgeRequest(BaseModel):
    from_bpm: float
    to_bpm: float
    max_step_pct: float = 6.0
    max_steps: int = 7


@app.post("/api/bridge/plan")
def post_bridge_plan(req: BridgeRequest):
    """BRIDGE PATH: BPM ladder (<= max_step_pct per song, half/double links)."""
    from app.music_brain.render.bridge import bridge_ladder

    try:
        return bridge_ladder(req.from_bpm, req.to_bpm, req.max_step_pct, req.max_steps)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/audio/stems/{track_id}/vocals")
def get_vocal_clip(track_id: str, start: float = 0.0, dur: float = 30.0):
    """A trimmed slice of a track's separated vocal stem (small WAV for the browser)."""
    import soundfile as sf
    from app.music_brain.config import PREVIEWS_CACHE_DIR

    if not (0.0 <= start <= 3600.0) or not (0.5 <= dur <= 120.0):
        raise HTTPException(status_code=400, detail="start must be 0-3600 s, dur 0.5-120 s")
    _track_path(track_id)  # 404 on unknown id; id is a validated registry key from here on
    PREVIEWS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = PREVIEWS_CACHE_DIR / f"{track_id}_vocals_{start:.3f}_{dur:.3f}.wav"
    if not out.exists():
        try:
            stem = _vocals_stem(track_id)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"separation error: {exc}") from exc
        info = sf.info(stem)
        first = int(start * info.samplerate)
        data, sr = sf.read(stem, start=first, frames=int(dur * info.samplerate), always_2d=True)
        sf.write(out, data, sr)
    return FileResponse(out, media_type="audio/wav")


@app.post("/api/preview")
def post_preview(req: PreviewRequest):
    track_a_path = _track_path(req.track_a_id)
    track_b_path = _track_path(req.track_b_id)

    track_a = analyze_track(track_a_path)
    track_b = analyze_track(track_b_path)

    try:
        candidate = _matcher.resolve_candidate(
            track_a, track_b,
            recipe_name=req.recipe, a_time=req.a_time, b_time=req.b_time,
            **_pair_vibe(req.track_a_id, req.track_b_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    output_filename = f"preview_{req.track_a_id}_{req.track_b_id}_{candidate.recipe.slug}_{int(candidate.a_time)}_{int(candidate.b_time)}.mp3"
    output_path = CACHE_DIR / "previews" / output_filename
    result = render_preview(
        track_a_path, track_b_path, candidate,
        output_path=output_path, preview_seconds=req.preview_seconds,
    )
    return {
        "recipe": candidate.recipe.name,
        "explanation": candidate.explanation,
        "score": candidate.score,
        "a_time": candidate.a_time,  # actual (phrase-snapped) point used, for the UI to show where it landed
        "b_time": candidate.b_time,
        "audio_url": f"/api/audio/previews/{output_filename}",
        "duration_seconds": result.duration_seconds,
        "render_time_seconds": result.render_time_seconds,
        "peak_dbfs": result.peak_dbfs,
    }


@app.get("/api/audio/previews/{filename}")
def get_preview_audio(filename: str):
    path = CACHE_DIR / "previews" / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Preview not found")
    return FileResponse(path, media_type="audio/mpeg")


@app.post("/api/render")
def post_render(req: RenderRequest):
    """Renders the full two-track mix (not just a 15-30s preview) for the
    currently loaded deck pair, using the same recipe-resolution rules as
    /api/preview so a manually-picked point or recipe carries over exactly.
    """
    track_a_path = _track_path(req.track_a_id)
    track_b_path = _track_path(req.track_b_id)

    track_a = analyze_track(track_a_path)
    track_b = analyze_track(track_b_path)

    try:
        candidate = _matcher.resolve_candidate(
            track_a, track_b,
            recipe_name=req.recipe, a_time=req.a_time, b_time=req.b_time,
            **_pair_vibe(req.track_a_id, req.track_b_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    output_filename = f"render_{req.track_a_id}_{req.track_b_id}_{candidate.recipe.slug}_{int(candidate.a_time)}_{int(candidate.b_time)}.mp3"
    output_path = CACHE_DIR / "renders" / output_filename
    result = render_full_mix(
        [track_a_path, track_b_path], [candidate],
        output_path=output_path, bpm=track_a.bpm,
    )
    return {
        "recipe": candidate.recipe.name,
        "explanation": candidate.explanation,
        "a_time": candidate.a_time,
        "b_time": candidate.b_time,
        "audio_url": f"/api/audio/renders/{output_filename}",
        "duration_seconds": result.duration_seconds,
        "render_time_seconds": result.render_time_seconds,
        "peak_dbfs": result.peak_dbfs,
    }


@app.get("/api/audio/renders/{filename}")
def get_render_audio(filename: str):
    path = CACHE_DIR / "renders" / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Render not found")
    return FileResponse(path, media_type="audio/mpeg")


class AutopilotSuggestRequest(BaseModel):
    track_id: str
    # One id per set per browser tab (autopilot.js setId). Scopes the cross-set
    # memory so this set's own songs are never "earlier sets" and another tab's
    # set never leaks in; also keeps two tabs from sharing one cached answer.
    set_id: str = ""
    occasion: Optional[str] = None
    history: list[str] = []
    set_position: Optional[float] = None  # 0.0=start, 1.0=end; computed from history if omitted
    set_mode: str = "hybrid"  # long | quick | hybrid
    # Punjabi scene profile (app/music_brain/analysis/scene_profile.py): off | on | auto.
    # Absent -> "off" so older clients get today's behaviour.
    punjabi_profile: str = "off"
    # SCENE ANCHOR (autopilot.js sceneAnchorNext): the set's scene while the playing song is an
    # off-scene mistake (a fallback / BAD PAIR brought it in). "" = no recovery.
    scene_anchor: str = Field(default="", max_length=80)
    # Relaxed session (autopilot.js RELAXED_OCCASION): picks never lift the energy.
    relaxed: bool = False
    # "dip": the set has sat near its loudness peak for a while, so ask for a
    # track that lets energy fall back before building again (set study rule 9).
    energy_note: Optional[str] = None
    # "reprise" only: display name of the set's recurring hook (dj-mind.js
    # motifHook, set study mDtud5fLgFQ section 5). Ignored for other notes.
    energy_hook: Optional[str] = None
    # Measured 1-10 levels of the songs played so far (oldest first, playing one last):
    # the cumulative-fall rule (energy.next_ok) needs the set's recent peak.
    energy_history: list[float] = []
    # Look-ahead (songs for AFTER the booked next one): lowest LLM priority,
    # waits behind any transition plan (app/ui/services/llm_gate.py).
    lookahead: bool = False
    # Variety: how many songs in a row were the same subgenre, and which one.
    variety_run: int = 0
    variety_genre: str = ""
    # Set position by clock: elapsed / length when elapsed is sent (length
    # defaults to DEFAULT_SET_LENGTH_S); otherwise set_position / history length.
    elapsed_seconds: Optional[float] = None
    set_length_seconds: Optional[float] = None
    # Bridge ladder step (bridge.py): aim the next song at this tempo instead of
    # the current one. Tempo only - never an occasion, theme lock or steering.
    tempo_target: Optional[float] = None
    tempo_note: Optional[str] = None
    # LEAD TO (user destination): "Artist - Title" / artist / genre, step k of N
    lead_to: str = ""
    # titles rejected this round (download failed / vibe gate): never suggested
    # again this round, but NOT played - kept out of the cross-set memory
    avoid: list[str] = []
    # Songs already booked after this one (the console's queue, in play order): shown
    # to the model beside the last 3 played (autopilot_service.prompt_history).
    queue: list[str] = []
    lead_step: int = 0
    lead_steps: int = 0
    lead_bpm: Optional[float] = None


DEFAULT_SET_LENGTH_S = 3600.0


VARIETY_RUN_MAX = 6  # wiki "What Do I Play Next": contrast once a style plateaus


def _variety_note(run: int, genre: str) -> str:
    if run < VARIETY_RUN_MAX or not genre:
        return ""
    return (f"the last {run} songs were all {genre[:40]} and the floor is getting bored - "
            f"take ONE step to a NEIGHBOURING subgenre (a crossover song that shares the current genre) that still fits the occasion (e.g. bhangra -> "
            f"Punjabi hip-hop / Punjabi pop / bhangra-house / Bollywood dance; melodic house -> "
            f"afro house / tech house / UK garage), entered through a beat-matched remix or bridge, "
            f"energy kept up; do NOT suggest another {genre[:40]} song")


_ENERGY_NOTES = {
    "dip": "the set has been at peak energy for a while - pick something that lets "
           "the energy dip a little before building again, not another peak",
    # Set study rule 7: bookend the set by recalling the opening idea late on.
    "callback": "the set is near its end - one track that calls back the opening "
                "track (first title in the history: same artist, hook or key family) "
                "would bookend the set, recontextualized, not a repeat",
    # research/notes/set-study-mDtud5fLgFQ.md sections 5-6: a hook the set has
    # already returned to comes back once more, as a DIFFERENT version (the
    # repeat filter drops the identical one), instead of an open/close bookend.
    "reprise": "the crowd has already heard \"{hook}\" more than once tonight - it is "
               "this set's recurring hook: bring it back once more as a different "
               "version of that same song (remix, edit or rework, not the version "
               "already played), re-approached through a build",
}

ENERGY_HOOK_MAX = 120  # a display name, not free text: capped before it enters the prompt


def _clean_hook(hook: Optional[str]) -> str:
    """Printable, quote-free, length-capped track name for the reprise note."""
    s = "".join(ch for ch in str(hook or "") if ch.isprintable()).replace('"', "'")
    return " ".join(s.split())[:ENERGY_HOOK_MAX]


def _fame_counts(names: list[str], last: int = 8) -> tuple[int, int]:
    """(famous, known) over the last songs played that already have a fame answer (no lookups here)."""
    if not _fame and FAME_PATH.exists():
        try:
            _fame.update(json.loads(FAME_PATH.read_text()))
        except ValueError:
            pass
    ids = {nm: tid for tid, nm in _track_names.items()}
    got = [_fame[ids[n]] for n in names[-last:] if n in ids and ids[n] in _fame]
    return sum(1 for f in got if f.get("famous")), len(got)


def _occasion_with_note(occasion: Optional[str], note: Optional[str], variety: str = "",
                        hook: Optional[str] = None) -> str:
    base = occasion or ""
    text = _ENERGY_NOTES.get(note or "")
    if note == "reprise":
        h = _clean_hook(hook)
        text = text.format(hook=h) if h else None  # no hook named -> nothing to reprise
    extras = [x for x in (text, variety) if x]
    return f"{base} ({'; '.join(extras)})".strip() if extras else base


# Normalised suggested title -> genre the model gave it, so the next suggest
# call for that track can ground on the matching ./DJ genre playbook.
_suggested_genres: Dict[str, str] = {}
# Normalised title -> release era the model gave it ("1990s"), same lifetime
# as _suggested_genres: the library fallback holds the set's decade too.
_suggested_eras: Dict[str, str] = {}
# Both persist in the app DB, CACHE_DIR/app.db (app.music_brain.analysis.genre_labels): lost on a
# restart, the library fallback had no labelled Punjabi song (session 2026-09-30_102327).
LABELS_PATH: Optional[Path] = None        # None: genre_labels.path(); tests point it at tmp_path
_labels_dirty = False


def _set_label(store: Dict[str, str], key: str, value) -> None:
    """Newest label last (the store keeps the newest genre_labels.MAX_LABELS)."""
    global _labels_dirty
    v = str(value)
    if key and store.get(key) != v:
        store.pop(key, None)
        store[key] = v
        _labels_dirty = True


def _save_labels() -> None:
    global _labels_dirty
    if not _labels_dirty:
        return
    from app.music_brain.analysis import genre_labels as gl

    if gl.merge_save(_suggested_genres, _suggested_eras, LABELS_PATH):
        _labels_dirty = False


def _load_labels() -> int:
    """Startup: the stored labels, then the knowledge export's for songs that have none
    (matched by name; no model call). Returns how many came from the export."""
    from app.music_brain.analysis import genre_labels as gl
    from app.music_brain.matching import knowledge

    g, e = gl.load(LABELS_PATH)
    _suggested_genres.update(g)
    _suggested_eras.update(e)
    try:
        tracked = json.loads((knowledge.KNOWLEDGE_DIR / knowledge.LABELS).read_text(encoding="utf-8"))
        names = json.loads((knowledge.KNOWLEDGE_DIR / knowledge.NAMES).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return gl.backfill(_suggested_genres, _suggested_eras, tracked, names)


@app.on_event("startup")
def _startup_labels() -> None:
    # at startup, not at import: importing the server (tests, tools) must never open or
    # migrate the real label store (it did, and renamed the owner's live genre_labels.json)
    try:
        _load_labels()
    except Exception as exc:  # noqa: BLE001 -- a bad label file must not stop the server
        print(f"WARNING [labels] not loaded: {type(exc).__name__}: {exc}", flush=True)
_set_memory = None  # app.ui.services.set_memory.SetMemory, created on first suggest


def _clean_set_id(raw) -> str:
    """Browser-sent set id: keep it only if it's a short plain token."""
    s = str(raw or "").strip()[:64]
    return s if re.fullmatch(r"[A-Za-z0-9_-]+", s) else ""


def _genre_key(title: str) -> str:
    from app.ui.services.track_identity import clean_title
    return " ".join(clean_title(title).lower().split())


def _track_vibe(track_id: str) -> Dict[str, Optional[str]]:
    """track_id -> {"genre", "era"} labels the model already gave this title
    (suggestions / current_genre), same keying as the library fallback.
    Unknown -> None, which RecipeMatcher treats as no penalty."""
    from app.ui.services.track_identity import clean_identity

    path = _tracks.get(track_id)
    name = _track_names.get(track_id) or (path.stem if path else "")
    if not name:
        return {"genre": None, "era": None}
    key = _genre_key(clean_identity(name)[1])
    return {"genre": _suggested_genres.get(key) or None, "era": _suggested_eras.get(key) or None}


def _vibe_by_name(name: str) -> Dict[str, Optional[str]]:
    from app.ui.services.track_identity import clean_identity

    key = _genre_key(clean_identity(str(name or ""))[1]) if name else ""
    return {"genre": _suggested_genres.get(key) or None, "era": _suggested_eras.get(key) or None}


# ---- OWNER VETO + booking vet (app/music_brain/atlas/vetoes.py, app/ui/services/booking_vet.py) ----
_VETO_MEMO: dict = {}


def _vetoes() -> list:
    """The owner's vetoes (seed + the user DB), re-read when a veto is stored."""
    from app.music_brain.atlas import vetoes as vt

    stamp = vt.stamp(CACHE_DIR)
    if _VETO_MEMO.get("stamp") != stamp:
        _VETO_MEMO.update(stamp=stamp, rows=vt.load(CACHE_DIR))
    return _VETO_MEMO["rows"]


def _name_of(track_id: str) -> str:
    path = _tracks.get(track_id)
    return _track_names.get(track_id) or (path.stem if path else "")


class VetoRequest(BaseModel):
    kind: str = "pair"                          # "pair" (A -> B) | "song" (B in A's scene)
    a_id: Optional[str] = Field(default=None, max_length=64)
    b_id: Optional[str] = Field(default=None, max_length=64)
    a_name: Optional[str] = Field(default=None, max_length=300)
    b_name: Optional[str] = Field(default=None, max_length=300)
    note: str = Field(default="", max_length=200)


@app.get("/api/vetoes")
def get_vetoes():
    return {"vetoes": _vetoes()}


@app.post("/api/vetoes")
def post_veto(req: VetoRequest):
    """Console "bad pair": the owner's live veto. Stored at once (atomic) and read by every booking
    path through /api/autopilot/vet; the next atlas build turns it into PLAYED_BAD evidence."""
    from app.music_brain.atlas import vetoes as vt
    from app.music_brain.analysis.genre import genre_scenes

    a = req.a_name or (_name_of(req.a_id) if req.a_id else "")
    b = req.b_name or (_name_of(req.b_id) if req.b_id else "")
    scene = ""
    if req.kind == "song" and a:
        sc = sorted(genre_scenes(_vibe_by_name(a)["genre"]))
        scene = sc[0] if sc else ""
    try:
        e = vt.make(req.kind, b, a, scene=scene, source="console", note=req.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    added = vt.add(e, CACHE_DIR)
    _VETO_MEMO.clear()
    print(f"[veto] {'added' if added else 'already there'}: {e.get('a', '')} -> {e['b']}", flush=True)
    return {"added": added, "veto": e}


class VetCand(BaseModel):
    track_id: Optional[str] = Field(default=None, max_length=64)
    name: str = Field(default="", max_length=300)
    stored: bool = False                        # a macro step / FOLLOW SET song / studied combo


class VetRequest(BaseModel):
    a_id: Optional[str] = Field(default=None, max_length=64)
    a_name: str = Field(default="", max_length=300)
    history: List[str] = Field(default_factory=list, max_length=400)
    set_id: str = Field(default="", max_length=64)
    punjabi_profile: str = "off"
    anchor_genre: str = Field(default="", max_length=80)   # the set's scene while recovering from a mistake
    cands: List[VetCand] = Field(default_factory=list, max_length=60)


@app.post("/api/autopilot/vet")
def autopilot_vet(req: VetRequest):
    """Every candidate the console is about to book, whatever path found it (booking_vet.py)."""
    from app.ui.services import booking_vet as bv
    from app.ui.services.set_memory import MAX_SONGS, SetMemory

    global _set_memory
    a_name = req.a_name or (_name_of(req.a_id) if req.a_id else "")
    av = _track_vibe(req.a_id) if req.a_id else _vibe_by_name(a_name)
    if not av["genre"] and a_name:
        av = _vibe_by_name(a_name)
    earlier: List[str] = []
    if any(c.stored for c in req.cands):
        if _set_memory is None:
            _set_memory = SetMemory(CACHE_DIR / "set_memory.json")
        earlier = _set_memory.earlier_sets(list(req.history), set_id=_clean_set_id(req.set_id), limit=MAX_SONGS)
    rows = []
    for c in req.cands:
        name = c.name or (_name_of(c.track_id) if c.track_id else "")
        v = _track_vibe(c.track_id) if c.track_id else {"genre": None, "era": None}
        if not v["genre"]:
            v = _vibe_by_name(name)
        rows.append({"track_id": c.track_id, "name": name, "genre": v["genre"], "era": v["era"], "stored": c.stored})
    res = bv.vet(a_name, rows, a_genre=av["genre"], a_era=av["era"], history=req.history, earlier=earlier,
                 vetoes=_vetoes(), punjabi_profile=req.punjabi_profile, anchor_genre=req.anchor_genre or None)
    for r, row in zip(res, rows):
        r["genre"], r["era"] = row["genre"], row["era"]
    from app.music_brain.analysis.genre import scene_keys
    return {"a_name": a_name, "a_genre": av["genre"], "a_era": av["era"],
            "a_families": scene_keys(av["genre"]), "results": res}


def _pair_vibe(track_a_id: str, track_b_id: str) -> Dict[str, Optional[str]]:
    """Genre/era kwargs for RecipeMatcher.match / resolve_candidate."""
    a, b = _track_vibe(track_a_id), _track_vibe(track_b_id)
    return {"genre_a": a["genre"], "genre_b": b["genre"], "era_a": a["era"], "era_b": b["era"]}


# Identical suggest requests share ONE LLM call. A browser whose request timed
# out retries, but the server cannot cancel a generation, so retries used to
# queue behind the original and snowball (gate: in_flight=suggest, queued=
# [suggest, ...]; each call ~50 s on gemma-3-27b).
import threading as _threading

_suggest_inflight: Dict[str, dict] = {}
_suggest_lock = _threading.Lock()
SUGGEST_REUSE_S = 30.0   # a result this fresh is handed to an identical retry


def _suggest_key(req: "AutopilotSuggestRequest") -> str:
    return json.dumps(req.dict(), sort_keys=True, default=str)


@app.post("/api/autopilot/suggest")
def autopilot_suggest(req: AutopilotSuggestRequest):
    import time as _time

    key = _suggest_key(req)
    with _suggest_lock:
        slot = _suggest_inflight.get(key)
        fresh = slot and slot.get("done_at") and _time.time() - slot["done_at"] < SUGGEST_REUSE_S
        if slot and (not slot.get("done_at") or fresh):
            owner = False
        else:
            slot = {"event": _threading.Event(), "result": None, "error": None, "done_at": None}
            _suggest_inflight[key] = slot
            owner = True
        # forget stale entries
        for k in [k for k, v in _suggest_inflight.items()
                  if v.get("done_at") and _time.time() - v["done_at"] > SUGGEST_REUSE_S]:
            _suggest_inflight.pop(k, None)
    if not owner:
        slot["event"].wait(300)
        if slot["error"] is not None:
            raise slot["error"]
        if slot["result"] is None:
            raise HTTPException(status_code=504, detail="suggest still running")
        return slot["result"]
    try:
        slot["result"] = _autopilot_suggest_impl(req)
        return slot["result"]
    except HTTPException as exc:
        slot["error"] = exc
        raise
    except Exception as exc:
        slot["error"] = HTTPException(status_code=500, detail=f"LLM suggest error: {exc}")
        raise
    finally:
        slot["done_at"] = _time.time()
        slot["event"].set()


def _autopilot_suggest_impl(req: AutopilotSuggestRequest):
    """Use local LLM (Ollama gemma3:4b by default) to suggest next tracks."""
    import traceback
    import numpy as np
    from app.ui.services import engine
    from app.ui.services.autopilot_service import SET_MODES, hit_share_note

    suggest_next_tracks = engine.current().suggest

    try:
        path = _track_path(req.track_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"_track_path error: {exc}") from exc

    try:
        analysis = analyze_track(path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"analyze error: {exc}") from exc

    curve = list(analysis.energy_curve) if analysis.energy_curve is not None else []
    avg_energy = float(np.mean(curve)) if curve else 0.5
    measured_energy = None
    try:
        from app.music_brain.analysis import energy as en

        measured_energy = en.level(path, analysis.bpm)["level"]
    except Exception:
        measured_energy = None           # best-effort: the prompt falls back to the relative number
    camelot = analysis.key.camelot if analysis.key else "unknown"

    from app.ui.services.track_identity import clean_identity, credited_artists

    display = _track_names.get(req.track_id, path.stem)
    # Primary artist + clean title only: featured artists and "(Official Video)"
    # noise confuse the model.
    artist_part, title_part = clean_identity(display)
    # Collabs: give the model every credit ("LATIN MAFIA & Fred again..") and let
    # it anchor on the best-known producer (see CREDITS rule in the prompt).
    credits = credited_artists(display)
    if len(credits) > 1:
        artist_part = " & ".join(credits[:3])
    history_display = [" - ".join(clean_identity(h)).removeprefix("Unknown - ") for h in req.history]
    # rejected titles: excluded from suggestions like played ones, never recorded as played
    avoid_display = [" - ".join(clean_identity(h)).removeprefix("Unknown - ") for h in req.avoid]
    queue_display = [" - ".join(clean_identity(h)).removeprefix("Unknown - ") for h in req.queue[:6]]
    genre = _suggested_genres.get(_genre_key(title_part), "")

    # set_position: by clock when elapsed_seconds is sent (elapsed / set length);
    # else caller-supplied; else from history length (0→10 tracks = 0→1.0).
    length = req.set_length_seconds if req.set_length_seconds and req.set_length_seconds > 0 \
        else DEFAULT_SET_LENGTH_S
    if req.elapsed_seconds is not None and req.elapsed_seconds >= 0:
        set_position = max(0.0, min(1.0, req.elapsed_seconds / length))
    elif req.set_position is not None:
        set_position = max(0.0, min(1.0, req.set_position))
    else:
        set_position = min(len(req.history) / 10.0, 1.0)

    # Cross-set memory: remember this set's songs, offer the earlier sets' ones
    # to the prompt as "heard recently, prefer fresh" (not on look-ahead calls).
    from app.ui.services.set_memory import MAX_SONGS, SetMemory
    global _set_memory
    if _set_memory is None:
        _set_memory = SetMemory(CACHE_DIR / "set_memory.json")
    set_id = _clean_set_id(req.set_id)
    if not req.lookahead:
        _set_memory.record(history_display, set_id)
    # every remembered song, not just the prompt's 40: the filter must know them all
    earlier = _set_memory.earlier_sets(history_display + avoid_display, set_id=set_id, limit=MAX_SONGS)

    # Absolute loudness (Avg Energy is peak-normalised per song). Best-effort.
    loudness_dbfs = None
    try:
        from app.music_brain.analysis.vibe import analyze_vibe

        loudness_dbfs = analyze_vibe(path).loudness_dbfs
    except Exception as exc:
        print(f"[suggest] loudness unavailable: {exc}", flush=True)

    meta: dict = {}
    try:
        suggestions = suggest_next_tracks(
            title=title_part,
            artist=artist_part,
            bpm=analysis.bpm or 128.0,
            camelot=camelot,
            duration=analysis.duration or 0.0,
            avg_energy=avg_energy,
            measured_energy=measured_energy,
            occasion=_occasion_with_note(req.occasion, req.energy_note,
                                         "; ".join(x for x in (_variety_note(req.variety_run, req.variety_genre),
                                                               hit_share_note(req.occasion, *_fame_counts(req.history)))
                                                   if x),
                                         hook=req.energy_hook),
            history=req.history + req.avoid + req.queue[:6],
            set_position=set_position,
            set_mode=req.set_mode if req.set_mode in SET_MODES else "hybrid",
            relaxed=bool(req.relaxed),
            meta=meta,
            genre=genre,
            history_display=history_display,
            queue_display=queue_display,
            avoid_display=avoid_display,
            lookahead=req.lookahead,
            earlier_sets=earlier,
            energy_history=[int(round(v)) for v in req.energy_history[-8:] if 1 <= v <= 10],
            energy_reset=(req.energy_note == "dip"),
            # No favourites: memory can't tell user picks from AI picks, so the
            # AI's own picks became "favourites" and fed back (Fred again.. loop).
            favourite_artists=[],
            loudness_dbfs=loudness_dbfs,
            tempo_target=req.tempo_target,
            tempo_note=req.tempo_note or "",
            lead_to=req.lead_to[:160],
            lead_step=max(0, min(req.lead_step, 12)),
            lead_steps=max(0, min(req.lead_steps, 12)),
            lead_bpm=req.lead_bpm,
            punjabi_profile=req.punjabi_profile,
            scene_anchor=req.scene_anchor.strip(),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"LLM suggest error: {exc}") from exc
    for s in suggestions:
        if s.get("title") and s.get("genre"):
            _set_label(_suggested_genres, _genre_key(s["title"]), s["genre"])
        if s.get("title") and s.get("era"):
            _set_label(_suggested_eras, _genre_key(s["title"]), s["era"])
    # The playing song's own genre (the model's current_genre): library songs
    # get labels as they play, so the library fallback can check genre. A stored
    # label wins (suggest_next_tracks grounds current_genre on it): the model called
    # Four Tet "deep house" / "melodic techno" over its stored label (session 191133).
    if meta.get("current_genre") and not req.lookahead and not genre:
        _set_label(_suggested_genres, _genre_key(title_part), meta["current_genre"])
    if meta.get("current_era") and not req.lookahead and not _suggested_eras.get(_genre_key(title_part)):
        _set_label(_suggested_eras, _genre_key(title_part), meta["current_era"])
    _save_labels()
    _song_step("suggest", req.track_id,
               decision=f"{len(suggestions)} next-song pick(s)" + (" (look-ahead)" if req.lookahead else ""),
               why=meta.get("current_genre"),
               result=[{k: s.get(k) for k in ("artist", "title", "reason", "why") if k in s}
                       for s in suggestions[:8] if isinstance(s, dict)])
    return {"suggestions": suggestions, "set_position": round(set_position, 2), **meta}


class MindPlanRequest(BaseModel):
    track_a_id: str                 # playing song
    track_b_id: str                 # next song, already matched
    now: float = 0.0                # current position in A (s)
    entry: float = 0.0              # where A came in (s)
    window_lo: float                # exit window on A, track seconds
    window_hi: float
    set_mode: str = "hybrid"
    set_position: float = 0.0
    recent_moves: list[str] = []
    remix_used: list[str] = []      # remix moves already played on A
    mashup_possible: bool = False
    subdrop_last_track: bool = False
    peak_moves: bool = False        # PEAK MOVES toggle: allow fakeout / peak_roll / beat_boost
    big_moment_ok: bool = False     # browser's set-wide big-moment ledger allows one on A


@app.post("/api/autopilot/plan")
def autopilot_plan(req: MindPlanRequest):
    """One LLM plan per song pair (candidate, exit phrase, DJ-mind moves).

    The model proposes; app.ui.services.mind_plan.validate_plan keeps only what the
    DJ-mind caps allow. The browser re-checks each move against live state.
    """
    from app.ui.services.mind_plan import build_facts, plan_pair

    if not req.window_hi > req.window_lo:
        raise HTTPException(status_code=400, detail="window_hi must be > window_lo")
    track_a = analyze_track(_track_path(req.track_a_id))
    track_b = analyze_track(_track_path(req.track_b_id))
    candidates = [c.to_dict() for c in _matcher.match(
        track_a, track_b, top_n=3, **_pair_vibe(req.track_a_id, req.track_b_id),
    )]
    if not candidates:
        raise HTTPException(status_code=422, detail="No match candidates for this pair")
    facts = build_facts(track_a.to_dict(), track_b.to_dict(), candidates, req.model_dump()
                        if hasattr(req, "model_dump") else req.dict())
    try:
        plan = plan_pair(facts)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM plan error: {exc}") from exc
    plan["exit_options"] = facts["exit_options"]
    _song_step("mind_plan", req.track_b_id, phase="planning",
               decision=str((plan.get("candidate") or {}).get("recipe") if isinstance(plan.get("candidate"), dict) else plan.get("candidate")),
               why=plan.get("why") or plan.get("reason"), inputs={"a_id": req.track_a_id, "now": req.now},
               result={k: plan.get(k) for k in list(plan)[:12] if k != "exit_options"})
    return plan


@app.get("/api/live/ear")
def get_live_ear_status():
    from app.ui.services import live_ear

    return live_ear.status()


@app.post("/api/live/ear")
async def post_live_ear(metrics: str = Form(...), clip: Optional[UploadFile] = None):
    """One hold-loop decision: watchdog numbers (JSON form field) plus an
    optional few-second master-bus WAV. Always answers; the model only
    proposes (see app.ui.services.live_ear)."""
    from starlette.concurrency import run_in_threadpool

    from app.ui.services import live_ear

    if len(metrics) > 4000:
        raise HTTPException(status_code=400, detail="metrics too large")
    try:
        m = json.loads(metrics)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"metrics must be JSON: {exc}") from exc
    if not isinstance(m, dict):
        raise HTTPException(status_code=400, detail="metrics must be a JSON object")
    m = {k: v for k, v in m.items() if isinstance(v, (int, float, str, bool)) or v is None}
    wav = None
    if clip is not None:
        wav = await clip.read(live_ear.MAX_AUDIO_BYTES + 1)
        try:
            live_ear.check_wav(wav)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await run_in_threadpool(live_ear.decide, wav, m)


@app.post("/api/samples")
async def upload_sample(file: UploadFile, label: Optional[str] = Form(default=None)):
    """Uploads a custom one-shot for the console's sampler pad grid."""
    sample_id, dest = await _store_blob(file, SAMPLES_CACHE_DIR, _samples, ".wav",
                                        max_bytes=SAMPLE_MAX_BYTES)
    resolved_label = label or Path(file.filename or dest.name).stem
    _sample_labels[sample_id] = resolved_label
    _save_sample_labels()
    return {
        "sample_id": sample_id,
        "url": f"/api/samples/{sample_id}",
        "label": resolved_label,
    }


@app.get("/api/samples")
def list_samples():
    return {
        "samples": [
            {
                "sample_id": sid,
                "label": _sample_labels.get(sid, path.stem),
                "url": f"/api/samples/{sid}",
            }
            for sid, path in _samples.items()
        ]
    }


@app.get("/api/samples/{sample_id}")
def get_sample_audio(sample_id: str):
    path = _samples.get(sample_id)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail=f"Unknown sample_id: {sample_id}")
    return FileResponse(path, media_type=_media_type_for(path))


@app.post("/api/recordings")
async def upload_recording(file: UploadFile):
    """Stores a MediaRecorder capture of a live console mix (webm/opus)."""
    recording_id, _ = await _store_blob(file, RECORDINGS_CACHE_DIR, _recordings, ".webm")
    return {"recording_id": recording_id, "url": f"/api/recordings/{recording_id}"}


@app.get("/api/recordings/{recording_id}")
def get_recording_audio(recording_id: str):
    path = _recordings.get(recording_id)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail=f"Unknown recording_id: {recording_id}")
    return FileResponse(path, media_type=_media_type_for(path))


@app.post("/api/set-logs")
def save_set_log(payload: dict):
    """Validate and locally cache a djset-v1 log plus its Obsidian export."""

    try:
        validated = validate_set_log(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raw = json.dumps(validated, ensure_ascii=False, indent=2).encode("utf-8")
    log_id = hashlib.sha256(raw).hexdigest()[:16]
    json_path = SET_LOGS_CACHE_DIR / f"{log_id}.djset.json"
    markdown_path = SET_LOGS_CACHE_DIR / f"{log_id}.md"
    json_path.write_bytes(raw)
    markdown_path.write_text(export_set_log_markdown(validated), encoding="utf-8")
    _set_logs[log_id] = json_path
    try:
        from app.music_brain import history

        history.add_set_log(json_path, SET_LOGS_CACHE_DIR.parent)     # the set-history index (user DB)
    except Exception:  # noqa: BLE001 -- the file is saved; the index catches up on rebuild()
        pass
    return {
        "set_log_id": log_id,
        "json_url": f"/api/set-logs/{log_id}",
        "markdown_url": f"/api/set-logs/{log_id}/markdown",
    }


@app.get("/api/set-logs/{set_log_id}")
def get_set_log(set_log_id: str):
    path = _set_logs.get(set_log_id)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail=f"Unknown set_log_id: {set_log_id}")
    return FileResponse(path, media_type="application/json")


@app.get("/api/set-logs/{set_log_id}/markdown")
def get_set_log_markdown(set_log_id: str):
    path = SET_LOGS_CACHE_DIR / f"{set_log_id}.md"
    if set_log_id not in _set_logs or not path.exists():
        raise HTTPException(status_code=404, detail=f"Unknown set_log_id: {set_log_id}")
    return FileResponse(path, media_type="text/markdown; charset=utf-8")


@app.get("/api/audio/tracks/{track_id}")
def get_track_audio(track_id: str):
    path = _track_path(track_id)
    return FileResponse(path)


from app.ui.services.atlas_api import router as _atlas_router  # noqa: E402 -- pair atlas + macros (/api/atlas, /api/macros)

app.include_router(_atlas_router)
from app.ui.services.replay_api import router as _replay_router  # noqa: E402 -- set history, replay, time travel, liked

app.include_router(_replay_router)

class _RevalidatingStatic(StaticFiles):
    """The console's own JS/CSS/HTML: always revalidated (ETag -> cheap 304), never taken from the
    browser's heuristic cache. Without Cache-Control the browser kept an old mascot.js next to new
    modules after an update, so NULL-BOT ran stale code."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


if STATIC_DIR.exists():
    app.mount("/", _RevalidatingStatic(directory=str(STATIC_DIR), html=True), name="static")
