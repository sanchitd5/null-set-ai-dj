"""One folder per song: the single resolver for every song file (G25).

    <cache>/songs/<track_id>/
        audio.<ext>                       the song file
        stems/<model>/<stem>.<m4a|flac|wav>   vocals, drums, bass, other (no_vocals for the 2-stem set)
        lyrics.txt  lyrics.lrc            fetched lyrics (lrc only when synced)
        asr.txt                           faster-whisper transcript of the vocal stem (language line, then text)
        meta.json                         artist, title, source video, duration, language, verdict, timestamps

Readers never build these paths themselves: they ask this module. Every lookup tries the song folder first and then
the old flat layout (uploads/<id>.<ext>, stems/<audio sha256>_<model>/), so nothing breaks while files are still
being moved (app/ui/services/song_migrate.py moves them, never deletes, and leaves a symlink at each old path).
All directories are resolved at call time so tests (and AIDJ_CACHE_DIR) can redirect them.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Optional

AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aiff", ".aif", ".opus")
STEM_EXTS = (".m4a", ".flac", ".wav")          # preferred first: the phone-sized copy is the cheapest to serve
VERDICTS = ("verified", "mismatch", "no_lyrics", "instrumental")
_SAFE_ID = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


def _cache() -> Path:
    from app.music_brain.config import CACHE_DIR
    return Path(CACHE_DIR)


def songs_dir() -> Path:
    return _cache() / "songs"


def song_dir(track_id: str) -> Path:
    """<cache>/songs/<track_id>. A track id that is not a plain token (path separators, dots) is refused."""
    if not _SAFE_ID.match(track_id or ""):
        raise ValueError(f"bad track id: {track_id!r}")
    return songs_dir() / track_id


def audio_path(track_id: str, legacy: Optional[Path] = None) -> Optional[Path]:
    """The song file: audio.<ext> in its folder, else the old flat path (`legacy`, or uploads/<id>.<ext>)."""
    d = song_dir(track_id)
    for ext in AUDIO_EXTS:
        p = d / f"audio{ext}"
        if p.is_file():
            return p
    if legacy is not None and Path(legacy).is_file():
        return Path(legacy)
    up = _cache() / "uploads"
    for ext in AUDIO_EXTS:
        p = up / f"{track_id}{ext}"
        if p.is_file():
            return p
    return None


def stems_dir(track_id: str, model: str) -> Path:
    return song_dir(track_id) / "stems" / model


def legacy_stems_dir(audio_hash: str, model: str) -> Path:
    return _cache() / "stems" / f"{audio_hash}_{model}"


def stem_files(track_id: str, model: str, audio_hash: Optional[str] = None) -> Dict[str, Path]:
    """{stem name: file} of a song for one separation model. The folder wins; a name missing there falls back to the
    old stem cache (needs the song's audio sha256 for its directory name, and its manifest.json)."""
    out: Dict[str, Path] = {}
    d = stems_dir(track_id, model)
    if d.is_dir():
        for ext in reversed(STEM_EXTS):                  # later (preferred) extension overwrites
            for f in d.glob(f"*{ext}"):
                if f.is_file():
                    out[f.stem] = f
    if audio_hash:
        from app.music_brain.audio.audio_io import read_manifest
        for name, p in (read_manifest(legacy_stems_dir(audio_hash, model)) or {}).items():
            out.setdefault(name, Path(p))
    return out


def vocal_stem(track_id: str, models=("htdemucs_ft", "htdemucs"), audio_hash: Optional[str] = None) -> Optional[Path]:
    """The song's vocal stem, best model first, or None."""
    for m in models:
        v = stem_files(track_id, m, audio_hash).get("vocals")
        if v is not None:
            return v
    return None


def lyrics_path(track_id: str, synced: bool = False) -> Path:
    return song_dir(track_id) / ("lyrics.lrc" if synced else "lyrics.txt")


def asr_path(track_id: str) -> Path:
    return song_dir(track_id) / "asr.txt"


def meta_path(track_id: str) -> Path:
    return song_dir(track_id) / "meta.json"


def read_meta(track_id: str) -> dict:
    try:
        d = json.loads(meta_path(track_id).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_text(path: Path, text: str) -> None:
    """Atomic text write (tmp in the same folder, then replace); parents are created."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def update_meta(track_id: str, **fields) -> dict:
    """Merge fields into meta.json (None values drop the key) and stamp `updated`. Returns the new document."""
    meta = read_meta(track_id)
    for k, v in fields.items():
        if v is None:
            meta.pop(k, None)
        else:
            meta[k] = v
    meta["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    write_text(meta_path(track_id), json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


def verdict(track_id: str) -> Optional[dict]:
    """{verdict, score} from meta.json, or None when the song was never checked."""
    m = read_meta(track_id)
    v = m.get("verdict")
    return {"verdict": v, "score": m.get("score")} if v in VERDICTS else None
