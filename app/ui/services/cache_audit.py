"""Song cache audit (HF9): flags library files that are probably not the song they are named after.

FLAG ONLY: nothing here deletes or moves audio. The `cache_audit` job (jobs_builtin.py) scans the library,
writes <cache>/cache_flags.json, and /admin/jobs shows the result; GET /api/admin/cache-audit lists it and
POST /api/admin/cache-audit/{track_id}/refetch downloads the song again from its source video (the old file
stays). The phone reads GET /api/library/bad and drops its own cached copy of every flagged id.

Classes (each one is a way a wrong file got into the cache, see wt/HF9_AUDIT.md):
  duplicate_source  the same YouTube video was downloaded twice (a watch URL never reused the first copy and the
                    content-hash id differs per download): every copy after the oldest is flagged, `of` = the kept id
  bad_length        shorter than 90 s or 9 min and longer: a clip, a spoken video, a full album or a set
  not_a_track       the name says mix / set / live / interview / full album
  lyric_mismatch    the lyric check (G25, lyric_check job) found the vocal stem does not sing this song's lyrics
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

MIN_SECS = 90
MAX_SECS = 9 * 60
FILE = "cache_flags.json"
BAD_STATUSES = ("open", "refetched")          # a refetched flag still marks the OLD file as wrong
_YT_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([0-9A-Za-z_-]{11})(?![0-9A-Za-z_-])")
_lock = threading.Lock()


def video_id_of(url: str) -> Optional[str]:
    m = _YT_ID.search(url or "")
    return m.group(1) if m else None


def probe(path: Path) -> dict:
    """{dur, vid}: duration and the YouTube video id from the purl tag yt-dlp wrote. Missing values are None."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags=purl",
                            "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
        fm = json.loads(r.stdout or "{}").get("format", {})
        dur = float(fm["duration"]) if fm.get("duration") else None
        return {"dur": dur, "vid": video_id_of((fm.get("tags") or {}).get("purl", ""))}
    except Exception:
        return {"dur": None, "vid": None}


def scan(tracks: Dict[str, Path], names: Dict[str, str], known_sources: Dict[str, str],
         probe_fn: Callable[[Path], dict] = probe, shape_fn: Optional[Callable[[str], bool]] = None,
         verdicts: Optional[Dict[str, dict]] = None) -> dict:
    """{track_id: {"classes": [...], "detail": str, "video": id|None, "of": id|None}} for every suspect file.
    known_sources: track id -> video id already on record (art/sources.json); the file's own tag fills the rest."""
    if shape_fn is None:
        from app.ui.services.download_service import _is_live, _is_mix, _is_non_music

        def shape_fn(name: str) -> bool:
            return bool(_is_mix(name) or _is_non_music(name) or _is_live(name))

    flags: Dict[str, dict] = {}
    seen: Dict[str, list] = {}                      # video id -> [(mtime, track id)]

    def add(tid: str, cls: str, detail: str, video: Optional[str], of: Optional[str] = None) -> None:
        f = flags.setdefault(tid, {"classes": [], "detail": [], "video": video, "of": None})
        f["classes"].append(cls)
        f["detail"].append(detail)
        f["of"] = f["of"] or of

    for tid, path in tracks.items():
        if not path.exists():
            continue
        info = probe_fn(path)
        vid = known_sources.get(tid) or info.get("vid")
        name = names.get(tid) or path.stem
        dur = info.get("dur")
        if dur is not None and not (MIN_SECS <= dur < MAX_SECS):
            add(tid, "bad_length", f"{dur:.0f}s", vid)
        if shape_fn(name):
            add(tid, "not_a_track", name[:80], vid)
        v = (verdicts or {}).get(tid)
        if v and v.get("verdict") == "mismatch":
            add(tid, "lyric_mismatch", f"lyric score {v.get('score')}", vid)
        if vid:
            try:
                seen.setdefault(vid, []).append((path.stat().st_mtime, tid))
            except OSError:
                pass
    for vid, rows in seen.items():
        if len(rows) < 2:
            continue
        rows.sort()
        keep = rows[0][1]
        for _, tid in rows[1:]:
            add(tid, "duplicate_source", f"same video {vid} as {keep}", vid, of=keep)
    for f in flags.values():
        f["detail"] = "; ".join(f["detail"])
    return flags


