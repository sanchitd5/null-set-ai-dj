"""AFRIX: excluded songs (dj-config EXCLUDED_RULES; matcher app.music_brain.excluded).

  sweep(tracks, names, apply)   which library songs match; apply=True quarantines the strong ones (reversible)
  quarantine(tid, ...)          songs/<id>/ -> <cache>/_excluded_quarantine/<stamp>/songs/<id>/, ledger entry, no delete
  restore(tid)                  put a quarantined song back
  gate_name(name)               download gate: a reason string when a name / title must never be fetched
  GET /api/library/removed      the "Removed" review list the clients show (review=true entries stay in the library)

Ledger: <cache>/excluded.json  {track_id: {name, reason, cue, review, ts, qpath|None}}.
Weak cues (one sung-language or title cue only) are review-only: listed, never moved.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

from fastapi import APIRouter

from app.music_brain import excluded, song_store
from app.music_brain.analysis import genre_labels, lang_id

QDIR = "_excluded_quarantine"
LEDGER = "excluded.json"
_lock = threading.Lock()
router = APIRouter()


def _cache() -> Path:
    from app.music_brain.config import CACHE_DIR
    return Path(CACHE_DIR)


def _read() -> Dict[str, dict]:
    try:
        d = json.loads((_cache() / LEDGER).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(d: Dict[str, dict]) -> None:
    p = _cache() / LEDGER
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def gate_name(name: str, **fields) -> Optional[str]:
    """Download gate: the refusal text when `name` (artist - title, a delivered title, a channel) is excluded, else None.
    Weak cues never refuse a download on their own."""
    m = excluded.excluded_reason(display_name=name or "", **fields)
    return m["reason"] if m and m["strong"] else None


def evaluate(tid: str, name: str, genres: Dict[str, str]) -> Optional[dict]:
    g = genres.get(genre_labels.name_key(name))
    rec = lang_id.get(tid) or {}
    return excluded.excluded_reason(display_name=name, genres=[g] if g else [], lang_id=rec.get("lang") if not rec.get("instr") else None)


def sweep(tracks: Iterable[str], names: Dict[str, str], apply: bool = False,
          on_removed: Optional[Callable[[str], None]] = None) -> list:
    """Evaluate every library track. Returns [{track_id, name, reason, cue, review}]. apply=True quarantines the strong
    matches (on_removed(tid) lets the server drop its in-memory registry) and records review-only ones in the ledger."""
    genres, _eras = genre_labels.load()
    out = []
    for tid in list(tracks):
        name = names.get(tid) or ""
        m = evaluate(tid, name, genres)
        if not m:
            continue
        out.append({"track_id": tid, "name": name, "reason": m["reason"], "cue": m["cue"], "review": not m["strong"]})
    if apply:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for e in out:
            if e["review"]:
                with _lock:
                    led = _read()
                    led.setdefault(e["track_id"], {"name": e["name"], "reason": e["reason"], "cue": e["cue"], "review": True,
                                                   "ts": time.time(), "qpath": None})
                    _write(led)
            else:
                quarantine(e["track_id"], e["name"], e["reason"], e["cue"], stamp)
                if on_removed:
                    on_removed(e["track_id"])
    return out


def quarantine(tid: str, name: str, reason: str, cue: str = "", stamp: Optional[str] = None) -> Optional[str]:
    """Move songs/<id>/ out of the library (reversible). Returns the quarantine path or None when there was no folder."""
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    src = song_store.song_dir(tid)
    dst = _cache() / QDIR / stamp / "songs" / tid
    q = None
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        q = str(dst)
    with _lock:
        led = _read()
        led[tid] = {"name": name, "reason": reason, "cue": cue, "review": False, "ts": time.time(), "qpath": q}
        _write(led)
    print(f"[excluded] quarantined {tid} {name!r}: {reason}", flush=True)
    return q


def restore(tid: str) -> bool:
    with _lock:
        led = _read()
        e = led.get(tid)
        if not e:
            return False
        if e.get("qpath") and Path(e["qpath"]).exists():
            dst = song_store.song_dir(tid)
            if dst.exists():
                return False
            shutil.move(e["qpath"], str(dst))
        led.pop(tid, None)
        _write(led)
    return True


def removed_list() -> list:
    return [{"track_id": t, "display_name": e.get("name", ""), "reason": e.get("reason", ""), "review": bool(e.get("review")),
             "ts": e.get("ts", 0)} for t, e in _read().items()]


def quarantined_ids() -> set:
    return {t for t, e in _read().items() if not e.get("review")}


@router.get("/api/library/removed")
def get_removed() -> dict:
    return {"removed": removed_list()}
