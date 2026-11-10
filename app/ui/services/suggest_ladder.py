"""G20 guaranteed suggest: the deadline and the fallback ladder behind /api/autopilot/suggest.

Owner (2026-11-10): the phone only asks the server; the server answers within SUGGEST_DEADLINE_S (15 s) and there is a
song for sure. The LLM path gets a budget (SUGGEST_LLM_BUDGET_S, ~10 s); when it yields nothing or time is up the ladder
runs, first step that yields a pick wins:

  graph          graph picks (even in shadow mode)
  yt_fallback    YouTube related songs of the playing song (cached / graph first, fetch only if time remains)
  llm            LLM picks, only when graph + YouTube gave fewer than the target (a top-up, if time remains)
  library_scene  library song in the scene: atlas neighbours, then same-language songs with no genre jump
  library_language  any library song in the set language
  library_lru    least recently played library song in the set language (songs heard in earlier sets, oldest first)
  library_any    any library song not played this set

HARD gates never relax: not played / rejected / queued / vetoed (request `vetoed`) / the playing song, and the language of the set (a "block"
verdict never passes; a "penalty" verdict passes only at the very last step, library_any, so a mixed-language set
with nothing else still gets a song). SOFT gates (genre hop, heard in an earlier set) relax down the ladder.
Pure functions: every dependency is injected, so tests run with fakes.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional, Sequence

from app.music_brain.pick_accept import PickHooks, accept_candidate

DEADLINE_ENV = "SUGGEST_DEADLINE_S"
LLM_BUDGET_ENV = "SUGGEST_LLM_BUDGET_S"
DEFAULT_DEADLINE_S = 15.0
DEFAULT_LLM_BUDGET_S = 10.0
TARGET_PICKS = 3
YT_FETCH_MIN_REMAINING_S = 5.0     # on-demand YouTube fetch only when at least this much time is left
YT_FETCH_CAP_S = 3.0


def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, ""))
        return v if v > 0 else default
    except ValueError:
        return default


def deadline_s() -> float:
    return _env_float(DEADLINE_ENV, DEFAULT_DEADLINE_S)


def llm_budget_s() -> float:
    """The LLM path's share of the deadline, always leaving the ladder at least 2 s."""
    return max(1.0, min(_env_float(LLM_BUDGET_ENV, DEFAULT_LLM_BUDGET_S), deadline_s() - 2.0))


def _norm(s) -> str:
    return " ".join(str(s or "").lower().split())


def _label(row: dict) -> str:
    return f"{row.get('artist', '')} - {row.get('title', '')}".strip(" -")


def _lang(lang_check: Optional[Callable], row: dict) -> tuple:
    """(verdict, why) of the language check; ("ok", "") when there is none or it fails."""
    if lang_check is None:
        return "ok", ""
    try:
        v, why = lang_check(row)
        return v, why
    except Exception:
        return "ok", ""        # a gate bug must not empty the answer


