"""The song graph (graph.db) for the phone, which pulls it itself instead of waiting for an APK. Behind the app's
auth middleware like every /api route. The server only proxies: the file is built and read by the private dj-atlas
builder, reached through app.music_brain.atlas_bridge (nothing about the graph is in this repo).

GET /api/graph/version    {rev, seq, schema, built_at, nodes, edges, bytes}   what the server holds; cheap, poll it
GET /api/graph/snapshot   application/gzip of one consistent SQLite file; ETag = "<rev>";
                          If-None-Match answers 304 with no body.

Both answer 404 when the builder is not checked out or no graph.db has been built yet.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

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
