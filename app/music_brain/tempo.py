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


REPEAT_BARS = 8
REPEAT_HOP = 64          # ~3 ms onset frames at 22.05 kHz: sub-frame lag precision
REPEAT_SEARCH = 0.015    # +/-1.5 % around the candidates
REPEAT_STEP = 0.02       # BPM
REFINE_MAX_DEV = 0.04    # the beat regression must stay on the same metrical level


def _beat_regression_bpm(bpm: float, beat_times) -> float:
    """Tempo from a straight-line fit over the tracked beats (regular run only).
    Beat frames are ~23 ms quantised; hundreds of them average that out."""
    b = np.asarray(beat_times, dtype=float)
    if len(b) < 33:
        return bpm
    g = np.diff(b)
    med = float(np.median(g))
    ok = np.concatenate([[True], (g > 0.8 * med) & (g < 1.2 * med)])
    if ok.sum() < 32:
        return bpm
    period = float(np.polyfit(np.arange(len(b))[ok], b[ok], 1)[0])
    r = 60.0 / period if period > 0 else bpm
    return r if abs(r / bpm - 1) <= REFINE_MAX_DEV else bpm


def _lag_corr(env: np.ndarray, lag: float) -> float:
    i = int(lag)
    if i < 1 or i + 2 >= len(env):
        return -1.0
    f = lag - i
    a = env[: -i - 1]
    b = (1 - f) * env[i:-1] + f * env[i + 1:]
    a = a - a.mean()
    b = b - b.mean()
    den = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / den) if den > 0 else -1.0


def loop_tempo(y: np.ndarray, sr: int, bpm: float, beat_times) -> float:
    """The tempo at which an 8-bar loop of this song repeats seamlessly.

    beat_track reports tempo on its tempogram's discrete bins (123.05, 129.2,
    136.0, 143.55, 172.27 at 22.05 kHz / hop 512), up to ~2.5 % off. Decks
    loop and beatmatch by BPM, so 123.05 for a 125 BPM song puts every loop
    seam ~370 ms off per 8 bars: the hold loop flams, blends drift. Measured on
    30 library songs (2026-09-27): this search made the held-out 16-bar repeat
    correlate better on 29, worse on none, and landed on production tempos
    (174, 130, 125, 124, 140).
    """
    if not bpm or bpm <= 0:
        return bpm
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=REPEAT_HOP)
    if len(env) < 4 * REPEAT_BARS * 240 / bpm * sr / REPEAT_HOP:
        return bpm  # too short to measure a repeat
    cands = [bpm, _beat_regression_bpm(bpm, beat_times)]
    grid = np.arange(min(cands) * (1 - REPEAT_SEARCH), max(cands) * (1 + REPEAT_SEARCH), REPEAT_STEP)
    frames_per_bpm = REPEAT_BARS * 240 * sr / REPEAT_HOP
    scores = [_lag_corr(env, frames_per_bpm / g) for g in grid]
    best = float(grid[int(np.argmax(scores))])
    return round(best, 2) if max(scores) > _lag_corr(env, frames_per_bpm / bpm) else bpm


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
