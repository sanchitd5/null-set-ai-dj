"""From what the headless console did to the run the scorer judges.

The console (real scripts) leaves four kinds of evidence, all on the virtual clock:
  * session events  the console's own `track` events: transition_start / transition_end with the
                    recipe it PLANNED and the move it EXECUTED (autopilot.js executedMove)
  * console lines   its own explanations ("transition recipe: Long Blend -> Echo Out (keys clash..)",
                    "stem bridge a->b refused: ...", "Autopilot vibe reject: ...")
  * events          ai-activity / ai-cue / ai-supermove, the DJ mind's decisions and stem moves
  * the audible log what the recording audio graph would have played (js/graph.js): dead air,
                    bass overlap, vocal clash, tempo clash, per transition
plus the API traffic and the server side steps. This module turns them into the run dict scorer.py
takes and the feature table (features.py) reports. It reads evidence; it decides nothing.
"""
from __future__ import annotations

import json
import re
from typing import Optional

_REJECT = re.compile(r"Autopilot (vibe|energy|plan-fit) reject: (.*)")
_KEYRW = re.compile(r"transition recipe: (.*?) -> (.*?) \(keys clash, camelot ([\d.]+)\)")
_LEARNED = re.compile(r"transition recipe \(learned\): (.*)")
_REFUSED = re.compile(r"(stem bridge|stem blend|merge|mashup) .*?refused: (.*)", re.I)
_SKIPPED = re.compile(r"stem intro (\w) skipped: (.*)")
_EMPTY = re.compile(r"model returned 0 picks")
_RETRY = re.compile(r"Retrying with new suggestions \((\d)/3\)")
_NOPICK = re.compile(r"No next song yet after")


def _key_of(a: dict) -> str:
    return ((a or {}).get("key") or {}).get("camelot") or ""


