"""AI plan for one song pair: the LLM proposes, the rules check.

gemma3:4b is too slow for the per-phrase DJ-mind loop (app/ui/static/dj-mind.js),
so it gets ONE call per song pair, made once the next song is matched. It
picks a match candidate, an exit phrase inside the play window and a few
phrase-level moves (transition moves plus the live REMIX moves). Everything
it returns is validated here against the same caps and cooldowns the DJ mind
enforces; invalid parts are dropped or repaired, the rest is kept. The mind
then re-checks each move against the live deck state before playing it.

Grounding: research/notes/set-study-gfF8jzBVWvM.md section 2 (rules 2, 3, 4,
8, tension/release) and the DJ/ cookbook notes Loops & Beat Jumps, Loop
Transition, Echo Out and Filter Transition.

The constants below mirror dj-mind.js; test_mind_plan.py fails if they drift.
"""
from __future__ import annotations

import logging
import re
import os
import time
from functools import lru_cache
from typing import Any, Optional

from app.ui.autopilot_service import _extract_json, chat_raw

log = logging.getLogger("mind_plan")

# -- shared with dj-mind.js (checked by test_mind_plan.py) --------------------
PHRASE_BARS = 8
MIN_SECTION_BARS = 8
MIN_BARS_ON_TRACK = 16
EXIT_GUARD_BARS = 24
HOLD_BARS = 8
PRECLEAR_SLACK_BARS = 4
SUBDROP_MAX_ENERGY = 0.75
REMIX_MAX_PER_SONG = 4
REMIX_GAP_PHRASES = 2        # remix phrases at least 2 apart: never two in a row
BEAT_LAYER_MIN_SCORE = 65

TRANSITION_MOVES = ("hold", "preclear", "subdrop")
REMIX_MOVES = ("loop_extend", "beat_jump", "stutter", "filter_build", "echo_freeze", "beat_layer")
ALLOWED_MOVES = TRANSITION_MOVES + REMIX_MOVES
DROP_LEADINS = ("stutter", "filter_build", "echo_freeze")  # tension -> release into a drop
MAX_MOVES = 5
PLAN_MAX_TOKENS = 600          # mlx_lm.server's default output length truncates JSON

MOVE_HELP = {
    "hold": "push the exit back one phrase so a build resolves on the old record (only within 12 bars of the exit)",
    "preclear": "pull the outgoing sub down ~12 dB 8-16 bars before the exit (not for instant-style recipes)",
    "subdrop": "kill the sub for one phrase on a vocal verse/breakdown, then slam it back (max once per song)",
    "loop_extend": "loop the best 4 or 8 bars of a groove once more, then release on the phrase line (param bars: 4|8)",
    "beat_jump": "skip a weak/boring phrase: jump 8 or 16 bars forward on the phrase line (param bars: 8|16)",
    "stutter": "loop roll 4 > 2 > 1 > 1/2 beat over the last 2 bars, released into the drop (phrase right before a drop)",
    "filter_build": "sweep lows and mids out over 8 bars, snap back open on the drop downbeat (phrase right before a drop)",
    "echo_freeze": "echo tail on the last beat of the phrase, then cut straight into the drop (phrase right before a drop)",
    "beat_layer": "lay the next song's vocal over this one with its lows cut (only if a mashup is possible and the match is strong)",
}

# -- DJ wiki grounding (./DJ/, extracted once and cached) ---------------------
GROUNDING_MAX_CHARS = 1550
RECIPE_STEP_CHARS = 75

# (move, note stem, heading fragment, line keyword or None, max chars)
_MOVE_SOURCES = (
    ("loop_extend", "Loops & Beat Jumps", "Extending the", "Solution", 105),
    ("stutter", "Loops & Beat Jumps", "Pre-Drop Tension", None, 130),
    ("beat_jump", "Loops & Beat Jumps", "Why Every Pro DJ", "Bypassing", 110),
    ("loops", "Loops & Beat Jumps", "Loop Length vs", "power-of-two", 90),
    ("preclear/subdrop", "EQ & Frequency Management", "Sub/Kick Conflict", "The Law", 110),
    ("filter_build", "EQ & Frequency Management", "Dedicated Filters", "DJ Usage", 100),
)
# (move, recipe, step index): the step that says what the move does on the drop
_REMIX_RECIPES = (("echo_freeze", "Echo Out", 3), ("filter_build", "Filter Transition", 3))


