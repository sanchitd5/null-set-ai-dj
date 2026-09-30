"""Audio analysis: tempo/beatgrid, downbeats, phrase boundaries, Camelot key,
energy curve/structure segmentation, and (when a vocals stem is available)
a ground-truth vocal-presence map.

Pure-librosa/numpy/scipy — no Demucs/torch dependency. The vocal-presence
map is computed from an already-separated vocals stem (see
music_brain/audio/stem_service.py); pass its path in via `vocals_stem_path` to
enable it, otherwise that field comes back empty.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import librosa
import numpy as np

from app.music_brain.config import (
    ANALYSIS_CACHE_DIR,
    BEATS_PER_BAR,
    BEATS_PER_PHRASE,
    DEMUCS_MODEL,
    STEMS_CACHE_DIR,
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
    # v6 (analysis/structure.py): phrase-grid drops, the main one, and how they were found.
    # Empty / None on a v5 record: readers fall back to blend.drop_lines on the energy curve.
    drops: List[dict] = field(default_factory=list)
    main_drop: Optional[dict] = None
    structure: Optional[dict] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _file_hash(path: Path) -> str:
    """Content hash of an audio file, through the installed engine's host."""
    from app.ui.services import engine

    return engine.current().host.file_hash(path)


def _file_hash_impl(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def analyze_tempo_and_beats(y: np.ndarray, sr: int) -> Tuple[float, np.ndarray]:
    """Returns (bpm, beat_times). Bar/phrase phase comes from estimate_downbeat_phase.

    Section-consensus tempo (music_brain/analysis/tempo.py): a whole-song beat_track
    read "Cola" (122 BPM) as 80.7 because most of the song sits on a 2/3 pulse.
    """
    from app.music_brain.analysis.tempo import loop_tempo, robust_tempo

    bpm, beat_times = robust_tempo(y, sr)
    return loop_tempo(y, sr, bpm, beat_times), beat_times


def compute_downbeats(
    beat_times: np.ndarray, beats_per_bar: int = BEATS_PER_BAR, offset: int = 0,
) -> np.ndarray:
    """Every Nth beat starting at beat index `offset` (4/4; see estimate_downbeat_phase)."""
    if len(beat_times) == 0:
        return beat_times
    return beat_times[offset % beats_per_bar::beats_per_bar]


def compute_phrase_boundaries(
    beat_times: np.ndarray, beats_per_phrase: int, offset: int = 0,
) -> np.ndarray:
    """Every Nth beat starting at beat index `offset` (the phrase anchor's index mod N)."""
    if len(beat_times) == 0:
        return beat_times
    return beat_times[offset % beats_per_phrase::beats_per_phrase]


_GRID_HOP = 512


def _norm(x: np.ndarray) -> np.ndarray:
    m = float(np.mean(x)) if len(x) else 0.0
    return x / m if m > 0 else x


def estimate_downbeat_phase(
    y: np.ndarray, sr: int, beat_times: np.ndarray, beats_per_bar: int = BEATS_PER_BAR,
) -> int:
    """Which beat offset (0..beats_per_bar-1) carries bar 1.

    The tracker's beat 0 is wherever it locked on, often not a bar line. Two
    cues land on downbeats: low-band onsets (bar-1 kicks and crashes hit
    hardest) and harmonic change (chords move on the bar). Both are summed at
    beat_times[offset::4]; the strongest offset wins.
    """
    n = len(beat_times)
    if n < beats_per_bar * 4:
        return 0
    frames = librosa.time_to_frames(np.asarray(beat_times), sr=sr, hop_length=_GRID_HOP)
    low = librosa.onset.onset_strength(y=y, sr=sr, hop_length=_GRID_HOP, fmax=150.0, n_mels=32)
    frames = np.clip(frames, 0, len(low) - 1)
    low_at = np.array([low[max(0, f - 2):f + 3].max() for f in frames])  # tracker jitter
    chroma = librosa.feature.chroma_stft(y=y, sr=sr, hop_length=_GRID_HOP)
    sync = librosa.util.sync(chroma, frames, aggregate=np.median)  # col i+1 = beat i..i+1
    sync = sync / (np.linalg.norm(sync, axis=0, keepdims=True) + 1e-9)
    change = np.zeros(n)
    k = min(n, sync.shape[1] - 1)
    if k > 1:
        change[1:k] = 1.0 - np.sum(sync[:, 2:k + 1] * sync[:, 1:k], axis=0)
    cue = _norm(low_at) + _norm(change)
    scores = [float(np.mean(cue[o::beats_per_bar])) for o in range(beats_per_bar)]
    return int(np.argmax(scores))


def beat_log_energy(y: np.ndarray, sr: int, beat_times: np.ndarray) -> np.ndarray:
    """Mean log RMS of each beat (beat i to beat i+1; the last beat to the end)."""
    if len(beat_times) == 0:
        return np.zeros(0)
    rms = librosa.feature.rms(y=y, hop_length=_GRID_HOP)[0]
    log_rms = np.log(rms + 1e-5)
    frames = np.clip(
        librosa.time_to_frames(np.asarray(beat_times), sr=sr, hop_length=_GRID_HOP),
        0, len(log_rms) - 1,
    )
    ends = np.append(frames[1:], len(log_rms))
    return np.array([
        log_rms[f:e].mean() if e > f else log_rms[f] for f, e in zip(frames, ends)
    ])


def _energy_jumps(beat_energy: np.ndarray, w: int = BEATS_PER_BAR) -> np.ndarray:
    """|mean of the next w beats - mean of the previous w| at every beat."""
    n = len(beat_energy)
    jumps = np.zeros(n)
    if n <= 2 * w:
        return jumps
    c = np.concatenate([[0.0], np.cumsum(beat_energy)])
    i = np.arange(w, n - w + 1)
    jumps[i] = np.abs((c[i + w] - c[i]) - (c[i] - c[i - w])) / w
    return jumps


# A phrase-offset energy vote must beat the other offsets by this many
# standard deviations; flatter songs fall back to the loudness anchor.
PHRASE_VOTE_MIN_Z = 1.5


def estimate_phrase_offset(
    beat_times: np.ndarray,
    downbeat_phase: int,
    energy_times: np.ndarray,
    energy: np.ndarray,
    beats_per_bar: int = BEATS_PER_BAR,
    beats_per_phrase: int = BEATS_PER_PHRASE,
    beat_energy: Optional[np.ndarray] = None,
    candidates: Optional[List[int]] = None,
) -> int:
    """Beat index (mod beats_per_phrase) where phrases start.

    1. Energy vote (when `beat_energy` is given): sections change on phrase
       lines, so the offset whose lines carry the largest energy jumps wins,
       if it stands out by PHRASE_VOTE_MIN_Z. `candidates` restricts the vote
       (16-bar phrases choose between the two 16-bar lines of an 8-bar grid)
       and then skips the z test. On the 93-song library, held-out (choose on
       the first half, score on the second) this beat both beat 0 and the
       loudness anchor alone.
    2. Loudness anchor: the first downbeat at or after the first energy
       window above 0.2 * max (the music proper, not silence or a fade-in).
    """
    beat_times = np.asarray(beat_times)
    n = len(beat_times)
    phase = downbeat_phase % beats_per_bar
    if n == 0:
        return phase
    if beat_energy is not None and len(beat_energy) >= 2 * beats_per_phrase:
        jumps = _energy_jumps(np.asarray(beat_energy, dtype=float), beats_per_bar)
        offs = list(candidates) if candidates else list(range(beats_per_phrase))
        scores = np.array([jumps[o % beats_per_phrase::beats_per_phrase].mean() for o in offs])
        best = int(np.argmax(scores))
        if candidates:
            return offs[best] % beats_per_phrase
        z = (scores[best] - scores.mean()) / (scores.std() + 1e-9)
        if z >= PHRASE_VOTE_MIN_Z:
            return offs[best] % beats_per_phrase
    start_t = 0.0
    if len(energy):
        loud = np.nonzero(np.asarray(energy) > 0.2 * float(np.max(energy)))[0]
        if len(loud):
            start_t = float(energy_times[loud[0]])
    downbeat_idx = np.arange(phase, n, beats_per_bar)
    after = downbeat_idx[beat_times[downbeat_idx] >= start_t - 1e-6]
    anchor = int(after[0]) if len(after) else (int(downbeat_idx[0]) if len(downbeat_idx) else 0)
    return anchor % beats_per_phrase


def detect_camelot_key(y: np.ndarray, sr: int) -> KeyEstimate:
    """Chroma-based Krumhansl-Schmuckler key estimation mapped to Camelot notation.

    Tuning is estimated first (detuned masters otherwise smear across two pitch
    classes), and the first and last 8% are dropped from the chroma mean
    (intros/outros are often drums-only or in a different key).
    """
    tuning = float(librosa.estimate_tuning(y=y, sr=sr))
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, tuning=tuning)
    n = chroma.shape[1]
    trim = int(n * 0.08)
    if n - 2 * trim >= 1:
        chroma = chroma[:, trim:n - trim]
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
    from app.ui.services import engine

    y, sr = engine.load_audio(str(vocals_stem_path), sr=SAMPLE_RATE, mono=True)
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
# v3: downbeat phase + phrase offset (grid no longer assumes beat 0 is bar 1).
# v4: key chroma tuning-corrected, first/last 8% trimmed.
# v5: bpm = tempo at which an 8-bar loop repeats (tempo.loop_tempo), not the
#     tempogram bin (loops and blends drifted ~150 ms per 8 bars).
# v6: sections on the 8-bar phrase grid, a `drops` list and `main_drop`
#     (analysis/structure.py, drums+bass stems when cached). Derived from the v5
#     record alone, so v5 -> v6 needs no audio decode of the mix.
ANALYSIS_VERSION = 6


