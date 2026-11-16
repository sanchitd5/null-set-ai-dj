"""REST surface of the LYRIC REMIX move (included by server.py).

The data file lyric_remix.json.gz (plans + lines, built by the private atlas builder, shipped with the knowledge checkout and
snapshot) is only READ here. The decision lives in dj-core (service fn "lyric_remix_plan"); this module adds the data subset of
the songs concerned to the client's context and relays the answer unchanged. Contract: wt/LYRICMIX_CONTRACT.txt.

POST /api/lyric_remix/plan   {a, b|null, ctx}   -> plan {ok: true, ...} | refusal {ok: false, gate, reason}; always 200 once the
                                                  body is valid (dj-core missing -> gate "unavailable": the move just does not run)
GET  /api/lyric_remix/has?a=&b=                 -> {within, pair}: how many plans exist for song a alone / for the pair a -> b
"""
from __future__ import annotations

import gzip
import json
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.music_brain import djcore_bridge
from app.music_brain.matching import knowledge as kn

router = APIRouter()

_ID_RE = re.compile(r"[0-9A-Za-z_-]{1,64}")
MAX_CTX_BYTES = 256 * 1024                     # drops / vocal spans of a long song are the bulk of it
_cache: Dict[str, Any] = {"key": None, "doc": None}


class PlanBody(BaseModel):
    a: str
    b: Optional[str] = None
    ctx: Dict[str, Any]


def _empty() -> dict:
    return {"songs": {}, "plans": []}


def load() -> dict:
    """The data file as {songs, plans}; missing / corrupt = empty. Re-read only when its (mtime, size) changes."""
    p = kn.KNOWLEDGE_DIR / kn.LYRIC_REMIX
    try:
        st = p.stat()
    except OSError:
        _cache.update(key=None, doc=None)
        return _empty()
    key = (str(p), st.st_mtime_ns, st.st_size)
    if _cache["key"] == key and _cache["doc"] is not None:
        return _cache["doc"]
    try:
        raw = json.loads(gzip.decompress(p.read_bytes()).decode("utf-8"))
    except (OSError, ValueError, EOFError):
        raw = None
    doc = _empty()
    if isinstance(raw, dict):
        if isinstance(raw.get("songs"), dict):
            doc["songs"] = raw["songs"]
        if isinstance(raw.get("plans"), list):
            doc["plans"] = [x for x in raw["plans"] if isinstance(x, dict)]
    _cache.update(key=key, doc=doc)
    return doc


def _plans_for(doc: dict, a: str, b: Optional[str]) -> List[dict]:
    """Plans of song a: the within-song ones when b is None, else the cross-deck ones for exactly a -> b."""
    return [p for p in doc["plans"] if p.get("a") == a and (p.get("b") is None if b is None else p.get("b") == b)]


def _check_id(x: Optional[str]) -> Optional[str]:
    if x is None or x == "":
        return None
    if not _ID_RE.fullmatch(x):
        raise HTTPException(status_code=422, detail="bad track id")
    return x


@router.post("/api/lyric_remix/plan")
def lyric_remix_plan(body: PlanBody):
    a, b = _check_id(body.a), _check_id(body.b)
    if a is None:
        raise HTTPException(status_code=422, detail="bad track id")
    if len(json.dumps(body.ctx, separators=(",", ":"))) > MAX_CTX_BYTES:
        raise HTTPException(status_code=422, detail="ctx too large")
    doc = load()
    songs = {k: doc["songs"][k] for k in ((a, b) if b else (a,)) if k in doc["songs"]}
    ctx = dict(body.ctx)
    ctx.update(aId=a, bId=b, songs=songs, plans=_plans_for(doc, a, b))
    try:
        return djcore_bridge.call("lyric_remix_plan", {"ctx": ctx})
    except djcore_bridge.DjCoreUnavailable as e:
        return {"ok": False, "gate": "unavailable", "reason": f"dj-core is not available: {e}"}
    except djcore_bridge.DjCoreError as e:
        return {"ok": False, "gate": "unavailable", "reason": f"dj-core refused the call: {e}"}


@router.get("/api/lyric_remix/has")
def lyric_remix_has(a: str, b: str = ""):
    a, b = _check_id(a), _check_id(b)
    if a is None:
        raise HTTPException(status_code=422, detail="bad track id")
    doc = load()
    return {"within": len(_plans_for(doc, a, None)), "pair": len(_plans_for(doc, a, b)) if b else 0}
