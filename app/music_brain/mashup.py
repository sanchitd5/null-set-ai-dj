"""Live mashup planner: the vocal of one track over the beat of another.

Fred again.. style "A x B": while track B (host) plays, lay the separated vocal
of track A (guest) over one of B's instrumental phrases, tempo-locked and on
the 8-bar grid.

Grounding (./DJ/):
  [[Live Mashup]], [[Stems Transition]], [[Harmonic Mixing & Camelot System]]
  (key score >= 0.8 or the vocal fights the chords), [[Phrasing & Structure]]
  (vocal phrase and host window both start on an 8-bar boundary),
  [[EQ & Frequency Management]] (the guest layer is vocal-only, so it adds no
  second bass line).

The guest is re-pitched by playbackRate to lock tempo, so the rate must stay
within MAX_RATE_DEVIATION (about +/-0.7 semitone) or the vocal drifts off key.
Half/double time counts as a match (87 BPM vocal over a 174 BPM beat).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from app.music_brain import waveform_params as wp
from app.music_brain.analyzer import TrackAnalysis, vocal_presence_map
from app.music_brain.recipe_matcher import camelot_distance_score

MASHUP_DEMUCS_MODEL = "htdemucs"  # single model: ~4x faster than htdemucs_ft
MAX_RATE_DEVIATION = 0.04
MIN_KEY_SCORE = 0.8
MIN_GUEST_VOCAL_COVERAGE = 0.5
MAX_HOST_VOCAL_COVERAGE = 0.2
ALLOWED_BARS = (8, 16, 32)  # 32 = full mashup: the guest's whole vocal phrase over the host

Regions = List[Tuple[float, float]]


def _coverage(regions: Regions, start: float, end: float) -> float:
    """Fraction of [start, end) covered by the given (start, end) regions."""
    if end <= start:
        return 0.0
    hit = 0.0
    for a, b in regions:
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            hit += hi - lo
    return hit / (end - start)


def tempo_lock(host_bpm: float, guest_bpm: float) -> Optional[Tuple[float, float]]:
    """(playback_rate, guest_multiplier) that locks the guest to the host, or None.

    guest_multiplier in {0.5, 1, 2} covers half/double-time detections.
    """
    if host_bpm <= 0 or guest_bpm <= 0:
        return None
    best = None
    for mult in (1.0, 2.0, 0.5):
        rate = host_bpm / (guest_bpm * mult)
        if best is None or abs(rate - 1) < abs(best[0] - 1):
            best = (rate, mult)
    if abs(best[0] - 1) > MAX_RATE_DEVIATION:
        return None
    return best


def _touches_edge_section(track: TrackAnalysis, start: float, end: float) -> bool:
    """True if [start, end) overlaps the intro or outro anywhere, not just at its start."""
    return any(
        s.label in ("intro", "outro") and s.start < end and s.end > start
        for s in track.sections
    )


FALLBACK_LEVEL = 0.9   # the console's old fixed guest gain (mashup-layer.js LEVEL), used only when nothing was measured


def _layer_mix(host: TrackAnalysis, guest: TrackAnalysis, guest_vocals_path: Callable[[], str], guest_start: float,
               g_len: float, entries: List[float], h_len: float) -> Tuple[List[float], float, dict]:
    """(gain per host entry, high-pass corner Hz, sources) measured from the guest's vocal stem and the host's
    own audio: the guest voice band sits level with the host's voice band at each entry, and the vocal
    leaves the low end to the host down to where the host's own low end stops (never under 120 Hz)."""
    g = wp.profile(guest_vocals_path())
    h = wp.profile(host.path) if getattr(host, "path", None) else None
    g_db = wp.mean_db(g, "voice_db", guest_start, guest_start + g_len, active_only=True)
    levels, srcs = [], set()
    for e in entries:
        lvl, s = wp.guest_level(g_db, wp.mean_db(h, "voice_db", e, e + h_len), FALLBACK_LEVEL)
        levels.append(round(lvl, 3))
        srcs.add(s)
    hp, hp_src = wp.sub_corner_hz((h or {}).get("low_hz90"))
    sources = {"guest_level": wp.FALLBACK if wp.FALLBACK in srcs or not srcs else wp.MEASURED, "hp_hz": hp_src}
    wp.note("mashup_layer", sources)
    return levels, round(hp, 1), sources


