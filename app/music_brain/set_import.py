"""IMPORT a studied set's songs into the library, so the console can mimic the set.

The set learner already downloaded most songs of a studied set into
CACHE_DIR/sets/<set_id>/songs/ (and separated + analysed them, caches keyed by content).
`import-set` registers those files as library tracks exactly like an upload does: the id is
sha256(bytes)[:16], the display name is the tracklist's "Artist - Title". It goes through
the app's own upload code: POST /api/tracks when the console is running (its in-memory
registry and name map stay the truth), else server._register_downloaded (copy, no move).
The stems and analysis the study made are found again by content hash: nothing is
separated or analysed twice, nothing is downloaded.

Skipped: no file, the learner's "probably the wrong download", "ID" entries (no song),
sample packs, isolated stems, live / mix / non-music files (the download filter's own
rules), and songs the library already holds (same bytes, or the same recording by name
through the dedup identity + alias map: that copy is reused).
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

APP_URL = "http://127.0.0.1:8000"

# "ID", "ID - ID", "ID ID - Higher", "Adam Beyer - ID": an unreleased track, no song to import
_ID_TITLE = re.compile(r"^\s*id(\s*[-–—]?\s*id)?\s*([-–—]|$)|[-–—]\s*id\s*$", re.I)
_NOT_THE_SONG = re.compile(r"\b(samples?|sample[\s_-]*pack|presets?|serum|wav[\s_-]*samples|construction[\s_-]*kit|"
                           r"isolated[\s_-]*(vocals?|stems?)|acapella|a[\s_-]*cappella|instrumental[\s_-]*only)\b", re.I)


# a release version, not a DJ mix: "Extended Mix", "Original Mix" (the mix filter must not see them)
_VERSION_MIX = re.compile(r"\b(extended|original|club|radio|dub|vocal|instrumental|edit)\s+mix\b", re.I)


def _why_skip(t: dict) -> Optional[str]:
    from app.ui import download_service as dl

    title = str(t.get("title") or "")
    path = t.get("path")
    if _ID_TITLE.search(title):
        return "ID (no song named)"
    if not path or not Path(path).is_file():
        return "not downloaded by the learner"
    if t.get("likely_wrong_song"):
        return f"probably the wrong download (heard {float(t.get('heard_share') or 0):.0%} of its slot)"
    stem = Path(path).stem.replace("_", " ")
    if _NOT_THE_SONG.search(stem):
        return f"not the song: {Path(path).name}"
    if dl._is_live(stem) or dl._is_mix(_VERSION_MIX.sub(" ", stem)) or dl._is_non_music(stem):
        return f"live / mix / non-music file: {Path(path).name}"
    return None


def content_id(path: Path) -> str:
    """The library id of a file: sha256(bytes)[:16], exactly like POST /api/tracks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def plan(cache_dir: Path, set_id: str) -> List[dict]:
    """One row per tracklist entry, set order: {position, title, path, action, id, why}.
    action: import | reuse (the library holds it) | skip."""
    from app.music_brain import studied_combos as sc
    from app.ui import dedup_songs as ds

    cache_dir = Path(cache_dir)
    study = json.loads((cache_dir / "sets" / set_id / "study.json").read_text(encoding="utf-8"))
    names = sc.library_names(cache_dir)
    uploads = {p.stem for p in (cache_dir / "uploads").glob("*") if not p.name.startswith("_")} \
        if (cache_dir / "uploads").is_dir() else set()
    res = sc.Resolver({k: v for k, v in names.items() if k in uploads}, ds.load_aliases(cache_dir))
    rows, seen = [], {}
    for i, t in enumerate(study.get("tracks") or []):
        title = str(t.get("title") or "")
        row = {"position": i + 1, "title": title, "path": t.get("path"), "action": "skip", "id": None, "why": _why_skip(t)}
        rows.append(row)
        if row["why"]:
            continue
        cid = content_id(Path(t["path"]))
        if cid in seen:                                     # listed twice in the set
            row.update(action="reuse", id=seen[cid], why="listed earlier in the set")
        elif cid in uploads:
            row.update(action="reuse", id=ds.resolve_alias(cid, cache_dir), why="already in the library (same file)")
        elif res.find(title):
            row.update(action="reuse", id=res.find(title), why=f"library copy of the same recording: {names.get(res.find(title))}")
        else:
            row.update(action="import", id=cid, why="new")
        seen[cid] = row["id"]
    return rows


def _safe_name(title: str) -> str:
    return re.sub(r"[/\\:\0]+", " ", title).strip()[:180] or "track"


def _post_upload(path: Path, name: str, url: str = APP_URL) -> dict:
    """POST /api/tracks on the running console (multipart, stdlib only; localhost)."""
    boundary = uuid.uuid4().hex
    fname = f"{_safe_name(name)}{path.suffix or '.mp3'}"
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fname}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
    body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{url}/api/tracks", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def _register_offline(path: Path, name: str) -> dict:
    """The console is not running: its own registration code, copying (the study keeps its file)."""
    from app.ui import server

    return server._register_downloaded([path], names=[name], move=False, queue=False)[0]


def apply(rows: List[dict], app_running: Optional[bool] = None,
          upload: Optional[Callable[[Path, str], dict]] = None) -> List[dict]:
    """Register every `import` row; -> the rows, each import carrying `result` or `error`."""
    from app.ui import dedup_songs as ds

    if upload is None:
        running = ds.app_is_running() if app_running is None else app_running
        upload = _post_upload if running else _register_offline
    for r in rows:
        if r["action"] != "import":
            continue
        try:
            res = upload(Path(r["path"]), r["title"])
            tid = res.get("track_id")
            if tid != r["id"]:
                raise ValueError(f"registered as {tid}, expected {r['id']}")
            r["result"] = res
        except Exception as exc:  # noqa: BLE001 -- one bad file must not stop the set
            r["error"] = str(exc)[:200]
    return rows


def summary(rows: List[dict]) -> dict:
    playable = [r for r in rows if r["action"] in ("import", "reuse") and not r.get("error")]
    return {"entries": len(rows), "playable": len(playable),
            "imported": sum(1 for r in rows if r["action"] == "import" and not r.get("error")),
            "reused": sum(1 for r in rows if r["action"] == "reuse"),
            "skipped": [{"position": r["position"], "title": r["title"], "why": r["why"]} for r in rows if r["action"] == "skip"],
            "errors": [{"position": r["position"], "title": r["title"], "error": r["error"]} for r in rows if r.get("error")]}
