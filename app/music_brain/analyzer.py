"""Audio analysis: tempo/beatgrid, downbeats, phrase boundaries, Camelot key,
energy curve/structure segmentation, and (when a vocals stem is available)
a ground-truth vocal-presence map.

Pure-librosa/numpy/scipy — no Demucs/torch dependency. The vocal-presence
map is computed from an already-separated vocals stem (see
music_brain/stem_service.py); pass its path in via `vocals_stem_path` to
enable it, otherwise that field comes back empty.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import librosa
import numpy as np

from app.music_brain.config import (
    ANALYSIS_CACHE_DIR,
    BEATS_PER_BAR,
    BEATS_PER_PHRASE,
    VOCAL_PRESENCE_THRESHOLD_DBFS,
)

SAMPLE_RATE = 22050

# Camelot wheel: (pitch_class, is_major) -> Camelot notation.
# Pitch classes: 0=C, 1=C#/Db, 2=D, ... 11=B (librosa chroma convention).
_CAMELOT_MAJOR = {
    0: "8B", 1: "3B", 2: "10B", 3: "5B", 4: "12B", 5: "7B",
    6: "2B", 7: "9B", 8: "4B", 9: "11B", 10: "6B", 11: "1B",
}
_CAMELOT_MINOR = {
    0: "5A", 1: "12A", 2: "7A", 3: "2A", 4: "9A", 5: "4A",
    6: "11A", 7: "6A", 8: "1A", 9: "8A", 10: "3A", 11: "10A",
}

# Krumhansl-Schmuckler key profiles.
_MAJOR_PROFILE = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
)
_MINOR_PROFILE = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
)

PITCH_CLASS_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


@dataclass
class KeyEstimate:
    camelot: str
    key_name: str
    is_major: bool
    confidence: float


@dataclass
class StructureSection:
    label: str  # intro | verse | chorus | breakdown | build | drop | outro
    start: float
    end: float
    energy: float


@dataclass
class TrackAnalysis:
    path: str
    duration: float
    bpm: float
    beat_times: List[float] = field(default_factory=list)
    downbeat_times: List[float] = field(default_factory=list)
    phrase_boundaries_8bar: List[float] = field(default_factory=list)
    phrase_boundaries_16bar: List[float] = field(default_factory=list)
    key: Optional[KeyEstimate] = None
    energy_curve: List[float] = field(default_factory=list)
    energy_times: List[float] = field(default_factory=list)
    sections: List[StructureSection] = field(default_factory=list)
    vocal_active_regions: List[Tuple[float, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def analyze_tempo_and_beats(y: np.ndarray, sr: int) -> Tuple[float, np.ndarray]:
    """Returns (bpm, beat_times). Beat 0 is treated as the downbeat anchor.

    Section-consensus tempo (music_brain/tempo.py): a whole-song beat_track
    read "Cola" (122 BPM) as 80.7 because most of the song sits on a 2/3 pulse.
    """
    from app.music_brain.tempo import robust_tempo

    return robust_tempo(y, sr)


def compute_downbeats(beat_times: np.ndarray, beats_per_bar: int = BEATS_PER_BAR) -> np.ndarray:
    """Every Nth beat starting from beat 0 (assumes 4/4 and that beat 0 is bar 1)."""
    if len(beat_times) == 0:
        return beat_times
    return beat_times[::beats_per_bar]


def compute_phrase_boundaries(beat_times: np.ndarray, beats_per_phrase: int) -> np.ndarray:
    if len(beat_times) == 0:
        return beat_times
    return beat_times[::beats_per_phrase]


def detect_camelot_key(y: np.ndarray, sr: int) -> KeyEstimate:
    """Chroma-based Krumhansl-Schmuckler key estimation mapped to Camelot notation."""
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_mean = chroma.mean(axis=1)

    best_score = -np.inf
    best_pitch_class = 0
    best_is_major = True

    for shift in range(12):
        major_profile = np.roll(_MAJOR_PROFILE, shift)
        minor_profile = np.roll(_MINOR_PROFILE, shift)

        major_corr = np.corrcoef(chroma_mean, major_profile)[0, 1]
        minor_corr = np.corrcoef(chroma_mean, minor_profile)[0, 1]

        if major_corr > best_score:
            best_score = major_corr
            best_pitch_class = shift
            best_is_major = True
        if minor_corr > best_score:
            best_score = minor_corr
            best_pitch_class = shift
            best_is_major = False

    camelot = (_CAMELOT_MAJOR if best_is_major else _CAMELOT_MINOR)[best_pitch_class]
    key_name = f"{PITCH_CLASS_NAMES[best_pitch_class]} {'Major' if best_is_major else 'Minor'}"
    confidence = float(np.clip((best_score + 1) / 2, 0.0, 1.0))
    return KeyEstimate(camelot=camelot, key_name=key_name, is_major=best_is_major, confidence=confidence)


def analyze_energy_curve(y: np.ndarray, sr: int, hop_seconds: float = 1.0) -> Tuple[np.ndarray, np.ndarray]:
    """Returns (times, normalized_rms_energy) sampled every `hop_seconds`."""
    hop_length = int(sr * hop_seconds)
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop_length)
    if rms.max() > 0:
        normalized = rms / rms.max()
    else:
        normalized = rms
    return times, normalized


def segment_structure(times: np.ndarray, energy: np.ndarray, duration: float) -> List[StructureSection]:
    """Heuristic structural segmentation from the normalized energy curve.

    Not a trained classifier — a percentile/derivative heuristic:
    - First 8% of the track: intro
    - Last 8% of the track: outro
    - Sustained multi-window rises: build
    - A window immediately after a build that jumps to a local energy peak: drop
    - Energy below the 30th percentile away from the intro/outro edges: breakdown
    - Everything else: verse (used as a catch-all incl. chorus-strength sections)
    """
    if len(times) == 0:
        return []

    n = len(energy)
    low_thresh = float(np.percentile(energy, 30))
    high_thresh = float(np.percentile(energy, 70))
    intro_end_idx = max(1, int(n * 0.08))
    outro_start_idx = min(n - 1, int(n * 0.92))

    labels = ["verse"] * n
    for i in range(intro_end_idx):
        labels[i] = "intro"
    for i in range(outro_start_idx, n):
        labels[i] = "outro"

    for i in range(intro_end_idx, outro_start_idx):
        if energy[i] < low_thresh:
            labels[i] = "breakdown"
        elif energy[i] > high_thresh:
            # A high-energy window preceded by a sustained rise reads as a drop.
            window_start = max(intro_end_idx, i - 3)
            rising = i > window_start and np.all(np.diff(energy[window_start:i + 1]) >= -1e-6)
            labels[i] = "drop" if rising else "build" if energy[i] > energy[max(0, i - 1)] else "verse"

    sections: List[StructureSection] = []
    current_label = labels[0]
    start_idx = 0
    for i in range(1, n):
        if labels[i] != current_label:
            sections.append(StructureSection(
                label=current_label,
                start=float(times[start_idx]),
                end=float(times[i]),
                energy=float(np.mean(energy[start_idx:i])),
            ))
            current_label = labels[i]
            start_idx = i
    sections.append(StructureSection(
        label=current_label,
        start=float(times[start_idx]),
        end=float(duration),
        energy=float(np.mean(energy[start_idx:n])),
    ))
    return sections


def vocal_presence_map(
    vocals_stem_path: Path,
    threshold_dbfs: float = VOCAL_PRESENCE_THRESHOLD_DBFS,
    hop_seconds: float = 0.5,
) -> List[Tuple[float, float]]:
    """Moving-RMS vocal activity regions from a separated vocals stem.

    Flags windows where vocal RMS > threshold_dbfs as active, then merges
    consecutive active windows into (start, end) region tuples.
    """
    y, sr = librosa.load(str(vocals_stem_path), sr=SAMPLE_RATE, mono=True)
    hop_length = int(sr * hop_seconds)
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop_length)

    with np.errstate(divide="ignore"):
        dbfs = 20 * np.log10(np.maximum(rms, 1e-12))
    active = dbfs > threshold_dbfs

    regions: List[Tuple[float, float]] = []
    region_start: Optional[float] = None
    for i, is_active in enumerate(active):
        if is_active and region_start is None:
            region_start = float(times[i])
        elif not is_active and region_start is not None:
            regions.append((region_start, float(times[i])))
            region_start = None
    if region_start is not None:
        regions.append((region_start, float(times[-1] + hop_seconds)))
    return regions


# Bump when analysis output changes so stale cached results are recomputed.
# v2: section-consensus tempo (tempo.py).
ANALYSIS_VERSION = 2


def _cache_path_for(audio_path: Path) -> Path:
    return ANALYSIS_CACHE_DIR / f"{_file_hash(audio_path)}.v{ANALYSIS_VERSION}.json"


def analyze(
    audio_path: str | Path,
    vocals_stem_path: Optional[str | Path] = None,
    use_cache: bool = True,
) -> TrackAnalysis:
    """Full analysis pipeline for one track, with JSON disk caching."""
    audio_path = Path(audio_path)
    cache_path = _cache_path_for(audio_path)

    if use_cache and cache_path.exists() and vocals_stem_path is None:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        key = KeyEstimate(**data["key"]) if data.get("key") else None
        sections = [StructureSection(**s) for s in data.get("sections", [])]
        data["key"] = key
        data["sections"] = sections
        data["vocal_active_regions"] = [tuple(r) for r in data.get("vocal_active_regions", [])]
        return TrackAnalysis(**data)

    y, sr = librosa.load(str(audio_path), sr=SAMPLE_RATE, mono=True)
    duration = float(librosa.get_duration(y=y, sr=sr))

    bpm, beat_times = analyze_tempo_and_beats(y, sr)
    downbeats = compute_downbeats(beat_times)
    phrases_8 = compute_phrase_boundaries(beat_times, BEATS_PER_PHRASE)
    phrases_16 = compute_phrase_boundaries(beat_times, BEATS_PER_PHRASE * 2)
    key = detect_camelot_key(y, sr)
    energy_times, energy = analyze_energy_curve(y, sr)
    sections = segment_structure(energy_times, energy, duration)

    vocal_regions: List[Tuple[float, float]] = []
    if vocals_stem_path is not None:
        vocal_regions = vocal_presence_map(Path(vocals_stem_path))

    result = TrackAnalysis(
        path=str(audio_path),
        duration=duration,
        bpm=bpm,
        beat_times=[float(t) for t in beat_times],
        downbeat_times=[float(t) for t in downbeats],
        phrase_boundaries_8bar=[float(t) for t in phrases_8],
        phrase_boundaries_16bar=[float(t) for t in phrases_16],
        key=key,
        energy_curve=[float(e) for e in energy],
        energy_times=[float(t) for t in energy_times],
        sections=sections,
        vocal_active_regions=vocal_regions,
    )

    if use_cache and vocals_stem_path is None:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2)

    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m music_brain.analyzer <audio_path>")
        raise SystemExit(1)

    result = analyze(sys.argv[1])
    print(json.dumps(result.to_dict(), indent=2)[:2000])
