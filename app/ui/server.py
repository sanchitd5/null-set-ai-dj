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
import shutil
import uuid
from pathlib import Path
from typing import Dict, List, Optional
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.music_brain.analyzer import analyze as analyze_track
from app.music_brain.config import (
    CACHE_DIR,
    RECORDINGS_CACHE_DIR,
    ROOT_DIR,
    SAMPLES_CACHE_DIR,
    SET_LOGS_CACHE_DIR,
)
from app.music_brain.knowledge_parser import KnowledgeParser
from app.music_brain.recipe_matcher import RecipeMatcher
from app.music_brain.stem_service import separate as separate_stems
from app.music_brain.transition_renderer import render_full_mix, render_preview
from app.music_brain.set_log import export_set_log_markdown, validate_set_log
from app.ui.library_service import scan_library

UPLOAD_DIR = CACHE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="AI Music Brain", version="0.1.0")


@app.on_event("startup")
def _boot_llm() -> None:
    """Load the DJ's LLM (MLX on Apple Silicon, Ollama fallback) in the
    background at startup, so the first suggestion does not pay model load."""
    from app.ui import model_runtime

    model_runtime.start_background()


@app.get("/api/llm/status")
def get_llm_status():
    from app.ui import model_runtime
    from app.ui.llm_gate import gate

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
    from app.ui.download_service import detect_source, download_to_dir

    source = detect_source(req.url)
    if source == "unknown":
        raise HTTPException(
            status_code=400,
            detail="Unsupported URL. Paste a YouTube or YouTube Music link.",
        )

    # Download into a private temp dir so thumbnails / .part files from a failed
    # or filtered run never land in UPLOAD_DIR; only the final audio is moved over.
    tmp_dir = UPLOAD_DIR / f"_dl_{uuid.uuid4().hex}"
    try:
        try:
            # Off the event loop: a YouTube fetch can take a minute and
            # would otherwise stall every other request (audio, analysis, UI).
            from starlette.concurrency import run_in_threadpool

            paths = await run_in_threadpool(download_to_dir, req.url, tmp_dir)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

        results = _register_downloaded(paths)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return {"source": source, "tracks": results}


def _register_downloaded(paths: List[Path]) -> List[dict]:
    """Move downloaded audio into UPLOAD_DIR under its content-hash id."""
    results = []
    for path in paths:
        original_name = path.stem
        data = path.read_bytes()
        track_id = hashlib.sha256(data).hexdigest()[:16]
        dest = UPLOAD_DIR / f"{track_id}{path.suffix}"
        if not dest.exists():
            shutil.move(str(path), dest)
        _tracks[track_id] = dest
        _remember_track_name(track_id, original_name)
        results.append({"track_id": track_id, "filename": dest.name, "display_name": original_name})
    return results


class DownloadJobRequest(BaseModel):
    url: str
    label: str = ""


@app.get("/api/search/youtube")
def get_youtube_search(q: str, limit: int = 8):
    """Song search for LEAD TO (no download); filtered like downloads."""
    from app.ui.download_service import search_songs

    if not (2 <= len(q.strip()) <= 120):
        raise HTTPException(status_code=400, detail="query must be 2-120 characters")
    try:
        return {"results": search_songs(q, max(1, min(limit, 12)))}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"search failed: {exc}") from exc


@app.post("/api/download/jobs")
def post_download_job(req: DownloadJobRequest):
    """Start a background download (pre-download / prefetch); poll for progress."""
    from app.ui import download_jobs
    from app.ui.download_service import detect_source, download_to_dir

    if detect_source(req.url) == "unknown":
        raise HTTPException(status_code=400, detail="Unsupported URL.")
    job_id = download_jobs.start_job(
        req.url, req.label[:120], UPLOAD_DIR,
        download_fn=download_to_dir,
        register_fn=_register_downloaded,
        analyze_fn=lambda tid: analyze_track(_track_path(tid)),
    )
    return {"job_id": job_id}


@app.get("/api/download/jobs")
def get_download_jobs():
    from app.ui import download_jobs

    return {"jobs": download_jobs.list_jobs()[:20]}


@app.get("/api/download/jobs/{job_id}")
def get_download_job(job_id: str):
    from app.ui import download_jobs

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
    return {"track_id": track_id, "filename": file.filename}


@app.get("/api/tracks")
def list_tracks():
    from app.ui.download_service import _is_live, _is_mix

    def _entry(tid, p):
        name = _track_names.get(tid, p.stem)
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
        _tracks.setdefault(str(track["track_id"]), Path(str(track["path"])))
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