def _one_line(text: str, n: int) -> str:
    text = re.sub(r"\\text\{([^}]*)\}", r"\1", text).replace("-DJ Usage:-", "DJ usage:")
    text = " ".join(ln.lstrip("- ").strip() for ln in text.splitlines())
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


@lru_cache(maxsize=1)
def _knowledge():
    from app.music_brain.knowledge_parser import KnowledgeParser
    return KnowledgeParser()


@lru_cache(maxsize=None)
def _recipe_lines(name: str, n_steps: int = 3) -> tuple:
    try:
        r = _knowledge().get(name)
    except Exception as exc:  # grounding is best-effort; the plan still runs without it
        log.info("mind plan: recipe %r unavailable: %s", name, exc)
        return ()
    return tuple(_one_line(st, RECIPE_STEP_CHARS) for st in (r.steps if r else [])[:n_steps])


@lru_cache(maxsize=1)
def move_rules() -> str:
    """Compact remix-move rules pulled from the DJ/04 + DJ/05 notes."""
    try:
        from app.music_brain.dj_knowledge import _note, _section
    except Exception:
        return ""
    out = []
    for move, stem, heading, keyword, n in _MOVE_SOURCES:
        body = _section(_note(stem), heading)
        lines = [ln for ln in body.splitlines() if keyword is None or keyword.lower() in ln.lower()]
        if lines:
            out.append(f"- {move} ({stem.split(' ')[0]}): " + _one_line("\n".join(lines[:4]), n))
    for move, recipe, idx in _REMIX_RECIPES:
        steps = _recipe_lines(recipe, idx + 1)
        if steps:
            out.append(f"- {move} ({recipe}): " + _one_line(steps[-1], 95))
    return "\n".join(out)


def grounding(recipe_names: list[str]) -> str:
    """DJ-wiki block for the plan prompt: candidate recipe steps + move rules, capped."""
    parts = []
    rules = move_rules()
    if rules:
        parts.append("Move rules:\n" + rules)
    for name in dict.fromkeys(recipe_names):
        steps = _recipe_lines(name, 2)
        if steps:
            parts.append(f"{name} (recipe):\n" + "\n".join(f"  {i + 1}. {st}" for i, st in enumerate(steps)))
    text = "\n".join(parts)
    if len(text) > GROUNDING_MAX_CHARS:
        cut = text[:GROUNDING_MAX_CHARS]
        text = cut[: cut.rfind("\n")]
    return text


_SYSTEM = """You are the planning brain of a live DJ that re-edits songs as they play,
like Fred again.. does: loops, beat jumps, rolls, filter builds and echo throws
that build tension and release it on the drop. Never play a song as is, but be
restrained: a few well-placed moves beat many (crowds hate too many effects).
Use ONLY the facts given. Every time you write must be copied from the lists.

Rules:
1. candidate is an index from the candidate list.
2. exit is one number copied from exit_options.
3. Moves only from the Moves list, each in a phrase marked ok_remix.
4. stutter, filter_build and echo_freeze only in a phrase marked pre_drop.
5. One move per phrase. Remix moves never in two neighbouring phrases.
6. Each move type once. 1-4 moves. No remix move in the last 24 bars before exit.

Example facts:
  Candidates: 0: Bass Swap, score 80  1: Echo Out, score 70
  Phrases: 32.0: verse ok_remix | 48.0: verse ok_remix | 64.0: build, pre_drop ok_remix |
           80.0: drop ok_remix | 96.0: drop ok_remix | 112.0: breakdown ok_remix | 128.0: verse ok_remix
  exit_options: [112.0, 128.0, 144.0]
Example answer:
{"candidate": 0, "candidate_reason": "Keys and tempo fit, so a clean bass swap works.",
 "exit": 144.0, "exit_reason": "Leaves the second drop room to land before the swap.",
 "moves": [{"move": "beat_jump", "at": 32.0, "bars": 8, "reason": "The first verse drags, skip ahead."},
           {"move": "filter_build", "at": 64.0, "reason": "Sweep the build so the drop hits harder."}]}

Reply with ONE JSON object in exactly that shape and nothing else."""


