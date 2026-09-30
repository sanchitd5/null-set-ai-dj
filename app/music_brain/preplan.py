"""Pre-planned transitions: the silent ear chooses WHEN and HOW before the master plays it.

User: "the next song can start in the previous song, that's what mixing is; the
ear should pre-plan using waveforms / silent tracks mixed; the master plays
what the ear already pre-planned; bidirectional."

A plan is (a_in, b_start, bars, combo):
  a_in     A's song time where B starts (a phrase line)
  b_start  B's song time at that moment (one of B's own phrase lines)
  bars     how long both records share the air (16 or 32, on A's tempo)
  combo    which deck owns each stem meanwhile (app.music_brain.merge)
then B takes every stem on the handover line a_in + bars.

Both directions are searched: B's intro under A's outro (B starts early, at its
own beginning), and A's tail over B's drop or first vocal (B starts at its big
moment while A plays on). Candidates are scored from the stems' measured energy
per bar (continuity across the handover, no dead air, the chosen stems really
play), the top few are rendered offline and the omni ear rates them (merge.audition
machinery). Nothing here plays; the console executes the winner.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np

from app.music_brain import merge
from app.music_brain.config import CACHE_DIR

PREPLAN_DIR = CACHE_DIR / "preplan"
SR = 11025
OVERLAPS = (16, 32)
MAX_HANDOVERS = 6
EAR_TOP = 4
MIN_LEAD_S = 20.0          # B cannot start sooner than this from now: room to cue it
SHORT_OVERLAPS = (8, 16, 32)   # QUICK-sized windows: a 16-bar overlap alone fits nowhere
SHORT_WINDOW_S = 120.0     # window end (song s) at or under this uses SHORT_OVERLAPS
PLAN_BUDGET_S = 20.0       # rendering + the ear on up to EAR_TOP clips; measured 0.5-16 s, so 40 was 2x too high
UNHEARD_TTL_S = 120.0      # a plan the ear could not hear is reused this long (no re-decode per retry)
_unheard: Dict[str, tuple] = {}


def bar_rms(path: str, bar_s: float, sr: int = SR) -> np.ndarray:
    """RMS per bar of one stem (song time = bar index * bar_s)."""
    from app.ui.services import engine

    y, _ = engine.load_audio(path, sr=sr, mono=True)
    n = int(bar_s * sr)
    k = len(y) // n
    if k == 0:
        return np.zeros(0)
    return np.sqrt(np.mean(np.square(y[:k * n].reshape(k, n)), axis=1))


@__import__("functools").lru_cache(maxsize=64)
def _bar_rms_cached(path: str, mtime: float, bar_s: float) -> tuple:
    return tuple(bar_rms(path, bar_s))


def energies(stems: Dict[str, str], bar_s: float) -> Dict[str, np.ndarray]:
    """Per-bar RMS per stem, decoded once per (file, bar length) for the process."""
    import os

    return {r: np.asarray(_bar_rms_cached(stems[r], os.path.getmtime(stems[r]), round(bar_s, 5)))
            for r in merge.ROLES}


def mean_over(e: Dict[str, np.ndarray], t0: float, t1: float, bar_s: float) -> Dict[str, float]:
    a, b = int(max(0, t0) / bar_s), max(int(max(0, t0) / bar_s) + 1, int(t1 / bar_s))
    return {r: float(np.mean(v[a:b])) if len(v[a:b]) else 0.0 for r, v in e.items()}


def total_over(e: Dict[str, np.ndarray], t0: float, t1: float, bar_s: float) -> float:
    m = mean_over(e, t0, t1, bar_s)
    return float(np.sqrt(sum(v * v for v in m.values())))


def b_starts(b: dict, bar_b: float, dur_b: float) -> List[dict]:
    """B's candidate entry lines: its intro, its first vocal phrase, its first drop."""
    lines = sorted(b.get("phrase_boundaries_8bar") or [0.0])
    out = [{"t": float(lines[0]), "why": "B's intro"}]
    voc = b.get("vocal_active_regions") or []
    if voc:
        v0 = voc[0][0]
        at = max([x for x in lines if x <= v0 + 0.5] or [lines[0]])
        out.append({"t": float(at), "why": "B's first vocal phrase"})
    et, ec = b.get("energy_times") or [], b.get("energy_curve") or []
    if et and ec:
        hi = np.percentile(ec, 75)
        t_hi = next((t for t, c in zip(et, ec) if c >= hi and t > 15), None)
        if t_hi is not None:
            at = min([x for x in lines if x >= t_hi - bar_b] or [lines[-1]])
            out.append({"t": float(at), "why": "B's first drop"})
    seen, uniq = set(), []
    for s in out:
        k = round(s["t"], 1)
        if k not in seen and s["t"] < dur_b * 0.6:
            seen.add(k)
            uniq.append(s)
    return uniq