def _path(cache: Path) -> Path:
    return Path(cache) / FILE


def load(cache: Path) -> dict:
    try:
        d = json.loads(_path(cache).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(cache: Path, data: dict) -> None:
    p = _path(cache)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def merge(cache: Path, found: dict, now: float) -> dict:
    """Store a scan: new suspects open, known ones keep their status, files no longer suspect drop out.
    Returns the counts by class."""
    with _lock:
        old = load(cache)
        out = {}
        for tid, f in found.items():
            prev = old.get(tid) or {}
            out[tid] = {**f, "status": prev.get("status", "open"), "at": prev.get("at", now),
                        "new_id": prev.get("new_id")}
        _save(cache, out)
    return summary(out)


def summary(flags: dict) -> dict:
    counts: Dict[str, int] = {}
    for f in flags.values():
        for c in f.get("classes", []):
            counts[c] = counts.get(c, 0) + 1
    return {"flagged": len(flags), "by_class": counts}


def mark(cache: Path, track_id: str, **fields) -> bool:
    with _lock:
        data = load(cache)
        if track_id not in data:
            return False
        data[track_id].update(fields)
        _save(cache, data)
    return True


def apply_verdicts(cache: Path, verdicts: Dict[str, dict], now: float) -> dict:
    """Fold lyric-check verdicts into the flags without a full scan: a mismatch adds the lyric_mismatch class, a song
    whose verdict is no longer a mismatch loses it (the flag goes when no class is left)."""
    with _lock:
        data = load(cache)
        for tid, v in verdicts.items():
            f = data.get(tid)
            if v.get("verdict") == "mismatch":
                if f is None:
                    f = data[tid] = {"classes": [], "detail": "", "video": None, "of": None, "status": "open", "at": now,
                                     "new_id": None}
                if "lyric_mismatch" not in f["classes"]:
                    f["classes"].append("lyric_mismatch")
                    f["detail"] = "; ".join(x for x in (f.get("detail"), f"lyric score {v.get('score')}") if x)
            elif f is not None and "lyric_mismatch" in f["classes"]:
                f["classes"].remove("lyric_mismatch")
                if not f["classes"]:
                    del data[tid]
        _save(cache, data)
    return summary(data)


def bad_ids(cache: Path) -> list:
    """Ids the phone must drop from its own cache."""
    return sorted(t for t, f in load(cache).items() if f.get("status") in BAD_STATUSES)


def backfill_sources(tracks: Dict[str, Path], probe_fn: Callable[[Path], dict] = probe) -> int:
    """Record the video id of every library file that has one in its tags but none on record, so a second
    request for the same video finds the first copy. Returns how many were added."""
    from app.ui.services import album_art

    n = 0
    for tid, path in tracks.items():
        if album_art._video_id(tid) or not path.exists():
            continue
        vid = probe_fn(path).get("vid")
        if vid:
            album_art.remember_source(tid, f"https://www.youtube.com/watch?v={vid}")
            n += 1
    return n


def run(tracks: Dict[str, Path], names: Dict[str, str], cache: Path, now: float,
        log: Callable[[str], None] = lambda s: None) -> dict:
    from app.ui.services import album_art

    added = backfill_sources(tracks)
    if added:
        log(f"recorded {added} missing source video ids")
    sources = {t: v for t in tracks if (v := album_art._video_id(t))}
    from app.music_brain import song_store

    verdicts = {t: v for t in tracks if (v := song_store.verdict(t))}
    found = scan(tracks, names, sources, verdicts=verdicts)
    res = merge(cache, found, now)
    log(f"{res['flagged']} suspect files {res['by_class']}")
    return res
