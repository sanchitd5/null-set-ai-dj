"""ON-DEMAND lyric remix plans (OD-4): the song menu asks for a pair that has no precomputed plan; the plan is made now.

The candidates, the judge prompt and the plan store are the nightly build's own (atlas_bridge.plan_pair_on_demand adapts the private
builder); this module only runs it safely on the shared server:
  - ONE plan at a time (a single worker thread; the server LLM is shared with the live set and its priority gate), at most
    MAX_QUEUE pairs waiting, a hard deadline per plan, no retry storm: a failed pair keeps its reason for FAIL_TTL_S.
  - the LLM call is the server's own chat path (autopilot_service.chat_raw, priority SUGGEST, bounded by CALL_TIMEOUT_S); there is no
    other model to fall back to, a missing / slow LLM is a plain "try again" answer.

POST /api/song_menu/plan {a, b}      -> status (starts the plan when none runs and the pair is eligible)
GET  /api/song_menu/plan?a=&b=       -> status
status {state: none | in_flight | done | failed, reason, eta_s}; "done" means the plan is in the plan store
(lyric_remix_api.load()), the next /api/song_menu/actions answer carries it.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.music_brain import atlas_bridge, song_store
from app.ui.services import lyric_remix_api as lra

router = APIRouter()

_ID = r"[0-9A-Za-z_-]{1,64}"
CALL_TIMEOUT_S = 75.0        # one judge call, queue wait for the LLM slot included
PLAN_DEADLINE_S = 160.0      # a whole plan (at most two judge calls) never runs longer than this
DEFAULT_ETA_S = 40.0
FAIL_TTL_S = 600.0           # a failed pair is not tried again for this long (no GPU storms)
BUSY_TTL_S = 20.0
MAX_QUEUE = 3                # pairs running + waiting


def _default_chat(system: str, user: str) -> str:
    from app.ui.services import autopilot_service as ap
    from app.ui.services import llm_gate

    return ap.chat_raw(system, user, temperature=0.2, timeout=CALL_TIMEOUT_S, max_tokens=1500, priority=llm_gate.SUGGEST,
                       kind="lyric_remix_ondemand")


def _cache_dir() -> Path:
    from app.music_brain.config import CACHE_DIR

    return Path(CACHE_DIR)


class Host:
    """What the server provides; tests replace it."""
    chat: Callable[[str, str], str] = staticmethod(_default_chat)
    cache_dir: Callable[[], Path] = staticmethod(_cache_dir)
    planner: Callable[..., dict] = staticmethod(lambda *a, **k: atlas_bridge.plan_pair_on_demand(*a, **k))
    now: Callable[[], float] = staticmethod(time.time)


def configure(**kw) -> None:
    for k, v in kw.items():
        setattr(Host, k, staticmethod(v))


_lock = threading.Lock()
_jobs: Dict[tuple, dict] = {}          # (a, b) -> {state, reason, started, until, queued_at}
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lyric-remix-ondemand")
_avg_s = [DEFAULT_ETA_S]               # moving average of finished plans


_names: Dict[str, Any] = {"key": None, "d": {}}


def name_of(tid: str) -> str:
    try:
        p = song_store.names_file(Host.cache_dir())
        st = p.stat()
        if _names["key"] != (str(p), st.st_mtime_ns, st.st_size):
            d = json.loads(p.read_text(encoding="utf-8"))
            _names.update(key=(str(p), st.st_mtime_ns, st.st_size), d=d if isinstance(d, dict) else {})
        n = _names["d"].get(tid)
    except (OSError, ValueError):
        n = None
    return n if isinstance(n, str) and n else "this song"


def has_table(doc: dict, tid: str) -> bool:
    """A cached line table exists for the song (the plan store's, or the builder's own cache)."""
    if (doc["songs"].get(tid) or {}).get("lines"):
        return True
    return (Host.cache_dir() / "lyric_remix" / "lines" / f"{tid}.json").is_file()


def eligibility(doc: dict, a: str, b: str) -> Optional[str]:
    """None when a plan can be made for a -> b, else the plain reason it cannot."""
    if not atlas_bridge.AVAILABLE:
        return "lyric remix planning is not available on this server"
    for tid in (a, b):
        if not has_table(doc, tid):
            return f"no lyrics transcribed yet for {name_of(tid)}"
    return None


def _eta(j: dict, now: float) -> float:
    """Seconds until this job is done: the running one's remaining time plus everything queued ahead, by the moving average."""
    ahead = sum(1 for k, x in _jobs.items() if x["state"] == "in_flight" and x["queued_at"] < j["queued_at"])
    run = max(0.0, _avg_s[0] - (now - j["started"])) if j.get("started") else _avg_s[0]
    return round(run + ahead * _avg_s[0], 1)


