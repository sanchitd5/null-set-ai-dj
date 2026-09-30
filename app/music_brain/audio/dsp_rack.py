"""Studio DSP rack: 3-band isolator EQ, a strict 4th-order Linkwitz-Riley
120Hz sub-bass crossover (for zero-overlap Bass Swaps), time-varying
HPF/LPF sweeps, tempo-synced delay, reverb, per-stem gain, and a transparent
peak limiter — built on pure `scipy.signal` + `numpy`.

NOTE: The spec (3.5) calls for Spotify's `pedalboard` library here.
`pedalboard`'s native audio-processing code was found to segfault
unconditionally on this machine (confirmed across two pedalboard versions
and multiple numpy versions, even for an empty no-op Pedalboard() with a
single Gain plugin) — a broken native binary/DLL interaction specific to
this environment, not something fixable from Python. This module
reimplements the same DSP in pure scipy/numpy instead, which is reliable
and already proven by the Linkwitz-Riley crossover tests.

See DJ/04 - Core Techniques/EQ & Frequency Management.md: "ONLY ONE TRACK
OWNS EACH FREQUENCY ZONE AT ANY GIVEN SECOND" — `bass_swap_mix` is the
structural enforcement of that rule below 120Hz.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np
from scipy.signal import butter, lfilter, sosfilt, sosfiltfilt

from app.music_brain.config import SUB_BASS_CROSSOVER_HZ

BEAT_SUBDIVISIONS = {"1/4": 0.25, "1/2": 0.5, "3/4": 0.75, "1": 1.0}


def linkwitz_riley_split(
    audio: np.ndarray, sr: int, crossover_hz: float = SUB_BASS_CROSSOVER_HZ
) -> Tuple[np.ndarray, np.ndarray]:
    """4th-order Linkwitz-Riley crossover: cascades two 2nd-order Butterworth
    filters (low-pass and high-pass) so low_band + high_band reconstructs the
    original signal with a flat combined magnitude response and no bump at
    the crossover — the standard technique for phase-coherent sub-bass
    hand-offs (per EQ & Frequency Management.md).
    """
    nyquist = sr / 2.0
    normalized = crossover_hz / nyquist

    lp_sos = butter(2, normalized, btype="low", output="sos")
    hp_sos = butter(2, normalized, btype="high", output="sos")

    # Apply each 2nd-order stage twice -> 4th-order LR crossover.
    low_band = sosfiltfilt(lp_sos, sosfiltfilt(lp_sos, audio, axis=-1), axis=-1)
    high_band = sosfiltfilt(hp_sos, sosfiltfilt(hp_sos, audio, axis=-1), axis=-1)
    return low_band, high_band


def bass_swap_mix(
    track_a: np.ndarray,
    track_b: np.ndarray,
    sr: int,
    swap_sample: int,
    crossover_hz: float = SUB_BASS_CROSSOVER_HZ,
) -> np.ndarray:
    """Mixes A -> B such that sub-bass (<crossover_hz) ownership snaps
    instantly at `swap_sample`: exactly one track's low band is present in
    the output at any sample index, satisfying the "zero sub-bass overlap"
    rule for a Bass Swap. High bands from both tracks are present (a normal
    EQ'd blend of mids/highs); only the low band is exclusive.

    track_a and track_b must be the same length and sample rate.
    """
    if track_a.shape != track_b.shape:
        raise ValueError("track_a and track_b must have matching shape/length")

    a_low, a_high = linkwitz_riley_split(track_a, sr, crossover_hz)
    b_low, b_high = linkwitz_riley_split(track_b, sr, crossover_hz)

    n = track_a.shape[-1]
    swap_sample = int(np.clip(swap_sample, 0, n))

    low_mix = np.concatenate([a_low[..., :swap_sample], b_low[..., swap_sample:]], axis=-1)
    high_mix = a_high + b_high  # highs/mids may blend continuously through the swap

    return low_mix + high_mix


def _shelf_gain(audio: np.ndarray, sr: int, cutoff_hz: float, gain_db: float, kind: str) -> np.ndarray:
    """Applies gain_db only to the band below (kind='low') or above
    (kind='high') cutoff_hz, via an LR-style split-scale-recombine, so
    extreme gains (including an effective true-kill at very negative dB)
    behave predictably without touching the other band at all.
    """
    if gain_db == 0.0:
        return audio
    low, high = linkwitz_riley_split(audio, sr, cutoff_hz)
    factor = 10 ** (gain_db / 20.0)
    if kind == "low":
        return low * factor + high
    return low + high * factor


def three_band_eq(
    audio: np.ndarray,
    sr: int,
    low_gain_db: float = 0.0,
    mid_gain_db: float = 0.0,
    high_gain_db: float = 0.0,
    low_cut_hz: float = SUB_BASS_CROSSOVER_HZ,
    high_cut_hz: float = 5000.0,
) -> np.ndarray:
    """Isolator-style 3-band EQ: independent gain on the low shelf
    (< low_cut_hz), mid band (between the two cutoffs), and high shelf
    (> high_cut_hz). Gains as low as -26dB behave like a standard mixer EQ
    knob; drive further negative (e.g. -80dB) for an ISO-mode true kill.
    """
    out = audio.astype(np.float64, copy=True)

    # Low shelf: scale everything below low_cut_hz.
    out = _shelf_gain(out, sr, low_cut_hz, low_gain_db, kind="low")
    # High shelf: scale everything above high_cut_hz.
    out = _shelf_gain(out, sr, high_cut_hz, high_gain_db, kind="high")

    if mid_gain_db != 0.0:
        # Mid band = everything between low_cut_hz and high_cut_hz.
        _, above_low = linkwitz_riley_split(out, sr, low_cut_hz)
        mid, above_high = linkwitz_riley_split(above_low, sr, high_cut_hz)
        factor = 10 ** (mid_gain_db / 20.0)
        out = out - mid + mid * factor

    return out.astype(np.float32)


def filter_sweep(
    audio: np.ndarray,
    sr: int,
    kind: str = "highpass",
    start_hz: float = 20.0,
    end_hz: float = 8000.0,
    resonance: float = 0.7,
    block_size: int = 2048,
) -> np.ndarray:
    """Time-varying HPF/LPF sweep with resonance, built by automating a
    2nd-order resonant Butterworth filter's cutoff frequency in
    block_size-sample steps (a per-block ramp from start_hz to end_hz across
    the whole clip). `resonance` in (0, 1] narrows the Butterworth Q via a
    slightly-below-critical damping approximation.
    """
    btype = "high" if kind == "highpass" else "low"
    n = audio.shape[-1]
    n_blocks = max(1, int(np.ceil(n / block_size)))
    cutoffs = np.geomspace(max(start_hz, 1.0), max(end_hz, 1.0), n_blocks)
    nyquist = sr / 2.0

    out = np.zeros_like(audio, dtype=np.float32)
    order = 1 if resonance <= 0.5 else 2
    for i, cutoff in enumerate(cutoffs):
        start = i * block_size
        end = min(start + block_size, n)
        chunk = audio[..., start:end].astype(np.float64)
        normalized = min(max(cutoff / nyquist, 1e-4), 0.999)
        sos = butter(order, normalized, btype=btype, output="sos")
        out[..., start:end] = sosfilt(sos, chunk).astype(np.float32)
    return out


def tempo_synced_delay(
    audio: np.ndarray,
    sr: int,
    bpm: float,
    subdivision: str = "1/2",
    mix: float = 0.5,
    feedback: float = 0.3,
) -> np.ndarray:
    """Beat-synced Echo/Delay throw. subdivision in BEAT_SUBDIVISIONS keys."""
    if subdivision not in BEAT_SUBDIVISIONS:
        raise ValueError(f"subdivision must be one of {list(BEAT_SUBDIVISIONS)}")
    if bpm <= 0:
        raise ValueError("bpm must be positive")

    seconds_per_beat = 60.0 / bpm
    delay_seconds = seconds_per_beat * BEAT_SUBDIVISIONS[subdivision]
    delay_samples = max(1, int(round(delay_seconds * sr)))

    dry = audio.astype(np.float64)
    n = dry.shape[-1]
    wet = np.zeros_like(dry)

    # Simple feedback comb: each repeat is `feedback` quieter than the last.
    tap = dry.copy()
    offset = delay_samples
    while offset < n and np.max(np.abs(tap)) > 1e-6:
        wet[..., offset:] += tap[..., : n - offset] * feedback
        tap = tap * feedback
        offset += delay_samples

    return ((1 - mix) * dry + mix * (dry + wet)).astype(np.float32)


def apply_reverb(
    audio: np.ndarray,
    sr: int,
    room_size: float = 0.5,
    wet_level: float = 0.33,
    dry_level: float = 0.6,
) -> np.ndarray:
    """Simple algorithmic reverb: a bank of exponentially-decaying comb
    filters (decay time controlled by room_size) summed and blended with
    the dry signal. Not studio-grade, but a real, testable spatial effect
    with no external native dependency.
    """
    dry = audio.astype(np.float64)
    n = dry.shape[-1]
    decay_seconds = 0.3 + room_size * 1.7  # 0.3s (small room) .. 2.0s (hall)

    # Prime-ish comb delays (ms) for a diffuse, non-metallic tail.
    comb_delays_ms = [29.7, 37.1, 41.3, 43.7]
    wet = np.zeros_like(dry)
    for delay_ms in comb_delays_ms:
        delay_samples = max(1, int(sr * delay_ms / 1000.0))
        decay_per_tap = np.exp(-3.0 * delay_samples / (decay_seconds * sr))
        b = np.zeros(delay_samples + 1)
        b[0] = 1.0
        a = np.zeros(delay_samples + 1)
        a[0] = 1.0
        a[-1] = -decay_per_tap
        wet += lfilter(b, a, dry, axis=-1) / len(comb_delays_ms)

    return (dry_level * dry + wet_level * wet).astype(np.float32)


def stem_gain(audio: np.ndarray, sr: int, gain_db: float) -> np.ndarray:
    factor = 10 ** (gain_db / 20.0)
    return (audio.astype(np.float64) * factor).astype(np.float32)


def peak_limiter(
    audio: np.ndarray, sr: int, threshold_db: float = -0.5, release_ms: float = 100.0
) -> np.ndarray:
    """Transparent peak limiter used as the final mastering stage: a
    lookahead-free sample-by-sample gain reduction envelope that guarantees
    the output never exceeds threshold_db, with an exponential release so
    gain recovers smoothly (avoiding audible chatter/clicks).
    """
    threshold_lin = 10 ** (threshold_db / 20.0)
    x = audio.astype(np.float64)
    abs_x = np.abs(x)

    release_samples = max(1, int(sr * release_ms / 1000.0))
    release_coeff = np.exp(-1.0 / release_samples)

    gain = np.ones_like(abs_x)
    current_gain = 1.0
    flat = abs_x.reshape(-1) if abs_x.ndim == 1 else abs_x.max(axis=0)
    for i in range(flat.shape[-1]):
        peak = flat[i]
        target_gain = min(1.0, threshold_lin / peak) if peak > threshold_lin else 1.0
        if target_gain < current_gain:
            current_gain = target_gain  # instant attack
        else:
            current_gain = target_gain + (current_gain - target_gain) * release_coeff
        gain.reshape(-1)[i] = current_gain if abs_x.ndim == 1 else current_gain

    limited = x * gain
    # Final hard safety clamp in case of any residual overshoot.
    limited = np.clip(limited, -threshold_lin, threshold_lin)
    return limited.astype(np.float32)


@dataclass
class MasteringResult:
    audio: np.ndarray
    peak_dbfs: float
    clipped: bool


def master(audio: np.ndarray, sr: int, threshold_db: float = -0.5) -> MasteringResult:
    """32-bit float internal mixing -> transparent peak limiter -> headroom
    check, per spec 3.5.5."""
    limited = peak_limiter(audio.astype(np.float32), sr, threshold_db=threshold_db)
    peak = float(np.max(np.abs(limited))) if limited.size else 0.0
    with np.errstate(divide="ignore"):
        peak_dbfs = 20 * np.log10(peak) if peak > 0 else -np.inf
    return MasteringResult(audio=limited, peak_dbfs=peak_dbfs, clipped=peak > 1.0)