HIGH_PCT = 85              # A's energy at or above this percentile of the song = its high point
HIGH_OVER_MEDIAN = 0.3     # ... and this share of the song's range above its median
HIGH_LEAD_BARS = 16        # "at its high OR about to reach it": the build into it is protected too


def high_spans(ana: dict, bar_s: float) -> List[tuple]:
    """A's high-energy sections [(t0, t1)] (song s): energy >= its HIGH_PCT percentile,
    joined across gaps under 2 bars, padded HIGH_LEAD_BARS before (the build) and 1 bar after.
    The console's autopilot.js highSpans() is the same rule with numpy's quantiles;
    app/tests/fixtures/rule_vectors.json checks both."""
    et, ec = ana.get("energy_times") or [], ana.get("energy_curve") or []
    if len(et) < 4 or len(et) != len(ec):
        return []
    ec_arr = np.asarray(ec, float)
    # the high must stand out: top percentile AND well above the song's typical level
    # (a record loud from start to end has no "high point" to protect)
    med, rng = float(np.median(ec_arr)), float(ec_arr.max() - ec_arr.min())
    if rng <= 1e-6:
        return []                         # flat: no high point to protect
    thr = max(float(np.percentile(ec_arr, HIGH_PCT)), med + HIGH_OVER_MEDIAN * rng)
    spans: List[list] = []
    for t, c in zip(et, ec):
        if c >= thr and c > med:          # must stand above the song's typical level
            if spans and t - spans[-1][1] <= 2 * bar_s:
                spans[-1][1] = t
            else:
                spans.append([t, t])
    return [(max(0.0, x - HIGH_LEAD_BARS * bar_s), y + bar_s) for x, y in spans]


def in_high(spans: Sequence[tuple], t0: float, t1: float) -> bool:
    return any(t0 < y and t1 > x for x, y in spans)


# S22 breakdown ownership (research/notes/artist-signature-techniques.md): "rushing through breakdowns" is a
# mistake, breakdowns "are emotional peaks" (melodic techno guide, SECONDARY). A breakdown here is a measured
# low: energy at or under the song's BD_PCT percentile AND BD_BELOW_MEDIAN x range under its median (it must
# stand out, as high_spans' highs do), between the song's first and last frame at or above the median (so not
# the intro or outro), joined across gaps up to BD_JOIN_BARS, at least BD_MIN_BARS long. On the analysis cache
# (452 songs): 48 % of songs have one or more, median 25 s long (p90 44 s). The thresholds are GUESSES
# tuned on those counts, not on listening.
BD_PCT = 30
BD_BELOW_MEDIAN = 0.15
BD_JOIN_BARS = 2
BD_MIN_BARS = 8


def breakdown_spans(ana: dict, bar_s: float) -> List[tuple]:
    """A's breakdowns [(t0, t1)] (song s). The console's autopilot.js breakdownSpans() is the same rule;
    app/tests/fixtures/rule_vectors.json checks both."""
    et, ec = ana.get("energy_times") or [], ana.get("energy_curve") or []
    if len(et) < 4 or len(ec) != len(et):
        return []
    ec_arr = np.asarray(ec, float)
    rng = float(ec_arr.max() - ec_arr.min())
    if rng <= 1e-6:
        return []                                    # flat: nothing stands out as a breakdown
    med = float(np.median(ec_arr))
    thr = min(float(np.percentile(ec_arr, BD_PCT)), med - BD_BELOW_MEDIAN * rng)
    loud = [i for i, c in enumerate(ec) if c >= med]
    first, last = et[loud[0]], et[loud[-1]]
    spans: List[list] = []
    for t, c in zip(et, ec):
        if c > thr or t <= first or t >= last:
            continue
        if spans and t - spans[-1][1] <= BD_JOIN_BARS * bar_s:
            spans[-1][1] = t
        else:
            spans.append([t, t])
    hop = et[1] - et[0]
    return [(x, y + hop) for x, y in spans if y + hop - x >= BD_MIN_BARS * bar_s]


def in_breakdown(spans: Sequence[tuple], t: float) -> bool:
    return any(x <= t < y for x, y in spans)


