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
DROP_HOLD_BARS = 8             # play at least this much of A's first drop before leaving
FINALE_S = 30.0                # a drop whose high-energy run ends this close to the song's end plays out whole
ENTRY_SEARCH_FRACTION = 0.45   # B's entry must leave >= 55% of the song to play
ENERGY_MATCH_WEIGHT = 1.5      # |A exit energy - B entry energy| penalty (both 0-1, own-peak normalised)
ALLOWED_BARS = (8, 16, 32)
ENTRY_MODES = ("match", "drop")
DROP_MIN_BARS = 8              # a "long" drop: same sliver filter as dj-mind.js mergeSections
DROP_JUMP = 0.2                # drop line: phrase energy jumps this much (0-1) over the one before
DROP_QUARTILE = 0.75           # ... and sits in the song's top quartile of phrase energies

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


def _curve_mean(times, curve, start: float, end: float) -> Optional[float]:
    vals = [e for x, e in zip(times or [], curve or []) if start <= x < end]
    return sum(vals) / len(vals) if vals else None


def drop_lines(phrases, times, curve, bar: float) -> List[Tuple[float, float, float]]:
    """(t, energy, previous phrase energy) of the 8-bar phrase lines where a drop
    hits. The analyzer's section labels flicker (1-3 s slivers; real tracks
    rarely merge into a long "drop"), so a drop is found acoustically: the
    phrase's mean energy is in the song's top quartile AND jumps >= DROP_JUMP
    over the phrase before it (./DJ/05 [[Double Drop]] / [[Drop Swap]]: the
    drop is the downbeat where the full track slams in). Same rule as
    dropLines() in app/ui/static/dj-mind.js."""
    L = 8 * bar
    ph = [p for p in phrases or []]
    es = [_curve_mean(times, curve, p, p + L) for p in ph]
    known = sorted(e for e in es if e is not None)
    if len(known) < 3:
        return []
    q3 = known[int(DROP_QUARTILE * (len(known) - 1))]
    out = []
    for i in range(1, len(ph)):
        e, pe = es[i], es[i - 1]
        if e is not None and pe is not None and e >= q3 and e - pe >= DROP_JUMP - 1e-9:
            out.append((ph[i], e, pe))
    return out


def _vocal_in_bars(regions: Optional[Regions], entry: float, bar: float) -> Optional[float]:
    """Bars from `entry` until the first vocal region (0 if one is sounding at entry);
    None when vocals are unknown or there is none after the entry."""
    if regions is None or bar <= 0:
        return None
    starts = [max(a, entry) for a, b in regions if b > entry]
    if not starts:
        return None
    return round((min(starts) - entry) / bar, 2)


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


def _breakdown_drops(a, bar: float, known) -> List[Tuple[float, float]]:
    """Drops that come out of a breakdown but are no louder than the song's intro, so
    drop_lines' top-quartile test misses them (Anyma - Atoma: a loud synth intro, a
    breakdown 1:00-1:45, the drop after it). A phrase counts when it is within 90 % of
    the loudest phrase and 0.25 above the quieter of the two phrases before it. Used only
    by the exit floor; drop_lines (shared with dj-mind.js dropLines) is unchanged."""
    L = 8 * bar
    ph = sorted(a.phrase_boundaries_8bar or [])
    es = [_curve_mean(a.energy_times, a.energy_curve, p, p + L) for p in ph]
    top = max((e for e in es if e is not None), default=None)
    if top is None:
        return []
    out = []
    for i in range(2, len(ph)):
        e, p1, p2 = es[i], es[i - 1], es[i - 2]
        if e is None or p1 is None or p2 is None or ph[i] in known:
            continue
        # the phrase just before is still clearly lower (a build into it), not already the drop
        if e >= 0.9 * top and e - min(p1, p2) >= 0.25 and p1 <= 0.85 * e:
            out.append((ph[i], e))
    return out