def status(a: str, b: str) -> dict:
    now = Host.now()
    with _lock:
        j = _jobs.get((a, b))
        if j and j["state"] != "in_flight" and now > j["until"]:
            _jobs.pop((a, b), None)
            j = None
        if j is None:
            return {"state": "none", "reason": None, "eta_s": None}
        return {"state": j["state"], "reason": j.get("reason"), "eta_s": _eta(j, now) if j["state"] == "in_flight" else None}


def _run(a: str, b: str) -> None:
    key = (a, b)
    t0 = Host.now()
    with _lock:
        _jobs[key]["started"] = t0
    deadline = t0 + PLAN_DEADLINE_S
    try:
        res = Host.planner(a, b, Host.cache_dir(), Host.chat, log=lambda m: None, names=name_of,
                           should_stop=lambda: Host.now() > deadline)
    except Exception as e:                                     # noqa: BLE001 -- a failed plan never takes the server down
        res = {"ok": False, "reason": f"the lyric remix could not be planned ({type(e).__name__})"}
    done = Host.now()
    with _lock:
        if res.get("ok"):
            _avg_s[0] = round(0.7 * _avg_s[0] + 0.3 * max(1.0, done - t0), 1)
            _jobs[key].update(state="done", reason=None, until=done + 30.0)
        else:
            _jobs[key].update(state="failed", reason=str(res.get("reason") or "no lyric remix could be made for these two songs"),
                              until=done + FAIL_TTL_S)


def start(a: str, b: str) -> dict:
    """Queue the plan of a -> b unless one exists, runs, or failed recently. Returns the status."""
    doc = lra.load()
    if lra._plans_for(doc, a, b):
        return {"state": "done", "reason": None, "eta_s": None}
    why = eligibility(doc, a, b)
    if why:
        return {"state": "failed", "reason": why, "eta_s": None}
    now = Host.now()
    with _lock:
        j = _jobs.get((a, b))
        if j and (j["state"] == "in_flight" or now <= j["until"]):
            pass
        elif sum(1 for x in _jobs.values() if x["state"] == "in_flight") >= MAX_QUEUE:
            return {"state": "failed", "reason": "other lyric remixes are being planned, try again in a minute", "eta_s": None}
        else:
            _jobs[(a, b)] = {"state": "in_flight", "reason": None, "started": None, "queued_at": now, "until": now + PLAN_DEADLINE_S * MAX_QUEUE}
            _pool.submit(_run, a, b)
    return status(a, b)


def ctx_fields(doc: dict, a: str, b: str) -> Dict[str, Any]:
    """The plan fields dj-core's song_menu_actions reads (canPlan / planState / planEtaS / planReason) for a pair without a plan."""
    st = status(a, b)
    if st["state"] == "in_flight":
        return {"canPlan": False, "planState": "in_flight", "planEtaS": st["eta_s"]}
    if st["state"] == "failed":
        return {"canPlan": False, "planState": "failed", "planReason": st["reason"]}
    why = eligibility(doc, a, b)
    if why:
        return {"canPlan": False, "planState": "failed", "planReason": why}
    return {"canPlan": True}


class PlanBody(BaseModel):
    a: str = Field(pattern=f"^{_ID}$")
    b: str = Field(pattern=f"^{_ID}$")


@router.post("/api/song_menu/plan")
def song_menu_plan(body: PlanBody):
    if body.a == body.b:
        raise HTTPException(status_code=422, detail="pick two different songs")
    return start(body.a, body.b)


@router.get("/api/song_menu/plan")
def song_menu_plan_status(a: str, b: str):
    if not (re.fullmatch(_ID, a) and re.fullmatch(_ID, b)):
        raise HTTPException(status_code=422, detail="bad track id")
    st = status(a, b)
    if st["state"] == "none" and lra._plans_for(lra.load(), a, b):
        st = {"state": "done", "reason": None, "eta_s": None}
    return st