def _stems_for(digest: str) -> Optional[dict]:
    """{name: path} of the cached Demucs stems for a content hash, or None."""
    from app.music_brain.audio.audio_io import read_manifest

    try:
        return read_manifest(STEMS_CACHE_DIR / f"{digest}_{DEMUCS_MODEL}")
    except Exception:  # noqa: BLE001 -- a broken stem cache falls back to the mix rule
        return None


def _refine(data: dict, digest: str) -> dict:
    """v5-shaped record -> v6 record (structure.refine), in place."""
    from app.music_brain.analysis import structure

    return structure.refine(data, _stems_for(digest))


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def _cache_path_for(audio_path: Path, version: int = ANALYSIS_VERSION, digest: Optional[str] = None) -> Path:
    return ANALYSIS_CACHE_DIR / f"{digest or _file_hash(audio_path)}.v{version}.json"


def _from_dict(data: dict) -> "TrackAnalysis":
    data = dict(data)
    data["key"] = KeyEstimate(**data["key"]) if data.get("key") else None
    data["sections"] = [StructureSection(**s) for s in data.get("sections", [])]
    data["vocal_active_regions"] = [tuple(r) for r in data.get("vocal_active_regions", [])]
    return TrackAnalysis(**data)


# v4 -> v5 only changed the bpm (tempo.loop_tempo). A song analysed before
# must not be re-analysed from scratch: the v4 record answers at once and a
# background worker refines just its tempo (~1 s) into a v5 record.
_mem_index: dict = {}          # (path, size, mtime_ns) -> TrackAnalysis
_upgrade_lock = threading.Lock()
_upgrade_queue: List[Path] = []
_upgrade_started = False