def build_run(js: dict, world, meta: dict) -> dict:
    """js: the harness output; world: the sim world (its events, steps, pool entries)."""
    from app.music_brain.analysis import energy as en
    from app.music_brain.matching import techniques as tq
    from app.music_brain.matching.recipe_matcher import camelot_distance_score

    ev = [e for e in js["session_events"] if e["kind"] == "track" and e.get("data")]
    starts = [e for e in ev if e["data"].get("event") == "transition_start"]
    ends = [e for e in ev if e["data"].get("event") == "transition_end"]
    windows = {w["i"]: w for w in js["audible"]["transitions"]}
    cons = js["console"]
    status = js.get("status_log") or []
    by_name = {}
    for tid, entry in world._tracks.items():
        by_name.setdefault(entry.get("name"), entry)
    # the console's display names come from the server's registry (download file stem)
    from app.ui import server

    for tid, nm in server._track_names.items():
        if tid in world._tracks:
            by_name[nm] = world._tracks[tid]
    raws = en.library_raws()

    def song_info(name: str) -> dict:
        e = by_name.get(name) or {}
        a = e.get("analysis") or {}
        lvl = None
        if e.get("energy"):
            try:
                lvl = int(en.level_from_raw(en._raw(e["energy"]), raws))
            except Exception:
                lvl = None
        return {"bpm": float(a.get("bpm") or 0), "key": _key_of(a), "level": lvl, "duration": float(a.get("duration") or 0),
                "stems": bool(e.get("stems"))}

    names = []
    if starts:
        names.append(starts[0]["data"]["from"])
        names += [s["data"]["to"] for s in starts]
    play_start = js.get("play_start_t") or 0.0
    songs, t_prev = [], play_start
    for i, nm in enumerate(names):
        t_end = starts[i]["t"] if i < len(starts) else None
        info = song_info(nm)
        songs.append({"i": i, "id": nm, "name": nm, **info, "entry_pos": 0.0, "t_entry": round(t_prev, 2),
                      "seconds": round(t_end - t_prev, 2) if t_end is not None else None})
        if t_end is not None:
            t_prev = t_end
    transitions, prev_end = [], 0.0
    steps = world.steps
    for i, s in enumerate(starts):
        d, w = s["data"], windows.get(i, {})
        a, b = song_info(d["from"]), song_info(d["to"])
        lines = [c["text"] for c in cons if prev_end - 0.001 <= c["t"] <= s["t"] + 0.5]
        key_console = tq.camelot_score(a["key"], b["key"]) if a["key"] and b["key"] else None
        try:
            key_kb = camelot_distance_score(a["key"], b["key"])[0] if a["key"] and b["key"] else None
        except ValueError:
            key_kb = None
        rw = next((_KEYRW.search(x) for x in lines if _KEYRW.search(x)), None)
        learned = next((_LEARNED.search(x).group(1) for x in lines if _LEARNED.search(x)), None)
        refused = [x for x in lines if _REFUSED.search(x)]
        skipped = [x for x in lines if _SKIPPED.search(x)]
        executed = d.get("recipe")
        planned = d.get("planned") or executed
        gap = min(abs(a["bpm"] / (b["bpm"] * m) - 1) for m in (1, 2, 0.5)) if a["bpm"] and b["bpm"] else 0.0
        inp = d.get("in") or "b"
        pct = (w.get("rate_max_pct") or {}).get(inp, 0.0)
        mind = [st for st in steps if st.get("kind") == "mind_plan" and prev_end - 0.001 <= st["t"] - 1_790_000_000.0 <= s["t"] + 0.5]
        parsed = any(((st.get("result") or {}).get("parsed") is True) for st in mind)
        merge = _merge_facts(lines, executed)
        prep = _prepare_facts(lines)
        transitions.append({
            **merge, **prep,
            "i": i + 1, "from": d["from"], "to": d["to"], "from_id": d["from"], "to_id": d["to"],
            "from_bpm": round(a["bpm"], 2), "to_bpm": round(b["bpm"], 2), "from_key": a["key"], "to_key": b["key"],
            "key_score": key_console, "key_kb": key_kb,
            "beat_locked": gap <= 0.08, "tempo_pct": round(pct, 3), "tempo_jump": gap > 0.08,
            "recipe_matcher": None, "recipe_planned": planned, "recipe_executed": executed,
            "path": _path_of(executed, refused), "kind": _kind_of(executed),
            "refused": refused[0] if refused else ("; ".join(skipped) if skipped else ""),
            "degraded": bool(refused or skipped), "dead_air_s": w.get("dead_air_s", 0.0), "min_db": w.get("min_db", 0.0),
            "intro": None, "intro_rms": None, "silent_intro": (w.get("in_silent_s", 0.0) or 0) >= 6.0 or bool(skipped),
            "unlocked_overlap_s": w.get("unlocked_overlap_s", 0.0), "bass_overlap_s": w.get("bass_overlap_s", 0.0),
            "vocal_clash_s": w.get("vocal_clash_s", 0.0), "seconds": d.get("seconds"), "fire_t": s["t"],
            "learned": learned, "key_rewrite": ({"from": rw.group(1), "to": rw.group(2)} if rw else None),
            "blend_dropped": any("Blend plan unavailable" in x for x in lines), "layer": str(executed or "").startswith("LAYER"),
            "one_song": False, "vocal_rule": False, "plan_parsed": parsed, "plan_asked": bool(mind),
            "energy_a": a["level"], "energy_b": b["level"], "window": None,
            "start_pos": w.get("start"),
        })
        prev_end = (ends[i]["t"] if i < len(ends) else s["t"])
    net = js["net"]
    counters = {
        "llm_suggest": sum(1 for n in net if n["path"].startswith("/api/autopilot/suggest")),
        "downloads": sum(1 for n in net if n["method"] == "POST" and n["path"].startswith("/api/download")),
        "download_failures": sum(1 for n in net if n["path"].startswith("/api/download") and n.get("status", 0) >= 400),
        "picks_empty": sum(1 for c in cons if _EMPTY.search(c["text"])),
        "stalls": sum(1 for st in status if _NOPICK.search(st["text"])),
        "plans_asked": sum(1 for n in net if n["path"].startswith("/api/autopilot/plan")),
        "http_errors": sum(1 for n in net if n.get("status", 200) >= 400 or n.get("status") == 0),
        "candidates": 0,
    }
    rejects = []
    for c in cons:
        m = _REJECT.search(c["text"])
        if m:
            rejects.append({"t": c["t"], "gate": {"vibe": "reject_vibe", "energy": "reject_energy", "plan-fit": "reject_planfit"}[m.group(1)], "why": m.group(2)[:200]})
    preps = [{"rounds": 1 + max([int(_RETRY.search(st["text"]).group(1)) - 1 for st in status
                               if _RETRY.search(st["text"]) and (transitions[k - 1]["fire_t"] if k else 0) < st["t"] <= t["fire_t"]] or [0]),
              "song_index": k + 1} for k, t in enumerate(transitions)]
    counters["candidates"] = len(rejects) + len(transitions)
    played = {(by_name.get(s["name"]) or {}).get("hash") for s in songs} - {None}
    return {"meta": {**meta, "tracks_played": len(songs), "stalled": js["ended"] != "songs", "ended": js["ended"],
                     "llm": llm_summary(world.llm_calls), "llm_endpoint": world.fx.get("llm_endpoint"),
                     "wf": wf_summary(world.events),
                     "replay_misses": len(world.misses), "replay_drift": len(world.drift),
                     # threads ask in any order: the list is sorted so a run's outputs are byte-stable
                     "misses": sorted(world.misses, key=lambda m: (m["what"], m["key"]))[:20]},
            "prerender": world.prerender_report(played),
            "songs": songs, "transitions": transitions, "preps": preps, "rejects": rejects, "counters": counters,
            "atlas": atlas_facts(cons, transitions),
            "audible": {"set": js["audible"].get("set")}}


