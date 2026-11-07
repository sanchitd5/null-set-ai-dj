"""GET /api/tracks/{track_id}/art?size=600|160 -- the cached cover (album_art.py). Behind the app's auth
middleware like every /api route. 404 when the track has no art (the client keeps its gradient)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from app.ui.services import album_art

router = APIRouter()
CACHE_CONTROL = "private, max-age=604800"     # the art_url's ?v= changes with the file; ETag covers the rest


@router.get("/api/tracks/{track_id}/art")
def get_track_art(track_id: str, request: Request, size: int = 600):
    if size not in album_art.SIZES:
        raise HTTPException(status_code=400, detail=f"size must be one of {list(album_art.SIZES)}")
    if not album_art.has_art(track_id):
        raise HTTPException(status_code=404, detail="no art for this track")
    path = album_art.art_path(track_id, size)
    try:
        st = path.stat()
    except OSError:
        raise HTTPException(status_code=404, detail="no art for this track")
    etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    headers = {"ETag": etag, "Cache-Control": CACHE_CONTROL}
    if etag in [t.strip() for t in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type="image/jpeg", headers=headers)
