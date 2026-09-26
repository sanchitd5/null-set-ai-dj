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
ENTRY_SEARCH_FRACTION = 0.45   # B's entry must leave >= 55% of the song to play
ENERGY_MATCH_WEIGHT = 1.5      # |A exit energy - B entry energy| penalty (both 0-1, own-peak normalised)
ALLOWED_BARS = (8, 16, 32)
ENTRY_MODES = ("match", "drop")
DROP_MIN_BARS = 8              # a "long" drop: same sliver filter as dj-mind.js mergeSections

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


def _mean_energy(t: TrackAnalysis, start: float, end: float) -> Optional[float]:
    """Mean of the track's own peak-normalised energy curve over [start, end)."""
    if not t.energy_curve or not t.energy_times:
        return None
    vals = [e for x, e in zip(t.energy_times, t.energy_curve) if start <= x < end]
    return sum(vals) / len(vals) if vals else None


def long_drops(t: TrackAnalysis, bar: float) -> List[Tuple[float, float, float]]:
    """(start, end, energy) of drop sections at least DROP_MIN_BARS long, after
    merging adjacent same-label slivers (raw sections are 1-3 s on the energy
    grid). Same merge as mergeSections() in app/ui/static/dj-mind.js."""
    merged: List[list] = []
    for s in t.sections:
        if merged and merged[-1][0] == s.label and abs(merged[-1][2] - s.start) < 0.01:
            la, sa = merged[-1][2] - merged[-1][1], s.end - s.start
            merged[-1][3] = (merged[-1][3] * la + (s.energy or 0.0) * sa) / max(1e-6, la + sa)
            merged[-1][2] = s.end
        else:
            merged.append([s.label, s.start, s.end, s.energy or 0.0])
    return [(a, b, e) for lab, a, b, e in merged if lab == "drop" and b - a >= DROP_MIN_BARS * bar]


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
    entry_mode: str = "match",
) -> dict:
    """entry_mode "match": B enters where its energy matches A's exit.
    entry_mode "drop": B enters on its first long drop (peak moves DOUBLE DROP /
    DROP SWAP land B's drop on A's drop downbeat; ./DJ/05 Double Drop, Drop Swap)."""
    if bars not in ALLOWED_BARS:
        raise ValueError(f"bars must be one of {ALLOWED_BARS}")
    if entry_mode not in ENTRY_MODES:
        raise ValueError(f"entry_mode must be one of {ENTRY_MODES}")
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

    ex_score, exit_t, a_cov = max(exits)
    a_energy = _mean_energy(a, exit_t, exit_t + a_len)

    # ── entry: B phrase that MATCHES the energy A leaves at (may be well past
    # B's intro: coming out of a peak into a quiet intro killed the vibe),
    # instrumental for the whole blend, leaving enough of B to play.
    entries = []
    limit = b.duration * ENTRY_SEARCH_FRACTION
    drop_span = None
    if entry_mode == "drop":
        drops = long_drops(b, 240.0 / b.bpm)
        if not drops:
            return {"ok": False, "reasons": ["incoming song has no long drop"]}
        drop_span = drops[0]
        # Target B's drop energy, not A's exit energy: the drop IS the entry.
        a_energy = drop_span[2]
        limit = b.duration
    # Only B's own phrase grid: its first boundary is its first detected
    # downbeat (5.9 s into Lane 8 "Little By Little"), not 0:00. Entering at
    # 0:00 put B's downbeats off A's phrase line by whatever the intro pad is.
    for e in b.phrase_boundaries_8bar:
        if e > limit or e + b_len > b.duration:
            continue
        # drop mode: only B's phrase line on the drop downbeat (within a bar)
        if drop_span and abs(e - drop_span[0]) > 240.0 / b.bpm + 0.01:
            continue
        cov = _coverage(b_vocals, e, e + b_len) if b_vocals is not None else None
        score = -0.15 * (e / max(1.0, limit))            # mild: more of B left to play
        b_energy = _mean_energy(b, e, e + b_len)
        if a_energy is not None and b_energy is not None:
            score -= ENERGY_MATCH_WEIGHT * abs(a_energy - b_energy)
        else:
            score += _ENTRY_LABEL_BONUS.get(_label_at(b, e), 0.0)
        if cov is not None:
            score -= 2.0 * cov
        entries.append((score, e, cov, b_energy))
    if not entries:
        why = "no phrase line on the incoming drop" if drop_span else "incoming song too short for this blend"
        return {"ok": False, "reasons": [why]}

    en_score, entry_t, b_cov, b_energy = max(entries)
    reasons = []
    instrumental = True
    if a_cov is not None and a_cov > MAX_VOCAL_COVERAGE:
        instrumental = False
        reasons.append(f"no vocal-free exit in window (best {a_cov:.0%} vocal)")
    if b_cov is not None and b_cov > MAX_VOCAL_COVERAGE:
        instrumental = False
        reasons.append(f"no vocal-free entry (best {b_cov:.0%} vocal)")

    out_drop = None if drop_span is None else {"start": round(drop_span[0], 3), "end": round(drop_span[1], 3)}
    return {
        "ok": True,
        "entry_mode": entry_mode,
        "drop": out_drop,           # drop mode: B's long drop the entry lands on
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
        "exit_energy": None if a_energy is None else round(a_energy, 3),
        "entry_energy": None if b_energy is None else round(b_energy, 3),
        "exit_label": _label_at(a, exit_t),
        "entry_label": _label_at(b, entry_t),
    }
