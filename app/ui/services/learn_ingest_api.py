"""POST /api/mobile/learn: the phone's outbox lands here (included by server.py; behind the app's auth
middleware like every /api route).

    {"v": 1, "device_id", "device_name", "set_id", "items": [{"id", "kind", "at", "data"}]}     <= 100 items, <= 1 MB
    kind: set_log | transition | bad_pair | like        (what each becomes: music_brain/learning/phone_learn.py)
    -> {"ok": true, "accepted": [ids], "duplicate": [ids], "rejected": [{"id", "why"}], "publish": bool}

Idempotent: an item id seen before is "duplicate" and changes nothing, so the phone may resend freely.
The phone drops accepted, duplicate and rejected items from its outbox. A batch holding a set log also
schedules the knowledge publish (a commit to the private dj-atlas repo per set, when DJ_ATLAS_AUTOPUBLISH is on).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

router = APIRouter()
MAX_BODY = 1_000_000              # a long set's log is the biggest item (<= 5000 control events)


@router.post("/api/mobile/learn")
async def post_learn(request: Request):
    from app.music_brain.learning import phone_learn as pl

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        raise HTTPException(status_code=413, detail=f"at most {MAX_BODY} bytes")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(status_code=413, detail=f"at most {MAX_BODY} bytes")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail="body must be JSON") from None
    try:
        res = await run_in_threadpool(_ingest, body)
    except pl.LearnError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if res["accepted"]:
        pl.schedule(publish=res["publish"], cache_dir=_cache(), log=lambda m: print(m, flush=True))
    print(f"[learn] {len(res['accepted'])} new, {len(res['duplicate'])} duplicate, {len(res['rejected'])} rejected "
          f"from {str(body.get('device_id'))[:16]}", flush=True)
    return {"ok": True, "accepted": res["accepted"], "duplicate": res["duplicate"],
            "rejected": res["rejected"], "publish": res["publish"]}


def _ingest(body: dict) -> dict:
    from app.music_brain.learning import phone_learn as pl
    from app.ui import server
    from app.ui.services import replay_api

    return pl.ingest(body, _cache(), liked_dir=replay_api.REPLAY_CACHE_DIR, name_of=server._name_of,
                     save_set_log=server.save_set_log)


def _cache():
    from app.ui import server

    return server.CACHE_DIR
