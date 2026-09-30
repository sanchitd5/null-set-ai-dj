"""IMPORT a studied set's songs into the library, so the console can mimic the set.

The set learner already downloaded most songs of a studied set into
CACHE_DIR/sets/<set_id>/songs/ (and separated + analysed them, caches keyed by content).
`import-set` registers those files as library tracks exactly like an upload does: the id is
sha256(bytes)[:16], the display name is the tracklist's "Artist - Title". It goes through
the app's own upload code: POST /api/tracks when the console is running (its in-memory
registry and name map stay the truth), else server._register_downloaded (copy, no move).
The stems and analysis the study made are found again by content hash: nothing is
separated or analysed twice, nothing is downloaded.

An unreleased "ID" entry has no song to find: its slot is cut out of the set recording
(start to the next entry's start, set_learner.clip_audio), registered the same way under
studied_combos.cut_name ("<title> [set cut <set_id> #<n>]") and analysed by the app's own
analyzer (GET /api/tracks/{id}/analysis: bpm, key, beat grid, phrases), so the studied
chain and FOLLOW SET can play it. The cut holds the DJ's blends at both ends.

Skipped: no file, the learner's "probably the wrong download", sample packs, isolated stems, live / mix / non-music files (the download filter's own
rules), and songs the library already holds (same bytes, or the same recording by name
through the dedup identity + alias map: that copy is reused).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

APP_URL = "http://127.0.0.1:8000"

from app.music_brain.atlas.studied_combos import _ID_ONLY as _ID_TITLE     # "ID", "ID ID - Higher", "Adam Beyer - ID"
_NOT_THE_SONG = re.compile(r"\b(samples?|sample[\s_-]*pack|presets?|serum|wav[\s_-]*samples|construction[\s_-]*kit|"
                           r"isolated[\s_-]*(vocals?|stems?)|acapella|a[\s_-]*cappella|instrumental[\s_-]*only)\b", re.I)


# a release version, not a DJ mix: "Extended Mix", "Original Mix" (the mix filter must not see them)
_VERSION_MIX = re.compile(r"\b(extended|original|club|radio|dub|vocal|instrumental|edit)\s+mix\b", re.I)


def _why_skip(t: dict) -> Optional[str]:
    from app.ui.services import download_service as dl

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


MIN_CUT_S = 45.0          # an ID slot shorter than this is only a blend, not a track


def _set_audio(cache_dir: Path, set_id: str, study: dict) -> Optional[Path]:
    """The studied set's own recording (the study's set_path, else CACHE_DIR/sets/<id>.*)."""
    p = study.get("set_path")
    if p and Path(p).is_file():
        return Path(p)
    for q in sorted((Path(cache_dir) / "sets").glob(f"{set_id}.*")):
        if q.suffix.lower() in (".mp3", ".m4a", ".wav", ".flac", ".opus", ".ogg", ".webm"):
            return q
    return None


def plan(cache_dir: Path, set_id: str) -> List[dict]:
    """One row per tracklist entry, set order: {position, title, path, action, id, why}.
    action: import | cut (an unreleased ID, cut out of the set recording at its slot) |
    reuse (the library holds it) | skip."""
    cache_dir = Path(cache_dir)
    study = json.loads((cache_dir / "sets" / set_id / "study.json").read_text(encoding="utf-8"))
    return plan_tracks(cache_dir, set_id, study.get("tracks") or [], _set_audio(cache_dir, set_id, study))


def plan_tracks(cache_dir: Path, set_id: str, tracks: List[dict], set_audio: Optional[Path] = None) -> List[dict]:
    """plan() over tracklist rows [{title, start, path, likely_wrong_song?, heard_share?}]
    (a study's tracks, or a bare tracklist's found songs). No set_audio: IDs are skipped."""
    from app.music_brain.atlas import studied_combos as sc
    from app.ui.services import dedup_songs as ds

    cache_dir = Path(cache_dir)
    names = sc.library_names(cache_dir)
    uploads = {p.stem for p in (cache_dir / "uploads").glob("*") if not p.name.startswith("_")} \
        if (cache_dir / "uploads").is_dir() else set()
    res = sc.Resolver({k: v for k, v in names.items() if k in uploads}, ds.load_aliases(cache_dir))
    rows, seen = [], {}
    rev = {n: tid for tid, n in names.items() if tid in uploads}
    for i, t in enumerate(tracks):
        title = str(t.get("title") or "")
        row = {"position": i + 1, "title": title, "path": t.get("path"), "action": "skip", "id": None, "why": _why_skip(t)}
        rows.append(row)
        if _ID_TITLE.search(title):             # an unreleased ID: cut its slot out of the set recording
            name = sc.cut_name(title, set_id, i + 1)
            t0 = float(t.get("start") or 0.0)
            t1 = float(tracks[i + 1].get("start") or 0.0) if i + 1 < len(tracks) else None
            if rev.get(name):
                row.update(action="reuse", id=rev[name], why=f"already cut from the set: {name}")
            elif set_audio is None:
                row["why"] = "ID: no set recording to cut it from"
            elif t1 is None or t1 - t0 < MIN_CUT_S:
                row["why"] = f"ID: slot too short to cut ({(t1 or t0) - t0:.0f} s)"
            else:
                row.update(action="cut", name=name, src=str(set_audio), t0=t0, t1=t1,
                           why=f"cut from the set {t0 / 60:.1f}-{t1 / 60:.1f} min")
            continue
        if row["why"]:
            lib = None if row["why"].startswith("ID") else res.find(title)
            if lib:                                         # the set's file is unusable, the library has the song
                row.update(action="reuse", id=lib, why=f"library copy of the same recording: {names.get(lib)} ({row['why']})")
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


def _analysis_via_app(track_id: str, url: str = APP_URL) -> dict:
    """GET /api/tracks/{id}/analysis on the running console: the app's own analyzer + cache."""
    with urllib.request.urlopen(f"{url}/api/tracks/{track_id}/analysis", timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def _analysis_offline(track_id: str) -> dict:
    """The same handler the endpoint runs (server.get_analysis), in process."""
    from app.ui import server

    return server.get_analysis(track_id)


def _info(a: dict) -> dict:
    """bpm / key / duration of an analysis answer (the app's JSON shape)."""
    key = a.get("key")
    return {"bpm": a.get("bpm"), "key": key.get("camelot") if isinstance(key, dict) else key, "duration": a.get("duration")}


def apply(rows: List[dict], app_running: Optional[bool] = None,
          upload: Optional[Callable[[Path, str], dict]] = None,
          analysis: Optional[Callable[[str], dict]] = None, workdir: Optional[Path] = None) -> List[dict]:
    """Register every `import` row, and cut + register + analyse every `cut` row (an ID, cut
    out of the set with set_learner.clip_audio into a temporary dir, then uploaded like any
    file; its bpm / key / duration come from the app's analyzer). -> the rows, each carrying
    `result` (+ `info` for cuts) or `error`."""
    import tempfile

    from app.music_brain.set_learner import clip_audio
    from app.ui.services import dedup_songs as ds

    running = ds.app_is_running() if app_running is None else app_running
    if upload is None:
        upload = _post_upload if running else _register_offline
    if analysis is None:
        analysis = _analysis_via_app if running else _analysis_offline
    with tempfile.TemporaryDirectory(prefix="set-import-") as tmp:
        wd = Path(workdir) if workdir else Path(tmp)
        for r in rows:
            if r["action"] not in ("import", "cut"):
                continue
            try:
                if r["action"] == "cut":
                    path = clip_audio(Path(r["src"]), r["t0"], r["t1"], wd)
                    r["id"] = content_id(path)
                    res = upload(path, r["name"])
                else:
                    res = upload(Path(r["path"]), r["title"])
                tid = res.get("track_id")
                if tid != r["id"]:
                    raise ValueError(f"registered as {tid}, expected {r['id']}")
                r["result"] = res
                if r["action"] == "cut":
                    r["info"] = _info(analysis(tid))
            except Exception as exc:  # noqa: BLE001 -- one bad file must not stop the set
                r["error"] = str(exc)[:200]
    return rows


def summary(rows: List[dict]) -> dict:
    playable = [r for r in rows if r["action"] in ("import", "reuse", "cut") and not r.get("error")]
    return {"entries": len(rows), "playable": len(playable),
            "imported": sum(1 for r in rows if r["action"] == "import" and not r.get("error")),
            "cut": sum(1 for r in rows if r["action"] == "cut" and not r.get("error")),
            "reused": sum(1 for r in rows if r["action"] == "reuse"),
            "skipped": [{"position": r["position"], "title": r["title"], "why": r["why"]} for r in rows if r["action"] == "skip"],
            "errors": [{"position": r["position"], "title": r["title"], "error": r["error"]} for r in rows if r.get("error")]}


def learn_macros(set_id: str, cache_dir: Optional[Path] = None, log: Callable[[str], None] = lambda m: None,
                 build: Optional[Callable[..., dict]] = None) -> dict:
    """learn-set's last step: import-set then an incremental pair_atlas build (default, not --full:
    unchanged pairs are kept, only the new tracks' pairs are scored) that writes the macros.
    -> {"imported", "skipped", "written": this set's macro names, "error": None | str}; never raises.
    Serialized by an flock on CACHE_DIR/pair_atlas.lock, so two studies finishing together
    build one after the other (the second sees both sets) instead of dropping each other's macros."""
    from app.music_brain.config import CACHE_DIR

    cache_dir = Path(cache_dir or CACHE_DIR)
    out = {"imported": 0, "skipped": 0, "written": [], "error": None}
    try:
        rows = apply(plan(cache_dir, set_id))
        s = summary(rows)
        out["imported"], out["skipped"] = s["imported"] + s["cut"], len(s["skipped"])
        if s["errors"]:
            out["error"] = f"import: {len(s['errors'])} failed ({s['errors'][0]['error']})"[:200]
        if build is None:
            from app.music_brain.atlas.pair_atlas import build
        with _atlas_lock(cache_dir):
            doc = build(cache_dir, seed_macros_to=cache_dir, log=log)
        sid = re.escape(set_id.lower())   # macro names are slugs: lowercased (macros.slug)
        mine = re.compile(rf"studied-{sid}-\d+|studied-set-{sid}")
        out["written"] = [m["name"] for m in doc.get("seeded", []) if mine.fullmatch(m["name"])]
    except Exception as exc:  # noqa: BLE001 -- the learn result stands whatever happens here
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return out


_BARE_ID = re.compile(r"(?i)\s*id(\s*-\s*id)?\??\s*")


def tracklist_slots(text: str) -> tuple:
    """Tracklist text -> (slots [{start, title, layers[]}] in set order, bare-ID row count).
    A layered 'A x B' row is one slot: the first song plays it, the rest are noted as layers."""
    from app.music_brain.set_learner import _TS, parse_tracklist

    slots: List[dict] = []
    for e in parse_tracklist(text):
        if slots and slots[-1]["start"] == e.start:
            slots[-1]["layers"].append(e.title)
        else:
            slots.append({"start": e.start, "title": e.title, "layers": []})
    ids = sum(1 for line in (text or "").splitlines()
              if (m := _TS.match(line)) and _BARE_ID.fullmatch(m.group(2)))
    return slots, ids


def learn_tracklist_macro(source: str, tracklist: Optional[str] = None, download: bool = True,
                          cache_dir: Optional[Path] = None, log: Callable[[str], None] = lambda m: None,
                          build: Optional[Callable[..., dict]] = None, find: Optional[Callable] = None,
                          info: Optional[Callable[[str], tuple]] = None) -> dict:
    """learn-set --macros-only: a macro `set-<set_id>` straight from the tracklist, no set audio,
    no Demucs on the mix. Each song: library first, else downloaded (find_or_fetch_song), then
    registered through plan_tracks/apply like import-set, an incremental atlas build under the
    atlas lock, and macros.from_picks in tracklist order (locked).
    -> {set_id, macro, songs_found, songs_skipped[], macros{imported, skipped, written, error}}.
    ValueError when there is no usable tracklist (fewer than 2 songs)."""
    from app.music_brain.atlas import macros as mc
    from app.music_brain import set_learner as sl
    from app.music_brain.config import CACHE_DIR

    cache_dir = Path(cache_dir or CACHE_DIR)
    set_id, title, desc = (info or sl.set_info)(source)
    text = tracklist or ""
    if text and len(text) < 4096 and Path(text).expanduser().is_file():
        text = Path(text).expanduser().read_text(encoding="utf-8")
    slots, bare_ids = tracklist_slots(text or desc)
    if len(slots) < 2:
        raise ValueError("--macros-only needs a tracklist with at least 2 timestamped songs "
                         "(--tracklist, or the video description)")
    find = find or sl.find_or_fetch_song
    songs_dir = sl.SETS_DIR / set_id / "songs"
    tracks = []
    for s in slots:
        p = None if _ID_TITLE.search(s["title"]) else find(s["title"], songs_dir, download=download, exclude_ids=(set_id,))
        log(f"song {s['title']}: {p or 'NOT FOUND'}")
        tracks.append({"start": s["start"], "title": s["title"], "path": str(p) if p else None})
    rows = plan_tracks(cache_dir, set_id, tracks)
    skipped = [{"position": r["position"], "title": r["title"], "why": r["why"]} for r in rows if r["action"] == "skip"]
    out = {"imported": 0, "skipped": len(skipped), "written": [], "error": None}
    res = {"set_id": set_id, "macro": None, "songs_found": 0, "songs_skipped": skipped, "macros": out}
    name = f"set-{set_id}"
    try:
        rows = apply(rows)
        s = summary(rows)
        out["imported"] = s["imported"] + s["cut"]
        for e in s["errors"]:
            skipped.append({"position": e["position"], "title": e["title"], "why": f"import failed: {e['error']}"})
        if build is None:
            from app.music_brain.atlas.pair_atlas import build
        with _atlas_lock(cache_dir):
            atlas = build(cache_dir, seed_macros_to=cache_dir, log=log)
        ids, seen = [], set()
        for r in rows:
            if r["action"] not in ("import", "reuse", "cut") or r.get("error"):
                continue
            if r["id"] not in atlas.get("tracks", {}):
                skipped.append({"position": r["position"], "title": r["title"], "why": "not in the atlas (no analysis)"})
            elif r["id"] in seen:
                skipped.append({"position": r["position"], "title": r["title"], "why": "listed earlier (reprise)"})
            else:
                seen.add(r["id"])
                ids.append(r["id"])
        if len(ids) > mc.MAX_STEPS + 1:
            skipped += [{"position": None, "title": f"{len(ids) - mc.MAX_STEPS - 1} songs", "why": "over the macro limit"}]
            ids = ids[:mc.MAX_STEPS + 1]
        skipped.sort(key=lambda x: (x["position"] is None, x["position"] or 0))
        out["skipped"] = len(skipped)
        res["songs_found"] = len(ids)
        m = mc.from_picks(ids, atlas, locked=True, name=name)
        layers = [f"#{i} {s['title']} layered with {', '.join(s['layers'])}" for i, s in enumerate(slots, 1) if s["layers"]]
        notes = [f"#{x['position'] or '-'} {x['title']}: {x['why']}" for x in skipped]
        if bare_ids:
            notes.append(f"{bare_ids} ID rows (no song named)")
        m.update(source="tracklist", title=f"{mc.set_label(title)} (tracklist, {len(ids)} songs)",
                 note=(f"tracklist of {title}; skipped: " + "; ".join(notes) if notes else f"tracklist of {title}")
                 + ("; " + "; ".join(layers) if layers else ""))
        mc.write_seed(m, cache_dir, owner="tracklist")
        res["macro"] = name
        out["written"] = [name]
    except Exception as exc:  # noqa: BLE001 -- reported in macros.error, the rows above still stand
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return res


_HELD: Dict[str, list] = {}          # lock path -> [RLock, depth, open file]; one flock per process
_HELD_GUARD = threading.Lock()


class _atlas_lock:
    """Exclusive flock on CACHE_DIR/pair_atlas.lock (no-op where fcntl is missing, e.g. Windows).
    Re-entrant within a process: pair_atlas.build takes it itself, and callers that already hold it
    (import, learn, knowledge seed) nest without deadlocking on their own flock; other threads wait."""

    def __init__(self, cache_dir: Path):
        self.path = Path(cache_dir) / "pair_atlas.lock"

    def __enter__(self):
        with _HELD_GUARD:
            self.slot = _HELD.setdefault(os.path.abspath(self.path), [threading.RLock(), 0, None])
        self.slot[0].acquire()
        if self.slot[1] == 0:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fh = open(self.path, "a")
            except OSError:
                self.slot[0].release()
                raise
            try:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX)
            except ImportError:
                pass
            except OSError:
                fh.close()
                self.slot[0].release()
                raise
            self.slot[2] = fh
        self.slot[1] += 1
        return self

    def __exit__(self, *exc):
        self.slot[1] -= 1
        if self.slot[1] == 0:
            self.slot[2].close()          # closing releases the flock
            self.slot[2] = None
        self.slot[0].release()