_ATLAS_PLAN = re.compile(r"^atlas: plan (.+?) exit ")
_COMBO_WORKS = re.compile(r"works (\d+)")
_STREAK = re.compile(r"COMBO x(\d+)")


def atlas_facts(cons: list, transitions: list) -> dict:
    """Pair atlas / macro use, from macro-mode.js's own console lines (informational):
    `combo: picked|skipped: ...`, `macro: preferred|skipped ...`, `atlas: plan <recipe> ...`,
    `combo: COMBO x<n>: ...` (a combo landed)."""
    txt = [(c["t"], c["text"]) for c in cons]
    plans = [(t, m.group(1)) for t, x in txt for m in [_ATLAS_PLAN.search(x)] if m]
    kept = 0
    for tr in transitions:
        before = [r for t, r in plans if t <= tr["fire_t"]]
        if before and before[-1] == (tr.get("executed") or tr.get("recipe")):
            kept += 1
    picked = [x for _, x in txt if x.startswith("combo: picked")]
    works = [int(m.group(1)) for x in picked for m in [_COMBO_WORKS.search(x)] if m]
    streaks = [int(m.group(1)) for _, x in txt if x.startswith("combo: COMBO") for m in [_STREAK.search(x)] if m]
    return {"plans": len(plans), "plans_kept": kept, "combo_picked": len(picked),
            "combo_skipped": sum(1 for _, x in txt if x.startswith("combo: skipped")),
            "combo_landed": len(streaks), "max_streak": max(streaks, default=0),
            "combo_works_mean": round(sum(works) / len(works), 1) if works else 0.0,
            "macro_preferred": sum(1 for _, x in txt if x.startswith("macro: preferred") or x.startswith("macro: MACRO MODE")),
            "macro_explore": sum(1 for _, x in txt if x.startswith("macro: skipped (") and "explore" in x),
            "macro_invalid": sum(1 for _, x in txt if x.startswith("macro: skipped (invalid"))}


_PREPARE = re.compile(r"prepare ready: .*? at_booking=(\d) defer_s=(\d+) stems=(\d) tempo=(\d) gave_up=(\d) skip=(\d)")


def _prepare_facts(lines: list) -> dict:
    """Pre-render evidence for one transition (informational): read from the console's own line
    `prepare ready: <song> at_booking=1|0 defer_s=N stems=.. tempo=.. gave_up=.. skip=..` written by
    autopilot.js awaitBReady for the candidate that was booked (the last such line before the fire).
    at_booking: B's stems (and key-locked tempo stems when the gap needs them) were on the deck when the
    booking first looked; skip: no merge was possible anyway (A stemless, keys clash, gap over the cap)."""
    got = [m for m in (_PREPARE.search(x) for x in lines) if m]
    if not got:
        return {"prep_seen": False, "prep_at_booking": False, "prep_defer_s": 0.0, "prep_gave_up": False, "prep_skip": False}
    m = got[-1]
    return {"prep_seen": True, "prep_at_booking": m.group(1) == "1", "prep_defer_s": float(m.group(2)),
            "prep_gave_up": m.group(5) == "1", "prep_skip": m.group(6) == "1"}


_MERGE_GATE = re.compile(r"merge gate: (\w+): (.*?)(; classic merge)?$")
_MERGE_PHASES = re.compile(r"merge phases: (\{.*\})")


