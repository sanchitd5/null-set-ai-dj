"""The feedback signal: one deterministic number plus the metrics behind it.

`score_run(run)` takes the run dict a virtual set produces (virtual_set.Sim.run_data) and
returns {score, metrics, penalties, breakdown, worst, ...}. No clock, no randomness, no
dict-order dependence: the same run always scores the same.

HEADLINE `score`: LOWER IS BETTER, 0 is a set with nothing to complain about. It is the sum
of penalty points divided by the number of transitions, so 10-song and 14-song sets compare.

Key vocabulary. Two Camelot tables exist in the code base: the KB one (recipe_matcher
camelot_distance_score: 1.0 / 0.9 / 0.85 / 0.8 / diagonal 0.75 / -2h 0.6 / distant 0.3, and 0
for 3+ hours apart = a CLASH, CLAUDE.md section 4) and the console's (dj-mind.js camelotScore,
0 for everything not in the KB's first four rows). The scorer judges by the KB table
(`key_kb`); the console's number (`key_score`) is what its decisions used and is reported as
`key_clash_rate` (score < 0.8, the literal task metric).

Penalty weights (points), per event unless noted. They rank what a listener notices first:

  key_clash_blend      12    a tonal blend (Long Blend / Bass Swap / stem blend / layer) across a
                             KB clash: two harmonic records for 16-32 bars
  key_hard_clash        4    a KB clash (3+ hours) between two consecutive songs at all (the
                             selection let it through; Echo Out / Stem Bridge only limit the damage)
  key_weak_blend        1    a tonal blend across a weak key move (KB score 0.3-0.75)
  key_weak              0.5  a weak key move (KB score above 0 and below 0.8)
  key_false_rewrite     2    the console rewrote a blend to Echo Out for keys the KB does not
                             call a clash (a blend lost for nothing)
  tempo_over_cap        8    B stretched past the 8 % cap (should be impossible: the rule gate)
  tempo_stretch         0.4  per percent of stretch on every beat-locked transition (smearing)
  tempo_jump            2    a transition across a tempo gap the pitch fader cannot lock
  unlocked_overlap      0.5  per second two beats overlapped at tempos that never lock
  dead_air              1.5  per second the estimated master is near silent (moves.py)
  silent_intro          3    the intro stem picked for B does not really play there
  plan_mismatch         3    booked recipe != executed move (stem move refused, EQ path ran)
  degraded              1.5  a stem move was refused and fell back to the EQ path
  energy_fall           2    per level a fall exceeds 3 (energy arc max fall, set level)
  energy_falls_ge3      3    per fall of 3 or more levels (set level)
  same_artist           4    a song shares an artist with one of the previous 2 songs
  artist_run            2    per song beyond 2 in a row by one artist
  repeat_song          10    the same song twice
  short_song            2    a song under 90 s (set level; the last song is unfinished, not counted)
  long_song             1    per 60 s a song runs over 420 s
  empty_pick            2    a suggestion round the model answered with nothing usable
  stall                 6    a whole search that booked nothing (the set would have looped)
  no_plan               0.5  a transition without a parsed AI plan (rules only)
  discarded_plan        1    a blend / layer plan the rules threw away (key rewrite, beatless)

The `worst` list is the five transitions with the highest per-transition penalty, each with its
reasons. `breakdown` sums every category over the set: the top entries are the failures to fix.
"""
from __future__ import annotations

from collections import Counter
from typing import Callable, Optional

WEIGHTS = {
    "key_clash_blend": 12.0, "key_hard_clash": 4.0, "key_weak_blend": 1.0, "key_weak": 0.5, "key_false_rewrite": 2.0,
    "tempo_over_cap": 8.0, "tempo_stretch": 0.4, "tempo_jump": 2.0, "unlocked_overlap": 0.5, "dead_air": 1.5,
    "silent_intro": 3.0, "plan_mismatch": 3.0, "degraded": 1.5, "energy_fall": 2.0, "energy_falls_ge3": 3.0,
    "same_artist": 4.0, "artist_run": 2.0, "repeat_song": 10.0, "short_song": 2.0, "long_song": 1.0,
    "empty_pick": 2.0, "stall": 6.0, "no_plan": 0.5, "discarded_plan": 1.0,
}
KEY_SAFE_MIN = 0.8
TEMPO_CAP_PCT = 8.0
MIN_SONG_S, MAX_SONG_S = 90.0, 420.0
TONAL_KINDS = {"blend", "bass", "default", "filter", "loop", "double", "layer"}
NON_TONAL_MOVES = ("Echo Out", "Stem Bridge", "Backspin", "Breakdown", "EQ blend")

