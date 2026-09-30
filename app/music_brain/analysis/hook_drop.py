"""Where to go acapella on a song's emotional hook and make the drop yourself.

The move (learned kind `acapella_drop`, see set_learner.acapella_drops): pull
drums and bass under one sung line, let the voice hang alone, slam the beat
back in on the next phrase line. It lands when three things line up:

1. the line is the hook: the most repeated lyric, the one the room sings back
   (lyrics.hooks); a verse line held alone is just a gap;
2. the line ends on (or within a bar of) an 8-bar phrase boundary, so the
   beat returns on the one ([[Phrasing & Structure]]);
3. the song itself rises after that boundary (energy after > energy before),
   so the DJ's drop and the record's own drop are the same moment.

plan() scores every hook line on those, and takes the hold length from the
sets studied so far (the median `held_s` of learned acapella drops) when there
are any. Pure function: the server feeds it analysis + aligned lyrics.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np

from app.music_brain.analysis.lyrics import hooks

DEFAULT_HOLD_BARS = 2          # voice alone for 2 bars when no set has taught otherwise
MAX_HOLD_BARS = 8
MAX_SNAP_BARS = 1.0            # line end must sit this close to a phrase boundary
EARLY_DROP_BARS = 0.5          # the beat may land this much before the line ends (on its last word), no earlier


def learned_hold_s(store: Optional[Dict[str, dict]]) -> Optional[float]:
    obs = ((store or {}).get("acapella_drop") or {}).get("observations") or []
    held = [o.get("detail", {}).get("held_s") for o in obs if o.get("detail", {}).get("hook")] or \
           [o.get("detail", {}).get("held_s") for o in obs]
    held = [h for h in held if h]
    return float(np.median(held)) if held else None


def _energy(times: Sequence[float], curve: Sequence[float], t0: float, t1: float) -> Optional[float]:
    v = [c for t, c in zip(times, curve) if t0 <= t < t1]
    return float(np.mean(v)) if v else None


def plan(lines: List[dict], bpm: float, boundaries: Sequence[float], energy_times: Sequence[float] = (),
         energy_curve: Sequence[float] = (), learned: Optional[Dict[str, dict]] = None, top_n: int = 3,
         ai_lines: Sequence[dict] = ()) -> List[dict]:
    """Best acapella-drop points, best first:
    [{line_t, line_end, text, count, cut_at, drop_at, hold_s, energy_rise, score, why[]}]
    cut_at = drums + bass out; drop_at = back in (a phrase boundary). [] with no hook.
    ai_lines: set_ai.emotional_lines() picks [{text, intensity, why}]: candidates even
    when sung once, weighted by the model's intensity."""
    if not lines or bpm <= 0 or not boundaries:
        return []
    bar = 4 * 60.0 / bpm
    hold = learned_hold_s(learned)
    hold_src = "learned from studied sets" if hold else f"default {DEFAULT_HOLD_BARS} bars"
    # learned holds are seconds at another song's tempo: whole bars here, so the beat
    # leaves on a bar line (drop_at is a phrase boundary)
    hold = float(np.clip(round((hold or DEFAULT_HOLD_BARS * bar) / bar), 1, MAX_HOLD_BARS)) * bar
    from app.music_brain.analysis.lyrics import _norm

    hk = hooks(lines)[:3]
    ai = {_norm(x["text"]): x for x in ai_lines if x.get("text")}
    for key, x in ai.items():                      # an emotional line sung once is still a candidate
        if not any(_norm(h["text"]) == key for h in hk):
            ts = [l["t"] for l in lines if _norm(l["text"]) == key]
            if ts:
                hk.append({"text": x["text"], "count": len(ts), "times": ts})
    if not hk:
        return []
    top = max(h["count"] for h in hk)
    b = np.asarray(sorted(boundaries), float)
    out = []
    for h in hk:
        pick = ai.get(_norm(h["text"]))
        for t in h["times"]:
            line = next(l for l in lines if l["t"] == t)
            # never mid-line, and never at or before the line's own start (a line shorter
            # than EARLY_DROP_BARS would otherwise drop before the voice is alone: hold <= 0)
            i = int(np.searchsorted(b, line["end"] - EARLY_DROP_BARS * bar))
            while i < len(b) and b[i] <= line["t"]:
                i += 1
            if i >= len(b):
                continue
            drop_at = float(b[i])
            snap = abs(drop_at - line["end"]) / bar
            if snap > MAX_SNAP_BARS:
                continue
            cut_at = max(line["t"], drop_at - hold)
            before = _energy(energy_times, energy_curve, cut_at - 8 * bar, cut_at)
            after = _energy(energy_times, energy_curve, drop_at, drop_at + 8 * bar)
            rise = (after - before) if before is not None and after is not None else 0.0
            if ai:                                   # the model's read of the words carries weight
                score = (30 * h["count"] / top + 30 * (pick["intensity"] / 10 if pick else 0)
                         + 25 * (1 - snap / MAX_SNAP_BARS) + 15 * float(np.clip(rise * 4, 0, 1)))
            else:
                score = 50 * h["count"] / top + 30 * (1 - snap / MAX_SNAP_BARS) + 20 * float(np.clip(rise * 4, 0, 1))
            why = ([f"AI: {pick['why']} (intensity {pick['intensity']}/10)"] if pick else []) + [
                   f"sung {h['count']}x", f"ends {snap:.2f} bars from the phrase line at {drop_at:.1f}s",
                   f"energy {'rises' if rise > 0 else 'does not rise'} after the drop ({rise:+.2f})",
                   f"voice alone {drop_at - cut_at:.1f}s ({hold_src})"]
            out.append({"line_t": line["t"], "line_end": line["end"], "text": line["text"], "count": h["count"],
                        "cut_at": round(cut_at, 2), "drop_at": round(drop_at, 2), "hold_s": round(drop_at - cut_at, 2),
                        "energy_rise": round(rise, 3), "score": round(score, 1), "ai": bool(pick), "why": why})
    out.sort(key=lambda x: -x["score"])
    return out[:top_n]


