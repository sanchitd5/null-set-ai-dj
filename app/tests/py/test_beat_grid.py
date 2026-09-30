"""Downbeat phase + phrase offset: the grid no longer assumes beat 0 is bar 1."""

import numpy as np

from app.music_brain.analyzer import (
    ANALYSIS_VERSION,
    compute_downbeats,
    compute_phrase_boundaries,
    estimate_downbeat_phase,
    estimate_phrase_offset,
)

SR = 22050


def test_offsets_shift_the_grid():
    beats = np.arange(0, 64, 1.0)
    assert list(compute_downbeats(beats, 4, offset=2)[:3]) == [2.0, 6.0, 10.0]
    assert list(compute_phrase_boundaries(beats, 32, offset=8)) == [8.0, 40.0]
    assert list(compute_phrase_boundaries(beats, 32)) == [0.0, 32.0]  # default unchanged


def test_downbeat_phase_follows_the_kick():
    # 120 BPM clicks; every 4th beat from beat 2 carries a loud low kick.
    beat_s = 0.5
    n = 64
    y = np.zeros(int(SR * beat_s * (n + 1)))
    t = np.arange(int(0.08 * SR)) / SR
    for i in range(n):
        start = int(i * beat_s * SR)
        amp, freq = (1.0, 60.0) if i % 4 == 2 else (0.15, 2000.0)
        y[start:start + len(t)] += amp * np.sin(2 * np.pi * freq * t) * np.exp(-t * 30)
    beats = np.arange(n) * beat_s
    assert estimate_downbeat_phase(y, SR, beats) == 2


def test_phrase_offset_energy_vote():
    n = 256
    beats = np.arange(n) * 0.5
    # Sections change every 32 beats starting at beat 8 (an 8-beat pickup).
    level = ((np.arange(n) - 8) // 32) % 2
    beat_energy = np.where(np.arange(n) < 8, -3.0, level * 2.0 - 2.0)
    et = np.arange(0, n * 0.5, 1.0)
    e = np.ones_like(et)
    assert estimate_phrase_offset(beats, 0, et, e, beat_energy=beat_energy) == 8
    # 16-bar lines choose between the two 16-bar candidates of that 8-bar grid.
    off16 = estimate_phrase_offset(
        beats, 0, et, e, beats_per_phrase=64, beat_energy=beat_energy, candidates=[8, 40],
    )
    assert off16 in (8, 40)


def test_phrase_offset_falls_back_to_loudness_anchor():
    n = 256
    beats = np.arange(n) * 0.5
    et = np.arange(0, n * 0.5, 1.0)
    e = np.where(et < 10.2, 0.05, 1.0)  # quiet 10 s fade-in, music from 11 s
    flat = np.zeros(n)  # no energy structure -> no decisive vote
    # First downbeat (phase 1) at or after 11 s: beat 25 (12.5 s) -> 25 % 32.
    assert estimate_phrase_offset(beats, 1, et, e, beat_energy=flat) == 25
    assert estimate_phrase_offset(beats, 1, et, e) == 25


def test_analysis_version_bumped_for_grid():
    assert ANALYSIS_VERSION >= 3