# direction of "better" per metric for compare.py: -1 lower is better, +1 higher is better, 0 informational
DIRECTION = {
    "key_clash_rate": -1, "key_clash_count": -1, "key_hard_clash_count": -1, "key_clash_blends": -1, "key_weak_blends": -1,
    "key_false_rewrites": -1, "key_zero_count": -1,
    "tempo_stretch_max_pct": -1, "tempo_stretch_mean_pct": -1, "tempo_over_cap": -1, "tempo_jumps": -1,
    "dead_air_seconds": -1, "dead_air_transitions": -1, "unlocked_overlap_seconds": -1, "silent_intros": -1,
    "energy_max_fall": -1, "energy_falls_ge3": -1, "energy_peak": 0, "energy_final": 0,
    "same_artist_repeats": -1, "max_artist_run": -1, "repeat_songs": -1, "short_songs": -1, "song_min_s": +1, "song_max_s": 0,
    "song_mean_s": 0, "long_blend_share": 0, "echo_out_share": -1, "plan_exec_mismatch": -1, "degraded_moves": -1,
    "discarded_plans": -1, "no_plan": -1, "empty_picks": -1, "stalls": -1, "search_rounds_extra": -1, "transitions": +1,
    "songs_played": +1, "replay_misses": -1, "replay_drift": 0,
    "llm_calls": 0, "llm_empty_replies": -1, "llm_invalid_replies": -1, "llm_latency_mean_s": 0, "http_errors": -1, "download_failures": -1, "score": -1,
    "recipe_variety": 0, "distinct_recipes": 0, "max_recipe_repeat_run": 0, "fx_budget_spent": 0, "fx_budget_refused": 0,
    "fx_density_per_30min": 0, "exits_checked": 0, "exits_in_breakdown": -1, "overlap_seconds": 0,
}
SET_LEVEL_KEYS = ("recipe_variety", "distinct_recipes", "max_recipe_repeat_run", "fx_budget_spent", "fx_budget_refused",
                  "fx_budget_by_kind", "fx_density_per_30min", "exits_checked", "exits_in_breakdown", "overlap_seconds")


def _real_artists_of(name: str) -> set:
    from app.ui.autopilot_service import _artists_of

    return _artists_of(name)


def _identity(name: str) -> str:
    from app.ui.autopilot_service import _bare_title

    return _bare_title(name.split(" - ", 1)[-1])


def _kb(t: dict):
    """KB Camelot score of the pair (recorded by the driver); the console's when absent."""
    return t["key_kb"] if t.get("key_kb") is not None else t.get("key_score")


def _tonal(t: dict) -> bool:
    return t.get("path") in ("stem_blend", "eq", "layer") and t.get("kind") in TONAL_KINDS \
        and t.get("recipe_executed") not in NON_TONAL_MOVES


