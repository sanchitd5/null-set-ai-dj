"""Shadow mode of the graph picks (GRAPHDB G6): glue only, no atlas code.

Next to every LLM suggest call the server asks the song graph (graph.db, private dj-atlas, reached through
the atlas bridge as app.music_brain.atlas.graph_query) for its picks and logs both as one `graph_shadow`
session event: seed, the graph's top picks with scores, the LLM's picks, how many of the LLM's picks the
graph also had. Nothing the user sees changes, and a failure never reaches the request.

Mode (env DJ_GRAPH_PICKS): `shadow` (default) logs; `off` skips everything; `on` is G10 and behaves like
shadow until then.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
from pathlib import Path
from typing import Callable, Optional, Sequence

ENV_MODE = "DJ_GRAPH_PICKS"
ENV_DB = "DJ_GRAPH_DB"            # same override as the builder's graph_schema.graph_path
MODES = ("off", "shadow", "on")
LOGGED = 5                        # graph picks written to the event
K = 10                            # graph picks asked for; the overlap counts against all of them
COOLDOWN_NAMES = 8                # last played songs whose artists feed the cooldown


def mode() -> str:
    m = os.environ.get(ENV_MODE, "shadow").strip().lower()
    return m if m in MODES else "shadow"


def name_key(name: str) -> Optional[str]:
    """Graph key of a "Artist - Title" display name (pick_pool.key_of of the cleaned identity)."""
    from app.ui.services import pick_pool
    from app.ui.services.track_identity import clean_identity

    artist, title = clean_identity(str(name))
    if not title or artist == "Unknown":
        return None
    return pick_pool.key_of(artist, title)


def _keys(names: Sequence[str]) -> list:
    return [k for k in (name_key(n) for n in names) if k]


def state_from_request(req, history_display, avoid_display, queue_display, earlier, track_names,
                       lang_check=None, seed_bpm: Optional[float] = None) -> dict:
    """The graph query's set state from the suggest request. Played = history, queue and earlier sets (the
    pool excludes the same); rejected = avoid. Keys go through pick_pool.key_of, so they equal graph keys."""
    from app.music_brain.generated.dj_config import KEYLOCK_RANGE_PCT

    played = _keys(list(history_display) + list(queue_display) + list(earlier))
    recent = [k.split("|", 1)[0] for k in _keys(list(history_display)[-COOLDOWN_NAMES:])]
    return {"played_keys": played, "rejected_keys": _keys(avoid_display), "vetoed_keys": [],
            "recent_artist_keys": recent, "seed_bpm": float(seed_bpm) if seed_bpm else None,
            "bpm_window_pct": KEYLOCK_RANGE_PCT}


def graph_path() -> Path:
    env = os.environ.get(ENV_DB)
    if env:
        return Path(env).expanduser()
    from app.music_brain.config import CACHE_DIR

    return Path(CACHE_DIR) / "graph.db"


def shadow(seed_track_id: str, state: dict, llm_suggestions: Sequence[dict],
           lang_check: Optional[Callable] = None, seed_key: Optional[str] = None) -> Optional[dict]:
    """Log one `graph_shadow` event; returns the logged fields (None when nothing was logged).
    Silent when off, when the atlas builder or graph.db is missing, and on any error."""
    try:
        if mode() == "off":
            return None
        from app.music_brain import atlas_bridge

        if not atlas_bridge.AVAILABLE:
            return None
        path = graph_path()
        if not path.is_file():
            return None
        gq = importlib.import_module("app.music_brain.atlas.graph_query")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            res = gq.next_result(conn, {"key": seed_key, "track_id": seed_track_id}, state, K, None,
                                 (lambda n: lang_check(n)[0]) if lang_check else None)
        finally:
            conn.close()
        picks = res.get("picks") or []
        llm = [k for k in (name_key(f"{s.get('artist', '')} - {s.get('title', '')}")
                           for s in llm_suggestions if isinstance(s, dict)) if k]
        have = {p["key"] for p in picks}
        fields = {"seed": seed_key or seed_track_id, "reason": res.get("reason"), "n_graph": len(picks),
                  "graph": [{"key": p["key"], "score": p["score"], "hops": p["hops"]} for p in picks[:LOGGED]],
                  "llm": llm, "overlap": sum(1 for k in llm if k in have)}
        from app.ui.services import session_log

        session_log.log("graph_shadow", **fields)
        return fields
    except Exception:
        return None
