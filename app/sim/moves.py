"""What a booked transition would sound like, estimated from measured stems and energy.

The sim has no audio device, so a transition is "executed" as the same decisions the
console makes in executeTransition (autopilot.js), with the audio effect estimated from the
per-second stem RMS of the two songs (fixtures/_pool) instead of played:

  1. Stem Bridge / echo kinds   stemMoves stemBridgePlan + gates()  (loudness floor + audible band)
  2. stem blend kinds           stemMoves fitStemBlend (intro stem from B's measured energy,
                                loudness-floor fixes)  ->  refused = the EQ path runs instead
  3. EQ path                    the crossfader shapes of executeTransition, estimated master level
                                from the songs' own energy curves; eqIntro's silent-intro guard

All the plan / gate functions are the console's own (bridge.js -> stem-moves.js), only the
audio graph is missing. Estimates, not measurements: see README "what the sim cannot judge".
"""
from __future__ import annotations

import math
from typing import Optional

STEM_BLEND_KINDS = {"bass", "blend", "filter", "loop", "double"}
INTRO_MIN_RMS = 0.01                       # stem-moves.js: below this a stem is not really playing
SILENCE_DB, QUIETER_DB = 15.0, 6.0         # masterAudibility: the master 15 dB under A, and 6 under A alone
EQ_BARS = {"bass": 8, "echo": 8, "filter": 8, "loop": 8, "double": 8.5, "blend": 16, "default": 16}


def _interp(times, vals, t: float) -> float:
    if not times or not vals:
        return 0.0
    if t <= times[0]:
        return float(vals[0])
    if t >= times[-1]:
        return float(vals[-1])
    lo, hi = 0, len(times) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if times[mid] <= t:
            lo = mid
        else:
            hi = mid
    f = (t - times[lo]) / max(1e-9, times[hi] - times[lo])
    return float(vals[lo]) + (float(vals[hi]) - float(vals[lo])) * f


def _fader(kind: str, total_bars: float, u: float) -> float:
    """Crossfader position 0 (A) .. 1 (B) at bar `u` of an EQ-path move (executeTransition shapes)."""
    if kind == "double":
        return 0.5 if u < total_bars - 1.5 else 0.5 + 0.5 * (u - (total_bars - 1.5)) / 1.5
    if kind in ("blend", "default"):
        return 0.5 * u / 8 if u < 8 else 0.5 + 0.5 * (u - 8) / 8
    # bass / echo / filter / loop: to the centre by bar 4, across in the last 4
    return 0.5 * u / 4 if u < 4 else 0.5 + 0.5 * (u - 4) / 4


def eq_dead_air(kind: str, bars: float, bar_s: float, a_curve, b_curve, a_pos: float, b_pos: float) -> tuple:
    """Seconds the estimated master is near silent during an EQ-path move, and its worst dB.

    Level = incoherent sum of the two songs' own (peak-normalised) energy under the equal-power
    fader. Silent = 15 dB under A's level at the start AND 6 dB under what A alone would play
    (masterAudibility's rule: A's own breaks are not the move's doing). a_curve / b_curve:
    (times, values) of the analysis energy curve."""
    at, av = a_curve
    bt, bv = b_curve
    ref = max(1e-6, _interp(at, av, a_pos))
    floor = max(0.02, ref * 10 ** (-SILENCE_DB / 20))
    quieter = 10 ** (-QUIETER_DB / 20)
    step = min(0.5, bar_s / 4)
    dead, min_db, run, t, worst = 0.0, 0.0, 0.0, 0.0, 0.0
    total = bars * bar_s
    while t <= total + 1e-9:
        u = t / bar_s
        x = _fader(kind, bars, u)
        fo, fi = math.cos(x * math.pi / 2), math.sin(x * math.pi / 2)
        ea, eb = _interp(at, av, a_pos + t), _interp(bt, bv, b_pos + t)
        lvl = math.sqrt((fo * ea) ** 2 + (fi * eb) ** 2)
        min_db = min(min_db, 20 * math.log10(max(1e-9, lvl) / ref))
        if lvl < floor and lvl < ea * quieter:
            run += step
            dead += step
            worst = max(worst, run)
        else:
            run = 0.0
        t += step
    return round(dead, 2), round(min_db, 1), round(worst, 2)