@app.post("/api/match")
def post_match(req: MatchRequest):
    import dataclasses

    from app.music_brain import stem_service
    from app.music_brain.config import DEMUCS_MODEL
    from app.music_brain.mashup import MASHUP_DEMUCS_MODEL

    def cached_vocals(track_id: str) -> Optional[list]:
        """Vocal regions only when a Demucs vocal stem is already cached:
        matching must never start a separation."""
        if track_id in _vocal_regions:
            return _vocal_regions[track_id]
        audio_hash = stem_service.file_hash(_track_path(track_id))
        for model, two in ((MASHUP_DEMUCS_MODEL, "vocals"), (DEMUCS_MODEL, "vocals"), (DEMUCS_MODEL, None)):
            stems = stem_service._load_from_cache(stem_service._cache_dir_for(audio_hash, model, two))
            if not stems or not stems.get("vocals"):
                continue
            if (model, two) == (MASHUP_DEMUCS_MODEL, "vocals"):
                return _vocal_regions_for(track_id)  # hits the same stem cache
            try:
                from app.music_brain.analyzer import vocal_presence_map

                regions = [list(r) for r in vocal_presence_map(Path(stems["vocals"]))]
            except Exception as exc:
                print(f"[match] vocal map unavailable for {track_id}: {exc}", flush=True)
                return None
            _vocal_regions[track_id] = regions
            return regions
        return None

    tracks = []
    for track_id in (req.track_a_id, req.track_b_id):
        track = analyze_track(_track_path(track_id))
        try:
            regions = cached_vocals(track_id)
        except Exception as exc:  # best-effort: never break matching
            print(f"[match] vocal lookup failed for {track_id}: {exc}", flush=True)
            regions = None
        if regions:
            track = dataclasses.replace(track, vocal_active_regions=[tuple(r) for r in regions])
        tracks.append(track)
    track_a, track_b = tracks
    candidates = _matcher.match(track_a, track_b, top_n=req.top_n)
    # Measured vibe continuity (loudness / brightness / onset density / energy).
    # Best-effort: a vibe failure must never break matching.
    vibe = None
    try:
        from app.music_brain.vibe import analyze_vibe, vibe_distance
        vibe = vibe_distance(
            analyze_vibe(_track_path(req.track_a_id)),
            analyze_vibe(_track_path(req.track_b_id)),
        )
    except Exception:
        vibe = None
    return {"candidates": [c.to_dict() for c in candidates], "vibe": vibe}


class MashupRequest(BaseModel):
    host_id: str
    guest_id: str
    bars: int = 8


def _vocals_stem(track_id: str) -> str:
    from app.music_brain.mashup import MASHUP_DEMUCS_MODEL

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
        from app.music_brain.analyzer import vocal_presence_map

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
    from app.music_brain.blend import ALLOWED_BARS, ENTRY_MODES, plan_blend

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
    from app.music_brain.mashup import ALLOWED_BARS, plan_mashup

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
    from app.music_brain.mashup import MASHUP_DEMUCS_MODEL
    from app.music_brain.stem_service import _cache_dir_for, _load_from_cache, file_hash

    try:
        path = _track_path(track_id)
        return _load_from_cache(_cache_dir_for(file_hash(Path(path)), MASHUP_DEMUCS_MODEL, "vocals")) is not None
    except Exception:
        return False


def _layer_third(req: LayerRequest, a, b, layer: dict) -> Optional[dict]:
    """Vocal stem of a third song over the layer: cached stems only, key and
    tempo fit BOTH playing songs, on a B phrase with no vocal from A or B."""
    from app.music_brain.blend import tempo_lock
    from app.music_brain.layer import key_fits, pitch_fits, vocal_clash
    from app.music_brain.mashup import MAX_RATE_DEVIATION, plan_mashup

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
                               guest_vocals_path=lambda gid=gid: _vocals_stem(gid), bars=bars)
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
    from app.music_brain.layer import LAYER_HOLD_BARS, LAYER_UNWIND_BARS, plan_layer

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
    from app.music_brain.bridge import bridge_ladder

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
    occasion: Optional[str] = None
    history: list[str] = []
    set_position: Optional[float] = None  # 0.0=start, 1.0=end; computed from history if omitted
    set_mode: str = "hybrid"  # long | quick | hybrid
    # "dip": the set has sat near its loudness peak for a while, so ask for a
    # track that lets energy fall back before building again (set study rule 9).
    energy_note: Optional[str] = None
    # Look-ahead (songs for AFTER the booked next one): lowest LLM priority,
    # waits behind any transition plan (app/ui/llm_gate.py).
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
    lead_step: int = 0
    lead_steps: int = 0
    lead_bpm: Optional[float] = None


