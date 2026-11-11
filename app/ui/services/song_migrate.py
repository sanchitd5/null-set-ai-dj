"""G26 migration v2: move the flat song files into one folder per song (app/music_brain/song_store.py). Rename only.

For every library track it plans these steps:
  audio   uploads/<id>.<ext>                  -> songs/<id>/audio.<ext>        (rename)
  stem    stems/<audio sha256>_<variant>/<f>  -> songs/<id>/stems/<variant>/<f> (rename)
  manifest  the new stem folder gets its manifest.json (the cache-hit marker, a few hundred bytes)
  retire  the old stem folder's manifest.json is removed and the folder dropped when it is empty
  unlink  an old path that is a symlink left by the halted G25 run (it points into songs/) is removed, once the
          file it points at is verified readable. Only symlinks are ever removed; audio is never deleted.

No symlinks are created (exfat has none: mergerfs fails with EPERM when it places a link there) and no bytes are
copied (bulk writes wedge the exfat writeback): a rename is a metadata change on the file's own disk. A rename that
would cross disks (EXDEV) is skipped and reported, never copied.

Work runs in batches. After each batch every track it touched must still resolve to a readable file of the recorded
size; on the first failure the batch's renames are moved back and the run stops. Dry run (the default) writes nothing.
Idempotent and resumable: every step is planned from what is on disk now. Each batch is also written to the backup
manifest songs/_migration/<timestamp>.json (old path, new path, size) before the next one starts.
"""
from __future__ import annotations

import errno
import json
import os
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from app.music_brain import song_store as ss

BATCH = 25


def _lexists(p: Path) -> bool:
    return os.path.lexists(p)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _file_step(kind: str, tid: str, src: Path, dst: Path) -> Optional[dict]:
    """The step that brings one file to its place, or None when nothing is to do (or it is not safe to act)."""
    m = {"kind": kind, "track": tid, "src": str(src), "dst": str(dst)}
    if not _lexists(src):
        return None                                       # already gone from the old place
    if src.is_symlink():
        return {**m, "kind": "unlink"} if dst.is_file() and _same_file(src, dst) else None
    if not src.is_file():
        return None
    if dst.is_file():                                     # the same name at both places: leave both, report
        return {**m, "kind": "conflict"}
    return m


def plan(tracks: Dict[str, Path], hash_fn: Optional[Callable[[Path], str]] = None) -> List[dict]:
    """[{kind, track, src, dst, ...}] for every step not done yet. tracks: track id -> a current audio path."""
    from app.music_brain.audio.audio_io import manifest_stems, read_manifest
    from app.music_brain.audio.stem_service import file_hash

    hash_fn = hash_fn or file_hash
    cache = ss._cache()
    out: List[dict] = []
    for tid, path in sorted(tracks.items()):
        path = Path(path)
        have = ss.audio_path(tid, legacy=path)
        if have is None:
            continue
        songd = ss.song_dir(tid)
        flat = {path, *(cache / "uploads" / f"{tid}{e}" for e in ss.AUDIO_EXTS)}
        for f in sorted(flat):
            if f.parent == songd or not _lexists(f):
                continue
            step = _file_step("audio", tid, f, songd / f"audio{f.suffix.lower()}")
            if step:
                out.append(step)
        try:
            audio_hash = hash_fn(have)
        except OSError:
            continue
        for d in sorted((cache / "stems").glob(f"{audio_hash}_*")):
            variant = d.name[len(audio_hash) + 1:]
            if not variant or not d.is_dir():
                continue
            try:
                raw = manifest_stems(json.loads((d / "manifest.json").read_text(encoding="utf-8")))
            except (OSError, ValueError):
                raw = None
            if not raw:
                continue
            new_dir = ss.stems_dir(tid, variant)
            dsts: Dict[str, str] = {}
            for name, p in sorted(raw.items()):
                p = Path(p)
                dst = new_dir / f"{name}{p.suffix.lower()}"
                dsts[name] = str(dst)
                step = _file_step("stem", tid, p, dst)
                if step:
                    out.append(step)
            if read_manifest(new_dir) is None:
                out.append({"kind": "manifest", "track": tid, "src": str(d / "manifest.json"),
                            "dst": str(new_dir / "manifest.json"), "stems": dsts})
            out.append({"kind": "retire", "track": tid, "src": str(d), "dst": str(new_dir / "manifest.json")})
    return out


def _move(m: dict) -> dict:
    """Rename one file into its folder. Returns the backup-manifest row (with the size).
    A rename across disks (EXDEV) raises "skipped": no copy fallback by design."""
    src, dst = Path(m["src"]), Path(m["dst"])
    size = src.stat().st_size
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise OSError(f"skipped (cross-disk rename, no copy by design): {src}") from exc
        raise
    if dst.stat().st_size != size:
        os.rename(dst, src)                  # put it back; never leave a short file
        raise OSError(f"size changed while moving {src}")
    return {**m, "size": size}


def _readable(p: Path, size: Optional[int] = None) -> bool:
    try:
        with open(p, "rb") as f:
            f.read(1)
        return size is None or p.stat().st_size == size
    except OSError:
        return False


