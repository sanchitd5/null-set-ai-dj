"""Section-consensus tempo for librosa's beat tracker.

librosa.beat.beat_track on a whole song regularly settles on a wrong metrical
level for most of it: live set, "Cola" read 123 BPM for its first minute and
80.7 (= 2/3) for the rest, so the whole-song estimate was 80.7 and the next
song could not be tempo-locked. Other songs flip to half time in sparse
sections (Marea 61.5 in its intro, Rumble 70 / 92 in its halftime parts).

robust_tempo():
  1. estimate tempo per 30 s window;
  2. keep window readings that hold for >= MIN_SUPPORT of the song;
  3. among those, take the one closest (on a log scale) to dance tempo, 122 BPM
     (a song's real pulse is present in some sections; the misreads are
     metrically related slower/faster pulses);
  4. re-track the full song with that tempo as a strong prior so the beat grid
     (phrases, downbeats) follows it.

Measured on the set's songs: Cola 80.7 -> 123, Marea 123 (not 61.5), Rumble
136-144 (not 70), Lane 8 123 (not 80.7), Jungle 136, Te Estoy Correteando
95.7 (its only reading: a real 96 BPM dembow track stays 96).
"""

from __future__ import annotations

from collections import Counter
from typing import Tuple

import librosa
import numpy as np

WINDOW_S = 30.0
MIN_SUPPORT = 0.15
DANCE_CENTER_BPM = 122.0
BPM_RANGE = (60.0, 200.0)


def _beat_track(y: np.ndarray, sr: int, **kw) -> Tuple[float, np.ndarray]:
    tempo, frames = librosa.beat.beat_track(y=y, sr=sr, units="frames", **kw)
    return float(np.atleast_1d(tempo)[0]), frames


def window_tempos(y: np.ndarray, sr: int, window_s: float = WINDOW_S) -> list:
    n = int(window_s * sr)
    out = []
    for start in range(0, max(1, len(y) - n + 1), n):
        seg = y[start:start + n]
        if len(seg) < n // 2:
            break
        bpm, _ = _beat_track(seg, sr)
        if BPM_RANGE[0] <= bpm <= BPM_RANGE[1]:
            out.append(round(bpm, 1))
    return out


def choose_tempo(readings: list, fallback: float) -> float:
    """Closest-to-dance-tempo reading among those with enough support."""
    if not readings:
        return fallback
    counts = Counter(readings)
    # merge readings within 2% (librosa's tempo bins jitter)
    merged: dict = {}
    for bpm, c in counts.items():
        key = next((k for k in merged if abs(k / bpm - 1) < 0.02), bpm)
        merged[key] = merged.get(key, 0) + c
    total = sum(merged.values())
    supported = [b for b, c in merged.items() if c / total >= MIN_SUPPORT] or [max(merged, key=merged.get)]
    return min(supported, key=lambda b: abs(np.log2(b / DANCE_CENTER_BPM)))


def robust_tempo(y: np.ndarray, sr: int) -> Tuple[float, np.ndarray]:
    """(bpm, beat_times) with section-consensus tempo and a beat grid that follows it."""
    raw_bpm, raw_frames = _beat_track(y, sr)
    chosen = choose_tempo(window_tempos(y, sr), raw_bpm)
    if abs(chosen / raw_bpm - 1) < 0.02:
        return raw_bpm, librosa.frames_to_time(raw_frames, sr=sr)
    bpm, frames = _beat_track(y, sr, start_bpm=chosen, tightness=400)
    if abs(bpm / chosen - 1) > 0.04:
        bpm = chosen  # tracker grid may still wander; report the consensus tempo
    return bpm, librosa.frames_to_time(frames, sr=sr)
