"""SONG MENU actions (included by server.py): for the song on air (A) and each candidate B of a visible list, can the console
book "Lyric remix" or "Vocal swap" as B's transition, in what state, and with which plan. The decision is dj-core's (service fn
"song_menu_actions", OD-1); this module only builds its context from data the server already has (stem readiness, analyses, the
lyric remix data file and timed lyrics) and relays the answer. No gate lives here.

POST /api/song_menu/actions  {a, bs: [id, ...], pos_s?, relaxed?, quick?}
    -> {a, items: {<b>: {lyricRemix: Action, vocalSwap: Action}}}
    Action {state: available | preparing | not_possible, reason, eta_s, plan, plan_needed, hold}; always 200 once the body is valid
    (dj-core missing -> every action not_possible with the reason).
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.music_brain import djcore_bridge, song_store
from app.music_brain.matching import techniques as tq
from app.ui.services import lyric_remix_api as lra

router = APIRouter()
MAX_BS = 50
_ID = r"[0-9A-Za-z_-]{1,64}"
_LRC_LINE = re.compile(r"^\s*\[\d+:\d+(?:[.:]\d+)?\]\s*\S", re.M)


class Host:
    """What the server provides (set by server.py); tests replace it."""
    analysis: Callable[[str], Optional[dict]] = staticmethod(lambda tid: None)     # stored analysis record, never computes
    stem_readiness: Callable[[str], dict] = staticmethod(lambda tid: {"state": "none"})


def configure(**kw) -> None:
    for k, v in kw.items():
        setattr(Host, k, staticmethod(v))


class Body(BaseModel):
    a: str = Field(pattern=f"^{_ID}$")
    bs: List[str] = Field(max_length=MAX_BS)
    pos_s: float = Field(default=0.0, ge=0, le=36000)
    relaxed: bool = False
    quick: bool = False


def _stems(tid: str) -> dict:
    r = Host.stem_readiness(tid) or {}
    st = r.get("state")
    st = st if st in ("ready", "in_flight", "failed") else "none"
    eta = r.get("ready_in_s")
    return {"state": st, "eta_s": eta if isinstance(eta, (int, float)) and eta >= 0 else None}


def timed_lines(doc: dict, tid: str) -> int:
    """How many timed lyric lines the song has: the lyric remix data file's, else a synced lyrics file's."""
    n = len((doc["songs"].get(tid) or {}).get("lines") or [])
    if n:
        return n
    try:
        return len(_LRC_LINE.findall(song_store.lyrics_path(tid, synced=True).read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return 0


def _key(an: Optional[dict]) -> Optional[str]:
    k = (an or {}).get("key")
    return k.get("camelot") if isinstance(k, dict) else None


def build_ctx(a: str, b: str, an_a: Optional[dict], an_b: Optional[dict], doc: dict, pos_s: float, relaxed: bool, quick: bool,
              stems_a: dict, stems_b: dict, lines: Dict[str, int]) -> dict:
    bpm_a, bpm_b = (an_a or {}).get("bpm"), (an_b or {}).get("bpm")
    ka, kb = _key(an_a), _key(an_b)
    plans = lra._plans_for(doc, a, b)
    ctx: Dict[str, Any] = {
        "aId": a, "bId": b, "relaxed": relaxed, "quick": quick, "posS": pos_s,
        "bpms": {k: v for k, v in ((a, bpm_a), (b, bpm_b)) if isinstance(v, (int, float)) and v > 0},
        "barS": 240.0 / bpm_a if isinstance(bpm_a, (int, float)) and bpm_a > 0 else 0,
        "keyScore": tq.camelot_score(ka, kb) if ka and kb else None,
        "stems": {"a": stems_a, "b": stems_b}, "plans": plans, "lines": {"a": lines[a], "b": lines[b]},
        # plans come only from the precomputed data file: a pair without one cannot be made on demand here
        "canPlan": False,
    }
    if not plans and lines[a] > 0 and lines[b] > 0:
        ctx.update(planState="failed", planReason="no lyric remix has been made for these two songs yet")
    if an_a:
        ctx["aAnalysis"] = {k: an_a[k] for k in ("duration", "music_span", "sections") if k in an_a}     # what song bounds read
    return ctx


def _all_no(reason: str) -> dict:
    n = {"state": "not_possible", "reason": reason, "eta_s": None, "plan": None, "plan_needed": False, "hold": None}
    return {"lyricRemix": n, "vocalSwap": n}


@router.post("/api/song_menu/actions")
def song_menu_actions(body: Body):
    ids = [x for x in dict.fromkeys(body.bs) if re.fullmatch(_ID, x)]
    doc = lra.load()
    an_a = Host.analysis(body.a)
    stems_a, lines = _stems(body.a), {body.a: timed_lines(doc, body.a)}
    items: Dict[str, dict] = {}
    for b in ids:
        if b == body.a:
            items[b] = _all_no("this song is already playing")
            continue
        lines[b] = timed_lines(doc, b)
        ctx = build_ctx(body.a, b, an_a, Host.analysis(b), doc, body.pos_s, body.relaxed, body.quick, stems_a, _stems(b), lines)
        try:
            items[b] = djcore_bridge.call("song_menu_actions", {"ctx": ctx})
        except djcore_bridge.DjCoreUnavailable as e:
            items[b] = _all_no(f"dj-core is not available: {e}")
        except djcore_bridge.DjCoreError as e:
            items[b] = _all_no(f"dj-core refused the call: {e}")
    return {"a": body.a, "items": items}
