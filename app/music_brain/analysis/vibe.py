"""Measured "vibe" features and a track-to-track vibe distance.

The LLM selector reasons about a track from its name; a 4B model cannot hear
it. This module measures what the name cannot tell us: absolute loudness
(RMS dBFS, not peak-normalised), brightness (spectral centroid), percussive
density (onset rate) and mean energy. `vibe_distance` turns the deltas into a
single number plus human-readable reasons so the autopilot can reject a
candidate that would flip the mood before it gets scheduled.

Cached beside the main analysis as `<hash>.vibe.json`.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List

import librosa
import numpy as np

from app.music_brain.analysis.analyzer import SAMPLE_RATE, _file_hash
from app.music_brain.config import ANALYSIS_CACHE_DIR

VIBE_VERSION = 1

# Per-feature scale: a delta equal to the scale counts as 1.0 in the distance.
LOUDNESS_SCALE_DB = 4.0        # reported only: a DJ trims gain, it is not a vibe clash
BRIGHTNESS_SCALE_OCT = 0.6     # whole-track centroid; 0.4 rejected every on-genre pick
ONSET_SCALE_PER_SEC = 2.5      # after a sparse vocal track (4 onsets/s) house sits at 6-7
ENERGY_SCALE = 0.12            # mean of normalised RMS curve
DEFAULT_THRESHOLD = 1.8
# Live set, Fred again.. "Marea" seed: Lane 8 / Ben Böhmer / Yotto were all rejected,
# mainly for "louder by 5-6 dB" (mastering level, fixed with the trim knob).


@dataclass
class VibeFeatures:
    path: str
    version: int
    loudness_dbfs: float       # RMS over the whole track, dBFS
    brightness_hz: float       # mean spectral centroid
    onset_rate: float          # detected onsets per second
    mean_energy: float         # mean of peak-normalised RMS curve (0..1)
    duration: float

    def to_dict(self) -> dict:
        return asdict(self)


def _cache_path_for(audio_path: Path) -> Path:
    return ANALYSIS_CACHE_DIR / f"{_file_hash(audio_path)}.vibe.json"


def analyze_vibe(audio_path: str | Path, use_cache: bool = True) -> VibeFeatures:
    audio_path = Path(audio_path)
    cache_path = _cache_path_for(audio_path)
    if use_cache and cache_path.exists():
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") == VIBE_VERSION:
            return VibeFeatures(**data)

    y, sr = librosa.load(str(audio_path), sr=SAMPLE_RATE, mono=True)
    duration = float(librosa.get_duration(y=y, sr=sr))
    if duration <= 0 or not len(y):
        raise ValueError(f"empty audio: {audio_path}")

    rms_total = float(np.sqrt(np.mean(np.square(y))))
    loudness_dbfs = 20.0 * math.log10(max(rms_total, 1e-9))

    centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    brightness_hz = float(np.mean(centroid)) if len(centroid) else 0.0

    onsets = librosa.onset.onset_detect(y=y, sr=sr, units="time")
    onset_rate = float(len(onsets) / duration)

    frame_rms = librosa.feature.rms(y=y)[0]
    peak = float(frame_rms.max()) if len(frame_rms) else 0.0
    mean_energy = float(np.mean(frame_rms / peak)) if peak > 0 else 0.0

    result = VibeFeatures(
        path=str(audio_path),
        version=VIBE_VERSION,
        loudness_dbfs=loudness_dbfs,
        brightness_hz=brightness_hz,
        onset_rate=onset_rate,
        mean_energy=mean_energy,
        duration=duration,
    )
    if use_cache:
        ANALYSIS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2)
    return result


def vibe_distance(a: VibeFeatures, b: VibeFeatures, threshold: float = DEFAULT_THRESHOLD) -> dict:
    """Compare the track that is playing (a) with a candidate (b).

    Returns {distance, ok, reasons[], components{}, threshold}. Each component
    is a signed delta in units of its scale; the distance is their Euclidean
    norm. A component beyond 1.0 in magnitude earns a reason string.
    """
    d_loud = (b.loudness_dbfs - a.loudness_dbfs) / LOUDNESS_SCALE_DB
    ratio = (b.brightness_hz or 1.0) / max(a.brightness_hz, 1.0)
    d_bright = math.log2(max(ratio, 1e-6)) / BRIGHTNESS_SCALE_OCT
    d_onset = (b.onset_rate - a.onset_rate) / ONSET_SCALE_PER_SEC
    d_energy = (b.mean_energy - a.mean_energy) / ENERGY_SCALE

    components = {
        "loudness": round(d_loud, 2),
        "brightness": round(d_bright, 2),
        "onset_rate": round(d_onset, 2),
        "energy": round(d_energy, 2),
    }
    # Loudness is NOT part of the distance: level differences are mastering, and
    # the deck trim fixes them. It is returned as gain_match_db for the caller.
    distance = math.sqrt(d_bright ** 2 + d_onset ** 2 + d_energy ** 2)
    gain_match_db = round(a.loudness_dbfs - b.loudness_dbfs, 1)

    reasons: List[str] = []
    if abs(d_bright) > 1.0:
        reasons.append(
            f"{'brighter' if d_bright > 0 else 'darker'} tone ({a.brightness_hz:.0f} Hz -> {b.brightness_hz:.0f} Hz)"
        )
    if abs(d_onset) > 1.0:
        reasons.append(
            f"{'busier' if d_onset > 0 else 'sparser'} percussion ({a.onset_rate:.1f} -> {b.onset_rate:.1f} onsets/s)"
        )
    if abs(d_energy) > 1.0:
        reasons.append(
            f"energy {'jump' if d_energy > 0 else 'drop'} too big ({a.mean_energy:.2f} -> {b.mean_energy:.2f})"
        )

    return {
        "distance": round(distance, 2),
        "threshold": threshold,
        "ok": distance <= threshold,
        "reasons": reasons,
        "components": components,
        "gain_match_db": gain_match_db,
        "a": a.to_dict(),
        "b": b.to_dict(),
    }
