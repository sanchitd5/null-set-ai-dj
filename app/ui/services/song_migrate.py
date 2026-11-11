"""G25 migration: move the flat song files into one folder per song (app/music_brain/song_store.py).

For every library track it plans two kinds of move:
  audio  uploads/<id>.<ext>                  -> songs/<id>/audio.<ext>
  stem   stems/<audio sha256>_<model>/<file> -> songs/<id>/stems/<model>/<file>

A move is copy, verify (size and sha256 of both files), then the old path is replaced by a symlink to the new file,
so a reader still using the old flat path keeps working and nothing is deleted. Dry run is the default and writes
nothing. A run is idempotent and resumable: a move whose old path already is a symlink to the new file is skipped,
a half-copied target (wrong size / hash) is copied again. Every move is logged and written to a backup manifest
songs/_migration/<timestamp>.json (old path, new path, size, sha256) before the old path is touched.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from app.music_brain import song_store as ss

BUF = 1 << 20


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(BUF):
            h.update(chunk)
    return h.hexdigest()


def plan(tracks: Dict[str, Path], models: Optional[List[str]] = None, hash_fn: Optional[Callable[[Path], str]] = None) -> List[dict]:
    """[{kind, track, src, dst}] for every file not yet moved. tracks: track id -> current audio path."""
    from app.music_brain.audio.audio_io import read_manifest
    from app.music_brain.audio.stem_service import file_hash

    hash_fn = hash_fn or file_hash
    out: List[dict] = []
    for tid, path in sorted(tracks.items()):
        path = Path(path)
        if not path.exists():
            continue
        out.append({"kind": "audio", "track": tid, "src": str(path), "dst": str(ss.song_dir(tid) / f"audio{path.suffix.lower()}")})
        try:
            audio_hash = hash_fn(path)
        except OSError:
            continue
        for d in sorted((ss._cache() / "stems").glob(f"{audio_hash}_*")):
            model = d.name[len(audio_hash) + 1:]
            if not model:
                continue
            for name, p in sorted((read_manifest(d) or {}).items()):
                p = Path(p)
                out.append({"kind": "stem", "track": tid, "src": str(p),
                            "dst": str(ss.stems_dir(tid, model) / f"{name}{p.suffix.lower()}")})
    return [m for m in out if not _done(m)]


def _done(m: dict) -> bool:
    src, dst = Path(m["src"]), Path(m["dst"])
    return src.is_symlink() and dst.is_file() and os.path.realpath(src) == os.path.realpath(dst)


def _move(m: dict) -> dict:
    """Copy-verify-link one file. Returns the manifest row. Raises on any mismatch, leaving the old path untouched."""
    src, dst = Path(m["src"]), Path(m["dst"])
    real = Path(os.path.realpath(src))
    row = {**m, "size": real.stat().st_size, "sha256": _sha(real)}
    if not (dst.is_file() and dst.stat().st_size == row["size"] and _sha(dst) == row["sha256"]):
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.part{os.getpid()}")
        try:
            shutil.copy2(real, tmp)
            if tmp.stat().st_size != row["size"] or _sha(tmp) != row["sha256"]:
                raise OSError(f"copy of {src} does not verify")
            os.replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)
    return row


def _link(src: Path, dst: Path) -> None:
    tmp = src.with_name(f".{src.name}.lnk{os.getpid()}")
    try:
        tmp.unlink(missing_ok=True)
        os.symlink(dst, tmp)
        os.replace(tmp, src)             # atomic: a reader sees the old file or the link, never neither
    finally:
        tmp.unlink(missing_ok=True)


def run(tracks: Dict[str, Path], dry_run: bool = True, log: Callable[[str], None] = lambda s: None,
        should_stop: Callable[[], bool] = lambda: False, limit: int = 0,
        hash_fn: Optional[Callable[[Path], str]] = None) -> dict:
    """Plan, then (unless dry_run) move. -> {planned, moved, failed, bytes, dry_run, manifest}."""
    moves = plan(tracks, hash_fn=hash_fn)
    if limit:
        moves = moves[:limit]
    res = {"planned": len(moves), "moved": 0, "failed": 0, "bytes": 0, "dry_run": dry_run, "manifest": None}
    if dry_run:
        for m in moves:
            log(f"would move {m['kind']} {m['src']} -> {m['dst']}")
        return res
    mdir = ss.songs_dir() / "_migration"
    mdir.mkdir(parents=True, exist_ok=True)
    manifest = mdir / f"{time.strftime('%Y%m%dT%H%M%S')}.json"
    rows: List[dict] = []
    res["manifest"] = str(manifest)
    for m in moves:
        if should_stop():
            log("stopped: the next run resumes here")
            break
        try:
            row = _move(m)
            rows.append(row)
            ss.write_text(manifest, json.dumps({"moves": rows}, indent=1))      # on disk before the old path changes
            _link(Path(m["src"]), Path(m["dst"]))
            res["moved"] += 1
            res["bytes"] += row["size"]
            log(f"moved {m['kind']} {m['src']} -> {m['dst']}")
        except OSError as exc:
            res["failed"] += 1
            log(f"FAILED {m['src']}: {exc}")
    return res
