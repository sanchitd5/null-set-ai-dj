"""Late entry: where an incoming song may come in (Python twin of autopilot.js entryLines /
pickEntryLine / hashSeed / seededRng).

OWNER RULE (from "Neverland -> Nocturnal"): B enters on a STRONG downbeat, an 8-bar phrase line
with a beat under it, anywhere in the song, never inside a quiet / silent / drumless stretch
unless the recipe plays B's intro on purpose (./DJ/05 [[Breakdown Transition]],
[[Reverb Transition]]). The live pick is uniform among the lines whose window level fits the set
energy, from a seeded RNG so a session / sim seed replays it.

Both twins run the same cases (app/tests/fixtures/entry_line_cases.json) so they cannot drift.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence

# "Strong downbeat" levels, measured (read only, 2026-09-30) on 22 library songs: 10 with quiet
# intros and 10 that start on the beat (picked from their energy curves) plus Nocturnal and
# Neverland, every 8-bar line (442). Each level is the 2 bars after the line, RMS over the song's
# median bar. Nocturnal's whole intro (bars 0-14) sits at 0.46 drums / 0.49 low, its beat at 1.4.
ENTRY_DRUMS_MIN = 0.5     # drum stem: first-line hits 19/20; Nocturnal 3.8 out, 18.79 in
ENTRY_LOW_MIN = 0.6       # mix low band (no stems): 93 % per-line agreement with the drum stem, first lines 20/20
ENTRY_QUIET_FRAC = 0.5    # no audio at all: phrase energy / the song's median phrase (last resort, unmeasured)
ENTRY_HANDOVER_BARS = 8   # B owns the room this many bars after it enters (the 8-bar Echo Out / Bass Swap)
LOW_BAND_HZ = 150.0
LOW_SR = 11025
ENTRY_OVERLAP_BARS = 16   # B must not sing inside this many bars after its entry (as before)
ENERGY_MATCH_TOL = 3      # autopilot.js ENERGY_MATCH_TOL: window level within 3 of the set level
INTRO_RECIPES = ("Breakdown Transition", "Reverb Transition")
_EPS = 1e-3
_M32 = 0xFFFFFFFF


def _fin(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def intro_recipe(recipe: Optional[str]) -> bool:
    return str(recipe or "") in INTRO_RECIPES


def _mean_over(times: Sequence, curve: Sequence, t0: float, t1: float) -> Optional[float]:
    s, n = 0.0, 0
    for t, e in zip(times or [], curve or []):
        if t0 <= t < t1 and _fin(e):
            s += e
            n += 1
    return s / n if n else None


def low_band_bars(channels: Sequence, sr: int, anchor: float, bar: float) -> List[float]:
    """autopilot.js lowBandBars: mono mix decimated to ~LOW_SR, two one-pole low-passes at
    LOW_BAND_HZ, RMS per bar on B's phrase grid (from `anchor`). No stems needed."""
    import numpy as np
    from scipy.signal import lfilter

    chs = [np.asarray(c, dtype=np.float64) for c in channels if c is not None and len(c)]
    if not chs or not (bar > 0) or not (sr > 0):
        return []
    n = min(len(c) for c in chs)
    step = max(1, int(round(sr / LOW_SR)))
    y = sum(c[:n:step] for c in chs) / len(chs)
    fs = sr / step
    k = 1.0 - math.exp(-2 * math.pi * LOW_BAND_HZ / fs)
    y = lfilter([k], [1.0, k - 1.0], lfilter([k], [1.0, k - 1.0], y))
    out, t = [], anchor % bar
    while True:
        i0, i1 = int(math.floor(t * fs)), int(math.floor((t + bar) * fs))
        if i1 > len(y):
            break
        seg = y[i0:i1]
        out.append(float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0)
        t += bar
    return out


