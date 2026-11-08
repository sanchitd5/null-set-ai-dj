"""The knowledge (dj-atlas checkout) for the phone, which pulls it itself instead of waiting for an
APK. Behind the app's auth middleware like every /api route.

GET /api/knowledge/version    {sha, rules_hash, files, bytes, dirty}   what the server holds; cheap, poll it
GET /api/knowledge/snapshot   application/zip in the layout of the phone's assets/atlas (meta.json, names.json,
                              genre_labels.json, learned_techniques.json, macros.json, pairs/<a>.json.gz,
                              version.json); ETag = the checkout's commit sha ("<sha>+<hash>" when the
                              checkout has uncommitted changes); If-None-Match answers 304 with no body.

The phone: compare `sha` (or the ETag) with the last imported one, download on change, import through the
existing snapshot import, keep the bundled asset as the fallback.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from app.music_brain.matching import knowledge, knowledge_publish

router = APIRouter()
CACHE_CONTROL = "private, no-cache"           # always revalidate: the ETag makes that a cheap 304


def _matches(request: Request, etag: str) -> bool:
    norm = lambda t: t.strip().removeprefix("W/").strip('"')  # noqa: E731
    tags = [norm(t) for t in request.headers.get("if-none-match", "").split(",")]
    return norm(etag) in tags or "*" in tags


@router.get("/api/knowledge/version")
def get_version():
    return knowledge_publish.version(knowledge.KNOWLEDGE_DIR)


@router.get("/api/knowledge/snapshot")
def get_snapshot(request: Request):
    src = knowledge.KNOWLEDGE_DIR
    if not src.is_dir():
        raise HTTPException(status_code=404, detail="no knowledge")
    tag = knowledge_publish.etag(src)
    headers = {"ETag": tag, "Cache-Control": CACHE_CONTROL}
    if _matches(request, tag):
        return Response(status_code=304, headers=headers)
    tag, body = knowledge_publish.snapshot_zip(src)
    headers["ETag"] = tag
    headers["Content-Disposition"] = 'attachment; filename="atlas-knowledge.zip"'
    return Response(content=body, media_type="application/zip", headers=headers)