DEFAULT_SET_LENGTH_S = 3600.0


VARIETY_RUN_MAX = 6  # wiki "What Do I Play Next": contrast once a style plateaus


def _variety_note(run: int, genre: str) -> str:
    if run < VARIETY_RUN_MAX or not genre:
        return ""
    return (f"the last {run} songs were all {genre[:40]} and the floor is getting bored - "
            f"switch to a NEIGHBOURING subgenre now that still fits the occasion (e.g. bhangra -> "
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
}


def _occasion_with_note(occasion: Optional[str], note: Optional[str], variety: str = "") -> str:
    base = occasion or ""
    extras = [x for x in (_ENERGY_NOTES.get(note or ""), variety) if x]
    return f"{base} ({'; '.join(extras)})".strip() if extras else base


# Normalised suggested title -> genre the model gave it, so the next suggest
# call for that track can ground on the matching ./DJ genre playbook.
_suggested_genres: Dict[str, str] = {}
_set_memory = None  # app.ui.set_memory.SetMemory, created on first suggest


def _genre_key(title: str) -> str:
    from app.ui.track_identity import clean_title
    return " ".join(clean_title(title).lower().split())


@app.post("/api/autopilot/suggest")
def autopilot_suggest(req: AutopilotSuggestRequest):
    """Use local LLM (Ollama gemma3:4b by default) to suggest next tracks."""
    import traceback
    import numpy as np
    from app.ui.autopilot_service import SET_MODES, suggest_next_tracks

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
    camelot = analysis.key.camelot if analysis.key else "unknown"

    from app.ui.track_identity import clean_identity, credited_artists

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
    from app.ui.set_memory import SetMemory
    global _set_memory
    if _set_memory is None:
        _set_memory = SetMemory(CACHE_DIR / "set_memory.json")
    if not req.lookahead:
        _set_memory.record(history_display)
    earlier = _set_memory.earlier_sets(history_display)

    # Absolute loudness (Avg Energy is peak-normalised per song). Best-effort.
    loudness_dbfs = None
    try:
        from app.music_brain.vibe import analyze_vibe

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
            occasion=_occasion_with_note(req.occasion, req.energy_note,
                                         _variety_note(req.variety_run, req.variety_genre)),
            history=req.history,
            set_position=set_position,
            set_mode=req.set_mode if req.set_mode in SET_MODES else "hybrid",
            meta=meta,
            genre=genre,
            history_display=history_display,
            lookahead=req.lookahead,
            earlier_sets=earlier,
            loudness_dbfs=loudness_dbfs,
            tempo_target=req.tempo_target,
            tempo_note=req.tempo_note or "",
            lead_to=req.lead_to[:160],
            lead_step=max(0, min(req.lead_step, 12)),
            lead_steps=max(0, min(req.lead_steps, 12)),
            lead_bpm=req.lead_bpm,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"LLM suggest error: {exc}") from exc
    for s in suggestions:
        if s.get("title") and s.get("genre"):
            _suggested_genres[_genre_key(s["title"])] = str(s["genre"])
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

    The model proposes; app.ui.mind_plan.validate_plan keeps only what the
    DJ-mind caps allow. The browser re-checks each move against live state.
    """
    from app.ui.mind_plan import build_facts, plan_pair

    if not req.window_hi > req.window_lo:
        raise HTTPException(status_code=400, detail="window_hi must be > window_lo")
    track_a = analyze_track(_track_path(req.track_a_id))
    track_b = analyze_track(_track_path(req.track_b_id))
    candidates = [c.to_dict() for c in _matcher.match(track_a, track_b, top_n=3)]
    if not candidates:
        raise HTTPException(status_code=422, detail="No match candidates for this pair")
    facts = build_facts(track_a.to_dict(), track_b.to_dict(), candidates, req.model_dump()
                        if hasattr(req, "model_dump") else req.dict())
    try:
        plan = plan_pair(facts)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM plan error: {exc}") from exc
    plan["exit_options"] = facts["exit_options"]
    return plan


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


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