def _merge_facts(lines: list, executed: Optional[str]) -> dict:
    """MERGE -> HOLD -> TRANSITION evidence for one transition (informational, never scored).

    Read from the console's own lines (the sim has no browser step log): `merge gate: <gate>: <why>`
    (autopilot.js mergeGateLog: the gate that refused the hold plan, `; classic merge` when the fixed
    16 / 32 bar merge ran instead) from booking on, and `merge phases: {json}` when a measured
    merge -> hold -> handover fires. outcome: hold = a measured hold ran, classic = the fixed merge
    ran, refused = a gate stopped every merge, n/a = the transition never tried one."""
    gates = [m for m in (_MERGE_GATE.search(x) for x in lines) if m]
    phases = next((json.loads(m.group(1)) for m in (_MERGE_PHASES.search(x) for x in lines) if m), None)
    last = gates[-1] if gates else None
    if phases:
        outcome = "hold"
    elif executed == "Stem Merge" and last is not None and last.group(3):
        outcome = "classic"
    elif last is not None:
        outcome = "refused"
    else:
        outcome = "n/a"
    hold = (phases or {}).get("hold") or {}
    return {"merge_outcome": outcome, "merge_gate": last.group(1) if last and outcome != "hold" else "",
            "hold_s": float(hold.get("seconds") or 0.0), "hold_bars": int(hold.get("bars") or 0)}


def wf_summary(events: list) -> dict:
    """What the layered moves took from the waveform in this run: the server logs one `derived_params` event
    per plan (app.music_brain.analysis.waveform_params.note). Counts parameters measured versus fallen back to the old
    constant, and the vocal-gap fit of the riff's rap moves (share of the rap actually voiced under the bar the
    move picked, against the old fixed bar)."""
    ev = [e for e in events if e.get("kind") == "derived_params"]
    by_move: dict = {}
    fit: dict = {"hold": [], "hold_fixed": [], "dropout": [], "dropout_fixed": []}
    for e in ev:
        row = by_move.setdefault(e.get("move", "?"), {"plans": 0, "measured": 0, "fallback": 0})
        row["plans"] += 1
        row["measured"] += int(e.get("measured") or 0)
        row["fallback"] += int(e.get("fallback") or 0)
        f = e.get("fit") or {}
        for k, src in (("hold", "hold_active"), ("hold_fixed", "hold_active_fixed")):
            fit[k] += [v for v in (f.get(src) or []) if v is not None]
        for k, src in (("dropout", "dropout_active"), ("dropout_fixed", "dropout_active_fixed")):
            if f.get(src) is not None:
                fit[k].append(f[src])
    mean = lambda xs: round(sum(xs) / len(xs), 3) if xs else None  # noqa: E731
    return {"plans": len(ev), "measured": sum(r["measured"] for r in by_move.values()),
            "fallback": sum(r["fallback"] for r in by_move.values()), "by_move": dict(sorted(by_move.items())),
            "hold_fit": mean(fit["hold"]), "hold_fit_fixed": mean(fit["hold_fixed"]),
            "dropout_fit": mean(fit["dropout"]), "dropout_fit_fixed": mean(fit["dropout_fixed"])}


def llm_summary(calls: list) -> dict:
    """What the model did in this run: calls by kind, replies that were empty (no picks / "{}") or not JSON at
    all, and the mean latency of the calls that have one (a recording keeps it; the stub has none)."""
    task = [c for c in calls if c["kind"] in ("suggest", "lookahead", "plan")]
    lat = [c["latency_s"] for c in calls if c.get("latency_s") is not None]
    by_kind: dict = {}
    for c in calls:
        by_kind[c["kind"]] = by_kind.get(c["kind"], 0) + 1
    return {"calls": len(calls), "by_kind": dict(sorted(by_kind.items())),
            "empty": sum(1 for c in task if c["quality"] == "empty"), "invalid": sum(1 for c in task if c["quality"] == "invalid"),
            "latency_mean_s": round(sum(lat) / len(lat), 2) if lat else None}


def _kind_of(recipe: Optional[str]) -> str:
    r = str(recipe or "").lower()
    for key, kind in (("double drop", "double"), ("bass swap", "bass"), ("drop swap", "bass"), ("echo", "echo"), ("filter", "filter"),
                      ("cut", "bass"), ("loop", "loop"), ("blend", "blend")):
        if key in r:
            return kind
    if r.startswith("layer"):
        return "layer"
    return "default"


def _path_of(executed: Optional[str], refused: list) -> str:
    r = str(executed or "")
    if r == "Stem Bridge":
        return "stem_bridge"
    if r.startswith("LAYER"):
        return "layer"
    if r in ("EQ blend",) or refused:
        return "eq"
    if r in ("Stem Merge", "Mashup → Transition", "RIFF OVER RAP"):
        return "stem_blend"
    return "stem_blend"