# ------------------------------------------------------------------ facts
def merged_sections(sections: list[dict], bar_secs: float) -> list[dict]:
    """Merge adjacent same-label sections, drop those shorter than 8 bars.

    Same algorithm as mergeSections() in dj-mind.js.
    """
    merged: list[dict] = []
    for s in sections or []:
        last = merged[-1] if merged else None
        energy = float(s.get("energy") or 0.0)
        if last and last["label"] == s["label"] and abs(last["end"] - s["start"]) < 0.01:
            la, sa = last["end"] - last["start"], s["end"] - s["start"]
            last["energy"] = (last["energy"] * la + energy * sa) / max(1e-6, la + sa)
            last["end"] = s["end"]
        else:
            merged.append({"label": s["label"], "start": float(s["start"]),
                           "end": float(s["end"]), "energy": energy})
    return [s for s in merged if s["end"] - s["start"] >= MIN_SECTION_BARS * bar_secs]


RELEASE_LABELS = ("drop", "chorus")


def is_pre_drop(label: Optional[str], next_label: Optional[str]) -> bool:
    """The phrase right before a release: a drop/chorus comes next, or a build
    ends here (tension -> release, set study). Same as isPreDrop() in dj-mind.js."""
    if label in RELEASE_LABELS or next_label is None:
        return False
    return next_label in RELEASE_LABELS or (label == "build" and next_label != "build")


def phrase_label(sections: list[dict], t0: float, t1: float) -> tuple[Optional[str], Optional[float]]:
    """(label covering most of [t0, t1), overlap-weighted energy). Same as
    phraseLabel() in dj-mind.js: analysed sections are often shorter than a
    phrase, so a phrase takes the label that owns most of it."""
    cover: dict[str, float] = {}
    e_sum = w_sum = 0.0
    for s in sections or []:
        ov = min(t1, float(s["end"])) - max(t0, float(s["start"]))
        if ov > 0:
            cover[s["label"]] = cover.get(s["label"], 0.0) + ov
            e_sum += float(s.get("energy") or 0.0) * ov
            w_sum += ov
    if not cover:
        return None, None
    return max(cover, key=cover.get), round(e_sum / w_sum, 2)


def _vocal_share(regions, t0: float, t1: float) -> float:
    if t1 <= t0:
        return 0.0
    covered = 0.0
    for r in regions or []:
        a, b = max(t0, r[0]), min(t1, r[1])
        if b > a:
            covered += b - a
    return min(1.0, covered / (t1 - t0))


def build_facts(a: dict, b: dict, candidates: list[dict], ctx: dict) -> dict:
    """Compact, validated facts for one plan. `a`/`b` are analysis dicts.

    ctx: now (current position in A), window_lo/window_hi (exit window, track
    seconds), set_mode, set_position, recent_moves, mashup_possible,
    subdrop_last_track, remix_used (moves already played on A).
    """
    bpm = float(a.get("bpm") or 128.0)
    bar = 240.0 / bpm
    secs = merged_sections(a.get("sections") or [], bar)
    phrases = [float(t) for t in a.get("phrase_boundaries_8bar") or []]
    now = float(ctx.get("now") or 0.0)
    entry = float(ctx.get("entry") or 0.0)
    lo, hi = float(ctx["window_lo"]), float(ctx["window_hi"])
    duration = float(a.get("duration") or (phrases[-1] if phrases else 0.0))

    raw_secs = a.get("sections") or []
    spans = [(t, phrases[i + 1] if i + 1 < len(phrases) else t + PHRASE_BARS * bar)
             for i, t in enumerate(phrases)]
    labels = [phrase_label(raw_secs, t, end) for t, end in spans]
    rows = []
    for i, (t, end) in enumerate(spans):
        if t < now or t > hi or end > duration + 0.5:
            continue
        label, energy = labels[i]
        nxt = labels[i + 1][0] if i + 1 < len(labels) else None
        rows.append({
            "t": round(t, 2),
            "end": round(end, 2),
            "section": label or "?",
            "energy": energy,
            "vocal": round(_vocal_share(a.get("vocal_active_regions"), t, end), 2),
            "pre_drop": is_pre_drop(label, nxt),
        })
    exits = [r["t"] for r in rows if lo - 0.01 <= r["t"] <= hi + 0.01]
    return {
        "bar_secs": bar,
        "bpm": round(bpm, 1),
        "key": (a.get("key") or {}).get("camelot", "?"),
        "next_bpm": round(float(b.get("bpm") or 0.0), 1),
        "next_key": (b.get("key") or {}).get("camelot", "?"),
        "sections": [{k: round(v, 2) if isinstance(v, float) else v for k, v in s.items()} for s in secs],
        "phrases": rows,
        "exit_options": exits,
        "candidates": [
            {"recipe": c["recipe"], "score": c["score"], "a_time": c["a_time"],
             "b_time": c["b_time"], "overlap_style": c.get("overlap_style", "standard"),
             "pre_clear_bars": c.get("pre_clear_bars", 8)}
            for c in candidates[:3]
        ],
        "now": round(now, 2),
        "entry": round(entry, 2),
        "window": [round(lo, 2), round(hi, 2)],
        "set_mode": ctx.get("set_mode") or "hybrid",
        "set_position": round(float(ctx.get("set_position") or 0.0), 2),
        "recent_moves": list(ctx.get("recent_moves") or [])[-4:],
        "mashup_possible": bool(ctx.get("mashup_possible")),
        "subdrop_last_track": bool(ctx.get("subdrop_last_track")),
        "remix_used": [m for m in ctx.get("remix_used") or [] if m in REMIX_MOVES],
    }


