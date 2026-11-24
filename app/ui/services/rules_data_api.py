"""GET /api/rules_data: the generated rules data file (dj-config OTARULES) for the phone, so tuning reaches phones
without an APK. The response body is the bytes of music_brain/generated/rules_data.json unchanged (the phone hashes the
received `data` text, so nothing here re-serialises it). ETag = the file's "version"; If-None-Match answers 304.
Behind the app's auth middleware like every /api route.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

router = APIRouter()
PATH = Path(__file__).resolve().parents[2] / "music_brain" / "generated" / "rules_data.json"
CACHE_CONTROL = "private, no-cache"           # always revalidate: the ETag makes that a cheap 304
_cache: dict = {}                              # (mtime_ns, size) -> (body, etag)


def _load() -> tuple[bytes, str]:
    try:
        st = PATH.stat()
        key = (st.st_mtime_ns, st.st_size)
        if _cache.get("key") != key:
            body = PATH.read_bytes()
            version = str(json.loads(body)["version"])
            _cache.update(key=key, val=(body, f'"{version}"'))
        return _cache["val"]
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(status_code=404, detail="no rules data")


def _matches(request: Request, etag: str) -> bool:
    norm = lambda t: t.strip().removeprefix("W/").strip('"')  # noqa: E731
    tags = [norm(t) for t in request.headers.get("if-none-match", "").split(",")]
    return norm(etag) in tags or "*" in tags


@router.get("/api/rules_data")
def get_rules_data(request: Request):
    body, etag = _load()
    headers = {"Cache-Control": CACHE_CONTROL, "ETag": etag}
    if _matches(request, etag):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)
