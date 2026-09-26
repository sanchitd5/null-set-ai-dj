"""LAYER transition planner: hold two records together, then unwind the old one.

The set study (research/notes/set-study-gfF8jzBVWvM.md, sections 2 and 8 item 4)
found that layering 2-3 songs for 1-7 minutes and unwinding them slowly was the
most common pattern (14 of 30 segments). The LAYER transition does that for a
tempo-locked, key-compatible pair (both songs with a clear groove):

  1. B enters under A as a texture: lows killed, highs trimmed.
  2. Both play for `hold_bars` (16-64, set mode decides).
  3. The bass goes to B on a phrase line (one sub owner at any time).
  4. A unwinds over `unwind_bars` (8-16): highs, then mids, then the fader.

Two vocals never sound together: the whole overlap is checked against the
Demucs vocal regions of both songs, so a LAYER needs both vocal maps.

Grounding (./DJ/): [[3-Deck Layering]] (frequency slotting, swap the bass on a
phrase line, never two lows), [[Long Blend]], [[Bass Swap]],
[[EQ & Frequency Management]] (sub < 120 Hz has one owner),
[[Harmonic Mixing & Camelot System]] (key score >= 0.8),
[[Phrasing & Structure]] (start, swap and unwind on the 8-bar grid).
"""

from __future__ import annotations

import math
import statistics
from typing import List, Optional

from app.music_brain.analyzer import TrackAnalysis
from app.music_brain.blend import (
    ENTRY_SEARCH_FRACTION,
    Regions,
    _coverage,
    _label_at,
    drop_lines,
    min_exit_floor,
    tempo_lock,
)
from app.music_brain.recipe_matcher import camelot_distance_score

LAYER_MIN_KEY = 0.8            # same bar as the vocal mashup: below this the chords fight
LAYER_HOLD_BARS = (16, 32, 48, 64)
LAYER_UNWIND_BARS = (8, 16)
LAYER_MAX_VOCAL_CLASH = 0.03   # share of the overlap where both songs sing
GROOVE_MAX_CV = 0.08           # beat-interval spread that still counts as a steady beat
GROOVE_MIN_BEATS = 16
B_TAIL_BARS = 32               # B must have this much left after A is gone

_ENTRY_BONUS = {"intro": 0.3, "build": 0.15, "breakdown": 0.1}


def groove_steady(t: TrackAnalysis, start: float, end: float) -> Optional[bool]:
    """True when [start, end) has a steady beat, None when there are too few beats to say."""
    beats = [x for x in (t.beat_times or []) if start <= x < end]
    if len(beats) < GROOVE_MIN_BEATS:
        return None
    ibis = [b - a for a, b in zip(beats, beats[1:]) if b > a]
    if len(ibis) < GROOVE_MIN_BEATS - 1:
        return None
    mean = statistics.fmean(ibis)
    return mean > 0 and statistics.pstdev(ibis) / mean <= GROOVE_MAX_CV


def _nearest(values: List[float], t: float, tol: float) -> Optional[float]:
    best = min(values, key=lambda v: abs(v - t), default=None)
    return best if best is not None and abs(best - t) <= tol else None


def vocal_clash(a_vocals: Regions, b_vocals: Regions, a_start: float, b_start: float,
                a_len: float, a_per_b: float) -> float:
    """Share of A's [a_start, a_start + a_len) where both songs have a vocal.

    B's regions (B track seconds from b_start) are mapped onto A's clock with
    a_per_b = A track-seconds per B track-second.
    """
    if a_len <= 0:
        return 0.0
    mapped = [(a_start + (s - b_start) * a_per_b, a_start + (e - b_start) * a_per_b)
              for s, e in b_vocals]
    hit = 0.0
    for s, e in a_vocals:
        s, e = max(s, a_start), min(e, a_start + a_len)
        if e <= s:
            continue
        hit += _coverage(mapped, s, e) * (e - s)
    return hit / a_len


def hold_options(max_hold_bars: int) -> List[int]:
    return [h for h in sorted(LAYER_HOLD_BARS, reverse=True) if h <= max_hold_bars]


