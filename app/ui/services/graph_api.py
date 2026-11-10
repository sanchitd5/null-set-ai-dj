"""The song graph (graph.db) for the phone, which pulls it itself instead of waiting for an APK. Behind the app's
auth middleware like every /api route. The server only proxies: the file is built and read by the private dj-atlas
builder, reached through app.music_brain.atlas_bridge (nothing about the graph is in this repo).

GET /api/graph/version    {rev, seq, schema, built_at, nodes, edges, bytes}   what the server holds; cheap, poll it
GET /api/graph/snapshot   application/gzip of one consistent SQLite file; ETag = "<rev>";
                          If-None-Match answers 304 with no body.

GET /api/graph/song?track_id=|key=|yt=[&artist=&title=&k=]
                          {source: graph | stub, center, nodes, edges, pending}   the graph around ONE song for the phone's
                          always-on graph card (G21). Same node/edge shape as /api/admin/graph/neighbours, but for any
                          signed-in device. A song with no graph node answers a stub centre (id -1) and, as neighbours,
                          YouTube's related songs (ids < -1; cache first, one short fetch otherwise); they are stored as
                          yt_edge marks by yt_related, so the graph learns the song at its next import. Never 404/500:
                          `pending` is true while a YouTube fetch is still running (ask again in a few seconds).

POST /api/graph/veto      {set_id, from_*, to_*} -> {ok, added, vetoed}: a 'skipped' learn edge A -> B + B on the set's vetoed list.

version and snapshot answer 404 when the builder is not checked out or no graph.db has been built yet.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.music_brain import atlas_bridge

router = APIRouter()
CACHE_CONTROL = "private, no-cache"           # always revalidate: the ETag makes that a cheap 304


def _store():
    if not atlas_bridge.AVAILABLE:
        raise HTTPException(status_code=404, detail="no graph")
    try:
        from app.music_brain.atlas import graph_store
    except ImportError:                       # builder checkout older than the graph store
        raise HTTPException(status_code=404, detail="no graph")
    return graph_store


def _matches(request: Request, etag: str) -> bool:
    norm = lambda t: t.strip().removeprefix("W/").strip('"')  # noqa: E731
    tags = [norm(t) for t in request.headers.get("if-none-match", "").split(",")]
    return norm(etag) in tags or "*" in tags


@router.get("/api/graph/version")
def get_version():
    store = _store()
    v = store.version()
    if v is None:
        raise HTTPException(status_code=404, detail="no graph")
    try:
        rev, body = store.snapshot_gz()       # cached per rev; the phone gates mobile data on the size
        if rev == v["rev"]:
            v["bytes"] = len(body)
    except (FileNotFoundError, OSError):
        pass
    return v


@router.get("/api/graph/snapshot")
def get_snapshot(request: Request):
    store = _store()
    v = store.version()
    if v is None:
        raise HTTPException(status_code=404, detail="no graph")
    headers = {"Cache-Control": CACHE_CONTROL}
    tag = f'"{v["rev"]}"'
    if _matches(request, tag):
        return Response(status_code=304, headers={**headers, "ETag": tag})
    try:
        rev, body = store.snapshot_gz()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no graph")
    headers["ETag"] = f'"{rev}"'            # the rev the bytes are, even when a build landed after version()
    headers["Content-Disposition"] = 'attachment; filename="graph.db.gz"'
    return Response(content=body, media_type="application/gzip", headers=headers)


# ---------------------------------------------------------------------------------------------- G21: one song
SONG_YT_WAIT_S = 0.8            # the phone waits for this at most; a slower YouTube fetch fills the cache for the next ask
SONG_MIN_SONGS = 12             # owner: the card shows at least this many songs; fewer is topped up (hops, then YouTube)
HOP2_SEEDS = 8
HOP2_K = 6
SONG_K = 12
YT_WEIGHT = 0.4                 # = GRAPH_PARAMS kind_weight yt_related


def _identity(track_id: str, artist: str, title: str) -> tuple[str, str]:
    if (artist and title) or not track_id:
        return artist, title
    try:
        from app.ui import server
        from app.ui.services.track_identity import clean_identity

        name = server._track_names.get(track_id)
        if name:
            a, t = clean_identity(name)
            return ("" if a == "Unknown" else a), t
    except Exception:
        pass
    return artist, title


def _find_node(yt: str, key: str, artist: str, title: str):
    """The graph's song node (a find() view) for a YouTube id or a song key, or None."""
    from app.ui.services import pick_pool

    want = key or (pick_pool.key_of(artist, title) if title else "")
    for q in (yt, key, title):
        if not q:
            continue
        for r in atlas_bridge.graph_view("find", q, 25) or []:
            if (yt and r.get("yt_id") == yt) or (want and r.get("title")
                                                  and pick_pool.key_of(r.get("artist") or "", r["title"]) == want):
                return r
    return None


