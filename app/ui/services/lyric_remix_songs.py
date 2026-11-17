"""LYRIC REMIX LIST and AI PICKS: the accepted lyric remix plans as a browsable list, and the rule that lets the AI DJ choose one.

A lyric remix is NOT a rendered track. The DJ performs it live: the real song (or the A -> B pair) plays and the lyric_remix move lays the
plan's vocal pieces over the instrumental from the stems, on the web console and on the phone (decision: dj-core `lyric_remix_plan` with
ctx `wantPlan`, see lyric-remix.js and the phone's engine). This module only

- lists the plans (title, new lyric, score, the song ids), `GET /api/lyric_remix/songs`;
- keeps the owner's settings (`ai_picks` switch, `min_songs` rate limit), `GET|PUT /api/lyric_remix/settings`;
- attaches a remix to the AI DJ's next pick when the switch is on (`attach_pick`, called by the suggest route): the rule itself is dj-core's
  `lyric_remix_pick` (candidate plans whose song(s) are among the picks that already passed the shared gates, rate limit, tempo fit).

The old render pipeline (songs/_remix cache, /api/audio/remix) is gone; `cleanup_legacy` removes what it left on disk and carries the
setting over.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from fastapi import APIRouter
from pydantic import BaseModel

from app.music_brain import djcore_bridge, song_store
from app.ui.services import lyric_remix_api as lra

log = logging.getLogger(__name__)
router = APIRouter()

DEFAULT_MIN_SONGS = 4        # at most one lyric remix per this many songs (the owner can change it, 1..50)
_STATE_CAP = 64


class Host:
    """What the server provides (set by server.py): library lookups. Tests replace it."""
    track_path: Callable[[str], Optional[Path]] = staticmethod(lambda tid: None)
    name_of: Callable[[str], str] = staticmethod(lambda tid: tid)


def configure(**kw) -> None:
    for k, v in kw.items():
        setattr(Host, k, staticmethod(v) if callable(v) else v)
    cleanup_legacy()


# ------------------------------------------------------------------------------------------------ settings


def settings_path() -> Path:
    return song_store._cache() / "lyric_remix_settings.json"


def _legacy_dir() -> Path:
    return song_store.songs_dir() / "_remix"


def _clean_min_songs(v: Any) -> int:
    try:
        return max(1, min(50, int(v)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_SONGS


def get_settings() -> dict:
    try:
        d = json.loads(settings_path().read_text(encoding="utf-8"))
        return {"ai_picks": bool(d.get("ai_picks")), "min_songs": _clean_min_songs(d.get("min_songs", DEFAULT_MIN_SONGS)),
                "auto_remix": bool(d.get("auto_remix", True))}
    except (OSError, ValueError, AttributeError):
        return {"ai_picks": False, "min_songs": DEFAULT_MIN_SONGS, "auto_remix": True}


def set_settings(ai_picks: Optional[bool] = None, min_songs: Optional[int] = None, auto_remix: Optional[bool] = None) -> dict:
    cur = get_settings()
    new = {"ai_picks": cur["ai_picks"] if ai_picks is None else bool(ai_picks),
           "min_songs": cur["min_songs"] if min_songs is None else _clean_min_songs(min_songs),
           "auto_remix": cur["auto_remix"] if auto_remix is None else bool(auto_remix)}
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(new), encoding="utf-8")
    os.replace(tmp, p)
    return get_settings()


def cleanup_legacy() -> int:
    """Remove the render cache the old pipeline left (songs/_remix: audio, meta, settings) after carrying its setting over. -> entries removed."""
    d = _legacy_dir()
    if not d.is_dir():
        return 0
    try:
        if not settings_path().exists():
            old = json.loads((d / "settings.json").read_text(encoding="utf-8"))
            set_settings(ai_picks=bool(old.get("ai_picks")))
    except (OSError, ValueError, AttributeError):
        pass
    n = sum(1 for _ in d.rglob("*"))
    shutil.rmtree(d, ignore_errors=True)
    if n:
        log.info("lyric remix: removed the old render cache (%d entries)", n)
    return n


# ------------------------------------------------------------------------------------------------ list


def _title(plan: dict) -> str:
    a = Host.name_of(plan["a"])
    return f"{a} - lyric remix" if not plan.get("b") else f"{a} x {Host.name_of(plan['b'])} - lyric remix"


def _available(plan: dict) -> bool:
    return all(Host.track_path(t) is not None for t in (plan.get("a"), plan.get("b")) if t)


def _song(plan: dict) -> dict:
    return {"plan_id": plan["id"], "kind": "pair" if plan.get("b") else "within", "a": plan["a"], "b": plan.get("b"),
            "title": _title(plan), "text": plan.get("text") or "", "score": float(plan.get("score") or 0)}


def list_songs(q: str = "", limit: int = 1000) -> dict:
    doc = lra.load()
    out = []
    needle = q.strip().lower()
    for p in doc["plans"]:
        if not p.get("id") or not p.get("a") or not _available(p):
            continue
        s = _song(p)
        if needle and needle not in (s["title"] + " " + s["text"]).lower():
            continue
        out.append(s)
    out.sort(key=lambda s: (-s["score"], s["plan_id"]))
    return {"ai_picks": get_settings()["ai_picks"], "auto_remix": get_settings()["auto_remix"], "total": len(out), "songs": out[:max(1, limit)]}


# ------------------------------------------------------------------------------------------------ AI picks

_state: Dict[str, int] = {}          # set id -> history length when a remix was last attached (the rate limit); in memory, bounded
_state_lock = threading.Lock()


def attach_pick(suggestions: list, *, playing_id: str, set_id: str, history_len: int, recent_ids: Sequence[str] = ()) -> Optional[dict]:
    """The AI DJ's chance to pick a lyric remix. `suggestions` are the picks that already passed the shared gates (library picks carry a
    `track_id`). With the switch on and the rate limit open, dj-core `lyric_remix_pick` chooses a plan whose song(s) are among them; the
    chosen pick moves to the front and gets `lyric_remix` = {plan_id, kind, a, b, text, score}. -> that dict or None. Never raises."""
    try:
        st = get_settings()
        if not st["ai_picks"] or not playing_id:
            return None
        pool = [str(s["track_id"]) for s in suggestions if s.get("track_id")]
        if not pool:
            return None
        doc = lra.load()
        want = set(pool) | {playing_id}
        plans = [p for p in doc["plans"] if p.get("id") and p.get("a") in want and (p.get("b") is None or p.get("b") in want)]
        if not plans:
            return None
        with _state_lock:
            last = _state.get(set_id)
        ctx: Dict[str, Any] = {
            "enabled": True, "playingId": playing_id, "pool": pool, "plans": plans, "recent": list(recent_ids),
            "songsSinceRemix": (history_len - last) if last is not None else 1 << 20, "minSongs": st["min_songs"],
        }
        ctx["bpms"] = {t: b for t in {playing_id, *pool} if isinstance((b := (doc["songs"].get(t) or {}).get("bpm")), (int, float)) and b > 0}
        pick = djcore_bridge.call("lyric_remix_pick", {"ctx": ctx})
        if not isinstance(pick, dict) or not pick.get("plan_id"):
            return None
        row = next((s for s in suggestions if str(s.get("track_id")) == pick["song"]), None)
        if row is None:
            return None
        lr = {k: pick[k] for k in ("plan_id", "kind", "a", "b", "text", "score")}
        row["lyric_remix"] = lr
        suggestions.remove(row)
        suggestions.insert(0, row)
        with _state_lock:
            _state[set_id] = history_len
            while len(_state) > _STATE_CAP:
                _state.pop(next(iter(_state)))
        return lr
    except (djcore_bridge.DjCoreUnavailable, djcore_bridge.DjCoreError) as e:
        log.info("lyric remix pick unavailable: %s", e)
    except Exception as e:      # noqa: BLE001 - a remix is a bonus: the plain picks always go out
        log.warning("lyric remix pick failed: %r", e)
    return None


# ------------------------------------------------------------------------------------------------ REST


class SettingsBody(BaseModel):
    ai_picks: Optional[bool] = None
    min_songs: Optional[int] = None
    auto_remix: Optional[bool] = None


@router.get("/api/lyric_remix/songs")
def api_songs(q: str = "", limit: int = 1000):
    return list_songs(q, limit)


@router.get("/api/lyric_remix/settings")
def api_get_settings():
    return get_settings()


@router.put("/api/lyric_remix/settings")
def api_put_settings(body: SettingsBody):
    return set_settings(body.ai_picks, body.min_songs, body.auto_remix)
