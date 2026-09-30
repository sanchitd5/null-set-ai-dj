"""Genre / era labels the model gave songs, kept across server restarts.

The suggestion filters and the library fallback (/api/library/lockable) read these labels.
They used to live only in server memory, so after a restart a Punjabi set's library
fallback found no labelled song and the set sat in HOLD LOOP (session 2026-09-30_102327).

    CACHE_DIR/genre_labels.json
    {"version": 1, "labels": {"<title key>": {"genre": "punjabi pop", "era": "2020s"}}}

Keyed by the normalised clean title (server._genre_key), the same key the readers use:
the model labels songs by title, often before the song is downloaded and has a track id.
The knowledge export carries them per track id (knowledge/genre_labels.json), resolved
through each id's name, so another machine's library gets them by name.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

FILE = "genre_labels.json"
VERSION = 1
MAX_LABELS = 5000              # the model labels ~5 songs a call; a year of sets stays far below
MAX_FIELD = 80


def path(cache_dir: Optional[Path] = None) -> Path:
    from app.music_brain.config import CACHE_DIR

    return Path(cache_dir or CACHE_DIR) / FILE


def title_key(title: str) -> str:
    from app.ui.services.track_identity import clean_title

    return " ".join(clean_title(str(title or "")).lower().split())


def name_key(name: str) -> str:
    """ "Artist - Title (Official Video)" -> the title key."""
    from app.ui.services.track_identity import clean_identity

    return title_key(clean_identity(str(name or ""))[1])


def _clean(v) -> str:
    return str(v or "").strip()[:MAX_FIELD]


def load(p: Optional[Path] = None) -> Tuple[Dict[str, str], Dict[str, str]]:
    """(genres, eras) by title key; empty when the file is missing or unreadable."""
    try:
        d = json.loads(Path(p or path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    labels = d.get("labels") if isinstance(d, dict) else None
    genres, eras = {}, {}
    for k, v in (labels or {}).items():
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        if _clean(v.get("genre")):
            genres[k] = _clean(v.get("genre"))
        if _clean(v.get("era")):
            eras[k] = _clean(v.get("era"))
    return genres, eras


def save(genres: Dict[str, str], eras: Dict[str, str], p: Optional[Path] = None) -> bool:
    """Atomic write (tmp + replace). The newest MAX_LABELS keys win (dicts keep insertion
    order, the server re-inserts a relabelled key). False on an I/O error: never raises."""
    p = Path(p or path())
    labels: Dict[str, dict] = {}
    for k in list(dict.fromkeys(list(genres) + list(eras)))[-MAX_LABELS:]:
        e = {x: _clean(src.get(k)) for x, src in (("genre", genres), ("era", eras)) if _clean(src.get(k))}
        if e:
            labels[k] = e
    data = json.dumps({"version": VERSION, "labels": dict(sorted(labels.items()))},
                      sort_keys=True, indent=0, ensure_ascii=False)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(p)
        return True
    except OSError:
        return False


def backfill(genres: Dict[str, str], eras: Dict[str, str], tracked: dict, names: Dict[str, str]) -> int:
    """Labels from the knowledge export ({track id: {genre, era}} + its names.json) for songs
    that have none yet, matched by name, so the local label always wins. No model call.
    Returns how many labels were added."""
    added = 0
    for tid, lab in (tracked or {}).items():
        if not isinstance(lab, dict) or not names.get(tid):
            continue
        k = name_key(names[tid])
        if not k:
            continue
        if _clean(lab.get("genre")) and k not in genres:
            genres[k] = _clean(lab.get("genre"))
            added += 1
        if _clean(lab.get("era")) and k not in eras:
            eras[k] = _clean(lab.get("era"))
    return added


def for_export(names: Dict[str, str], genres: Dict[str, str], eras: Dict[str, str]) -> Dict[str, dict]:
    """{track id: {genre, era}} for every named song that has a label (knowledge export)."""
    out = {}
    for tid, n in names.items():
        k = name_key(n)
        e = {x: src[k] for x, src in (("genre", genres), ("era", eras)) if src.get(k)}
        if e:
            out[tid] = e
    return dict(sorted(out.items()))