def _yt_node(i: int, row: dict) -> dict:
    return {"id": i, "kind": "song", "label": " - ".join(x for x in (row["artist"], row["title"]) if x),
            "artist": row["artist"], "title": row["title"], "yt_id": row.get("yt_id"), "source": "yt", "lang": None,
            "scene": None, "bpm": None, "key": None, "energy": None, "library": False, "trend": None}


def _fill_hops(out: dict, want: int) -> None:
    """Grow a sparse neighbourhood to `want` songs: every edge kind of the centre (artist and scene nodes included, so
    same-scene library songs come in through the scene node), then the neighbours of its best neighbours (2 hops)."""
    from app.music_brain.atlas import graph_schema as gs

    kinds = list(gs.EDGE_KINDS)

    def songs() -> int:
        return sum(1 for n in out["nodes"] if n["kind"] == "song" and n["id"] != out["center"])

    def merge(more: Optional[dict]) -> None:
        if not more:
            return
        have = {n["id"] for n in out["nodes"]}
        seen = {(e["a"], e["b"], e["kind"]) for e in out["edges"]}
        out["nodes"] += [n for n in more["nodes"] if n["id"] not in have]
        out["edges"] += [e for e in more["edges"] if (e["a"], e["b"], e["kind"]) not in seen]

    merge(atlas_bridge.graph_view("neighbours", out["center"], 30, kinds))
    seeds = [n["id"] for n in out["nodes"] if n["id"] != out["center"]][:HOP2_SEEDS]
    for sid in seeds:
        if songs() >= want:
            break
        merge(atlas_bridge.graph_view("neighbours", sid, HOP2_K, kinds))


def _song(track_id: str, key: str, yt: str, artist: str, title: str, k: int) -> dict:
    from app.ui.services import yt_related

    artist, title = _identity(track_id, artist, title)
    node = _find_node(yt, key, artist, title)
    out = None
    if node is not None:
        out = atlas_bridge.graph_view("neighbours", node["id"], k)
    if out is not None:
        out["source"] = "graph"
        if len(out["nodes"]) - 1 < SONG_MIN_SONGS:
            _fill_hops(out, SONG_MIN_SONGS)
    else:
        centre = {**_yt_node(-1, {"artist": artist, "title": title or key.split("|")[-1] or yt, "yt_id": yt or None}),
                  "stub": True, "library": bool(track_id)}
        out = {"source": "stub", "center": -1, "nodes": [centre], "edges": []}
    pending = False
    nsongs = sum(1 for n in out["nodes"] if n["kind"] == "song") - 1
    if nsongs < SONG_MIN_SONGS:
        vid = yt or (node or {}).get("yt_id") or (yt_related.video_id(track_id) if track_id else None)
        rows, info = yt_related.for_video(vid or "", {"artist": artist, "title": title, "track_id": track_id},
                                          SONG_YT_WAIT_S)
        pending = info["source"] == "timeout"
        have = {n.get("yt_id") for n in out["nodes"] if n.get("yt_id")}
        have |= {n["label"].lower() for n in out["nodes"]}
        nid = -2
        for r in rows:
            if r.get("yt_id") in have or f"{r['artist']} - {r['title']}".lower() in have:
                continue
            out["nodes"].append(_yt_node(nid, r))
            out["edges"].append({"a": out["center"], "b": nid, "kind": "yt_related", "weight": YT_WEIGHT, "n": 1})
            nid -= 1
            nsongs += 1
            if nsongs >= SONG_MIN_SONGS + 4:
                break
    out["pending"] = pending
    return out


