"""Measured dance-floor energy, 1-10, comparable ACROSS songs.

The analysis' energy_curve (and vibe.mean_energy) are RMS normalised to each
song's own peak: they tell the shape of a song, not how hard it hits next to
another one. So selection flipped between an ambient cut and a peak-time
record. This measures what a DJ means by energy, level-independent (the trim
knob fixes loudness):

  onset density   how busy the rhythm is (onsets / s)
  low end         share of the spectrum under 150 Hz (kick + bass drive)
  tempo           folded into 80-150 BPM (half-time DnB counts as its full tempo feel)
  brightness      spectral centroid (a dark pad vs a bright lead)

combined into 0..1 and mapped to 1..10. With enough analysed tracks the level is
the song's percentile in the user's own library (robust to how the heuristic is
weighted); below LIBRARY_MIN it uses the fixed reference ranges.

Selection rule (next_ok): the next song moves at most MAX_STEP levels from the
one playing (RELAXED_STEP in a relaxed session), in the direction the set arc
allows.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from app.music_brain.config import ANALYSIS_CACHE_DIR

VERSION = 1
LIBRARY_MIN = 20
MAX_STEP = 2
RELAXED_STEP = 1
WEIGHTS = {"onset": 0.4, "low": 0.35, "tempo": 0.1, "bright": 0.15}   # tempo detection is the least reliable input
RANGES = {"onset": (2.0, 9.0), "low": (0.05, 0.35), "tempo": (80.0, 150.0), "bright": (1000.0, 3500.0)}


def _norm(v: float, lo: float, hi: float) -> float:
    return float(np.clip((v - lo) / (hi - lo), 0.0, 1.0))


def raw_score(onset_rate: float, low_share: float, bpm: float, brightness_hz: float) -> float:
    """0..1 from the four measurements (fixed reference ranges)."""
    t = float(bpm or 0)
    if t > 160:
        t = 150.0                  # DnB / hardcore (and their half-time feel): top of the tempo range
    elif 0 < t < 80:
        t *= 2                     # half-time counted
    parts = {"onset": _norm(onset_rate, *RANGES["onset"]), "low": _norm(low_share, *RANGES["low"]),
             "tempo": _norm(t, *RANGES["tempo"]), "bright": _norm(brightness_hz, *RANGES["bright"])}
    return round(sum(WEIGHTS[k] * parts[k] for k in WEIGHTS), 4)


def _raw(d: dict) -> float:
    return raw_score(d["onset_rate"], d["low_share"], d["bpm"], d["brightness_hz"])


def level_from_raw(raw: float, library: Optional[List[float]] = None) -> int:
    """1..10: the percentile in the library when it is big enough, else the raw scale."""
    if library and len(library) >= LIBRARY_MIN:
        pct = float(np.mean(np.asarray(library) <= raw))
        return int(np.clip(round(1 + 9 * pct), 1, 10))
    return int(np.clip(round(1 + 9 * raw), 1, 10))


def _cache(path: Path) -> Path:
    from app.music_brain.analyzer import _file_hash

    return ANALYSIS_CACHE_DIR / f"{_file_hash(path)}.energy.json"


def measure(audio_path, bpm: float, use_cache: bool = True) -> dict:
    """{raw, onset_rate, low_share, brightness_hz, bpm} for one file (cached)."""
    import librosa

    audio_path = Path(audio_path)
    cp = _cache(audio_path)
    if use_cache and cp.exists():
        d = json.loads(cp.read_text(encoding="utf-8"))
        if d.get("version") == VERSION:
            d["raw"] = _raw(d)            # the weights may have changed since it was measured
            return d
    from app.music_brain.vibe import analyze_vibe

    v = analyze_vibe(audio_path)
    y, sr = librosa.load(str(audio_path), sr=11025, mono=True)
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=1024))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    low_share = float(S[freqs < 150].sum() / max(S.sum(), 1e-9))
    d = {"version": VERSION, "onset_rate": round(v.onset_rate, 3), "low_share": round(low_share, 4),
         "brightness_hz": round(v.brightness_hz, 1), "bpm": round(float(bpm or 0), 2)}
    d["raw"] = raw_score(d["onset_rate"], d["low_share"], d["bpm"], d["brightness_hz"])
    if use_cache:
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(d), encoding="utf-8")
    return d


def library_raws() -> List[float]:
    out = []
    for p in ANALYSIS_CACHE_DIR.glob("*.energy.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("version") == VERSION:
                out.append(_raw(d))
        except (OSError, ValueError, KeyError):
            pass
    return out


def level(audio_path, bpm: float) -> dict:
    """{level 1-10, raw, parts} for one file, ranked against the library."""
    d = measure(audio_path, bpm)
    return {"level": level_from_raw(d["raw"], library_raws()), **d}


def next_ok(cur: int, nxt: int, relaxed: bool = False, arc: str = "") -> dict:
    """May `nxt` follow `cur`? {ok, step, why}. arc: 'build' (only up or level),
    'cool' (only down or level), else either way."""
    step = nxt - cur
    lim = RELAXED_STEP if relaxed else MAX_STEP
    if abs(step) > lim:
        return {"ok": False, "step": step, "why": f"energy {'jump' if step > 0 else 'drop'} {cur} -> {nxt} (max {lim} a song)"}
    if arc == "build" and step < -1:
        return {"ok": False, "step": step, "why": f"energy falls {cur} -> {nxt} while the set is building"}
    if arc == "cool" and step > 1:
        return {"ok": False, "step": step, "why": f"energy rises {cur} -> {nxt} while the set is cooling down"}
    return {"ok": True, "step": step, "why": f"energy {cur} -> {nxt}"}