def _nearest(values: list[float], t: float, tol: float) -> Optional[float]:
    best = min(values, key=lambda v: abs(v - t), default=None)
    return best if best is not None and abs(best - t) <= tol else None


# ------------------------------------------------------------- validation
def validate_plan(raw: Any, facts: dict) -> dict:
    """Keep the valid parts of an LLM plan; log and list everything dropped.

    Returns {candidate, exit, reasons{candidate, exit}, moves[], dropped[], repaired[]}.
    """
    dropped: list[str] = []
    repaired: list[str] = []
    if not isinstance(raw, dict):
        dropped.append("plan is not an object")
        raw = {}
    bar = facts["bar_secs"]
    cands = facts["candidates"]
    exits = facts["exit_options"]
    rows = {r["t"]: r for r in facts["phrases"]}
    phrase_ts = sorted(rows)

    # candidate index
    idx = raw.get("candidate")
    if isinstance(idx, bool) or not isinstance(idx, (int, float)) or int(idx) != idx \
            or not 0 <= int(idx) < len(cands):
        if idx is not None:
            dropped.append(f"candidate {idx!r} not in 0..{len(cands) - 1}")
        idx = 0
        repaired.append("candidate -> 0")
    idx = int(idx)

    # exit phrase: must be a listed exit option (snapped within 1 bar)
    exit_t = None
    try:
        exit_t = _nearest(exits, float(raw.get("exit")), bar)
    except (TypeError, ValueError):
        pass
    if exit_t is None:
        if raw.get("exit") is not None:
            dropped.append(f"exit {raw.get('exit')!r} not a phrase inside the window")
        fallback = cands[idx]["a_time"] if cands else None
        exit_t = _nearest(exits, fallback, 1e9) if fallback is not None and exits else None
        repaired.append(f"exit -> {exit_t}")
    exit_bars_from = (lambda t: (exit_t - t) / bar) if exit_t is not None else (lambda t: None)

    moves_in = raw.get("moves") or []
    if not isinstance(moves_in, list):
        dropped.append("moves is not a list")
        moves_in = []
    style = cands[idx]["overlap_style"] if cands else "standard"
    used_kinds = set(facts["remix_used"])
    remix_count = len(facts["remix_used"])
    taken: set[float] = set()
    remix_ts: list[float] = []
    kept: list[dict] = []
    entry = facts["entry"]

    def drop(m, why):
        dropped.append(f"{m.get('move') if isinstance(m, dict) else m!r}: {why}")

    for m in moves_in:
        if len(kept) >= MAX_MOVES:
            drop(m, f"over {MAX_MOVES} moves")
            continue
        if not isinstance(m, dict):
            drop(m, "not an object")
            continue
        kind = str(m.get("move", "")).strip().lower()
        if kind not in ALLOWED_MOVES:
            drop(m, "unknown move")
            continue
        try:
            at = _nearest(phrase_ts, float(m.get("at")), bar)
        except (TypeError, ValueError):
            at = None
        if at is None:
            drop(m, f"at {m.get('at')!r} is not a listed phrase")
            continue
        if at in taken:
            drop(m, "second move in the same phrase")
            continue
        row = rows[at]
        to_exit = exit_bars_from(at)
        on_track = (at - entry) / bar
        if to_exit is not None and to_exit <= 0:
            drop(m, "at or after the exit")
            continue
        if kind in TRANSITION_MOVES and any(k["move"] == kind for k in kept):
            drop(m, "move type twice")
            continue

        bars = None
        if kind == "hold":
            if to_exit is None or not 0 < to_exit <= HOLD_BARS + PRECLEAR_SLACK_BARS:
                drop(m, "hold only just before the exit")
                continue
        elif kind == "preclear":
            if style == "instant":
                drop(m, "instant recipe never pre-clears")
                continue
            if to_exit is None or not 2 < to_exit <= 16 + PRECLEAR_SLACK_BARS:
                drop(m, "preclear only 8-16 bars before the exit")
                continue
        elif kind == "subdrop":
            if facts["subdrop_last_track"] or "subdrop" in used_kinds:
                drop(m, "subdrop already used (this or last track)")
                continue
            if row["section"] not in ("verse", "breakdown") or (row["energy"] or 0) > SUBDROP_MAX_ENERGY:
                drop(m, "subdrop only on a verse/breakdown below full energy")
                continue
            if on_track < MIN_BARS_ON_TRACK or (to_exit is not None and to_exit < EXIT_GUARD_BARS):
                drop(m, "subdrop too close to song start or exit")
                continue
        else:  # remix moves
            if remix_count >= REMIX_MAX_PER_SONG:
                drop(m, f"remix cap {REMIX_MAX_PER_SONG} per song reached")
                continue
            if kind in used_kinds:
                drop(m, "move type already used on this song")
                continue
            if on_track < MIN_BARS_ON_TRACK:
                drop(m, f"no remix in the first {MIN_BARS_ON_TRACK} bars")
                continue
            if to_exit is not None and to_exit < EXIT_GUARD_BARS:
                drop(m, f"no remix in the last {EXIT_GUARD_BARS} bars before the exit")
                continue
            if any(abs(at - t) < REMIX_GAP_PHRASES * PHRASE_BARS * bar - bar / 2 for t in remix_ts):
                drop(m, "remix moves in neighbouring phrases")
                continue
            if kind in DROP_LEADINS and not row["pre_drop"]:
                drop(m, "drop lead-in needs the phrase right before a drop")
                continue
            if kind == "loop_extend":
                bars = m.get("bars") if m.get("bars") in (4, 8) else 8
                if row["section"] in ("build", "outro"):
                    drop(m, "no loop on a build or outro")
                    continue
                # the loop adds `bars` of play time: keep the guard after it too
                if to_exit is not None and to_exit - bars < EXIT_GUARD_BARS:
                    drop(m, "loop would run into the exit guard")
                    continue
            elif kind == "beat_jump":
                bars = m.get("bars") if m.get("bars") in (8, 16) else 8
                if row["section"] in RELEASE_LABELS or row["pre_drop"]:
                    drop(m, "never jump over a drop or its lead-in")
                    continue
                land = row["end"] + bars * bar
                if to_exit is not None and (exit_t - land) / bar < EXIT_GUARD_BARS:
                    drop(m, "jump lands inside the exit guard")
                    continue
            elif kind == "beat_layer":
                score = cands[idx]["score"] if cands else 0
                if not facts["mashup_possible"] or score < BEAT_LAYER_MIN_SCORE:
                    drop(m, "beat layer needs a mashup-able pair with match score >= 65")
                    continue
            remix_count += 1
            used_kinds.add(kind)
            remix_ts.append(at)
        if kind == "subdrop":
            used_kinds.add(kind)
        taken.add(at)
        out = {"move": kind, "at": at, "reason": str(m.get("reason") or "")[:200]}
        if bars:
            out["bars"] = bars
        kept.append(out)

    kept.sort(key=lambda k: k["at"])
    for d in dropped:
        log.info("mind plan dropped: %s", d)
    chosen = dict(cands[idx]) if cands else None
    if chosen is not None and exit_t is not None:
        chosen["a_time"] = exit_t
    return {
        "candidate_index": idx,
        "candidate": chosen,
        "exit": exit_t,
        "reasons": {
            "candidate": str(raw.get("candidate_reason") or "")[:200],
            "exit": str(raw.get("exit_reason") or "")[:200],
        },
        "moves": kept,
        "dropped": dropped,
        "repaired": repaired,
    }


