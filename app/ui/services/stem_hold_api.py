"""REST relay of the STEMONLY readiness hold (included by server.py). The decision lives in dj-core (service fn "stem_hold"):
when a deck's stems are in flight with an ETA inside the bound, A is held on phrase boundaries instead of cutting; failed stems,
no ETA, no room or an intro / outro give the no-overlap cut. This module only validates the body and relays the answer.

POST /api/stem_hold   {a, b: {state, eta_s?}, bar_s, room_s?, intro_outro?, held_phrases?}
                      -> {action: go | hold | cut, phrases, hold_s, why}; always 200 once the body is valid
                         (dj-core missing -> {"action": "cut", ...}: no hold, the transition falls back to the cut)
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.music_brain import djcore_bridge

router = APIRouter()

State = Literal["ready", "in_flight", "failed", "none"]


class DeckStems(BaseModel):
    state: State
    eta_s: Optional[float] = Field(default=None, ge=0, le=3600)


class HoldBody(BaseModel):
    a: DeckStems
    b: DeckStems
    bar_s: float = Field(gt=0, le=10)
    room_s: Optional[float] = Field(default=None, ge=0, le=3600)
    intro_outro: bool = False
    held_phrases: int = Field(default=0, ge=0, le=16)


@router.post("/api/stem_hold")
def stem_hold(body: HoldBody):
    try:
        return djcore_bridge.call("stem_hold", body.model_dump(exclude_none=True))
    except djcore_bridge.DjCoreUnavailable as e:
        return {"action": "cut", "phrases": 0, "hold_s": 0, "why": f"dj-core is not available: {e}"}
    except djcore_bridge.DjCoreError as e:
        return {"action": "cut", "phrases": 0, "hold_s": 0, "why": f"dj-core refused the call: {e}"}
