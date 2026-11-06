"""POST /api/session/events: a batch of session events (included by server.py).

The phone queues its watchdog reports (glitch: silence / dropout / clipping / recovered, ear decisions)
and posts them a few at a time, so a slow network never holds the audio loop. Each item goes through
the same path as the console's single POST /api/session/event (kind whitelist, field renaming, session
log + song log); a bad item is skipped and counted, never a 4xx for the batch.
"""
from __future__ import annotations

from typing import Any, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()
MAX_EVENTS = 50                     # per request; the phone flushes every few seconds


class SessionEventBatch(BaseModel):
    events: List[Any] = Field(default_factory=list)


@router.post("/api/session/events")
def post_session_events(batch: SessionEventBatch):
    from app.ui import server

    if len(batch.events) > MAX_EVENTS:
        raise HTTPException(status_code=413, detail=f"at most {MAX_EVENTS} events per request")
    ok = skipped = 0
    session = None
    for item in batch.events:
        try:
            ev = server.SessionEvent(kind=str(item.get("kind", "")), data=item.get("data") or {})
            session = server.post_session_event(ev)["session"]
            ok += 1
        except (HTTPException, ValueError, TypeError, AttributeError):
            skipped += 1
    return {"ok": ok, "skipped": skipped, "session": session}