# ------------------------------------------------------------------- render
OTHER_DUCK = 0.35              # synths/top stay faintly under the voice (the strip & rebuild "voice nearly alone")
FADE_BARS = 0.25               # drums + bass leave over a quarter bar; they come back instantly (the slam)


def render(stems: Dict[str, str], item: dict, bpm: float, out_path, pre_s: float = 16.0, post_s: float = 16.0,
           with_original: bool = True) -> dict:
    """Audition one plan() item from the song's own 4 stems: the section around it with
    drums + bass pulled under the line and slammed back on drop_at. Writes out_path (WAV)
    and, with_original, '<out>_original.wav' of the same span untouched, for A/B."""
    import soundfile as sf
    from pathlib import Path

    need = ("vocals", "drums", "bass", "other")
    if any(n not in stems for n in need):
        raise ValueError("needs the 4 stems (vocals, drums, bass, other)")
    data, sr = {}, None
    for n in need:
        y, r = sf.read(stems[n], always_2d=True)
        if sr is not None and r != sr:
            raise ValueError("stems at different sample rates")
        sr, data[n] = r, y
    n_all = min(len(y) for y in data.values())
    cut, drop = float(item["cut_at"]), float(item["drop_at"])
    if not (0.0 <= cut < drop < n_all / sr):
        raise ValueError(f"cut_at {cut} / drop_at {drop} must satisfy 0 <= cut < drop < song length")
    t0 = max(0.0, item["cut_at"] - pre_s)
    t1 = min(n_all / sr, item["drop_at"] + post_s)
    a, b = int(t0 * sr), int(t1 * sr)
    t = np.arange(a, b) / sr
    bar = 4 * 60.0 / bpm if bpm > 0 else 2.0
    fade = FADE_BARS * bar
    beat = np.clip((cut - t) / fade, 0, 1)                     # 1 -> 0 over the fade ending at cut
    beat = np.where(t >= drop, 1.0, beat)
    beat = np.where((t >= drop) & (t < drop + 0.005), (t - drop) / 0.005, beat)   # 5 ms: no click
    top = np.where((t >= cut) & (t < drop), OTHER_DUCK, 1.0)
    k = max(1, int(0.02 * sr))                                  # 20 ms soft edges, no fade at the file ends
    top = np.convolve(np.pad(top, (k // 2, k - 1 - k // 2), mode="edge"), np.ones(k) / k, mode="valid")
    seg = {n: data[n][a:b] for n in need}
    mix = seg["vocals"] + (seg["drums"] + seg["bass"]) * beat[:, None] + seg["other"] * top[:, None]
    orig = sum(seg.values())
    peak = max(np.abs(mix).max(), np.abs(orig).max(), 1e-9)
    g = min(1.0, 0.98 / peak)                                  # same gain on both: a fair A/B
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(out_path, mix * g, sr)
    res = {"path": str(out_path), "span": [round(t0, 2), round(t1, 2)], "cut_in_file": round(cut - t0, 2),
           "drop_in_file": round(drop - t0, 2)}
    if with_original:
        op = out_path.with_name(out_path.stem + "_original.wav")
        sf.write(op, orig * g, sr)
        res["original"] = str(op)
    return res
