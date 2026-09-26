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

_knowledge = KnowledgeParser()
_matcher = RecipeMatcher(_knowledge)

# In-memory registry: track_id -> absolute file path. Rebuilt on restart
# from UPLOAD_DIR's contents (see _load_registry_from_disk below).
_tracks: Dict[str, Path] = {}
# Display names for downloaded tracks (track_id -> original filename stem).
_track_names: Dict[str, str] = {}


def _load_registry_from_disk() -> None:
    for path in UPLOAD_DIR.glob("*"):
        if path.is_file():
            _tracks[path.stem] = path


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


async def _store_blob(file: UploadFile, directory: Path, registry: Dict[str, Path],
                      default_suffix: str) -> tuple[str, Path]:
    suffix = Path(file.filename or "").suffix or default_suffix
    contents = await file.read()
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
    """Download a YouTube, YouTube Music, or Spotify URL and register as a track."""
    from app.ui.download_service import detect_source, download_to_dir

    source = detect_source(req.url)
    if source == "unknown":
        raise HTTPException(
            status_code=400,
            detail="Unsupported URL. Paste a YouTube, YouTube Music, or Spotify link.",
        )

    try:
        paths = download_to_dir(req.url, UPLOAD_DIR)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    results = []
    for path in paths:
        original_name = path.stem
        data = path.read_bytes()
        track_id = hashlib.sha256(data).hexdigest()[:16]
        dest = UPLOAD_DIR / f"{track_id}{path.suffix}"
        if not dest.exists():
            path.rename(dest)
        else:
            path.unlink(missing_ok=True)
        _tracks[track_id] = dest
        _track_names[track_id] = original_name
        results.append({"track_id": track_id, "filename": dest.name, "display_name": original_name})

    return {"source": source, "tracks": results}


@app.post("/api/tracks")
async def upload_track(file: UploadFile):
    """Uploads a track, returns its track_id for use in every other endpoint."""
    suffix = Path(file.filename or "track").suffix or ".mp3"
    contents = await file.read()
    track_id = hashlib.sha256(contents).hexdigest()[:16]
    dest = UPLOAD_DIR / f"{track_id}{suffix}"
    dest.write_bytes(contents)
    _tracks[track_id] = dest
    return {"track_id": track_id, "filename": file.filename}


@app.get("/api/tracks")
def list_tracks():
    return {
        "tracks": [
            {
                "track_id": tid,
                "path": str(p),
                "display_name": _track_names.get(tid, p.stem),
            }
            for tid, p in _tracks.items()
        ]
    }


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
    track_a = analyze_track(_track_path(req.track_a_id))
    track_b = analyze_track(_track_path(req.track_b_id))
    candidates = _matcher.match(track_a, track_b, top_n=req.top_n)
    return {"candidates": [c.to_dict() for c in candidates]}


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


@app.post("/api/autopilot/suggest")
def autopilot_suggest(req: AutopilotSuggestRequest):
    """Use local LLM (Ollama gemma3:4b by default) to suggest next tracks."""
    import numpy as np
    from app.ui.autopilot_service import suggest_next_tracks

    path = _track_path(req.track_id)
    analysis = analyze_track(path)

    curve = list(analysis.energy_curve) if analysis.energy_curve is not None else []
    avg_energy = float(np.mean(curve)) if curve else 0.5
    camelot = analysis.key.camelot if analysis.key else "unknown"

    display = _track_names.get(req.track_id, path.stem)
    # Best-effort split of "Artist - Title" or "Title" from display name.
    if " - " in display:
        artist_part, title_part = display.split(" - ", 1)
    else:
        artist_part, title_part = "Unknown", display

    suggestions = suggest_next_tracks(
        title=title_part,
        artist=artist_part,
        bpm=analysis.bpm or 128.0,
        camelot=camelot,
        duration=analysis.duration or 0.0,
        avg_energy=avg_energy,
        occasion=req.occasion or "",
        history=req.history,
    )
    return {"suggestions": suggestions}


@app.post("/api/samples")
async def upload_sample(file: UploadFile, label: Optional[str] = Form(default=None)):
    """Uploads a custom one-shot for the console's sampler pad grid."""
    sample_id, dest = await _store_blob(file, SAMPLES_CACHE_DIR, _samples, ".wav")
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
