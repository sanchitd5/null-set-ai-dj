"""REST surface for the phone ("lite" console), included by server.py. Thin wrappers over the
server's own state and route handlers; behind the app's auth middleware like every /api route.

GET  /api/mobile/library?q=&offset=0&limit=200  {total, offset, tracks: [entry]} by display name
GET  /api/mobile/tracks/{id}                   one entry
POST /api/mobile/tracks/{id}/labels {genre, era}  owner's genre / era correction (null: unchanged, "": clear)
GET  /api/mobile/tracks/{id}/lyrics            {track_id, lines, source, transcribed, hooks} (cached ASR only)
GET  /api/mobile/recordings                    {recordings: [{recording_id, url, bytes, modified, ext}]} newest first
GET  /api/mobile/sync                          macros, studied sets, supermoves, vetoes, liked, sessions,
                                               samples, learned moves in one bundle (+ errors per failed part)

entry = {track_id, display_name, artist, title, ext, bytes, genre, era, not_a_song, has_stems, art_url}
art_url = "/api/tracks/{id}/art?size=600&v=.." once a cover is cached (size=160 for a thumb), else null
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.ui.services import album_art

router = APIRouter()

_ID_RE = re.compile(r"[0-9A-Za-z_-]{1,64}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_LIMIT = 500
MAX_Q = 120
SESSIONS_IN_SYNC = 30
LYRICS_MISS_TTL_S = 600.0             # same as server.HOOK_MISS_TTL_S: no lyrics a moment ago -> don't re-ask
_lyrics_cache: Dict[str, list] = {}
_lyrics_miss: Dict[str, float] = {}


def _check_id(track_id: str) -> str:
    if not _ID_RE.fullmatch(track_id or ""):
        raise HTTPException(status_code=400, detail="bad track_id")
    return track_id


def _path_of(track_id: str) -> Path:
    from app.ui import server

    p = server._tracks.get(_check_id(track_id))
    if p is None or not p.exists():
        raise HTTPException(status_code=404, detail=f"Unknown track_id: {track_id}")
    return p


def _display_name(track_id: str, path: Path) -> str:
    """server.list_tracks' rule."""
    from app.ui import server

    return server._track_names.get(track_id) or server._name_from_tags(track_id, path) or path.stem


def _has_stems(track_id: str) -> bool:
    from app.ui import server

    try:
        return bool(server._cached_stems4(track_id))   # cache lookup only: never separates
    except Exception:  # noqa: BLE001 -- unreadable file / no stem cache
        return False


def _base(track_id: str, path: Path) -> dict:
    """The entry without has_stems (that one hashes the file: done for the served page only)."""
    from app.ui import server
    from app.ui.services.download_service import _is_live, _is_mix
    from app.ui.services.track_identity import clean_identity

    name = _display_name(track_id, path)
    artist, title = clean_identity(name)
    vibe = server._track_vibe(track_id)
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    return {"track_id": track_id, "display_name": name, "artist": "" if artist == "Unknown" else artist,
            "title": title, "ext": path.suffix.lower(), "bytes": size,
            "genre": vibe.get("genre"), "era": vibe.get("era"),
            "not_a_song": bool(_is_live(name) or _is_mix(name)), "art_url": album_art.art_url(track_id)}


def _entry(track_id: str, path: Path) -> dict:
    return {**_base(track_id, path), "has_stems": _has_stems(track_id)}


@router.get("/api/mobile/library")
def mobile_library(q: str = "", offset: int = 0, limit: int = 200):
    from app.ui import server

    if len(q) > MAX_Q:
        raise HTTPException(status_code=400, detail=f"q longer than {MAX_Q} characters")
    offset, limit = max(0, offset), max(1, min(MAX_LIMIT, limit))
    needle = q.strip().casefold()
    rows = []
    for tid, p in list(server._tracks.items()):
        if not p.exists():
            continue
        e = _base(tid, p)
        if needle and needle not in e["display_name"].casefold() and needle not in (e["genre"] or "").casefold():
            continue
        rows.append(e)
    rows.sort(key=lambda e: e["display_name"].casefold())
    page = [{**e, "has_stems": _has_stems(e["track_id"])} for e in rows[offset:offset + limit]]
    return {"total": len(rows), "offset": offset, "tracks": page}


@router.get("/api/mobile/tracks/{track_id}")
def mobile_track(track_id: str):
    return _entry(track_id, _path_of(track_id))