def simulate_move(js, m: dict) -> dict:
    """Run the console's move logic for one booked transition.

    m: {recipe, planned, xf_duration, oneSong, beat (tempo locks), a_eff, a_bpm, b_bpm,
        key_score, a_pos (A song s at the line), b_pos (B's entry), a_left_s,
        a_vocals, b_vocals, a_curve (stem curves | None), b_curve, a_energy (times, vals),
        b_energy, stems_both}
    -> {path, executed, refused, reason, bars, seconds, dead_air_s, min_db, intro, intro_rms,
        silent_intro, unlocked_overlap_s}"""
    recipe = m["recipe"]
    kind = js.call("autopilot.recipeKind", recipe)
    scale = 1 if m["xf_duration"] >= 16 else 0.5
    bar_s = 240.0 / max(1.0, m["a_eff"])
    out = {"path": "eq", "executed": recipe, "refused": "", "bars": 0, "seconds": 0.0, "dead_air_s": 0.0,
           "min_db": 0.0, "intro": None, "intro_rms": None, "silent_intro": False, "unlocked_overlap_s": 0.0,
           "degraded": False, "kind": kind}
    stems_both = bool(m["stems_both"])
    # -- 1. Stem Bridge (also the echo kinds): beatless, needs stems on both --------------------
    if stems_both and (kind == "echo" or recipe == "Stem Bridge") and m["a_curve"] and m["b_curve"]:
        bar_b = 240.0 / max(1.0, m["b_bpm"])
        r = js.call("sim.stemBridge", {
            "barA": bar_s, "barB": bar_b, "keyScore": m["key_score"], "pA": m["a_pos"],
            "bFrom": max(0.0, m["b_pos"] - 4 * bar_b), "aVocals": m["a_vocals"],
            "aCurve": m["a_curve"], "bCurve": m["b_curve"]})
        if not r["refused"]:
            out.update(path="stem_bridge", executed="Stem Bridge", seconds=float(r["total"] or 0), min_db=r["minDb"],
                       intro=r["intro"], bars=round(float(r["total"] or 0) / bar_s, 1))
            return out
        out["refused"] = f"stem bridge refused: {r['reason']}"
        out["degraded"] = True
    # -- 2. stem blend --------------------------------------------------------------------------
    elif stems_both and kind in STEM_BLEND_KINDS and m["a_curve"] and m["b_curve"] and recipe != "Stem Bridge":
        a_left_bars = m["a_left_s"] / bar_s
        bars = js.call("autopilot.stemBlendBars", kind, bar_s, a_left_bars, scale)
        r = js.call("sim.stemBlend", {
            "kind": kind, "bars": bars, "barS": bar_s, "dir": 1, "pA": m["a_pos"], "pB": m["b_pos"],
            "barSongA": 240.0 / max(1.0, m["a_bpm"]), "barSongB": 240.0 / max(1.0, m["b_bpm"]),
            "aVocals": m["a_vocals"], "bVocals": m["b_vocals"], "keyScore": m["key_score"],
            "aCurve": m["a_curve"], "bCurve": m["b_curve"]})
        out["intro"], out["intro_rms"] = r["intro"], r["introRms"]
        if not r["refused"]:
            out.update(path="stem_blend", bars=bars, seconds=round(bars * bar_s, 2), min_db=r["minDb"])
            return out
        out["refused"] = f"stem blend refused: {r['reason']}"
        out["degraded"] = True
        out["silent_intro"] = (not r["introAudible"]) or "no audible intro" in r["reason"]
    # -- 3. EQ path (also where a refused stem move lands) ----------------------------------------
    bars_eq = EQ_BARS.get(kind, 16) * scale
    out["path"] = "eq"
    out["bars"] = bars_eq
    out["seconds"] = round(bars_eq * bar_s, 2)
    if recipe in ("Stem Bridge", "Stem Merge"):
        out["executed"] = "EQ blend"                       # executeTransition: the stem move was refused
    dead, min_db, _ = eq_dead_air(kind, bars_eq, bar_s, m["a_energy"], m["b_energy"], m["a_pos"], m["b_pos"])
    out["dead_air_s"], out["min_db"] = dead, min_db
    if not m["beat"]:
        # two beats at tempos that never lock overlap for the whole EQ blend (a stem bridge that was
        # refused, or an echo out with no stems): the audible cost of the tempo gap
        out["unlocked_overlap_s"] = round(bars_eq * bar_s, 2) if kind not in ("echo",) else round(4 * bar_s, 2)
    return out