def plan_layer(
    a: TrackAnalysis,
    b: TrackAnalysis,
    window_lo: float,
    window_hi: float,
    a_bpm_effective: Optional[float] = None,
    a_vocals: Optional[Regions] = None,
    b_vocals: Optional[Regions] = None,
    max_hold_bars: int = 32,
    unwind_bars: int = 8,
    a_entry: Optional[float] = None,
) -> dict:
    """Plan a LAYER: A start (the fire point), B entry, hold and unwind length.

    Tries the longest hold the set mode allows first; returns {"ok": False,
    "reasons": [...]} when the pair cannot layer.
    """
    if unwind_bars not in LAYER_UNWIND_BARS:
        raise ValueError(f"unwind_bars must be one of {LAYER_UNWIND_BARS}")
    holds = hold_options(max_hold_bars)
    if not holds:
        raise ValueError(f"max_hold_bars must be >= {min(LAYER_HOLD_BARS)}")
    reasons: List[str] = []
    a_eff = a_bpm_effective or a.bpm
    lock = tempo_lock(a_eff, b.bpm)
    if lock is None:
        reasons.append(f"not tempo-locked ({b.bpm:.1f} vs {a_eff:.1f} BPM)")
    a_key = a.key.camelot if a.key else ""
    b_key = b.key.camelot if b.key else ""
    key_score = camelot_distance_score(a_key, b_key)[0] if a_key and b_key else 0.0
    if key_score < LAYER_MIN_KEY:
        reasons.append(f"keys too far for a layer ({a_key or '?'} / {b_key or '?'}, score {key_score:.2f})")
    if a_vocals is None or b_vocals is None:
        reasons.append("vocal maps needed (two vocals must never overlap)")
    if reasons:
        return {"ok": False, "reasons": reasons, "key_score": key_score}

    rate, mult = lock
    a_bar = 240.0 / a.bpm
    b_bar = 240.0 / (b.bpm * mult)
    a_per_b = a_bar / b_bar
    min_exit, lo, hi = min_exit_floor(a, a_entry, window_lo, window_hi, 16)
    b_drops = [t for t, _, _ in drop_lines(b.phrase_boundaries_8bar, b.energy_times, b.energy_curve,
                                           240.0 / b.bpm)]
    b_limit = b.duration * ENTRY_SEARCH_FRACTION
    groove_fail = None
    clash_best = None

    for hold in holds:
        total = hold + unwind_bars
        best = None
        for x in a.phrase_boundaries_8bar:
            if x < lo - 0.01 or x > hi + 0.01 or x + total * a_bar > a.duration - 1.0:
                continue
            swap_a = _nearest(a.phrase_boundaries_8bar, x + hold * a_bar, a_bar)
            if swap_a is None:
                continue
            ga = groove_steady(a, x, x + total * a_bar)
            if ga is False:
                groove_fail = "playing song has no steady beat there"
                continue
            for e in b.phrase_boundaries_8bar:
                if e > b_limit:
                    break
                if e + (total + B_TAIL_BARS) * b_bar > b.duration:
                    break
                swap_b = _nearest(b.phrase_boundaries_8bar, e + hold * b_bar, b_bar)
                if swap_b is None:
                    continue
                # B's drop is not wasted under A with its lows killed
                if any(e + 0.01 < d < swap_b - b_bar for d in b_drops):
                    continue
                gb = groove_steady(b, e, e + total * b_bar)
                if gb is False:
                    groove_fail = "incoming song has no steady beat there"
                    continue
                if ga is None and gb is None:
                    groove_fail = "no beat grid to confirm a steady groove"
                    continue
                clash = vocal_clash(a_vocals, b_vocals, x, e, total * a_bar, a_per_b)
                clash_best = clash if clash_best is None else min(clash_best, clash)
                if clash > LAYER_MAX_VOCAL_CLASH:
                    continue
                on_drop = any(abs(d - swap_b) <= b_bar for d in b_drops)
                score = (0.5 if on_drop else 0.0) + _ENTRY_BONUS.get(_label_at(b, e), 0.0)
                score += 0.2 * (x - lo) / max(1.0, hi - lo) - 2.0 * clash - 0.1 * e / max(1.0, b_limit)
                if best is None or score > best[0]:
                    best = (score, x, e, swap_a, swap_b, clash, on_drop)
        if best is not None:
            _, x, e, swap_a, swap_b, clash, on_drop = best
            return {
                "ok": True,
                "reasons": [],
                "start": round(x, 3),                 # A track time: B enters here (fire point)
                "entry": round(e, 3),                 # B track time at the start
                "hold_bars": hold,
                "unwind_bars": unwind_bars,
                "total_bars": total,
                "swap_at": round(swap_a, 3),          # A track time of the bass hand-off
                "b_swap": round(swap_b, 3),           # B track time of the bass hand-off
                "end_at": round(x + total * a_bar, 3),  # A track time when A is gone
                "b_end": round(e + total * b_bar, 3),
                "rate": round(rate, 5),
                "pitch_percent": round((rate - 1) * 100, 3),
                "key_score": key_score,
                "vocal_clash": round(clash, 3),
                "swap_on_drop": on_drop,
                "min_exit": None if min_exit is None else round(min_exit, 3),
                "entry_label": _label_at(b, e),
            }
    if groove_fail and clash_best is None:
        reasons.append(groove_fail)
    elif clash_best is not None:
        reasons.append(f"both songs sing through every layer window (best {clash_best:.0%} overlap)")
    else:
        reasons.append("no phrase window long enough for a layer")
    return {"ok": False, "reasons": reasons, "key_score": key_score}


def key_fits(a_key: str, b_key: str) -> bool:
    return bool(a_key and b_key) and camelot_distance_score(a_key, b_key)[0] >= LAYER_MIN_KEY


def pitch_fits(host_bpm: float, guest_bpm: float, max_dev: float) -> bool:
    lock = tempo_lock(host_bpm, guest_bpm)
    return lock is not None and abs(math.log(lock[0])) <= math.log1p(max_dev)
