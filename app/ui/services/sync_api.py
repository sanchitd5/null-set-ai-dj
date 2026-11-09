"""Op-log sync endpoints (G9). Behind the app auth middleware like every /api route; no admin gate.

    POST /api/sync/ops     {"v":1,"device_id","ops":[...]} -> {"ok","accepted","duplicate","rejected","head"}
    GET  /api/sync/ops     ?since=N&limit=500&device_id=   -> {"ops","head","floor","more"}; 410 {"code":"resnapshot"}
    GET  /api/sync/head                                     -> {"head","floor"}
    GET  /api/sync/stream  ?since=N&device_id=  (SSE; Last-Event-ID resumes) hello, then op events, ": ping" / 15 s

Design: docs in music_brain/sync/oplog.py. Config: DJ_SYNC (default on), DJ_SYNC_MAX_STREAMS (16), DJ_SYNC_RATE
(ops per minute per caller, 600), DJ_SYNC_KEEP_DAYS (30, the compaction job).
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.music_brain.sync import identity, oplog
from app.music_brain.sync.oplog import MAX_BATCH, OpLog

router = APIRouter()
MAX_BODY = 1_000_000
PING_S = 15.0
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

_log: Optional[OpLog] = None
_log_lock = threading.Lock()
_streams: Dict[str, asyncio.Event] = {}         # device_id -> its stop event (one stream per device)
_rate: Dict[str, collections.deque] = {}


def enabled() -> bool:
    return os.environ.get("DJ_SYNC", "1").strip().lower() not in ("0", "false", "off", "no")


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def get_log() -> OpLog:
    """The process's op log (CACHE_DIR/sync.db); the private builder's appliers are registered on first use."""
    global _log
    with _log_lock:
        if _log is None:
            from app.music_brain import atlas_bridge, db
            from app.ui import server
            _log = OpLog(db.db_path(server.CACHE_DIR, "sync.db"))
            atlas_bridge.register_appliers(_log.register_applier)
        return _log


def use(log: Optional[OpLog]) -> None:
    """Tests: replace (or with None reset) the process's op log and drop stream and rate state."""
    global _log
    with _log_lock:
        _log = log
    _streams.clear()
    _rate.clear()


def _gate() -> None:
    if not enabled():
        raise HTTPException(404, "Not Found")


def _rate_ok(who: str, n: int) -> bool:
    limit, now = _int_env("DJ_SYNC_RATE", 600), time.monotonic()
    q = _rate.setdefault(who, collections.deque())
    while q and now - q[0][0] > 60:
        q.popleft()
    if sum(c for _, c in q) + n > limit:
        return False
    q.append((now, n))
    return True


def _who(request: Request) -> str:
    return str((request.scope.get("dj_auth") or {}).get("key") or "local")


def _device(v, required: bool) -> str:
    if v in (None, "") and not required:
        return ""
    if not isinstance(v, str) or not SAFE_ID.fullmatch(v):
        raise HTTPException(422, "device_id: 1-64 letters, digits, - or _")
    if oplog.is_reserved_device(v):
        raise HTTPException(422, "device_id is reserved")
    return v


def _wire(op: dict) -> dict:
    return {k: op[k] for k in ("seq", "op_id", "tbl", "key", "kind", "delta", "ts", "scope", "device")}


@router.post("/api/sync/ops")
async def post_ops(request: Request):
    _gate()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY:
        raise HTTPException(413, f"at most {MAX_BODY} bytes")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, f"at most {MAX_BODY} bytes")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(422, "body must be JSON") from None
    if not isinstance(body, dict) or body.get("v") != 1:
        raise HTTPException(422, "v must be 1")
    device = _device(body.get("device_id"), True)
    ops = body.get("ops")
    if not isinstance(ops, list) or len(ops) > MAX_BATCH:
        raise HTTPException(422, f"ops: a list of at most {MAX_BATCH}")
    if not _rate_ok(_who(request) + "|" + device, len(ops)):
        raise HTTPException(429, "too many ops, retry later")
    res = await run_in_threadpool(get_log().ingest, ops, device, identity.scope_for(request))
    return {"ok": True, **res}


def _check_since(log: OpLog, since: int):
    floor = log.floor()
    if since < floor:
        return JSONResponse({"code": "resnapshot", "floor": floor, "head": log.head()}, status_code=410,
                            headers={"cache-control": "no-store"})
    return None


@router.get("/api/sync/head")
async def get_head():
    _gate()
    log = get_log()
    return JSONResponse({"head": log.head(), "floor": log.floor()}, headers={"cache-control": "no-store"})


@router.get("/api/sync/ops")
async def get_ops(request: Request, since: int = 0, limit: int = 500, device_id: str = ""):
    _gate()
    device = _device(device_id or None, False)
    limit = max(1, min(int(limit), 1000))
    log = get_log()
    if since < 0:
        raise HTTPException(422, "since must be >= 0")

    def read():
        gone = _check_since(log, since)
        if gone is not None:
            return gone
        rows = log.since(identity.visible_scopes(request), since, limit + 1, device)
        return JSONResponse({"ops": [_wire(o) for o in rows[:limit]], "head": log.head(), "floor": log.floor(),
                             "more": len(rows) > limit}, headers={"cache-control": "no-store"})
    return await run_in_threadpool(read)


@router.get("/api/sync/stream")
async def get_stream(request: Request, since: int = 0, device_id: str = ""):
    _gate()
    device = _device(device_id or None, True)
    log = get_log()
    scopes = identity.visible_scopes(request)
    last = request.headers.get("last-event-id", "")
    cursor = max(since, int(last)) if last.isdigit() else since
    if cursor < 0:
        raise HTTPException(422, "since must be >= 0")
    gone = await run_in_threadpool(_check_since, log, cursor)
    if gone is not None:
        return gone
    if device not in _streams and len(_streams) >= _int_env("DJ_SYNC_MAX_STREAMS", 16):
        raise HTTPException(429, "too many open streams")
    stop = asyncio.Event()
    old = _streams.get(device)
    if old is not None:
        old.set()                                      # one stream per device: the new one wins
    _streams[device] = stop
    loop = asyncio.get_running_loop()
    wake = asyncio.Event()
    off = log.subscribe(lambda: loop.call_soon_threadsafe(wake.set))

    async def gen():
        pos = cursor
        try:
            yield ("event: hello\ndata: " + json.dumps({"head": await run_in_threadpool(log.head),
                                                        "floor": await run_in_threadpool(log.floor)}) + "\n\n").encode()
            while not stop.is_set():
                wake.clear()
                rows = await run_in_threadpool(log.since, scopes, pos, 200, device)
                for o in rows:
                    pos = o["seq"]
                    yield f"id: {o['seq']}\nevent: op\ndata: {json.dumps(_wire(o), separators=(',', ':'))}\n\n".encode()
                if rows:
                    continue
                waiters = [asyncio.ensure_future(wake.wait()), asyncio.ensure_future(stop.wait())]
                try:
                    done, _ = await asyncio.wait(waiters, timeout=PING_S, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for w in waiters:
                        w.cancel()
                if not done:
                    yield b": ping\n\n"
        finally:
            off()
            if _streams.get(device) is stop:
                del _streams[device]

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"cache-control": "no-store", "x-accel-buffering": "no"})


def compact_job(ctx) -> dict:
    """Nightly: fold old applied ops away (DJ_SYNC_KEEP_DAYS, 30)."""
    res = get_log().compact(_int_env("DJ_SYNC_KEEP_DAYS", 30))
    if res["deleted"]:
        ctx.log(f"compacted {res['deleted']} ops, floor {res['floor']}")
    return res
