"""REST relay of dj-core's stem toggle rule (included by server.py). May a stem chip (drums / bass / vocals / synth) be pressed right
now? Readiness, transition ownership and "keep one stem up" are decided in dj-core (service fn "stem_toggle_rule"), the same rule the
phone deck console and the car lanes use. This module only validates the body and relays the answer.

POST /api/stem_toggle {stem_state: {role: on}, readiness: ready | in_flight | failed | none, transitioning, stem?, on?}
                      -> {ok, reason}; always 200 once the body is valid (dj-core missing -> {"ok": true, "reason": null}: the chip
                         is not locked by a rule nobody can evaluate, the deck itself still refuses a toggle without stems)
"""
from __future__ import annotations

from typing import Dict, Literal, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.music_brain import djcore_bridge

router = APIRouter()


class ToggleBody(BaseModel):
    stem_state: Dict[str, bool]
    readiness: Literal["ready", "in_flight", "failed", "none"]
    transitioning: bool = False
    stem: Optional[Literal["drums", "bass", "vocals", "other"]] = None
    on: Optional[bool] = None


@router.post("/api/stem_toggle")
def stem_toggle(body: ToggleBody):
    try:
        return djcore_bridge.call("stem_toggle_rule", body.model_dump(exclude_none=True))
    except (djcore_bridge.DjCoreUnavailable, djcore_bridge.DjCoreError):
        return {"ok": True, "reason": None}