@router.get("/api/graph/song")
def get_song(track_id: str = "", key: str = "", yt: str = "", artist: str = "", title: str = "", k: int = SONG_K):
    track_id, key, yt, artist, title = (x.strip()[:200] for x in (track_id, key, yt, artist, title))
    if not (track_id or key or yt or title):
        return JSONResponse({"detail": "track_id, key, yt or title required", "code": "bad_query"}, status_code=422)
    try:
        return _song(track_id, key, yt, artist, title, max(1, min(int(k), 30)))
    except Exception:                                            # never a 500: the card draws the song alone
        centre = {**_yt_node(-1, {"artist": artist, "title": title or yt or track_id, "yt_id": yt or None}),
                  "stub": True, "library": bool(track_id)}
        return {"source": "stub", "center": -1, "nodes": [centre], "edges": [], "pending": False}


# ---------------------------------------------------------------------------------------------- G21: veto from the graph
MARK_VETO = "graph_veto"        # private user-DB mark per (set, from, to): the set's vetoed list
VETO_KEEP = 20000
_SET_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class VetoBody(BaseModel):
    set_id: str = "live"
    device_id: str = "phone"
    from_track_id: str = ""
    from_artist: str = ""
    from_title: str = ""
    to_track_id: str = ""
    to_artist: str = ""
    to_title: str = ""


def _vetoed_keys(set_id: str, cache_dir) -> list:
    from app.music_brain import user_marks as um

    pre = f"{set_id}:"
    return sorted({r["to_key"] for s, r in um.rows(MARK_VETO, cache_dir) if s.startswith(pre) and r.get("to_key")})


def _safe_id(v: str) -> str:
    v = v.strip()
    return v if _SET_ID.fullmatch(v) else ""


@router.post("/api/graph/veto")
def post_veto(body: VetoBody):
    """The owner vetoed neighbour B of the current song A on the graph card: records a 'skipped' learn edge A -> B (the
    same private row a refused AI pick leaves, read by phone_learn.overlay) and adds B to the set's vetoed list
    (returned as `vetoed`, song keys as pick_pool.key_of builds them). Idempotent."""
    from app.music_brain import user_marks as um
    from app.music_brain.learning import phone_learn as pl
    from app.ui import server
    from app.ui.services import pick_pool

    sid = body.set_id.strip()
    if not _SET_ID.fullmatch(sid):
        return JSONResponse({"detail": "set_id: 1-64 letters, digits, - or _", "code": "bad_query"}, status_code=422)
    a_art, a_title = _identity(body.from_track_id.strip(), body.from_artist.strip()[:200], body.from_title.strip()[:200])
    b_art, b_title = _identity(body.to_track_id.strip(), body.to_artist.strip()[:200], body.to_title.strip()[:200])
    if not (a_title and b_title):
        return JSONResponse({"detail": "both songs need a title (or a known track_id)", "code": "bad_query"},
                            status_code=422)
    a_name = f"{a_art} - {a_title}" if a_art else a_title
    b_name = f"{b_art} - {b_title}" if b_art else b_title
    a_key, to_key = pick_pool.key_of(a_art, a_title), pick_pool.key_of(b_art, b_title)
    if a_key == to_key:
        return JSONResponse({"detail": "a veto needs two different songs", "code": "bad_query"}, status_code=422)
    dev = {"device_id": re.sub(r"[^A-Za-z0-9_-]", "_", body.device_id)[:64] or "phone", "set_id": sid}
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cache = server.CACHE_DIR
    try:
        added = um.add_new(MARK_VETO, f"{sid}:{a_key}>{to_key}"[:400],
                           {"to_key": to_key, "a_name": a_name, "b_name": b_name, "at": at}, cache, keep=VETO_KEEP)
        if added:
            pl._transition({"id": f"veto-{int(time.time() * 1000)}", "at": at},
                           {"a_id": _safe_id(body.from_track_id), "b_id": _safe_id(body.to_track_id),
                            "a_name": a_name, "b_name": b_name, "outcome": "skipped", "hand": True},
                           dev, cache)
        return {"ok": True, "added": added, "vetoed": _vetoed_keys(sid, cache)}
    except Exception as exc:
        return JSONResponse({"detail": f"could not record the veto: {type(exc).__name__}", "code": "store_failed"},
                            status_code=503)