def transition_penalty(t: dict) -> tuple:
    """(points, [reasons], {category: points}) of one transition row."""
    parts: dict = {}
    why: list = []

    def add(cat: str, pts: float, reason: str = "") -> None:
        parts[cat] = parts.get(cat, 0.0) + pts
        if reason:
            why.append(reason)

    kb = _kb(t)
    hard = kb is not None and kb == 0
    weak = kb is not None and 0 < kb < KEY_SAFE_MIN
    if hard:
        add("key_hard_clash", WEIGHTS["key_hard_clash"], f"key clash {t.get('from_key')}->{t.get('to_key')} (3+ hours apart)")
        if _tonal(t):
            add("key_clash_blend", WEIGHTS["key_clash_blend"], f"tonal {t.get('recipe_executed')} across the clash")
    elif weak:
        add("key_weak", WEIGHTS["key_weak"])
        if _tonal(t):
            add("key_weak_blend", WEIGHTS["key_weak_blend"], f"tonal {t.get('recipe_executed')} across weak keys {t.get('from_key')}->{t.get('to_key')} ({kb})")
    if t.get("key_rewrite") and kb is not None and kb > 0:
        add("key_false_rewrite", WEIGHTS["key_false_rewrite"],
            f"{t['key_rewrite']['from']} -> {t['key_rewrite']['to']} for keys the KB does not call a clash ({t.get('from_key')}->{t.get('to_key')}, KB {kb})")
    pct = float(t.get("tempo_pct") or 0.0)
    if t.get("beat_locked") and pct > 0:
        add("tempo_stretch", WEIGHTS["tempo_stretch"] * pct)
        if pct > TEMPO_CAP_PCT + 1e-6:
            add("tempo_over_cap", WEIGHTS["tempo_over_cap"], f"stretch {pct:.1f}% over the {TEMPO_CAP_PCT:.0f}% cap")
    if t.get("tempo_jump"):
        add("tempo_jump", WEIGHTS["tempo_jump"], f"tempo jump {t.get('from_bpm')}->{t.get('to_bpm')} BPM")
    uo = float(t.get("unlocked_overlap_s") or 0.0)
    if uo:
        add("unlocked_overlap", WEIGHTS["unlocked_overlap"] * uo, f"{uo:.0f}s of beats overlapping at tempos that never lock")
    da = float(t.get("dead_air_s") or 0.0)
    if da:
        add("dead_air", WEIGHTS["dead_air"] * da, f"{da:.1f}s estimated dead air")
    if t.get("silent_intro"):
        add("silent_intro", WEIGHTS["silent_intro"], f"silent intro stem ({t.get('intro')})")
    if t.get("recipe_planned") != t.get("recipe_executed"):
        add("plan_mismatch", WEIGHTS["plan_mismatch"], f"planned {t.get('recipe_planned')} but executed {t.get('recipe_executed')}")
    if t.get("degraded"):
        add("degraded", WEIGHTS["degraded"], str(t.get("refused") or "stem move refused"))
    if not t.get("plan_parsed"):
        add("no_plan", WEIGHTS["no_plan"])
    if t.get("blend_dropped") or t.get("key_rewrite"):
        add("discarded_plan", WEIGHTS["discarded_plan"])
    return sum(parts.values()), why, parts


