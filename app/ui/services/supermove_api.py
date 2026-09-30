"""REST surface of the $Up3R-M@SS!V3-M0v3 variants (included by server.py). Read only: variants are saved
through `python3 -m app.music_brain.supermove save` (it backs the USER DB up first).

GET /api/supermoves                         {move, variants: [summary]} (the first saved is the default)
GET /api/supermoves/plan/{name}?start=k     {plan}: the variant from its song k on (from_song)
GET /api/supermoves/handback?a=&level=&played=  {pick}: the Bass Swap partner one energy level under `level`
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.music_brain import supermove as smv
from app.music_brain.config import CACHE_DIR

router = APIRouter()
SUPERMOVE_CACHE_DIR: Path = CACHE_DIR      # tests point this at tmp_path


@router.get("/api/supermoves")
def supermoves():
    return {"move": smv.NAME, "variants": smv.list_variants(SUPERMOVE_CACHE_DIR)}


@router.get("/api/supermoves/plan/{name}")
def supermove_plan(name: str, start: int = 0):
    try:
        return {"plan": smv.load_variant(name, start=start, cache_dir=SUPERMOVE_CACHE_DIR)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no {smv.NAME} variant {name!r}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/supermoves/handback")
def supermove_handback(a: str, level: Optional[int] = None, played: str = ""):
    ids = {x for x in played.split(",") if x}
    return {"pick": smv.handback_pick(a, level, ids, cache_dir=SUPERMOVE_CACHE_DIR)}
