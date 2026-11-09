"""Shadow mode of the graph picks (GRAPHDB G6): glue only, no atlas code.

Next to every LLM suggest call the server asks the song graph (graph.db, private dj-atlas, reached through
the atlas bridge as app.music_brain.atlas.graph_query) for its picks and logs both as one `graph_shadow`
session event: seed, the graph's top picks with scores, the LLM's picks, how many of the LLM's picks the
graph also had. Nothing the user sees changes, and a failure never reaches the request.

Mode (env DJ_GRAPH_PICKS): `shadow` (default) logs; `off` skips everything; `on` (G10) lets the graph answer the
suggest call: picks_for() maps the graph's picks to suggestions, autopilot_service runs them through the same
filters as the LLM's picks and decide() says whether enough survive. Enough: no LLM call. Too few: the LLM
tops the list up and the graph's good picks stay first. Env DJ_GRAPH_MIN_GOOD / DJ_GRAPH_MIN_SCORE override the
dj-config GRAPH_PARAMS gates without a deploy.
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
ENV_MIN_GOOD = "DJ_GRAPH_MIN_GOOD"
ENV_MIN_SCORE = "DJ_GRAPH_MIN_SCORE"


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


def thresholds() -> dict:
    """{good, score}: the graph-first gates. dj-config GRAPH_PARAMS (min_good, min_score), env overrides."""
    from app.music_brain.generated.dj_config import GRAPH_PARAMS

    good, score = int(GRAPH_PARAMS["min_good"]), float(GRAPH_PARAMS["min_score"])
    try:
        good = int(os.environ.get(ENV_MIN_GOOD, good))
        score = float(os.environ.get(ENV_MIN_SCORE, score))
    except ValueError:
        pass
    return {"good": good, "score": score}


def log_decision(decision: str, **fields) -> None:
    """The one `graph_pick` session event: decision graph | llm_topup | llm_fallback, `why` for the last
    (seed_unknown | too_few | error | off). Never raises."""
    try:
        from app.ui.services import session_log

        session_log.log("graph_pick", decision=decision, **fields)
    except Exception:
        pass


def top_reason(reasons: Sequence[dict]) -> str:
    """The strongest positive reason of a graph pick in plain words, e.g. "graph: measured 0.77"."""
    best = max((r for r in reasons if r.get("kind") in ("edge", "hop2", "artist")),
               key=lambda r: r.get("value", 0.0), default=None)
    return f"graph: {best['via']} {best['value']:.2f}" if best else "graph"


def picks_for(seed_track_id: str, state: dict, n: int, lang_check: Optional[Callable] = None,
              seed_key: Optional[str] = None, label_of: Optional[Callable[[str], dict]] = None,
              extra: int = 6) -> tuple:
    """(suggestions, why). The graph's top n+extra picks in the shape the suggest filters expect, each marked
    `pool_source: "graph"`. why is None when picks came back, else `seed_unknown` or `error` (graph file
    missing, builder absent, query failed). label_of(name) -> {genre, era} is the server's label lookup;
    unknown fields stay absent, which the filters keep."""
    try:
        from app.music_brain import atlas_bridge

        if not atlas_bridge.AVAILABLE:
            return [], "error"
        path = graph_path()
        if not path.is_file():
            return [], "error"
        gq = importlib.import_module("app.music_brain.atlas.graph_query")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            res = gq.next_result(conn, {"key": seed_key, "track_id": seed_track_id}, state,
                                 max(K, n + extra), None,
                                 (lambda node: lang_check(node)[0]) if lang_check else None)
        finally:
            conn.close()
        if res.get("reason") == "seed_unknown":
            return [], "seed_unknown"
        out = []
        for p in res.get("picks") or []:
            if not p.get("artist") or not p.get("title"):
                continue
            lab = (label_of(f"{p['artist']} - {p['title']}") if label_of else None) or {}
            why = top_reason(p.get("reasons") or [])
            s = {"artist": p["artist"], "title": p["title"], "reason": why, "genre": lab.get("genre") or "",
                 "era": lab.get("era") or "", "pool_source": "graph", "graph_key": p["key"],
                 "graph_score": p["score"], "graph_why": why, "graph_reasons": p.get("reasons") or []}
            if p.get("track_id"):
                s["track_id"] = p["track_id"]
            if p.get("yt_id"):
                s["yt_id"] = p["yt_id"]
            out.append(s)
        return out, None
    except Exception:
        return [], "error"


def decide(survivors: Sequence[dict], gates: Optional[dict] = None) -> tuple:
    """(decision, good_picks). decision = {use_graph, good, why} from the dj-atlas mirror of dj-core's
    decideGraphFirst; good_picks are the survivors with graph_score >= the score gate, graph order kept.
    A builder without `decide` (older dj-atlas pin) gives why "error": the LLM path runs."""
    gates = gates or thresholds()
    try:
        gq = importlib.import_module("app.music_brain.atlas.graph_query")
        d = gq.decide([x["graph_score"] for x in survivors], gates["good"], gates["score"])
    except Exception:
        return {"use_graph": False, "good": 0, "why": "error"}, []
    return d, [x for x in survivors if x["graph_score"] >= gates["score"]]
