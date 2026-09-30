"""REST surface of the pair atlas and macros (included by server.py).

GET  /api/atlas/status                     built?, stats, rules hash
GET  /api/atlas/partners?a=&move=&n=&combo=  best partners of A (per move; combo=1: combos only)
GET  /api/atlas/pair?a=&b=                 one pair's summary + default plan (null: unknown)
GET  /api/studied/sets                     studied famous sets in set order (FOLLOW SET)
GET  /api/macros                           saved macros
GET  /api/macros/{name}                    one macro (+ offline validation per step)
POST /api/macros                           save {macro} (an existing name becomes <name>-v<k>)
POST /api/macros/from-session/{session}    a past session's logs as a macro
POST /api/macros/plan-from-picks           {ids, locked, name, save}: atlas-ordered macro
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.music_brain.atlas import macros as mc
from app.music_brain.atlas import pair_atlas as pa
from app.music_brain.config import CACHE_DIR

router = APIRouter()
ATLAS_CACHE_DIR: Path = CACHE_DIR      # tests point this at tmp_path


def _seed() -> None:
    """Tracked knowledge/ into the cache when either changed (a few stats otherwise)."""
    from app.music_brain.matching import knowledge
    knowledge.auto_seed(ATLAS_CACHE_DIR)


def _index() -> Optional[pa.Index]:
    _seed()
    return pa.cached_index(ATLAS_CACHE_DIR)


def _known(tid: str) -> bool:
    try:
        from app.ui import server
        server._track_path(tid)
        return True
    except Exception:  # noqa: BLE001 -- unknown id, or no server registry (tests)
        idx = _index()
        return bool(idx and tid in idx.names)


def _has_stems(tid: str) -> bool:
    try:
        from app.ui import server
        return bool(server._cached_stems4(tid))
    except Exception:  # noqa: BLE001
        return False


@router.get("/api/atlas/status")
def atlas_status():
    idx = _index()
    if idx is None:
        return {"built": False}
    return {"built": True, "rules": idx.rules, "built_at": idx.built_at, "stats": idx.stats,
            "rules_current": idx.rules == pa.rules_hash()}


def seed_rows(a: str, have: set) -> List[dict]:
    """The committed seed combos (seed_combos.json) for A that the atlas does not serve yet:
    offered as merge combos; the console's live gates (planHold) decide if they play."""
    rows = []
    for s in pa.load_seeds():
        if s["a"] != a or s["b"] in have:
            continue
        rows.append({"a": a, "b": s["b"], "a_name": s.get("a_name"), "b_name": s.get("b_name"), "b_bpm": None, "b_duration": None,
                     "works": pa.COMBO_MIN_WORKS, "best": "Merge → Hold", "recipe": "Stem Merge", "combo": s.get("move", "merge"),
                     "combo_label": pa.COMBO_LABEL.get(s.get("move", "merge")), "seed": True, "merge_ok": None,
                     "moves": {"merge": {"ok": None, "score": None, "gate": "seed: not in the atlas yet (live gates decide)"}},
                     "plan": {"recipe": "Stem Merge", "a_time": None, "b_time": None, "merge": None, "lock": None, "combo": "merge"}})
    return rows


@router.get("/api/atlas/partners")
def atlas_partners(a: str, move: Optional[str] = None, n: int = 10, combo: bool = False):
    idx = _index()
    try:
        pa.normalize_move(move)
        rows = idx.partners(a, move, 100 if combo else n) if idx else []
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if move in (None, "merge", "supermove"):
        rows = seed_rows(a, {r["b"] for r in (idx.rows(a) if idx else [])}) + rows
    if idx is None:
        return {"a": a, "partners": rows[:max(1, min(100, n))], "built": False}
    if combo:
        rows = [r for r in rows if r.get("combo")][:max(1, min(100, n))]
    rows = _mark_vetoed(a, idx.names.get(a), [dict(r) for r in rows])
    return {"a": a, "name": idx.names.get(a), "move": move, "partners": rows, "built": True}


def _mark_vetoed(a: str, a_name: Optional[str], rows: List[dict]) -> List[dict]:
    """rows with "vetoed": reason on each pair the owner vetoed (atlas/vetoes.py); the console's
    combo and backup rankers skip them. The build folds vetoes into played evidence too, but a
    live veto must hold before the next build."""
    from app.music_brain.atlas import vetoes as vt

    try:
        from app.ui import server
        vs = server._vetoes()
        a_name = a_name or server._name_of(a)
    except Exception:  # noqa: BLE001 -- no server registry (tests)
        vs = vt.load(ATLAS_CACHE_DIR)
    if not vs or not a_name:
        return rows
    for r in rows:
        why = vt.blocked(vs, a_name, r.get("b_name") or "")
        if why:
            r["vetoed"] = why
    return rows