def _unlink_link(m: dict) -> dict:
    """Remove an old symlink (never a regular file) after the file it points at is verified readable."""
    src, dst = Path(m["src"]), Path(m["dst"])
    if not src.is_symlink():
        raise OSError(f"not a symlink, left alone: {src}")
    if not _readable(dst):
        raise OSError(f"target unreadable, symlink kept: {dst}")
    size = dst.stat().st_size
    os.unlink(src)
    return {**m, "size": size}


def _verify(rows: List[dict]) -> Optional[str]:
    """Every song the batch touched must still resolve to a readable file of the size it had."""
    for r in rows:
        dst = Path(r["dst"])
        if not _readable(dst, r["size"]):
            return f"{dst} unreadable or wrong size"
        if r["kind"] == "audio":
            got = ss.audio_path(r["track"])
            if got is None or not _readable(got):
                return f"track {r['track']} no longer resolves to a readable file"
    return None


def _rollback(rows: List[dict], log: Callable[[str], None]) -> int:
    n = 0
    for r in reversed(rows):
        if r["kind"] in ("audio", "stem"):            # unlinked symlinks need no undo: the file is at its new path
            try:
                if Path(r["dst"]).is_file() and not _lexists(Path(r["src"])):
                    os.rename(r["dst"], r["src"])
                    n += 1
            except OSError as exc:
                log(f"ROLLBACK FAILED {r['dst']} -> {r['src']}: {exc}")
    return n


def _write_manifest_step(m: dict) -> bool:
    from app.music_brain.audio.audio_io import write_manifest
    dsts = m["stems"]
    if not all(Path(p).is_file() for p in dsts.values()):
        return False                                   # some file was skipped: no manifest until it is moved
    write_manifest(Path(m["dst"]).parent, dsts)
    return True


def _retire(m: dict) -> bool:
    """Drop the old stem folder's manifest (a few hundred bytes) and the folder when empty; only once the new
    folder reads as a complete set."""
    from app.music_brain.audio.audio_io import read_manifest
    if read_manifest(Path(m["dst"]).parent) is None:
        return False
    d = Path(m["src"])
    (d / "manifest.json").unlink(missing_ok=True)
    try:
        d.rmdir()
    except OSError:
        pass                                           # something else is still in there: leave it
    return True


def run(tracks: Dict[str, Path], dry_run: bool = True, log: Callable[[str], None] = lambda s: None,
        should_stop: Callable[[], bool] = lambda: False, limit: int = 0,
        hash_fn: Optional[Callable[[Path], str]] = None, batch: int = BATCH) -> dict:
    """Plan, then (unless dry_run) move. -> {planned, moved, unlinked, skipped, failed, bytes, dry_run, manifest}."""
    steps = plan(tracks, hash_fn=hash_fn)
    if limit:
        steps = steps[:limit]
    res = {"planned": len(steps), "moved": 0, "unlinked": 0, "skipped": 0, "failed": 0, "bytes": 0,
           "rolled_back": 0, "dry_run": dry_run, "manifest": None}
    if dry_run:
        for m in steps:
            log(f"would {m['kind']} {m['src']} -> {m['dst']}")
        return res
    mdir = ss.songs_dir() / "_migration"
    mdir.mkdir(parents=True, exist_ok=True)
    manifest = mdir / f"{time.strftime('%Y%m%dT%H%M%S')}.json"
    res["manifest"] = str(manifest)
    backup: List[dict] = []
    size = max(1, batch)
    for i in range(0, len(steps), size):
        if should_stop():
            log("stopped: the next run resumes here")
            break
        chunk = steps[i:i + size]
        done: List[dict] = []
        failed: Optional[str] = None
        for m in chunk:
            k = m["kind"]
            try:
                if k in ("audio", "stem"):
                    done.append(_move(m))
                elif k == "unlink":
                    done.append(_unlink_link(m))
                elif k == "conflict":
                    res["skipped"] += 1
                    log(f"SKIPPED both exist, left alone: {m['src']} and {m['dst']}")
            except OSError as exc:
                if "skipped (cross-disk" in str(exc):
                    res["skipped"] += 1
                    log(str(exc))
                else:
                    failed = f"{m['src']}: {exc}"
                    break
        failed = failed or _verify([r for r in done if r["kind"] != "unlink"])
        if failed:
            res["rolled_back"] += _rollback(done, log)
            res["failed"] += 1
            log(f"FAILED {failed}; batch moved back, stopping")
            break
        backup += done
        if done:
            ss.write_text(manifest, json.dumps({"moves": backup}, indent=1))
        for r in done:
            if r["kind"] == "unlink":
                res["unlinked"] += 1
            else:
                res["moved"] += 1
                res["bytes"] += r["size"]
            log(f"{r['kind']} {r['src']} -> {r['dst']}")
        for m in chunk:                                    # after the files are verified: markers and tidy-up
            try:
                if m["kind"] == "manifest":
                    _write_manifest_step(m)
                elif m["kind"] == "retire":
                    _retire(m)
            except OSError as exc:
                log(f"note: {m['kind']} {m['src']}: {exc}")
    return res
