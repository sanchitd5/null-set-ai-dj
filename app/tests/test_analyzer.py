"""Unit + integration tests for music_brain.analyzer (Phase 2)."""

import shutil

import numpy as np
import pytest

from app.music_brain.analyzer import (
    KeyEstimate,
    StructureSection,
    analyze,
    compute_downbeats,
    compute_phrase_boundaries,
    detect_camelot_key,
    segment_structure,
    vocal_presence_map,
)
from app.music_brain.config import ANALYSIS_CACHE_DIR, ROOT_DIR

SAMPLE_TRACK = ROOT_DIR / "data" / "songs" / "input.mp3"


# --- Pure-math helpers (no audio needed) -----------------------------------

def test_compute_downbeats_every_fourth_beat():
    beats = np.arange(0, 8, 0.5)  # 16 beats, 0.5s apart
    downbeats = compute_downbeats(beats, beats_per_bar=4)
    assert list(downbeats) == list(beats[::4])


def test_compute_downbeats_empty_input():
    assert len(compute_downbeats(np.array([]))) == 0


def test_compute_phrase_boundaries_32_beats():
    beats = np.arange(0, 64, 1.0)
    boundaries = compute_phrase_boundaries(beats, beats_per_phrase=32)
    assert list(boundaries) == [0.0, 32.0]


def test_segment_structure_labels_intro_and_outro():
    times = np.linspace(0, 100, 100)
    energy = np.concatenate([
        np.linspace(0.1, 0.1, 8),   # flat low intro
        np.linspace(0.5, 0.5, 84),  # flat mid body
        np.linspace(0.1, 0.1, 8),   # flat low outro
    ])
    sections = segment_structure(times, energy, duration=100.0)
    assert sections[0].label == "intro"
    assert sections[-1].label == "outro"


def test_segment_structure_empty_input_returns_empty():
    assert segment_structure(np.array([]), np.array([]), duration=0.0) == []


# --- Camelot key detection (synthetic sine-wave input) ----------------------

def _synthesize_key(freqs, sr=22050, duration=6.0):
    """A simple additive-tone signal to sanity-check chroma correlation runs
    without crashing and returns a plausible KeyEstimate — not a claim about
    perfect pitch detection on synthetic tones."""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    y = sum(np.sin(2 * np.pi * f * t) for f in freqs) / len(freqs)
    return y.astype(np.float32), sr


def test_detect_camelot_key_returns_valid_estimate():
    y, sr = _synthesize_key([261.63, 329.63, 392.00])  # C major triad
    estimate = detect_camelot_key(y, sr)
    assert isinstance(estimate, KeyEstimate)
    assert estimate.camelot[-1] in ("A", "B")
    assert 0.0 <= estimate.confidence <= 1.0


# --- Vocal presence map (synthetic loud/quiet stem) -------------------------

def test_vocal_presence_map_detects_loud_region(tmp_path):
    import soundfile as sf

    sr = 22050
    duration = 6.0
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    y = np.zeros_like(t)
    # Loud tone (well above -38 dBFS) from 2s-4s; silence elsewhere.
    loud_mask = (t >= 2.0) & (t < 4.0)
    y[loud_mask] = 0.8 * np.sin(2 * np.pi * 440 * t[loud_mask])

    stem_path = tmp_path / "vocals.wav"
    sf.write(stem_path, y, sr)

    regions = vocal_presence_map(stem_path, threshold_dbfs=-38.0, hop_seconds=0.25)
    assert len(regions) >= 1
    start, end = regions[0]
    assert 1.5 <= start <= 2.5
    assert 3.5 <= end <= 4.5


def test_vocal_presence_map_silence_yields_no_regions(tmp_path):
    import soundfile as sf

    sr = 22050
    y = np.zeros(sr * 3, dtype=np.float32)
    stem_path = tmp_path / "silence.wav"
    sf.write(stem_path, y, sr)

    regions = vocal_presence_map(stem_path)
    assert regions == []


# --- Full pipeline integration test on a real sample track ------------------

@pytest.mark.skipif(not SAMPLE_TRACK.exists(), reason="sample track not present")
def test_analyze_real_track_end_to_end():
    cache_file = ANALYSIS_CACHE_DIR
    result = analyze(SAMPLE_TRACK, use_cache=False)

    assert result.duration > 0
    assert 40 < result.bpm < 220  # sane BPM range
    assert len(result.beat_times) > 0
    assert len(result.downbeat_times) > 0
    assert len(result.phrase_boundaries_8bar) > 0
    assert result.key is not None
    assert result.key.camelot[-1] in ("A", "B")
    assert len(result.energy_curve) == len(result.energy_times)
    assert len(result.sections) > 0
    assert all(s.end > s.start for s in result.sections)
    # Sections should be contiguous and cover the full duration.
    assert result.sections[0].start == 0.0
    assert result.sections[-1].end == pytest.approx(result.duration)


@pytest.mark.skipif(not SAMPLE_TRACK.exists(), reason="sample track not present")
def test_analyze_uses_disk_cache_on_second_call():
    result_a = analyze(SAMPLE_TRACK, use_cache=True)
    result_b = analyze(SAMPLE_TRACK, use_cache=True)
    assert result_a.bpm == result_b.bpm
    assert result_a.duration == result_b.duration


@pytest.mark.skipif(not SAMPLE_TRACK.exists(), reason="sample track not present")
def test_to_dict_is_json_serializable():
    import json

    result = analyze(SAMPLE_TRACK, use_cache=True)
    json.dumps(result.to_dict())