def candidates(a: dict, b: dict, bpm_a: float, bpm_b: float, lo: float, hi: float, now: float,
               eA: Dict[str, np.ndarray], eB: Dict[str, np.ndarray], key_score: Optional[float],
               b_rap: bool = False) -> List[dict]:
    """Every (handover line, overlap, B entry) that fits, with its best combo, scored."""
    bar_a, bar_b = 240.0 / bpm_a, 240.0 / bpm_b
    dur_a, dur_b = float(a.get("duration") or 0), float(b.get("duration") or 0)
    lines_a = [t for t in (a.get("phrase_boundaries_8bar") or []) if lo <= t <= hi]
    highs = high_spans(a, bar_a)          # never transition out of A while A is at its high
    breakdowns = breakdown_spans(a, bar_a)  # S22: never start the blend inside A's breakdown (A owns the room)
    if len(lines_a) > MAX_HANDOVERS:                        # spread over the window
        idx = np.linspace(0, len(lines_a) - 1, MAX_HANDOVERS).round().astype(int)
        lines_a = [lines_a[i] for i in sorted(set(idx))]
    out = []
    overlaps = SHORT_OVERLAPS if hi <= SHORT_WINDOW_S else OVERLAPS
    for h in lines_a:
        for L in overlaps:
            a_in = h - L * bar_a
            if a_in < now + MIN_LEAD_S + PLAN_BUDGET_S or h + 8 * bar_a > dur_a:
                continue
            if in_high(highs, a_in, h + 8 * bar_a):   # B would come in / A leave during A's high
                continue
            if in_breakdown(breakdowns, a_in) or in_breakdown(breakdowns, h):  # B in / A out mid-breakdown
                continue
            for bs in b_starts(b, bar_b, dur_b):
                b_line = bs["t"] + L * bar_b                # B's song time on the handover line
                if b_line + 16 * bar_b > dur_b:
                    continue
                ea, eb = mean_over(eA, a_in, h, bar_a), mean_over(eB, bs["t"], b_line, bar_b)
                ranked = merge.rank(ea, eb, key_score, b_rap=b_rap)
                if not ranked:
                    continue
                best = ranked[0]
                # continuity: B after the line vs A before B came in (a rise is good,
                # a big drop is bad); dead air: the merge must not be quieter than A was
                before = total_over(eA, a_in - 8 * bar_a, a_in, bar_a)
                during = float(np.sqrt(sum((ea if best["combo"][r] == "a" else eb)[r] ** 2 for r in merge.ROLES)))
                after = total_over(eB, b_line, b_line + 8 * bar_b, bar_b)
                rise = (after - before) / max(before, 1e-6)
                sag = min(0.0, (during - before) / max(before, 1e-6))
                score = best["score"] + 15 * float(np.clip(rise, -1, 0.5)) + 20 * sag + (5 if L == 16 else 0)
                direction = "B's intro under A's outro" if bs["why"] == "B's intro" else f"A's tail over {bs['why']}"
                out.append({"a_in": round(a_in, 3), "b_start": round(bs["t"], 3), "handover": round(h, 3), "bars": L,
                            "combo": best["combo"], "label": best["label"], "direction": direction,
                            "score": round(score, 1),
                            "why": best["reasons"] + [f"energy {'rises' if rise > 0 else 'falls'} {abs(rise):.0%} across the line"]
                                   + ([f"merge sags {abs(sag):.0%} under A"] if sag < -0.05 else [])})
    return sorted(out, key=lambda x: -x["score"])


def preplan(a: dict, b: dict, stems_a: Dict[str, str], stems_b: Dict[str, str], bpm_a: float, bpm_b: float,
            lo: float, hi: float, now: float, key_score: Optional[float], b_rap: bool = False,
            ask: Optional[Callable] = None, cache_key: str = "", cache_dir: Path = PREPLAN_DIR) -> dict:
    """{ok, plan, candidates, ear}: the timing + combo the ear liked best."""
    h = hashlib.sha256(json.dumps([cache_key, round(lo), round(hi), round(bpm_a, 1)]).encode()).hexdigest()[:20]
    path = Path(cache_dir) / f"{h}.json"
    import time as _t

    try:
        hit = json.loads(path.read_text(encoding="utf-8"))
        if hit.get("plan") and hit["plan"]["a_in"] >= now + MIN_LEAD_S:
            return hit
    except (OSError, ValueError):
        pass
    soft = _unheard.get(h)
    if soft and _t.time() - soft[0] < UNHEARD_TTL_S and soft[1]["plan"]["a_in"] >= now + MIN_LEAD_S:
        return soft[1]
    eA, eB = energies(stems_a, 240.0 / bpm_a), energies(stems_b, 240.0 / bpm_b)
    cands = candidates(a, b, bpm_a, bpm_b, lo, hi, now, eA, eB, key_score, b_rap)
    if not cands:
        return {"ok": False, "plan": None, "candidates": [], "ear": False,
                "why": "no phrase line in the window fits a merge outside A's energy high"}
    top = cands[:EAR_TOP]
    heard = False
    for c in top:
        clip = merge.render_clip(stems_a, stems_b, c["combo"], c["a_in"], c["b_start"], bpm_a, bpm_b,
                                 bars=min(c["bars"], 16))
        e = merge.ear_rate(merge.wav_bytes(clip), f'{c["label"]}; {c["direction"]}', ask)
        if e:
            heard = True
            c["ear"] = e
            c["score"] = round(c["score"] + (e["score"] - 5.5) * 4, 1)
    top.sort(key=lambda x: -x["score"])
    res = {"ok": True, "plan": top[0], "candidates": top, "ear": heard}
    if heard:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(res), encoding="utf-8")
    else:
        _unheard[h] = (_t.time(), res)
        if len(_unheard) > 64:
            _unheard.pop(min(_unheard, key=lambda k: _unheard[k][0]))
    return res