def plan_mashup(
    host: TrackAnalysis,
    guest: TrackAnalysis,
    host_vocals_path: Callable[[], str],
    guest_vocals_path: Callable[[], str],
    bars: int = 8,
    host_mutable: bool = False,
) -> dict:
    """Plan a guest-vocal-over-host-beat layer.

    host_vocals_path / guest_vocals_path are called lazily (Demucs is slow), and
    only after the cheap key and tempo checks pass.

    host_mutable: the host deck has live stems, so its own vocal can be muted
    under the guest's (stem mashup). Any host phrase then qualifies; phrases
    that are already instrumental come first.
    """
    if bars not in ALLOWED_BARS:
        raise ValueError(f"bars must be one of {ALLOWED_BARS}")
    reasons: List[str] = []

    lock = tempo_lock(host.bpm, guest.bpm)
    if lock is None:
        reasons.append(f"tempo too far apart ({guest.bpm:.1f} vs {host.bpm:.1f} BPM)")
    host_key = host.key.camelot if host.key else ""
    guest_key = guest.key.camelot if guest.key else ""
    key_score, key_reason = (0.0, "unknown key")
    if host_key and guest_key:
        key_score, key_reason = camelot_distance_score(host_key, guest_key)
    if key_score < MIN_KEY_SCORE:
        reasons.append(f"keys clash ({guest_key} over {host_key}: {key_reason})")
    if reasons:
        return {"ok": False, "reasons": reasons}

    rate, mult = lock
    host_bar = 240.0 / host.bpm
    guest_bar = 240.0 / (guest.bpm * mult)

    guest_regions = vocal_presence_map(Path(guest_vocals_path()))
    g_len = bars * guest_bar
    best_start, best_cov = None, 0.0
    for b in guest.phrase_boundaries_8bar:
        if b + g_len > guest.duration:
            break
        cov = _coverage(guest_regions, b, b + g_len)
        if cov > best_cov:
            best_start, best_cov = b, cov
    if best_start is None or best_cov < MIN_GUEST_VOCAL_COVERAGE:
        return {"ok": False, "reasons": [f"no {bars}-bar vocal phrase in guest (best {best_cov:.0%} vocal)"]}

    host_regions = vocal_presence_map(Path(host_vocals_path()))
    h_len = bars * host_bar
    entries, muted = [], []
    for b in host.phrase_boundaries_8bar:
        if b + h_len > host.duration - 8 * host_bar:
            break
        if _touches_edge_section(host, b, b + h_len):
            continue
        cov = _coverage(host_regions, b, b + h_len)
        if cov <= MAX_HOST_VOCAL_COVERAGE:
            entries.append(round(b, 3))
        elif host_mutable:
            muted.append(round(b, 3))
    mute_host = False
    if not entries and muted:
        entries, mute_host = muted, True
    if not entries:
        return {"ok": False, "reasons": ["host has no instrumental phrase long enough"]}

    levels, hp_hz, sources = _layer_mix(host, guest, guest_vocals_path, best_start, g_len, entries, h_len)
    return {
        "ok": True,
        "reasons": [],
        "level": levels[0] if levels else FALLBACK_LEVEL,      # the guest vocal's gain, per host entry below
        "host_levels": levels,
        "hp_hz": hp_hz,                                       # high-pass corner of the guest vocal
        "param_sources": sources,
        "bars": bars,
        "mute_host_vocals": mute_host,
        "rate": round(rate, 5),
        "semitones": round(12 * math.log2(rate), 3),
        "guest_start": round(best_start, 3),
        "guest_duration": round(g_len, 3),
        "guest_vocal_coverage": round(best_cov, 3),
        "host_entries": entries,
        "host_duration": round(h_len, 3),
        "host_bpm": host.bpm,
        "guest_bpm": guest.bpm * mult,
        "key": f"{guest_key} over {host_key}",
        "key_score": key_score,
    }