# ------------------------------------------------------------------ prompt
def build_prompt(facts: dict) -> str:
    lines = [
        f"CURRENT song: {facts['bpm']} BPM, key {facts['key']}. NEXT song: {facts['next_bpm']} BPM, key {facts['next_key']}.",
        f"Set mode {facts['set_mode']}, set position {round(facts['set_position'] * 100)}%. Now at {facts['now']}s.",
        f"Recent DJ moves: {', '.join(facts['recent_moves']) or 'none'}. Already used on this song: {', '.join(facts['remix_used']) or 'none'}.",
        f"Mashup possible: {'yes' if facts['mashup_possible'] else 'no'}.",
        "",
        "Match candidates (index: recipe, score, overlap):",
    ]
    for i, c in enumerate(facts["candidates"]):
        near = _nearest(facts["exit_options"], c["a_time"], 1e9)
        lines.append(f"  {i}: {c['recipe']}, score {c['score']}, {c['overlap_style']}"
                     + (f", nearest exit option {near}" if near is not None else ""))
    lines += ["", "Phrase list (t = phrase start, seconds):"]
    bar = facts["bar_secs"]
    last_exit = max(facts["exit_options"], default=None)
    for r in facts["phrases"]:
        # ok_remix = the caps allow a remix here for at least one exit option
        ok = (r["t"] - facts["entry"]) / bar >= MIN_BARS_ON_TRACK and (
            last_exit is None or (last_exit - r["t"]) / bar >= EXIT_GUARD_BARS)
        tags = [r["section"], f"energy {r['energy']}", f"vocal {r['vocal']}"]
        if r["pre_drop"]:
            tags.append("pre_drop")
        if ok:
            tags.append("ok_remix")
        lines.append(f"  {r['t']}: {', '.join(tags)}")
    lines += [
        "",
        f"exit_options: {facts['exit_options']}",
        "No remix move within 24 bars before the exit you choose.",
        "",
        "Moves:",
    ]
    for k, v in MOVE_HELP.items():
        if k == "beat_layer" and not facts["mashup_possible"]:
            continue
        lines.append(f"  {k}: {v}")
    brief = grounding([c["recipe"] for c in facts["candidates"]])
    if brief:
        lines += ["", "DJ KNOWLEDGE (from the ./DJ wiki, follow it):", brief]
    lines += ["", "Pick 2-4 moves total. Reply with the JSON object only."]
    return "\n".join(lines)


def plan_model() -> str:
    """AUTOPILOT_PLAN_MODEL, else AUTOPILOT_MODEL, else gemma3:4b."""
    return os.environ.get("AUTOPILOT_PLAN_MODEL") or os.environ.get("AUTOPILOT_MODEL") or "gemma3:4b"


def plan_pair(facts: dict, timeout: float = 60.0, llm=None) -> dict:
    """Ask the model for a plan and validate it. `llm(system, user) -> str` is injectable."""
    model = plan_model()
    call = llm or (lambda s, u: chat_raw(s, u, temperature=0.3, timeout=timeout, model=model,
                                          max_tokens=PLAN_MAX_TOKENS))
    t0 = time.monotonic()
    raw_text = call(_SYSTEM, build_prompt(facts))
    latency = time.monotonic() - t0
    try:
        raw = _extract_json(raw_text)
    except (ValueError, TypeError) as exc:
        log.info("mind plan: unparseable LLM output: %s", exc)
        raw = None
    plan = validate_plan(raw, facts)
    plan["latency_seconds"] = round(latency, 2)
    plan["parsed"] = raw is not None
    plan["model"] = model
    return plan
