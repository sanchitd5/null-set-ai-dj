"""Retention for <cache>/_pick_quarantine/ (download_service._quarantine): wrong-song audio waits
AUDIO_DAYS for a look, its small JSON note NOTE_DAYS, and the audio never takes more than CAP_BYTES
(oldest first). Runs at startup and then daily as the `quarantine_cleanup` job (job_registry, see
jobs_builtin.py); GET /api/admin/quarantine reports the counts
(behind the same auth middleware as every /api route)."""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Depends

from app.ui.services import admin

AUDIO_DAYS = 7
NOTE_DAYS = 30
CAP_BYTES = 2 * 1024**3
INTERVAL_S = 24 * 3600                          # the job's period
DAY = 86400.0

router = APIRouter()


def _dir() -> Path:
    from app.music_brain.config import CACHE_DIR
    from app.ui.services.download_service import PICK_QUARANTINE

    return Path(CACHE_DIR) / PICK_QUARANTINE


def _stamp(p: Path) -> float:
    """When it was quarantined: the leading unix time of the file name, else its mtime."""
    head = p.name.split("-", 1)[0]
    try:
        return float(int(head))
    except ValueError:
        return p.stat().st_mtime


def _entries(qdir: Path) -> tuple[list[tuple[float, int, Path]], list[tuple[float, int, Path]]]:
    audio, notes = [], []
    if not qdir.is_dir():
        return audio, notes
    for p in qdir.iterdir():
        try:
            if not p.is_file():
                continue
            (notes if p.suffix == ".json" else audio).append((_stamp(p), p.stat().st_size, p))
        except OSError:
            continue
    return sorted(audio), sorted(notes)


def summary(qdir: Path | None = None) -> dict:
    audio, notes = _entries(qdir or _dir())
    return {"audio_files": len(audio), "audio_bytes": sum(s for _, s, _ in audio),
            "notes": len(notes), "note_bytes": sum(s for _, s, _ in notes),
            "oldest_audio": audio[0][0] if audio else None,
            "policy": {"audio_days": AUDIO_DAYS, "note_days": NOTE_DAYS, "cap_bytes": CAP_BYTES}}


def cleanup(qdir: Path | None = None, now: float | None = None) -> dict:
    """Apply the policy once. Returns what was removed."""
    qdir = qdir or _dir()
    now = time.time() if now is None else now
    audio, notes = _entries(qdir)
    gone = {"audio": 0, "notes": 0, "bytes": 0}

    def rm(p: Path, size: int, kind: str) -> None:
        try:
            p.unlink()
        except OSError:
            return
        gone[kind] += 1
        gone["bytes"] += size

    keep = []
    for ts, size, p in audio:
        if now - ts > AUDIO_DAYS * DAY:
            rm(p, size, "audio")
        else:
            keep.append((ts, size, p))
    total = sum(s for _, s, _ in keep)
    for ts, size, p in keep:                       # oldest first
        if total <= CAP_BYTES:
            break
        rm(p, size, "audio")
        total -= size
    for ts, size, p in notes:
        if now - ts > NOTE_DAYS * DAY:
            rm(p, size, "notes")
    return gone


@router.get("/api/admin/quarantine", dependencies=[Depends(admin.require_admin)])   # admin gate: DJ_ADMIN_EMAILS
def get_quarantine() -> dict:
    return summary()