def _earlier_set_keys(set_id: str) -> set:
    """Title keys of songs heard in earlier sets (set_memory.json), the list the suggest
    prompt avoids. Empty when there is no memory yet."""
    from app.ui.services import set_memory as sm

    try:
        from app.ui import server
        mem = server._set_memory or sm.SetMemory(ATLAS_CACHE_DIR / "set_memory.json")
    except Exception:  # noqa: BLE001 -- no server registry (tests)
        mem = sm.SetMemory(ATLAS_CACHE_DIR / "set_memory.json")
    return {sm._key(n) for n in mem.earlier_sets([], limit=sm.MAX_SONGS, set_id=set_id)}


@router.get("/api/atlas/backup")
def atlas_backup(a: str, n: int = 40, set_id: str = ""):
    """A's atlas partners for the console's preplanned backup B (autopilot.js rankAtlasBackups):
    the served rows (one shard read) with b_level (energy 1-10), a_level, and earlier_set
    (the song was heard in an earlier set: tried after the fresh ones, like the suggest
    prompt's "prefer fresh"). The live gates still decide at booking time."""
    from app.ui.services import set_memory as sm

    idx = _index()
    if idx is None:
        return {"a": a, "partners": [], "built": False}
    rows = [dict(r) for r in idx.partners(a, None, max(1, min(100, n)))]
    earlier = _earlier_set_keys(str(set_id or "")[:64])
    for r in rows:
        r["earlier_set"] = sm._key(r.get("b_name") or "") in earlier
    _mark_vetoed(a, idx.names.get(a), rows)
    a_level = ((idx._light.get("tracks") or {}).get(a) or {}).get("level")
    return {"a": a, "a_level": a_level, "partners": rows, "built": True}


_SETS_MEMO: dict = {}


@router.get("/api/studied/sets")
def studied_sets():
    """The studied famous sets in set order with each song's library id (FOLLOW SET).
    Recomputed when the library names or a study file change."""
    from app.music_brain.atlas import studied_combos as sc

    files = [ATLAS_CACHE_DIR / "uploads" / "_names.json", ATLAS_CACHE_DIR / "track_aliases.json",
             *sorted((ATLAS_CACHE_DIR / "sets").glob("*/study.json"))]
    stamp = tuple((str(p), p.stat().st_mtime_ns) for p in files if p.exists())
    if _SETS_MEMO.get("stamp") != stamp:
        _SETS_MEMO.update(stamp=stamp, sets=sc.set_songs(ATLAS_CACHE_DIR))
    return {"sets": _SETS_MEMO["sets"]}


@router.get("/api/atlas/pair")
def atlas_pair(a: str, b: str):
    idx = _index()
    return {"a": a, "b": b, "pair": idx.pair(a, b) if idx else None, "built": idx is not None}


class MacroBody(BaseModel):
    macro: dict
    new_version: bool = True


class PicksBody(BaseModel):
    ids: List[str] = Field(min_length=2, max_length=mc.MAX_STEPS + 1)
    locked: bool = False
    name: Optional[str] = None
    save: bool = True


@router.get("/api/macros")
def get_macros():
    _seed()
    return {"macros": mc.list_macros(ATLAS_CACHE_DIR)}


@router.get("/api/macros/{name}")
def get_macro(name: str):
    try:
        m = mc.load(name, ATLAS_CACHE_DIR)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no macro {name}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    from app.ui.services import dedup_songs as ds
    aliases = ds.load_aliases(ATLAS_CACHE_DIR)
    m = mc.resolve_ids(m, lambda t: ds.resolve_alias(t, ATLAS_CACHE_DIR, aliases))
    return {"macro": m, "validation": mc.validate(m, _known, _has_stems)}


@router.post("/api/macros")
def post_macro(body: MacroBody):
    try:
        m = mc.save(body.macro, ATLAS_CACHE_DIR, new_version=body.new_version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"macro": m}


@router.post("/api/macros/from-session/{session}")
def post_macro_from_session(session: str, name: Optional[str] = None):
    try:
        m = mc.save(mc.from_session(session, ATLAS_CACHE_DIR, name), ATLAS_CACHE_DIR)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"macro": m}


@router.post("/api/macros/plan-from-picks")
def post_plan_from_picks(body: PicksBody):
    atlas = pa.load_for(body.ids, ATLAS_CACHE_DIR)   # every pair of the picks (their shards only), not just top partners
    if atlas is None:
        raise HTTPException(status_code=409, detail="no pair atlas yet: run `python3 -m app.music_brain.atlas.pair_atlas build`")
    try:
        m = mc.from_picks(body.ids, atlas, body.locked, body.name)
        if body.save:
            m = mc.save(m, ATLAS_CACHE_DIR)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"macro": m}