def upgrade_to_current(audio_path: Path) -> bool:
    """Older record -> current one. v5 -> v6 is structure only (no mix decode);
    v4 also refines the tempo (~1 s). True when a current record now exists."""
    audio_path = Path(audio_path)
    digest = _file_hash(audio_path)
    new = _cache_path_for(audio_path, digest=digest)
    if new.exists():
        return True
    v5, v4 = _cache_path_for(audio_path, 5, digest), _cache_path_for(audio_path, 4, digest)
    if v5.exists():
        with open(v5, "r", encoding="utf-8") as f:
            data = json.load(f)
    elif v4.exists():
        from app.music_brain.analysis.tempo import loop_tempo

        with open(v4, "r", encoding="utf-8") as f:
            data = json.load(f)
        y, sr = librosa.load(str(audio_path), sr=SAMPLE_RATE, mono=True)
        data["bpm"] = float(loop_tempo(y, sr, float(data["bpm"]), data.get("beat_times") or []))
    else:
        return False
    _write_json(new, _refine(data, digest))
    return True


def _upgrade_worker() -> None:
    while True:
        with _upgrade_lock:
            if not _upgrade_queue:
                globals()["_upgrade_started"] = False
                return
            path = _upgrade_queue.pop(0)
        try:
            upgrade_to_current(path)
        except Exception as exc:  # a bad file must not stop the rest of the library
            print(f"[analysis] tempo upgrade failed for {path.name}: {exc}", flush=True)


