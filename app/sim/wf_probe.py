"""Would the waveform-derived parameters beat the old constants on the tracks of this run?

The played set rarely reaches RIFF x RAP or an ok mashup plan (they are gated by key, tempo and a rap
section), so the `derived_params` events alone are thin. This probe runs the same derivations
(app.music_brain.analysis.waveform_params, keylock.measured_lines) over the synthetic stems of every track the
run loaded, whether or not a move fired on it. The synthetic stems (synth.py) carry the real songs'
per-second stem energy, so a rap's gaps and a stem's loudness are the real ones; their spectra are
not (white noise plus a 60 Hz tone), so band and low-end numbers here only prove plumbing, not taste.

Reports informational metrics only (never in the score): the vocal-gap fit of the rap moves against the
old fixed bars, how far apart the derived guest gains are (the old constant is one value for all), and
how many of the parameters came from a measurement.
"""
from __future__ import annotations

from typing import Optional

from app.music_brain import keylock
from app.music_brain.analysis import waveform_params as wp


def _mean(xs) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def probe(world) -> dict:
    """{fit..., levels, measured, fallback} over the run's tracks (deterministic: sorted by hash)."""
    tracks = []
    for e in sorted(world._tracks.values(), key=lambda x: x.get("hash") or ""):
        files = world._synth.get(e.get("hash")) or {}
        if files.get("stems") and files.get("mix"):
            tracks.append((e, files))
    seen, uniq = set(), []
    for e, f in tracks:
        if e["hash"] not in seen:
            seen.add(e["hash"])
            uniq.append((e, f))
    hold, hold_fixed, drop, drop_fixed = [], [], [], []
    measured = fallback = 0
    for e, f in uniq:                                   # the rap moves, per track as the incoming B
        a = e.get("analysis") or {}
        bpm = float(a.get("bpm") or 0)
        prof = wp.profile(f["stems"]["vocals"])
        if not prof or bpm <= 0:
            continue
        bar = 240.0 / bpm
        for p in a.get("phrase_boundaries_8bar") or []:
            n = 32 if (wp.active_share(prof, p, p + 32 * bar) or 0) >= 0.7 else 16
            if p + (n + 16) * bar > float(a.get("duration") or 0):
                continue
            if (wp.active_share(prof, p, p + n * bar) or 0) < 0.5:
                continue                                # no rap here: the server would not book a riff
            tl, src = keylock.measured_lines(keylock.timeline(n), prof, p, bar)
            fit = tl["fit"]
            hold += fit["hold_active"]
            hold_fixed += fit["hold_active_fixed"]
            if "dropout_active" in fit:
                drop.append(fit["dropout_active"])
                drop_fixed.append(fit["dropout_active_fixed"])
            measured += sum(1 for s in src.values() if s == wp.MEASURED)
            fallback += sum(1 for s in src.values() if s != wp.MEASURED)
            break                                       # the first such entry, as the server takes it
    levels = []
    for (ea, fa), (eb, fb) in zip(uniq, uniq[1:]):      # the mashup gain, adjacent tracks as host / guest
        g = wp.mean_db(wp.profile(fb["stems"]["vocals"]), "voice_db", 0.0, 1e9, active_only=True)
        h = wp.mean_db(wp.profile(fa["mix"]), "voice_db", 0.0, 1e9)
        lvl, s = wp.guest_level(g, h, 0.9)
        levels.append(lvl)
        measured += s == wp.MEASURED
        fallback += s != wp.MEASURED
    spread = None
    if len(levels) > 1:
        m = sum(levels) / len(levels)
        spread = round((sum((x - m) ** 2 for x in levels) / len(levels)) ** 0.5, 3)
    return {"hold_fit": _mean(hold), "hold_fit_fixed": _mean(hold_fixed), "dropout_fit": _mean(drop), "dropout_fit_fixed": _mean(drop_fixed),
            "level_spread": spread, "level_mean": _mean(levels), "tracks": len(uniq), "measured": measured, "fallback": fallback}
