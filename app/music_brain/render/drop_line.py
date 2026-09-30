"""The ONE drop-line rule for every vocal-layering planner (Python twin of app/ui/static/drop-line.js).

OWNER RULE "never vocal mix a drop line": no vocal from either deck (acapella over, mashup vocal, riff over rap,
stem merge / hold carrying the other deck's vocal, learned vocal loop / chop / re-cut, artist vocal moves) may play
over a song's drop, or over its drop vocal line (the sung line that runs on into the drop).

NARROW (owner: "more quality mashups and transitions, not less"): only the drop WINDOW counts, the first
DROP_WINDOW_BARS after a drop line, plus the sung line running into it, and only while the layered vocal really
sounds there (sings: at least SING_MIN_S of it). A drop line is found acoustically (blend.drop_lines, the twin of
dj-mind.js dropLines), plus any section-map "drop" label at least MIN_SECTION_DROP_BARS long. The analyzer's
section labels flicker (1-3 s slivers all over a song, see blend.drop_lines): a sliver is not a drop.

Both twins run the same cases (app/tests/fixtures/drop_line_cases.json) so they cannot drift.
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence, Tuple

from app.music_brain.render.blend import drop_lines, track_drop_lines  # noqa: F401 -- drop_lines re-exported

MIN_SECTION_DROP_BARS = 2.0
DROP_WINDOW_BARS = 2.0        # the drop window after a drop line (= drop-line.js DROP_WINDOW_BARS)
SING_MIN_S = 1.0              # layered vocal seconds inside the drop window that refuse (= drop-line.js SING_MIN_S)
EPS = 1e-6

Span = Tuple[float, float]
Sings = Optional[Callable[[float, float], float]]


def _get(a: Any, k: str, default=None):
    if a is None:
        return default
    if isinstance(a, dict):
        return a.get(k, default)
    return getattr(a, k, default)


def _fin(x) -> bool:
    return isinstance(x, (int, float)) and x == x and x not in (float("inf"), float("-inf"))


def drop_hit(drops: Sequence, t0: float, t1: float) -> Optional[Span]:
    """The first drop span overlapping [t0, t1), else None."""
    for x in drops or []:
        if isinstance(x, (list, tuple)) and len(x) >= 2 and min(t1, x[1]) - max(t0, x[0]) > EPS:
            return (x[0], x[1])
    return None


def drop_vocal_line(drops: Sequence, vocals: Optional[Sequence], t0: float, at: float) -> Optional[Span]:
    """A vocal region live inside [t0, at) that runs on past `at` into a drop starting at `at`."""
    if not any(isinstance(x, (list, tuple)) and abs(x[0] - at) < 1e-3 for x in drops or []):
        return None
    for r in vocals or []:
        if isinstance(r, (list, tuple)) and len(r) >= 2 and r[1] > at + 1e-3 and r[0] < at and r[1] > t0:
            return (r[0], r[1])
    return None


def drop_line_busy(drops: Optional[Sequence], vocals: Optional[Sequence], t0: float, t1: float,
                   sings: Sings = None) -> Optional[dict]:
    """A vocal layered over [t0, t1) of this song: None when clear, else {gate, reason}.
    sings(lo, hi) -> seconds the layered vocal sings inside [lo, hi) on this song's clock; None: the move is
    the vocal itself (a chop, a loop), so it always sounds.
    drops None = no drop map: nothing can be ruled out, so it refuses ("unmeasured")."""
    if drops is None or not isinstance(drops, (list, tuple)):
        return {"gate": "unmeasured", "reason": "no drop map: cannot rule out a drop line"}

    def sounds(lo: float, hi: float) -> bool:
        return hi - lo > EPS and (sings is None or sings(lo, hi) >= min(SING_MIN_S, hi - lo) - EPS)

    for x in drops:
        if isinstance(x, (list, tuple)) and len(x) >= 2 and sounds(max(t0, x[0]), min(t1, x[1])):
            return {"gate": "drop_line", "reason": f"the vocal sings over the drop at {x[0]:.1f} s: never vocal mix a drop line"}
    vl = drop_vocal_line(drops, vocals, t0, t1)
    if vl and sounds(max(t0, vl[0]), t1):
        return {"gate": "drop_line",
                "reason": f"the sung line {vl[0]:.1f}-{vl[1]:.1f} s runs into the drop: never vocal mix a drop line"}
    return None


def mapped_sings(regions: Optional[Sequence], t0: float, at0: float, ratio: float) -> Callable[[float, float], float]:
    """sings() for a vocal from another song laid over this one (drop-line.js mappedSings): that song's regions,
    this song's s -> that song's s = at0 + (u - t0) * ratio (ratio = that song's seconds per this song's second)."""
    def f(lo: float, hi: float) -> float:
        a, b = at0 + (lo - t0) * ratio, at0 + (hi - t0) * ratio
        s = sum(max(0.0, min(b, r[1]) - max(a, r[0])) for r in regions or [] if isinstance(r, (list, tuple)) and len(r) >= 2)
        return s / ratio if ratio > 0 else 0.0
    return f


def drop_spans(analysis: Any, bpm: Optional[float] = None) -> List[Span]:
    """Drop windows of one analysed song (dict or TrackAnalysis): the first DROP_WINDOW_BARS after each energy
    drop line and each section-map "drop" label at least MIN_SECTION_DROP_BARS long."""
    bpm = bpm or _get(analysis, "bpm") or 128.0
    bar = 240.0 / float(bpm)
    w = DROP_WINDOW_BARS * bar
    out: List[Span] = []
    for s in _get(analysis, "sections") or []:
        label, st, en = _get(s, "label"), _get(s, "start"), _get(s, "end")
        if "drop" in str(label or "").lower() and _fin(st) and _fin(en) and en - st >= MIN_SECTION_DROP_BARS * bar - EPS:
            out.append((float(st), float(min(en, st + w))))
    for t, _, _ in track_drop_lines(analysis, bar):  # v6 drops, else the v5 energy rule
        out.append((float(t), float(t) + w))
    return out


def vocal_regions(analysis: Any) -> List[Span]:
    return [tuple(r[:2]) for r in (_get(analysis, "vocal_active_regions") or []) if isinstance(r, (list, tuple)) and len(r) >= 2]


def song_busy(analysis: Any, t0: float, t1: float, bpm: Optional[float] = None, sings: Sings = None) -> Optional[dict]:
    """drop_line_busy over one analysed song's own drops and vocal regions."""
    if analysis is None:
        return drop_line_busy(None, None, t0, t1)
    return drop_line_busy(drop_spans(analysis, bpm), vocal_regions(analysis), t0, t1, sings)