class LabelsBody(BaseModel):
    genre: Optional[str] = Field(default=None, max_length=80)
    era: Optional[str] = Field(default=None, max_length=40)


@router.post("/api/mobile/tracks/{track_id}/labels")
def mobile_labels(track_id: str, body: LabelsBody):
    """The owner's correction of a genre / era label, stored where the model's labels live."""
    from app.ui import server
    from app.ui.services.track_identity import clean_identity

    path = _path_of(track_id)
    for field, v in (("genre", body.genre), ("era", body.era)):
        if v is not None and _CONTROL_RE.search(v):
            raise HTTPException(status_code=400, detail=f"{field}: control characters are not allowed")
    key = server._genre_key(clean_identity(_display_name(track_id, path))[1])
    if not key:
        raise HTTPException(status_code=400, detail="this track has no title to label")
    for store, v in ((server._suggested_genres, body.genre), (server._suggested_eras, body.era)):
        if v is not None:
            server._set_label(store, key, v.strip())
    server._save_labels()
    vibe = server._track_vibe(track_id)
    return {"track_id": track_id, "genre": vibe.get("genre"), "era": vibe.get("era")}


def _lyric_lines(track_id: str, path: Path) -> list:
    """lyrics.for_file for a library track, aligned to the cached vocal stem when there is one.
    Reads only cached lyrics / ASR (for_file's default): never transcribes, never separates."""
    from app.music_brain.analysis import lyrics
    from app.ui import server

    if track_id in _lyrics_cache:
        return _lyrics_cache[track_id]
    miss = _lyrics_miss.get(track_id)
    if miss and time.time() - miss < LYRICS_MISS_TTL_S:
        return []
    name = _display_name(track_id, path)
    a = server.analyze_track(path)
    stems = None
    try:
        stems = server._cached_stems4(track_id)
    except Exception:  # noqa: BLE001 -- no stems: plain / LRC lines still come back
        pass
    vocal_path = (stems or {}).get("vocals") or None
    y = server._load_audio(vocal_path, sr=11025, mono=True)[0] if vocal_path else None
    lines = list(lyrics.for_file(name, y, 11025, duration=a.duration, vocal_path=vocal_path) or [])
    if lines:
        _lyrics_cache[track_id] = lines
        _lyrics_miss.pop(track_id, None)
    else:
        _lyrics_miss[track_id] = time.time()
    return lines


@router.get("/api/mobile/tracks/{track_id}/lyrics")
def mobile_lyrics(track_id: str):
    from app.music_brain.analysis import lyrics

    lines = _lyric_lines(track_id, _path_of(track_id))
    return {"track_id": track_id, "lines": lines, "source": (lines[0].get("source") if lines else None),
            "transcribed": lyrics.transcribed(lines), "hooks": lyrics.hooks(lines)[:5]}


@router.get("/api/mobile/recordings")
def mobile_recordings():
    from app.ui import server

    out = []
    for rid, p in list(server._recordings.items()):
        try:
            st = p.stat()
        except OSError:
            continue                       # missing file
        out.append({"recording_id": rid, "url": f"/api/recordings/{rid}", "bytes": st.st_size,
                    "modified": st.st_mtime, "ext": p.suffix.lower()})
    out.sort(key=lambda r: r["modified"], reverse=True)
    return {"recordings": out}


def _sessions():
    from app.ui.services import replay_api

    d = replay_api.get_sessions()
    return {**d, "sessions": list(d.get("sessions") or [])[:SESSIONS_IN_SYNC]}


def _sync_parts() -> dict:
    """part -> zero-arg fn returning that route's JSON object (read lazily: tests patch them)."""
    from app.ui import server
    from app.ui.services import atlas_api, replay_api, supermove_api

    return {"macros": atlas_api.get_macros, "studied_sets": atlas_api.studied_sets,
            "supermoves": supermove_api.supermoves, "vetoes": server.get_vetoes,
            "liked": replay_api.get_liked, "sessions": _sessions, "samples": server.list_samples,
            "learned_moves": server.get_learned_moves}


@router.get("/api/mobile/sync")
def mobile_sync():
    """Every part the phone keeps, one round trip. A part that fails is null with its error."""
    out: dict = {}
    errors: Dict[str, str] = {}
    for part, fn in _sync_parts().items():
        try:
            out[part] = fn()
        except Exception as exc:  # noqa: BLE001 -- one broken part never fails the bundle
            out[part] = None
            errors[part] = f"{type(exc).__name__}: {exc}"[:300]
    out["errors"] = errors
    return out
