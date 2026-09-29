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


@__import__("functools").lru_cache(maxsize=512)
def _hash_of(path: str, mtime: float, size: int) -> str:
    from app.music_brain.analyzer import _file_hash

    return _file_hash(Path(path))


def _cache(path: Path) -> Path:
    st = Path(path).stat()               # hash each file once per process, not per call
    return ANALYSIS_CACHE_DIR / f"{_hash_of(str(path), st.st_mtime, st.st_size)}.energy.json"


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


_lib_cache: tuple = (None, [])


def library_raws() -> List[float]:
    """The library's raw energy scores, through the installed engine's host."""
    from app.ui import engine

    return engine.current().host.library_raws()


def _library_raws_impl() -> List[float]:
    """Every measured track's raw score; re-read only when the cache dir changed."""
    global _lib_cache
    try:
        stamp = (ANALYSIS_CACHE_DIR.stat().st_mtime, VERSION, tuple(sorted(WEIGHTS.items())))
    except OSError:
        return []
    if _lib_cache[0] == stamp:
        return _lib_cache[1]
    out = []
    for p in ANALYSIS_CACHE_DIR.glob("*.energy.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("version") == VERSION:
                out.append(_raw(d))
        except (OSError, ValueError, KeyError):
            pass
    _lib_cache = (stamp, out)
    return out


def level(audio_path, bpm: float) -> dict:
    """{level 1-10, raw, parts} for one file, ranked against the library."""
    d = measure(audio_path, bpm)
    return {"level": level_from_raw(d["raw"], library_raws()), **d}


ENERGY_MIN_RAW = 0.1       # raw 0-1: below this the two songs measure the same, whatever the levels say
WARMUP_SONGS = 5           # the set builds over its first songs; open-ended after (no known end)
LOW_ENERGY_SET_MAX = 4     # cur at or below this: already a low-energy set (sufi, relaxing) -- don't force "build"


def next_ok(cur: int, nxt: int, relaxed: bool = False, force: bool = False,
            songs: Optional[int] = None, set_pos: Optional[float] = None,
            raw_delta: Optional[float] = None) -> dict:
    """May `nxt` follow `cur`? {ok, step, why}. The live rule is the console's
    autopilot.js energyStepOk(); this mirrors it exactly (golden vectors in
    app/tests/fixtures/rule_vectors.json check both).
    force: last-round fallback, one more level and no arc rule. songs: songs played
    so far (the first WARMUP_SONGS build: no fall of more than 1, unless cur is
    already LOW_ENERGY_SET_MAX or below -- a sufi/relaxing set isn't "building"
    just because it's early). set_pos 0-1, used only without songs: < 0.3 builds,
    > 0.85 cools. raw_delta: raw_b - raw_a; under ENERGY_MIN_RAW the songs measure
    the same, whatever the levels say."""
    step = nxt - cur
    base = RELAXED_STEP if relaxed else MAX_STEP
    # the last-round force only widens RISES: widening falls let 9 > 7 > 4 > 2 slide through
    lim = base + (1 if force and step > 0 else 0)
    if raw_delta is not None and abs(raw_delta) < ENERGY_MIN_RAW:
        return {"ok": True, "step": step, "why": f"energy {cur} -> {nxt} (measured almost the same)"}
    if songs is not None:
        arc = "build" if songs < WARMUP_SONGS else ""
    else:
        arc = "build" if set_pos is not None and set_pos < 0.3 else "cool" if set_pos is not None and set_pos > 0.85 else ""
    if arc == "build" and cur is not None and cur <= LOW_ENERGY_SET_MAX:
        arc = ""    # already a low-energy set (sufi, relaxing): don't force it to build
    if abs(step) > lim:
        return {"ok": False, "step": step, "why": f"energy {'jump' if step > 0 else 'drop'} {cur} -> {nxt} (max {lim} a song)"}
    if not force and arc == "build" and step < -1:
        return {"ok": False, "step": step, "why": f"energy falls {cur} -> {nxt} while the set is building"}
    if not force and arc == "cool" and step > 1:
        return {"ok": False, "step": step, "why": f"energy rises {cur} -> {nxt} while the set is cooling down"}
    return {"ok": True, "step": step, "why": f"energy {cur} -> {nxt}"}