def low_level_at(bars: Sequence[float], anchor: float, bar: float, t: float, n: int = 2) -> Optional[float]:
    """autopilot.js lowLevelAt: the n bars after t (RMS) over the song's median bar, None unmeasured."""
    if not bars or not (bar > 0):
        return None
    srt = sorted(bars)
    med = srt[(len(srt) - 1) // 2]
    i = int(round((t - anchor % bar) / bar))
    seg = [bars[j] for j in range(i, i + n) if 0 <= j < len(bars)]
    if not seg or not (med > 0):
        return None
    return math.sqrt(sum(x * x for x in seg) / len(seg)) / med


def main_drop_of(drops: Optional[Sequence[dict]]) -> Optional[dict]:
    """The song's biggest drop line (earliest on a tie). drops: [{t, energy}]."""
    best = None
    for d in drops or []:
        if d and _fin(d.get("t")) and (best is None or (d.get("energy") or 0) > (best.get("energy") or 0) + 1e-9):
            best = d
    return best


def entry_lines(lines: Sequence[float], include: Optional[float] = None,
                energy_times: Sequence[float] = (), energy_curve: Sequence[float] = (),
                vocals: Optional[Sequence] = None, drops: Optional[Sequence[dict]] = None,
                bar: Optional[float] = None, end: Optional[float] = None, room_s: float = 0.0,
                overlap_bars: Optional[float] = None, band: Optional[str] = None,
                intro_ok: bool = False,
                drums: Optional[Callable[[float], Optional[float]]] = None,
                low: Optional[Callable[[float], Optional[float]]] = None) -> Dict[str, Any]:
    """-> {lines: [t], rejected: [{t, why}], main_drop: t | None}. band None = band-free (the atlas
    stores these; the live pick applies the main-drop rule). drums / low: t -> level over the song's
    median bar (drum stem / mix low band), None unmeasured. A line is a strong downbeat when the
    beat is there at the line or at the handover ENTRY_HANDOVER_BARS later (B comes in on the last
    bars of its intro under A's tail and its kick lands as A leaves: Nocturnal 18.79 -> 33.81)."""
    bar = bar if _fin(bar) and bar > 0 else 240.0 / 128
    L = 8 * bar
    ov = (overlap_bars or ENTRY_OVERLAP_BARS) * bar
    raw = [x for x in [include] + list(lines or []) if _fin(x)]
    pts = sorted(x for i, x in enumerate(raw) if next(j for j, y in enumerate(raw) if abs(y - x) < _EPS) == i)
    es = sorted(e for e in (_mean_over(energy_times, energy_curve, p, p + L) for p in lines or [] if _fin(p))
                if e is not None)
    med = es[(len(es) - 1) // 2] if es else None
    main = main_drop_of(drops)
    stop = end if _fin(end) else math.inf
    room = room_s or 0.0

    def strong(x: float) -> Optional[bool]:
        dr = drums(x) if drums else None
        if _fin(dr):
            return dr >= ENTRY_DRUMS_MIN
        lo = low(x) if low else None
        if _fin(lo):
            return lo >= ENTRY_LOW_MIN
        e = _mean_over(energy_times, energy_curve, x, x + L)
        return None if med is None or e is None else e >= ENTRY_QUIET_FRAC * med

    out: List[float] = []
    rejected: List[dict] = []
    for t in pts:
        why = None
        if stop - t < room:
            why = "no room for its play window"
        elif any(r[0] < t + ov and r[1] > t for r in vocals or [] if r):
            why = "B sings in the overlap"
        elif not intro_ok:
            at = strong(t)
            hand = strong(t + L) if t + L < stop else None
            if at is False and hand is not True:
                why = "no beat at the line or the handover (not a strong downbeat)"
        if not why and main and band in ("relaxed", "middle") and t >= main["t"] - _EPS:
            why = "main drop already passed"
        if why:
            rejected.append({"t": t, "why": why})
        else:
            out.append(t)
    return {"lines": out, "rejected": rejected, "main_drop": main["t"] if main else None}


def hash_seed(s: str) -> int:
    """FNV-1a over UTF-16 code units (JS charCodeAt), as autopilot.js hashSeed."""
    h = 0x811C9DC5
    b = str(s).encode("utf-16-le")
    for i in range(0, len(b), 2):
        h ^= b[i] | (b[i + 1] << 8)
        h = (h * 0x01000193) & _M32
    return h


def seeded_rng(seed: int) -> Callable[[], float]:
    """mulberry32, bit-exact with autopilot.js seededRng."""
    state = [seed & _M32]

    def imul(a: int, b: int) -> int:
        return (a * b) & _M32

    def nxt() -> float:
        state[0] = (state[0] + 0x6D2B79F5) & _M32
        a = state[0]
        t = imul(a ^ (a >> 15), 1 | a)
        t = ((t + imul(t ^ (t >> 7), 61 | t)) & _M32) ^ t
        return ((t ^ (t >> 14)) & _M32) / 4294967296

    return nxt


def pick_entry_by_energy(cands: Sequence[dict], set_level: Optional[float]) -> Optional[float]:
    """autopilot.js pickEntryByEnergy: the line whose level is nearest the set, a clear gain only."""
    c = [x for x in cands or [] if x and _fin(x.get("t"))]
    if not c:
        return None
    if not _fin(set_level):
        return c[0]["t"]
    best = c[0]
    bd = abs(best["level"] - set_level) if _fin(best.get("level")) else math.inf
    for x in c[1:]:
        if not _fin(x.get("level")):
            continue
        d = abs(x["level"] - set_level)
        if d < bd - 0.5:
            best, bd = x, d
    return best["t"]


def pick_entry_line(cands: Sequence[dict], set_level: Optional[float],
                    rng: Optional[Callable[[], float]] = None) -> Optional[dict]:
    """Uniform pick among the GOOD lines (window level within ENERGY_MATCH_TOL of the set level; an
    unmeasured line counts as good). None good -> the nearest (pick_entry_by_energy)."""
    c = [x for x in cands or [] if x and _fin(x.get("t"))]
    if not c:
        return None
    good = ([x for x in c if not _fin(x.get("level")) or abs(x["level"] - set_level) <= ENERGY_MATCH_TOL]
            if _fin(set_level) else c)
    if not good:
        return {"t": pick_entry_by_energy(c, set_level), "good": 0, "of": len(c),
                "why": "no line near the set level: nearest"}
    r = rng() if callable(rng) else 0.0
    i = min(len(good) - 1, math.floor(max(0.0, r) * len(good)))
    return {"t": good[i]["t"], "good": len(good), "of": len(c), "why": f"random {i + 1} of {len(good)} good lines"}
