"""REST surface of set history, replay, time travel and liked transitions (included by server.py).

GET    /api/sessions                        past sessions (newest first)
GET    /api/sessions/{id}/timeline          songs + transitions in play order (recipe, exit, entry, moves)
GET    /api/sessions/{id}/state?at=         each deck's song and position at a moment
POST   /api/replay {session, at | step, to, save}
                                            replay macro (source replay:<session>) from a moment / a step,
                                            the deck state there and the song + position to load
GET    /api/liked                           liked transitions
POST   /api/liked {session, n | step, note, replay}
DELETE /api/liked?a=&b=
POST   /api/liked/seed                      the owner's picks (liked_seed.json) into liked.json
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.music_brain.atlas import macros as mc
from app.music_brain.config import CACHE_DIR
from app.music_brain.learning import history_api as history
from app.music_brain.learning import liked as lk
from app.music_brain.learning import replay as rp

router = APIRouter()
REPLAY_CACHE_DIR: Path = CACHE_DIR      # tests point this at tmp_path


def _err(exc: Exception):
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from None
    raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/api/sessions")
def get_sessions():
    return {"sessions": history.sessions(REPLAY_CACHE_DIR)}


@router.get("/api/sessions/{session}/timeline")
def get_timeline(session: str):
    try:
        tl = history.timeline(session, REPLAY_CACHE_DIR)
    except (KeyError, ValueError) as exc:
        _err(exc)
    liked = lk.load(REPLAY_CACHE_DIR)
    for t in tl["transitions"]:
        t["liked"] = f"{t['a']}>{t['b']}" in liked
    return tl


@router.get("/api/sessions/{session}/state")
def get_state(session: str, at: str):
    try:
        return history.state_at(session, at, REPLAY_CACHE_DIR)
    except (KeyError, ValueError) as exc:
        _err(exc)


class ReplayBody(BaseModel):
    session: str
    at: Optional[Union[float, str]] = None
    step: Optional[int] = None
    to: Optional[int] = None
    save: bool = True


@router.post("/api/replay")
def post_replay(body: ReplayBody):
    """Replay from a moment (at) or a transition (step; with to: that range only). The macro is
    saved (a new version when the name exists) so PLAY MACRO performs it like any macro."""
    if body.at is None and body.step is None:
        raise HTTPException(status_code=400, detail="give at or step")
    try:
        if body.step is not None and body.to is not None:
            out = {"session": body.session, "start_step": body.step,
                   "replay": rp.build(body.session, body.step, body.to, REPLAY_CACHE_DIR)}
            tr = history.timeline(body.session, REPLAY_CACHE_DIR)["transitions"][body.step - 1]
            out["state"] = history.state_at(body.session, tr["t"], REPLAY_CACHE_DIR)
            out["load"] = rp.load_for(out["replay"], out["state"], rp.PRE_ROLL_S)
            out["restarted_transition"] = True
        else:
            out = rp.travel(body.session, body.at, body.step, REPLAY_CACHE_DIR)
        if body.save and out.get("replay"):
            out["replay"]["macro"] = mc.save(out["replay"]["macro"], REPLAY_CACHE_DIR)
    except (KeyError, ValueError, IndexError) as exc:
        _err(exc)
    return out


class LikeBody(BaseModel):
    session: Optional[str] = None
    n: Optional[int] = None
    step: Optional[dict] = None
    note: str = ""
    replay: bool = True


@router.get("/api/liked")
def get_liked():
    return {"liked": list(lk.load(REPLAY_CACHE_DIR).values())}


@router.post("/api/liked")
def post_liked(body: LikeBody):
    """Like a played transition (session + n: exactly as it played) or a console step."""
    try:
        if body.session and body.n is not None:
            e = lk.like_played(body.session, body.n, body.note, body.replay, REPLAY_CACHE_DIR)
        elif body.step:
            e = lk.like(body.step, "console", body.note, body.replay, REPLAY_CACHE_DIR)
        else:
            raise ValueError("give session + n, or step")
    except (KeyError, ValueError, IndexError) as exc:
        _err(exc)
    return {"liked": e}


@router.delete("/api/liked")
def delete_liked(a: str, b: str):
    return {"removed": lk.unlike(a, b, REPLAY_CACHE_DIR)}


@router.post("/api/liked/seed")
def post_liked_seed():
    return {"seeded": lk.seed(REPLAY_CACHE_DIR)}
