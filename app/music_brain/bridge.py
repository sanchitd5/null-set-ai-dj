"""BRIDGE PATH planner: a BPM ladder for a big tempo / genre gap.

A single Echo Out across 128 -> 174 BPM is the fallback. The set study
(research/notes/set-study-gfF8jzBVWvM.md, section 6 and section 8 item 5)
shows the DnB stretch reached as a gradual multi-minute push: several
beat-matched songs, each a little faster, instead of one jump.

The ladder climbs or falls at most `max_step_pct` per song (every step stays
inside the +/-8% pitch lock of the blend planner, so each step is beat-matched),
and uses half/double-time links where they shorten the path: 87 <-> 174 and
70 <-> 140 are the same pulse ([[Genre Bridge Playbook]] "The Double-Time
Miracle", [[Beatmatching & Tempo]]).

The step count is capped (the occasion steering allows 5-7 bridge songs); a gap
that would need bigger steps than the lock allows is marked infeasible, and the
autopilot falls back to its tempo-jump budget (Echo Out).
"""

from __future__ import annotations

import math
from typing import List

from app.music_brain.blend import MAX_TEMPO_DEVIATION

DEFAULT_STEP_PCT = 6.0
DEFAULT_MAX_STEPS = 7
MIN_BPM, MAX_BPM = 40.0, 240.0


def bridge_ladder(from_bpm: float, to_bpm: float, max_step_pct: float = DEFAULT_STEP_PCT,
                  max_steps: int = DEFAULT_MAX_STEPS) -> dict:
    """BPM ladder from the playing tempo to the target.

    Returns target_bpm (the pulse the ladder ends on: to_bpm, or half/double of
    it), link ("direct" | "double" | "half": how the destination song locks to
    that pulse), steps (BPM of each song, the last one is the destination's
    pulse), step_count, step_pct and feasible.
    """
    if not (MIN_BPM <= from_bpm <= MAX_BPM and MIN_BPM <= to_bpm <= MAX_BPM):
        raise ValueError(f"BPM must be {MIN_BPM:.0f}-{MAX_BPM:.0f}")
    if not 1.0 <= max_step_pct <= MAX_TEMPO_DEVIATION * 100:
        raise ValueError(f"max_step_pct must be 1-{MAX_TEMPO_DEVIATION * 100:.0f}")
    if not 1 <= max_steps <= 12:
        raise ValueError("max_steps must be 1-12")

    # half/double-time links: the destination at 174 is the same pulse as 87
    options = [(to_bpm, "direct"), (to_bpm / 2, "double"), (to_bpm * 2, "half")]
    target, link = min(options, key=lambda o: (abs(math.log(o[0] / from_bpm)), o[1] != "direct"))
    dist = math.log(target / from_bpm)
    lock = math.log1p(MAX_TEMPO_DEVIATION)
    base = {"from_bpm": round(from_bpm, 2), "to_bpm": round(to_bpm, 2),
            "target_bpm": round(target, 2), "link": link,
            "direction": "up" if dist > 0 else "down" if dist < 0 else "same"}
    if abs(dist) <= lock:
        return {**base, "steps": [round(target, 1)], "step_count": 1,
                "step_pct": round((math.exp(abs(dist)) - 1) * 100, 2), "feasible": True,
                "capped": False, "reasons": ["already inside the pitch lock"]}

    n = math.ceil(abs(dist) / math.log1p(max_step_pct / 100.0) - 1e-9)
    capped = n > max_steps
    n = min(n, max_steps)
    ratio = math.exp(dist / n)
    step_pct = (max(ratio, 1 / ratio) - 1) * 100
    feasible = math.log(max(ratio, 1 / ratio)) <= lock + 1e-9
    steps: List[float] = [round(from_bpm * ratio ** i, 1) for i in range(1, n + 1)]
    reasons = []
    if capped:
        reasons.append(f"capped at {max_steps} steps ({step_pct:.1f}% each)")
    if not feasible:
        reasons.append("gap too big for beat-matched steps: tempo jump (Echo Out) instead")
    return {**base, "steps": steps, "step_count": n, "step_pct": round(step_pct, 2),
            "feasible": feasible, "capped": capped, "reasons": reasons}
