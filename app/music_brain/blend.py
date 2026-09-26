"""Beat-to-beat blend planner: where to leave A, where to enter B, and how fast
to play B so the two beats lock.

A good DJ blend hands one BEAT to another: it leaves A on an instrumental
phrase (outro / breakdown / drum section, no vocal) and brings B in on its
instrumental intro, tempo-matched and phase-locked, then swaps the bass on a
phrase line. Leaving A mid-vocal into B's beat is the "vocals switched to beat"
failure this module exists to prevent.

Grounding (./DJ/): [[Phrasing & Structure]] (both points on the 8-bar grid),
[[Beatmatching & Tempo]] (tempo lock via pitch, +/-8% max), [[EQ Blend]] /
[[Long Blend]] / [[Bass Swap]] (instrumental overlap, single bass owner),
[[EQ & Frequency Management]].

Vocal activity comes from Demucs vocal stems (vocal_presence_map). Without
stems the planner still phrase-aligns and tempo-locks, and falls back to the
section labels (prefers outro/breakdown exits and intro entries).
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

from app.music_brain.analyzer import TrackAnalysis

Regions = List[Tuple[float, float]]

MAX_TEMPO_DEVIATION = 0.08     # beyond this a pitch-locked blend sounds wrong
MAX_VOCAL_COVERAGE = 0.15      # "instrumental" = at most 15% of the blend has vocal
ENTRY_SEARCH_FRACTION = 0.45   # look for B's entry in its first 45%
ALLOWED_BARS = (8, 16, 32)

_EXIT_LABEL_BONUS = {"outro": 0.25, "breakdown": 0.2, "intro": 0.1, "build": 0.05}
_ENTRY_LABEL_BONUS = {"intro": 0.25, "build": 0.1, "breakdown": 0.05}


def _coverage(regions: Regions, start: float, end: float) -> float:
    if end <= start:
        return 0.0
    hit = 0.0
    for a, b in regions:
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            hit += hi - lo
    return hit / (end - start)


def _label_at(t: TrackAnalysis, x: float) -> str:
    for s in t.sections:
        if s.start <= x < s.end:
            return s.label
    return "verse"


def tempo_lock(a_bpm: float, b_bpm: float) -> Optional[Tuple[float, float]]:
    """(playback_rate for B, B bpm multiplier) so B's beat matches A's, or None."""
    if a_bpm <= 0 or b_bpm <= 0:
        return None
    best = None
    for mult in (1.0, 2.0, 0.5):
        rate = a_bpm / (b_bpm * mult)
        if best is None or abs(rate - 1) < abs(best[0] - 1):
            best = (rate, mult)
    return best if abs(best[0] - 1) <= MAX_TEMPO_DEVIATION else None


def plan_blend(
    a: TrackAnalysis,
    b: TrackAnalysis,
    window_lo: float,
    window_hi: float,
    a_bpm_effective: Optional[float] = None,
    a_vocals: Optional[Regions] = None,
    b_vocals: Optional[Regions] = None,
    bars: int = 16,
) -> dict:
    if bars not in ALLOWED_BARS:
        raise ValueError(f"bars must be one of {ALLOWED_BARS}")
    a_eff = a_bpm_effective or a.bpm
    lock = tempo_lock(a_eff, b.bpm)
    if lock is None:
        return {"ok": False, "reasons": [f"tempo gap too big for a beat blend ({b.bpm:.1f} vs {a_eff:.1f} BPM)"]}
    rate, mult = lock
    a_bar = 240.0 / a.bpm                  # A track-seconds per bar
    b_bar = 240.0 / (b.bpm * mult)         # B track-seconds per (A-locked) bar
    a_len, b_len = bars * a_bar, bars * b_bar
    vocals_known = a_vocals is not None and b_vocals is not None

    # ── exit: A phrase inside the play window, instrumental for the whole blend
    exits = []
    for x in a.phrase_boundaries_8bar:
        if x < window_lo - 0.01 or x > window_hi + 0.01 or x + a_len > a.duration:
            continue
        cov = _coverage(a_vocals, x, x + a_len) if a_vocals is not None else None
        score = _EXIT_LABEL_BONUS.get(_label_at(a, x), 0.0)
        score += 0.1 * (x - window_lo) / max(1.0, window_hi - window_lo)  # ride a little longer
        if cov is not None:
            score -= 2.0 * cov
        exits.append((score, x, cov))
    if not exits:
        return {"ok": False, "reasons": ["no phrase boundary inside the play window"]}

    # ── entry: B phrase early in the song, instrumental for the whole blend
    entries = []
    limit = b.duration * ENTRY_SEARCH_FRACTION
    for e in [0.0] + list(b.phrase_boundaries_8bar):
        if e > limit or e + b_len > b.duration:
            continue
        cov = _coverage(b_vocals, e, e + b_len) if b_vocals is not None else None
        score = _ENTRY_LABEL_BONUS.get(_label_at(b, e), 0.0) - 0.2 * (e / max(1.0, limit))
        if cov is not None:
            score -= 2.0 * cov
        entries.append((score, e, cov))
    if not entries:
        return {"ok": False, "reasons": ["incoming song too short for this blend"]}

    ex_score, exit_t, a_cov = max(exits)
    en_score, entry_t, b_cov = max(entries)
    reasons = []
    instrumental = True
    if a_cov is not None and a_cov > MAX_VOCAL_COVERAGE:
        instrumental = False
        reasons.append(f"no vocal-free exit in window (best {a_cov:.0%} vocal)")
    if b_cov is not None and b_cov > MAX_VOCAL_COVERAGE:
        instrumental = False
        reasons.append(f"no vocal-free entry (best {b_cov:.0%} vocal)")

    return {
        "ok": True,
        "instrumental": instrumental,   # False: blend anyway but keep it short
        "vocals_known": vocals_known,
        "reasons": reasons,
        "bars": bars,
        "exit": round(exit_t, 3),
        "entry": round(entry_t, 3),
        "rate": round(rate, 5),
        "pitch_percent": round((rate - 1) * 100, 3),
        "semitones": round(12 * math.log2(rate), 3),
        "a_vocal_coverage": None if a_cov is None else round(a_cov, 3),
        "b_vocal_coverage": None if b_cov is None else round(b_cov, 3),
        "exit_label": _label_at(a, exit_t),
        "entry_label": _label_at(b, entry_t),
    }