def queue_upgrade(paths) -> int:
    """Background v4 -> v5 tempo upgrades (library migration). Returns queued count."""
    global _upgrade_started
    n = 0
    with _upgrade_lock:
        for p in paths:
            p = Path(p)
            if p not in _upgrade_queue:
                _upgrade_queue.append(p)
                n += 1
        if _upgrade_queue and not _upgrade_started:
            _upgrade_started = True
            threading.Thread(target=_upgrade_worker, daemon=True).start()
    return n


def analyze(
    audio_path: str | Path,
    vocals_stem_path: Optional[str | Path] = None,
    use_cache: bool = True,
) -> TrackAnalysis:
    """Full analysis pipeline for one track, with JSON disk caching."""
    audio_path = Path(audio_path)
    # In-memory library index: an analysed file is never re-hashed or re-read
    # while it's unchanged (same size and mtime).
    try:
        st = audio_path.stat()
        mem_key = (str(audio_path), st.st_size, st.st_mtime_ns)
    except OSError:
        mem_key = None
    if use_cache and vocals_stem_path is None and mem_key in _mem_index:
        return _mem_index[mem_key]
    digest = _file_hash(audio_path)
    cache_path = _cache_path_for(audio_path, digest=digest)

    if use_cache and vocals_stem_path is None:
        if cache_path.exists():
            with open(cache_path, "r", encoding="utf-8") as f:
                res = _from_dict(json.load(f))
            if mem_key:
                _mem_index[mem_key] = res
            return res
        if _cache_path_for(audio_path, 5, digest).exists():
            # v5 -> v6 is structure only (phrase grid + stems, no mix decode): do it now.
            try:
                if upgrade_to_current(audio_path):
                    with open(cache_path, "r", encoding="utf-8") as f:
                        res = _from_dict(json.load(f))
                    if mem_key:
                        _mem_index[mem_key] = res
                    return res
            except (OSError, ValueError) as exc:
                print(f"[analysis] v6 upgrade failed for {audio_path.name}: {exc}", flush=True)
        old = _cache_path_for(audio_path, 4, digest)
        if old.exists():                        # analysed before: answer now, refine tempo later
            queue_upgrade([audio_path])
            with open(old, "r", encoding="utf-8") as f:
                return _from_dict(_refine(json.load(f), digest))

    y, sr = librosa.load(str(audio_path), sr=SAMPLE_RATE, mono=True)
    duration = float(librosa.get_duration(y=y, sr=sr))

    bpm, beat_times = analyze_tempo_and_beats(y, sr)
    energy_times, energy = analyze_energy_curve(y, sr)
    beat_energy = beat_log_energy(y, sr, beat_times)
    off_8 = estimate_phrase_offset(
        beat_times, estimate_downbeat_phase(y, sr, beat_times), energy_times, energy,
        beat_energy=beat_energy,
    )
    phase = off_8 % BEATS_PER_BAR  # a phrase line is always a downbeat
    off_16 = estimate_phrase_offset(
        beat_times, phase, energy_times, energy, beats_per_phrase=BEATS_PER_PHRASE * 2,
        beat_energy=beat_energy, candidates=[off_8, off_8 + BEATS_PER_PHRASE],
    )
    downbeats = compute_downbeats(beat_times, offset=phase)
    phrases_8 = compute_phrase_boundaries(beat_times, BEATS_PER_PHRASE, offset=off_8)
    phrases_16 = compute_phrase_boundaries(beat_times, BEATS_PER_PHRASE * 2, offset=off_16)
    key = detect_camelot_key(y, sr)
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
    record = _refine(result.to_dict(), digest)   # v6: phrase-grid sections + drops
    result = _from_dict(record)

    if use_cache and vocals_stem_path is None:
        _write_json(cache_path, record)
        if mem_key:
            _mem_index[mem_key] = result

    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m music_brain.analyzer <audio_path>")
        raise SystemExit(1)

    result = analyze(sys.argv[1])
    print(json.dumps(result.to_dict(), indent=2)[:2000])