def ladder(*, deadline_at: float, hard_played: Sequence[str], earlier: Sequence[str], playing_id: str,
           library: Sequence[dict], neighbours: Sequence[dict], cur_genre: str, genre_of: Callable[[dict], str],
           family_jump: Callable[[str, str], bool], lang_check: Optional[Callable],
           graph_picks: Callable[[], list], yt_rows: Callable[[float], list], yt_pick: Callable[[list], list],
           reason: str, vetoed: Sequence[str] = (), llm: Optional[Callable[[float], list]] = None, n: int = TARGET_PICKS, clock: Callable[[], float] = time.monotonic,
           set_state: Optional[dict] = None) -> dict:
    """{suggestions, pick_source, fallback_reason}. `library`: [{track_id, artist, title}] in library order;
    `neighbours`: the same shape, best atlas pair first; `earlier`: names heard in earlier sets, newest first;
    graph_picks() / yt_rows(remaining_s) are lazy so a step runs only when reached. `set_state`: the phone's set state
    (played, vetoed, scene_genre, punjabi_profile, language_strict, max_length_s, playing_ids), merged with the args;
    every acceptance decision goes through pick_accept.accept_candidate (G23). Never raises."""
    ss = dict(set_state or {})
    base_state = {
        "playing_ids": [x for x in [playing_id, *(ss.get("playing_ids") or [])] if x],
        "played": [*hard_played, *(ss.get("played") or [])],
        "vetoed": [x for x in [*vetoed, *(ss.get("vetoed") or [])] if x],     # track ids, song keys or display names
    }
    for k in ("max_length_s", "punjabi_profile", "language_strict", "cur_era"):
        if ss.get(k) is not None:
            base_state[k] = ss[k]
    heard_earlier = {_norm(x) for x in earlier}
    scene_hooks = PickHooks(lambda scene, genre, profile: bool(genre) and bool(family_jump(scene, genre)),
                            lambda a, b: None)

    def accept(r: dict, relax: Sequence[str] = (), scene_genre: Optional[str] = None) -> bool:
        verdict, why = _lang(lang_check, r)
        cand = {"ids": [x for x in (r.get("track_id"), r.get("graph_key"), _label(r), r.get("title"), _pool_key(r)) if x],
                "lang_verdict": verdict, "lang_reason": why}
        if r.get("not_music") is True:
            cand["not_music"] = True
        if r.get("duration_s") is not None:
            cand["duration_s"] = r["duration_s"]
        state = dict(base_state, relax=list(relax))
        if scene_genre:
            state["scene_genre"] = scene_genre
            cand["genre"] = str(genre_of(r) or "")
        return accept_candidate(cand, state, hooks=scene_hooks)[0]

    def hard_ok(r: dict, allow_penalty: bool = False) -> bool:
        return accept(r, ("language_penalty", "scene") if allow_penalty else ("scene",))

    def fresh(r: dict) -> bool:
        return _norm(_label(r)) not in heard_earlier

    def mk(r: dict, source: str, why: str) -> dict:
        s = {k: r[k] for k in ("artist", "title", "track_id", "yt_id", "genre", "era") if r.get(k)}
        s.update(reason=why, source=source, pick_source=source, fallback_reason=reason,
                 pool_source=r.get("pool_source") or ("library" if r.get("track_id") else "yt"))
        return s

    def take(rows: Sequence[dict], source: str, why: str, *, need_fresh=False, scene=False,
             allow_penalty=False) -> list:
        out, seen = [], set()
        for r in rows:
            if not isinstance(r, dict) or not r.get("title"):
                continue
            relax = ("language_penalty",) if allow_penalty else ()
            if not accept(r, relax, cur_genre if scene and cur_genre else None):
                continue
            key = _norm(_label(r))
            if key in seen:
                continue
            if need_fresh and not fresh(r):
                continue
            seen.add(key)
            out.append(mk(r, source, why))
            if len(out) >= n:
                break
        return out

    def done(picks: list, source: str) -> dict:
        return {"suggestions": prefer_library(picks), "pick_source": source, "fallback_reason": reason}

    def collect(fn, source):
        try:
            return [dict(p, pick_source=p.get("pick_source") or source, fallback_reason=reason)
                    for p in fn() if isinstance(p, dict) and hard_ok(p)]
        except Exception as exc:        # a failing step falls through to the next
            print(f"[suggest] ladder step {source} failed: {exc!r}", flush=True)
            return []

    # Owner order 2026-11-10: graph, then YouTube related, then the LLM (only when those gave fewer than n: a top-up,
    # if time remains), then the library steps. The first non-empty combination is the answer.
    picks: list = []
    keys: set = set()

    def add(rows):
        for r in rows:
            k = _norm(_label(r))
            if k not in keys and len(picks) < n:
                keys.add(k)
                picks.append(r)

    add(collect(lambda: [dict(p, source="graph") for p in graph_picks()], "graph"))
    if len(picks) < n and clock() < deadline_at:
        add(collect(lambda: yt_pick(yt_rows(max(0.0, deadline_at - clock()))), "yt_fallback"))
    if len(picks) < n and llm is not None and deadline_at - clock() >= 1.0:
        add(collect(lambda: llm(max(0.0, deadline_at - clock())), "llm"))
    if picks:
        return done(picks, picks[0]["pick_source"])

    library_steps = [
        ("library_scene", lambda: take(neighbours, "library_scene", "In-scene library song (atlas neighbour)",
                                       need_fresh=True, scene=True)
                          or take(library, "library_scene", "In-scene library song", need_fresh=True, scene=True)),
        ("library_language", lambda: take(library, "library_language", "Library song in the set language",
                                          need_fresh=True)),
        ("library_lru", lambda: _lru(library, earlier, take)),
        ("library_any", lambda: take(library, "library_any", "Any library song not played this set",
                                     allow_penalty=True)),
    ]
    for name, fn in library_steps:
        try:
            got = fn()
        except Exception as exc:
            print(f"[suggest] ladder step {name} failed: {exc!r}", flush=True)
            continue
        if got:
            return done(got, name)
    return {"suggestions": [], "pick_source": "none", "fallback_reason": f"{reason}; no song available"[:300]}


def prefer_library(picks: Sequence[dict]) -> list:
    """HF7: picks with audio on the server (a track_id) first, the rest keep their order (stable), and every pick says
    whether its audio is on the server (`on_server`), so the phone fetches the file instead of waiting for a download."""
    out = sorted((dict(p) for p in picks), key=lambda p: 0 if p.get("track_id") else 1)
    for p in out:
        p["on_server"] = bool(p.get("track_id"))
    return out


def _pool_key(r: dict) -> str:
    try:
        from app.ui.services import pick_pool

        return pick_pool.key_of(r.get("artist", ""), r.get("title", ""))
    except Exception:
        return ""


def _lru(library: Sequence[dict], earlier: Sequence[str], take: Callable) -> list:
    """Library songs ordered least recently played first: never-heard songs, then earlier-set songs oldest first."""
    heard = {_norm(x): i for i, x in enumerate(earlier)}        # 0 = newest
    def age(r):
        return -heard.get(_norm(_label(r)), -1)                 # unheard (-1) sorts first, then oldest (largest index)
    return take(sorted(library, key=age), "library_lru", "Least recently played library song")