def score_run(run: dict, artists_of: Optional[Callable[[str], set]] = None, identity: Optional[Callable[[str], str]] = None) -> dict:
    artists_of = artists_of or _real_artists_of
    identity = identity or _identity
    songs, trans, counters = run["songs"], run["transitions"], run["counters"]
    n_t = len(trans)
    rows, breakdown = [], Counter()
    for t in trans:
        p, why, parts = transition_penalty(t)
        rows.append((p, t["i"], t["from"], t["to"], why))
        breakdown.update(parts)

    ks = [t["key_score"] for t in trans if t.get("key_score") is not None]
    kbs = [_kb(t) for t in trans if _kb(t) is not None]
    clash = [t for t in trans if t.get("key_score") is not None and t["key_score"] < KEY_SAFE_MIN]
    hard = [t for t in trans if _kb(t) is not None and _kb(t) == 0]
    weak_blend = [t for t in trans if _kb(t) is not None and 0 < _kb(t) < KEY_SAFE_MIN and _tonal(t)]
    clash_blend = [t for t in hard if _tonal(t)]
    false_rw = [t for t in trans if t.get("key_rewrite") and _kb(t) is not None and _kb(t) > 0]
    pcts = [float(t["tempo_pct"]) for t in trans if t.get("beat_locked")]
    levels = [s["level"] for s in songs if s.get("level") is not None]
    drops = [a - b for a, b in zip(levels, levels[1:]) if a > b]
    secs = [s["seconds"] for s in songs if s.get("seconds") is not None]
    arts = [artists_of(s["name"]) for s in songs]
    same_artist = sum(1 for i in range(len(arts)) if any(arts[i] & arts[j] for j in range(max(0, i - 2), i)))
    max_run = run_len = 1 if arts else 0
    for i in range(1, len(arts)):
        run_len = run_len + 1 if arts[i] & arts[i - 1] else 1
        max_run = max(max_run, run_len)
    ids = [identity(s["name"]) for s in songs]
    repeats = len(ids) - len(set(ids))
    recipes = Counter(t["recipe_executed"] for t in trans)
    extra_rounds = sum(max(0, p.get("rounds", 1) - 1) for p in run.get("preps", []))

    m = {
        "songs_played": len(songs), "transitions": n_t,
        "key_clash_count": len(clash), "key_clash_rate": round(len(clash) / len(ks), 3) if ks else 0.0,
        "key_hard_clash_count": len(hard), "key_clash_blends": len(clash_blend), "key_weak_blends": len(weak_blend),
        "key_false_rewrites": len(false_rw), "key_zero_count": sum(1 for k in ks if k == 0),
        "tempo_stretch_max_pct": round(max(pcts), 2) if pcts else 0.0,
        "tempo_stretch_mean_pct": round(sum(pcts) / len(pcts), 2) if pcts else 0.0,
        "tempo_over_cap": sum(1 for x in pcts if x > TEMPO_CAP_PCT + 1e-6),
        "tempo_jumps": sum(1 for t in trans if t.get("tempo_jump")),
        "dead_air_seconds": round(sum(float(t.get("dead_air_s") or 0) for t in trans), 2),
        "dead_air_transitions": sum(1 for t in trans if (t.get("dead_air_s") or 0) > 0),
        "unlocked_overlap_seconds": round(sum(float(t.get("unlocked_overlap_s") or 0) for t in trans), 2),
        "silent_intros": sum(1 for t in trans if t.get("silent_intro")),
        "energy_max_fall": max(drops) if drops else 0, "energy_falls_ge3": sum(1 for d in drops if d >= 3),
        "energy_peak": max(levels) if levels else None, "energy_final": levels[-1] if levels else None,
        "same_artist_repeats": same_artist, "max_artist_run": max_run, "repeat_songs": repeats,
        "short_songs": sum(1 for s in secs if s < MIN_SONG_S),
        "song_min_s": round(min(secs), 1) if secs else None, "song_max_s": round(max(secs), 1) if secs else None,
        "song_mean_s": round(sum(secs) / len(secs), 1) if secs else None,
        "recipes": dict(sorted(recipes.items())),
        "long_blend_share": round(recipes.get("Long Blend", 0) / n_t, 3) if n_t else 0.0,
        "echo_out_share": round(recipes.get("Echo Out", 0) / n_t, 3) if n_t else 0.0,
        "plan_exec_mismatch": sum(1 for t in trans if t.get("recipe_planned") != t.get("recipe_executed")),
        "degraded_moves": sum(1 for t in trans if t.get("degraded")),
        "discarded_plans": sum(1 for t in trans if t.get("blend_dropped") or t.get("key_rewrite")),
        "no_plan": sum(1 for t in trans if not t.get("plan_parsed")),
        "empty_picks": counters.get("picks_empty", 0), "stalls": counters.get("stalls", 0),
        "search_rounds_extra": extra_rounds,
        "llm_calls": (run["meta"].get("llm") or {}).get("calls", 0),
        "llm_empty_replies": (run["meta"].get("llm") or {}).get("empty", 0),
        "llm_invalid_replies": (run["meta"].get("llm") or {}).get("invalid", 0),
        "llm_latency_mean_s": (run["meta"].get("llm") or {}).get("latency_mean_s") or 0.0,
        "replay_misses": run["meta"].get("replay_misses", 0), "replay_drift": run["meta"].get("replay_drift", 0), "http_errors": counters.get("http_errors", 0),
        "download_failures": counters.get("download_failures", 0),
    }
    # MERGE -> HOLD -> TRANSITION, informational only (not in the score): how many transitions ran a
    # measured merge + hold, how long the holds were, and which gate refused the rest.
    held = [t for t in trans if t.get("merge_outcome") == "hold"]
    m["merged_play_share"] = round(len(held) / n_t, 3) if n_t else 0.0
    m["classic_merge_share"] = round(sum(1 for t in trans if t.get("merge_outcome") == "classic") / n_t, 3) if n_t else 0.0
    m["hold_seconds_mean"] = round(sum(t["hold_s"] for t in held) / len(held), 1) if held else 0.0
    m["hold_seconds_max"] = round(max((t["hold_s"] for t in held), default=0.0), 1)
    m["merge_refusals"] = dict(sorted(Counter(t["merge_gate"] for t in trans
                                              if t.get("merge_outcome") in ("classic", "refused") and t.get("merge_gate")).items()))
    # PRE-RENDER, informational only: was B's stems / tempo stems ready when the booking first looked (the
    # transitions where a merge was possible at all), how long the booking waited, render seconds spent on songs
    # that never played, and the most heavy jobs (separation + key-locked render) that ran at once.
    seen = [t for t in trans if t.get("prep_seen") and not t.get("prep_skip")]
    m["ready_at_booking_share"] = round(sum(1 for t in seen if t["prep_at_booking"]) / len(seen), 3) if seen else 0.0
    dfr = [t["prep_defer_s"] for t in trans if t.get("prep_defer_s")]
    m["defer_seconds"] = round(sum(dfr), 1)
    m["deferred_transitions"] = len(dfr)
    m["defer_gave_up"] = sum(1 for t in trans if t.get("prep_gave_up"))
    pre = run.get("prerender") or {}
    m["wasted_render_seconds"] = pre.get("wasted_render_seconds", 0.0)
    m["max_concurrent_heavy_jobs"] = pre.get("max_concurrent_heavy_jobs", 0)
    # SET-LEVEL habits (features.set_level_report), informational only: variety, the FX budget, exits
    # inside A's breakdown (S22: should be 0), overlap seconds.
    sl = (run.get("features") or {}).get("set_level") or {}
    for k in SET_LEVEL_KEYS:
        if k in sl:
            m[k] = sl[k]
    long_over = sum(max(0.0, s - MAX_SONG_S) / 60.0 for s in secs)
    pen = {
        "transitions": round(sum(r[0] for r in rows), 3),
        "energy_fall": round(WEIGHTS["energy_fall"] * max(0, m["energy_max_fall"] - 3), 3),
        "energy_falls_ge3": WEIGHTS["energy_falls_ge3"] * m["energy_falls_ge3"],
        "same_artist": WEIGHTS["same_artist"] * same_artist,
        "artist_run": WEIGHTS["artist_run"] * max(0, max_run - 2),
        "repeat_song": WEIGHTS["repeat_song"] * repeats,
        "short_song": WEIGHTS["short_song"] * m["short_songs"],
        "long_song": round(WEIGHTS["long_song"] * long_over, 3),
        "empty_pick": WEIGHTS["empty_pick"] * m["empty_picks"],
        "stall": WEIGHTS["stall"] * m["stalls"],
    }
    total = sum(pen.values())
    score = round(total / max(1, n_t), 3)
    worst = sorted(rows, key=lambda r: (-r[0], r[1]))[:5]
    bd = {k: round(v, 3) for k, v in breakdown.items()}
    for k, v in pen.items():
        if k != "transitions" and v:
            bd[k] = round(v, 3)
    return {
        "version": 2, "score": score, "score_direction": "lower is better", "penalty_total": round(total, 3),
        "penalties": {k: round(v, 3) for k, v in sorted(pen.items())},
        "breakdown": dict(sorted(bd.items(), key=lambda kv: (-kv[1], kv[0]))),
        "metrics": m, "weights": WEIGHTS,
        "worst": [{"transition": r[1], "from": r[2], "to": r[3], "penalty": round(r[0], 2), "reasons": r[4]} for r in worst if r[0] > 0],
        "meta": {k: run["meta"].get(k) for k in ("seed", "mode", "tracks_played", "world", "name", "stalled", "fixture_source", "llm", "llm_endpoint")},
        "songs": [{"name": s["name"], "level": s.get("level"), "seconds": s.get("seconds")} for s in songs],
    }
