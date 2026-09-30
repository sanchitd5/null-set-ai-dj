"""REST surface of the $Up3R-M@SS!V3-M0v3 variants (included by server.py). Read only: the variants ship with the
app (app/music_brain/supermove/variants/*.json, loaded once at start); adding one = committing a file.

GET /api/supermoves                         {move, variants: [summary]} (the first file is the default)
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
VARIANTS_DIR: Path = smv.VARIANTS_DIR     # tests point this at tmp_path
ATLAS_CACHE_DIR: Path = CACHE_DIR         # the pair atlas (app.db, read) for the handback pick
smv.load_all(VARIANTS_DIR)                # read once at start


@router.get("/api/supermoves")
def supermoves():
    return {"move": smv.NAME, "variants": smv.list_variants(VARIANTS_DIR)}


@router.get("/api/supermoves/plan/{name}")
def supermove_plan(name: str, start: int = 0):
    try:
        return {"plan": smv.load_variant(name, start=start, d=VARIANTS_DIR)}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no {smv.NAME} variant {name!r}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/api/supermoves/handback")
def supermove_handback(a: str, level: Optional[int] = None, played: str = ""):
    ids = {x for x in played.split(",") if x}
    return {"pick": smv.handback_pick(a, level, ids, cache_dir=ATLAS_CACHE_DIR)}