def min_exit_floor(a: TrackAnalysis, a_entry: Optional[float], window_lo: float,
                   window_hi: float, bars: int) -> Tuple[Optional[float], float, float]:
    """(min_exit, window_lo, window_hi): the exit may not come before A's first
    drop after `a_entry` has played DROP_HOLD_BARS; the window stretches to
    allow it when `bars` more of A still fit. min_exit None = no floor."""
    a_own_bar = 240.0 / a.bpm if a.bpm > 0 else 2.0
    lines = [(t, e) for t, e, _ in drop_lines(a.phrase_boundaries_8bar, a.energy_times, a.energy_curve, a_own_bar)]
    lines += _breakdown_drops(a, a_own_bar, {t for t, _ in lines})
    after = sorted((t, e) for t, e in lines if t >= (a_entry or 0.0) - 0.01)
    if not after:
        return None, window_lo, window_hi
    # The first drop, and also the song's BIGGEST drop when it comes later (a short track
    # whose one real drop sits near the end, e.g. Anyma - Atoma 1:40-2:25 of 2:30): leaving
    # before it plays skips the moment the song is built for.
    first = after[0][0]
    main_t, main_e = max(after, key=lambda x: (x[1], x[0]))
    min_exit = max(first, main_t) + DROP_HOLD_BARS * a_own_bar
    # A FINALE drop (its high-energy run lasts to within FINALE_S of the end) plays out whole:
    # crossfading inside it cuts the song's climax. A drop in the middle of a long song keeps
    # the DROP_HOLD_BARS floor (leaving later in it is a normal DJ move).
    L = 8 * a_own_bar
    run_end = main_t + L
    for p in sorted(x for x in (a.phrase_boundaries_8bar or []) if x > main_t + 1e-6):
        e = _curve_mean(a.energy_times, a.energy_curve, p, p + L)
        if e is None or e < 0.85 * main_e:
            break
        run_end = p + L
    # ... and bar by bar into the phrase after it while the drop still carries (its tail)
    tail = run_end
    while tail + a_own_bar <= a.duration:
        e = _curve_mean(a.energy_times, a.energy_curve, tail, tail + a_own_bar)
        if e is None or e < 0.7 * main_e:
            break
        tail += a_own_bar
    run_end = min(tail, a.duration)
    if a.duration - run_end <= FINALE_S:
        min_exit = max(min_exit, run_end - a_own_bar)
    # Never drop the floor because a full `bars` blend no longer fits after the drop: the
    # caller then has less room (a shorter move, or an exit over the drop's tail), which is
    # better than crossfading out before the drop. Only a floor past the song's end is moot.
    if min_exit >= a.duration - a_own_bar:
        min_exit = max(first, a.duration - bars * a_own_bar) if first < a.duration - a_own_bar else None
        if min_exit is None:
            return None, window_lo, window_hi
    window_lo = max(window_lo, min_exit)
    return min_exit, window_lo, max(window_hi, window_lo)


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
    a_entry: Optional[float] = None,
) -> dict:
    """entry_mode "match": B enters where its energy matches A's exit.
    entry_mode "drop": B enters on its first long drop (peak moves DOUBLE DROP /
    DROP SWAP land B's drop on A's drop downbeat; ./DJ/05 Double Drop, Drop Swap)."""
    if bars not in ALLOWED_BARS:
        raise ValueError(f"bars must be one of {ALLOWED_BARS}")
    if entry_mode not in ENTRY_MODES:
        raise ValueError(f"entry_mode must be one of {ENTRY_MODES}")
    a_eff = a_bpm_effective or a.bpm
    # Don't skip A's drop: the exit may not come before A's first drop (after
    # where A came in) has played DROP_HOLD_BARS. The play window stretches to
    # allow it when the song is long enough. Returned even on a tempo gap so
    # the caller can respect it for echo-out exits too.
    min_exit, window_lo, window_hi = min_exit_floor(a, a_entry, window_lo, window_hi, bars)
    lock = tempo_lock(a_eff, b.bpm)
    if lock is None:
        return {"ok": False, "min_exit": min_exit,
                "reasons": [f"tempo gap too big for a beat blend ({b.bpm:.1f} vs {a_eff:.1f} BPM)"]}
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
        # ride the song: an early exit is only worth it for a clearly better
        # (vocal-free / outro) phrase. 0.1 let a breakdown bonus cut a QUICK
        # song at 0:46 of a 1-2 min window.
        score += 0.35 * (x - window_lo) / max(1.0, window_hi - window_lo)
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
        b_bar_own = 240.0 / b.bpm
        drops = [(t, t + 8 * b_bar_own, e) for t, e, _ in
                 drop_lines(b.phrase_boundaries_8bar, b.energy_times, b.energy_curve, b_bar_own)]
        drops = sorted(drops + long_drops(b, b_bar_own))
        if not drops:
            return {"ok": False, "reasons": ["incoming song has no drop"]}
        drop_span = drops[0]
        # Target B's drop energy, not A's exit energy: the drop IS the entry.
        a_energy = drop_span[2]
        limit = b.duration
    # Only B's own phrase grid: its first boundary is its first detected
    # downbeat (5.9 s into Lane 8 "Little By Little"), not 0:00. Entering at
    # 0:00 put B's downbeats off A's phrase line by whatever the intro pad is.
    b_first_drop = next(iter(t for t, _, _ in drop_lines(
        b.phrase_boundaries_8bar, b.energy_times, b.energy_curve, 240.0 / b.bpm if b.bpm > 0 else 2.0)), None)
    for e in b.phrase_boundaries_8bar:
        if e > limit or e + b_len > b.duration:
            continue
        # never enter past B's first drop: its biggest moment must be heard
        if entry_mode == "match" and b_first_drop is not None and e > b_first_drop + 0.01:
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
        # bars (A-locked) from B's entry until B's vocal first sounds: the
        # overlap must end before it or two vocals sing at once
        "b_vocal_in_bars": _vocal_in_bars(b_vocals, entry_t, b_bar),
        "min_exit": None if min_exit is None else round(min_exit, 3),
        "exit_energy": None if a_energy is None else round(a_energy, 3),
        "entry_energy": None if b_energy is None else round(b_energy, 3),
        "exit_label": _label_at(a, exit_t),
        "entry_label": _label_at(b, entry_t),
    }
