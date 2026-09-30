"""One event log per app session (one server start), for finding what lagged.

    data/cache/sessions/<YYYY-MM-DD_HHMMSS>/events.jsonl

Each line: {"t": epoch s, "at": "HH:MM:SS", "kind": ..., ...fields}. Kinds:
  track       the browser: a deck loaded a song, a transition started / ended
  llm         a text-model call: call (suggest/lookahead/plan/set_ai), priority, max_tokens,
              waited, elapsed, ok, prompt_chars, reply_chars, chars_per_s, cut_off,
              quality (ok/empty/no_json/cut_off/error)
  llm_retry   one bounded JSON retry: call (suggest/lookahead/plan/ear), reason
              (cut_off/no_json/empty/bad_json/parroted, "*_gave_up" when the retry failed too)
  ear         a live-ear call: latency, source (model/rules), action, error, timeout
              (the adaptive wait used), quality, retried
  ear_merge   the silent ear rating a song merge
  gate_skip   a look-ahead refused because the model was busy / the ear had it

GET /api/session/log returns the current session (or ?session=<id>), with a
summary: calls per kind, mean / max latency, and the slowest calls.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from app.music_brain.config import CACHE_DIR

SESSIONS_DIR = CACHE_DIR / "sessions"
SESSION_ID = time.strftime("%Y-%m-%d_%H%M%S")
_lock = threading.Lock()
MAX_FIELD = 300
MAX_NESTED = 4000
# Never pruned (owner: "always have all sets"): every session folder and its events.jsonl stay.


def _path(session: Optional[str] = None) -> Path:
    sid = session or SESSION_ID
    if not all(c.isalnum() or c in "-_" for c in sid):
        raise ValueError(f"bad session id {sid!r}")
    return SESSIONS_DIR / sid / "events.jsonl"


def log(kind: str, **fields) -> None:
    """One event into the installed engine's log sink (production: the session file below)."""
    from app.ui.services import engine

    engine.current().host.log_event(kind, **fields)


def _write(kind: str, **fields) -> None:
    """Append one event. Never raises: logging must not break a live set."""
    try:
        ev = {"t": round(time.time(), 3), "at": time.strftime("%H:%M:%S"), "kind": str(kind)[:40]}
        for k, v in fields.items():
            if isinstance(v, str) and len(v) > MAX_FIELD:
                v = v[:MAX_FIELD] + "..."
            elif not isinstance(v, (int, float, bool, type(None), str)):
                enc = json.dumps(v, default=str)
                if len(enc) > MAX_NESTED:          # a nested glitch report stays bounded
                    v = enc[:MAX_NESTED] + "..."
            ev[k] = v
        line = json.dumps(ev, ensure_ascii=False, default=str)
        p = _path()
        with _lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        from app.music_brain import history

        history.on_event(SESSION_ID, p)           # the set-history index (user DB); never raises
    except Exception:
        pass


def read(session: Optional[str] = None, limit: int = 500) -> list:
    try:
        lines = _path(session).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-max(1, limit):]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def sessions() -> list:
    if not SESSIONS_DIR.is_dir():
        return []
    return sorted((d.name for d in SESSIONS_DIR.iterdir() if (d / "events.jsonl").exists()), reverse=True)


def summary(events: list) -> dict:
    by: dict = {}
    for e in events:
        k = e.get("kind")
        s = by.setdefault(k, {"count": 0, "errors": 0, "latencies": []})
        s["count"] += 1
        if e.get("ok") is False or e.get("error"):
            s["errors"] += 1
        lat = e.get("elapsed") if e.get("elapsed") is not None else e.get("latency")
        if isinstance(lat, (int, float)):
            s["latencies"].append(lat)
    for s in by.values():
        lats = s.pop("latencies")
        s["mean_s"] = round(sum(lats) / len(lats), 2) if lats else None
        s["max_s"] = round(max(lats), 2) if lats else None
    slow = sorted((e for e in events if isinstance(e.get("elapsed", e.get("latency")), (int, float))),
                  key=lambda e: -(e.get("elapsed") if e.get("elapsed") is not None else e.get("latency")))[:5]
    tracks = sum(1 for e in events if e.get("kind") == "track" and e.get("event") == "transition_start")
    return {"by_kind": by, "transitions": tracks, "slowest": slow}
