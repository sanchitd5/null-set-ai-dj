"""Parameters of the layered moves (RIFF x RAP, mashup ...) measured from the two tracks' own audio.

A move used to carry constants that apply to every song (rap 9 dB under the riff, vocal high-pass at
120 Hz, guest vocal at 0.9, "hold on" on the last bar). This module measures the audio once per stem,
caches the measurement, and derives each value from it. Every derivation has a clamp, and a documented
fallback to the old constant when the measurement is unavailable; `note()` logs which one was used so
the sim can count measured versus fallback.

Measured once per stem file (hash keyed, cached under CACHE_DIR/wf_profile, mono at 11.025 kHz, one
FFT per 0.5 s hop, so a 4 min stem costs a few hundred FFTs; never per play):
  db         RMS dBFS per hop
  voice_db   level of the 300-3400 Hz band per hop (where a voice and a riff compete)
  low_db     level below 120 Hz per hop (the sub band with one owner, [[EQ & Frequency Management]])
  low_hz90   frequency under which 90 % of the 20-300 Hz energy sits (how far up this track's low end reaches)
  centroid_hz  spectral centroid of the whole stem

KB rules are NOT tuned here: 8-bar snap, sub-bass < 120 Hz single owner (a corner is never below
SUB_BASS_CROSSOVER_HZ), the Camelot table and the BPM policy stay where CLAUDE.md section 4 puts them.
Constants below that are still guesses are marked UNVERIFIED.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.music_brain.config import CACHE_DIR, SUB_BASS_CROSSOVER_HZ

PROFILE_VERSION = 1
PROFILE_DIR = CACHE_DIR / "wf_profile"
HOP_S = 0.5
SR = 11025
WIN = 4096
VOICE_BAND = (300.0, 3400.0)
LOW_SEARCH = (20.0, 300.0)
SILENT_DB = -90.0

MEASURED, FALLBACK = "measured", "fallback"

# ---- riff balance (keylock.balance) -------------------------------------------------------
RAP_UNDER_RIFF_DB = 9.0        # user, 2026-09-27: full-band offset that suits a riff with REF_VOICE_LOSS_DB in the voice band
REF_VOICE_LOSS_DB = 3.0        # a riff with half its energy in 300-3400 Hz reproduces the 9 dB exactly (UNVERIFIED reference)
RAP_OFFSET_CLAMP_DB = (5.0, 13.0)
RAP_LIFT_PER_DB = 0.4          # second-half lift = 0.4 x the offset: 9 dB -> 3.6 dB = x1.5, the user's "raise it by half" (UNVERIFIED shape)
RAP_LIFT_CLAMP_DB = (2.0, 5.0)

# ---- rap lines (riff hold on / drop out) --------------------------------------------------
GAP_BELOW_DB = 18.0            # a hop this far under the rap's own loud level (p90) is a gap, a breath (UNVERIFIED)
FULL_BAR_ACTIVE = 0.9          # a bar counts as full of rap when this share of its hops is not a gap

# ---- mashup guest vocal --------------------------------------------------------------------
LEVEL_CLAMP = (0.25, 1.0)
HP_CLAMP_HZ = (float(SUB_BASS_CROSSOVER_HZ), 200.0)   # never below the KB sub crossover


def _db(x: float) -> float:
    return float(10.0 * math.log10(max(x, 1e-12)))


# ------------------------------------------------------------------------------ measurement
def _read_mono(path: Path, sr: int = SR) -> Tuple[np.ndarray, int]:
    import soundfile as sf

    try:
        y, file_sr = sf.read(str(path), dtype="float32", always_2d=True)
        y = y.mean(axis=1)
    except Exception:                                   # a format libsndfile lacks (mp3 on older builds): librosa decodes it
        import librosa

        y, _ = librosa.load(str(path), sr=sr, mono=True)
        return y.astype(np.float32), sr
    if file_sr > sr:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(int(file_sr), sr)
        y = resample_poly(y, sr // g, int(file_sr) // g).astype(np.float32)
        return y, sr
    return y, int(file_sr)


def measure(y: np.ndarray, sr: int, hop_s: float = HOP_S) -> dict:
    """Per-hop levels and spectral extents of one mono signal (pure)."""
    hop = max(1, int(sr * hop_s))
    n_hops = int(math.ceil(len(y) / hop)) if len(y) else 0
    win = np.hanning(WIN).astype(np.float32)
    freqs = np.fft.rfftfreq(WIN, 1.0 / sr)
    voice = (freqs >= VOICE_BAND[0]) & (freqs <= min(VOICE_BAND[1], sr / 2.0))
    low = freqs < SUB_BASS_CROSSOVER_HZ
    low_search = (freqs >= LOW_SEARCH[0]) & (freqs <= LOW_SEARCH[1])
    pad = np.concatenate([np.zeros(WIN // 2, np.float32), y.astype(np.float32), np.zeros(WIN + hop, np.float32)])
    db, voice_db, low_db = [], [], []
    mean_spec = np.zeros(len(freqs))
    n_active = 0
    for i in range(n_hops):
        seg = y[i * hop:(i + 1) * hop]
        p = float(np.mean(np.square(seg))) if len(seg) else 0.0
        c = int((i + 0.5) * hop)                       # window centred on the hop (pad shifts by WIN // 2)
        spec = np.abs(np.fft.rfft(pad[c:c + WIN] * win)) ** 2
        tot = float(spec.sum()) + 1e-20
        db.append(_db(p))
        voice_db.append(_db(p * float(spec[voice].sum()) / tot))
        low_db.append(_db(p * float(spec[low].sum()) / tot))
        if p > 1e-8:                                   # active hop (> -80 dBFS): counts towards the spectral extents
            mean_spec += spec / tot
            n_active += 1
    hz90, centroid = None, None
    if n_active:
        band = mean_spec[low_search]
        if band.sum() > 0:
            cum = np.cumsum(band) / band.sum()
            hz90 = float(freqs[low_search][int(np.searchsorted(cum, 0.9))])
        if mean_spec.sum() > 0:
            centroid = float((freqs * mean_spec).sum() / mean_spec.sum())
    r = lambda xs: [round(v, 1) for v in xs]  # noqa: E731
    return {"v": PROFILE_VERSION, "hop": hop_s, "sr": sr, "db": r(db), "voice_db": r(voice_db), "low_db": r(low_db),
            "low_hz90": None if hz90 is None else round(hz90, 1), "centroid_hz": None if centroid is None else round(centroid, 1)}


_hash_memo: Dict[Tuple[str, int, int], str] = {}


def _content_hash(path: Path) -> str:
    st = path.stat()
    k = (str(path), st.st_size, st.st_mtime_ns)
    if k not in _hash_memo:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _hash_memo[k] = h.hexdigest()[:24]
    return _hash_memo[k]


def profile(path, cache_dir: Optional[Path] = None) -> Optional[dict]:
    """The measurement of one stem file, cached by content hash. None when it cannot be read."""
    try:
        p = Path(path)
        cache = Path(cache_dir) if cache_dir is not None else PROFILE_DIR
        f = cache / f"{_content_hash(p)}_v{PROFILE_VERSION}.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        y, sr = _read_mono(p)
        out = measure(y, sr)
        try:
            cache.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(out), encoding="utf-8")
        except OSError:
            pass                                        # a read-only cache only costs the next call a re-measure
        return out
    except Exception:                                   # unreadable / missing / bad file: the caller falls back
        return None


# ---------------------------------------------------------------------------------- slicing
def _slice(prof: dict, key: str, t0: float, t1: float) -> List[float]:
    hop = prof.get("hop") or HOP_S
    arr = prof.get(key) or []
    lo, hi = max(0, int(t0 / hop)), max(0, int(math.ceil(t1 / hop)))
    return arr[lo:min(hi, len(arr))]


def mean_db(prof: Optional[dict], key: str, t0: float, t1: float, active_only: bool = False) -> Optional[float]:
    """Power mean of a per-hop dB series over [t0, t1). active_only drops the gap hops (the level of the rap
    while it raps, not diluted by its breaths)."""
    if not prof:
        return None
    xs = _slice(prof, key, t0, t1)
    if active_only:
        floor = gap_floor_db(prof, key)
        xs = [v for v in xs if floor is None or v > floor]
    xs = [v for v in xs if v > SILENT_DB + 1]
    if not xs:
        return None
    return _db(sum(10 ** (v / 10.0) for v in xs) / len(xs))


def gap_floor_db(prof: Optional[dict], key: str = "voice_db") -> Optional[float]:
    """Level under which a hop of this stem is a gap: GAP_BELOW_DB under its own loud level (p90)."""
    xs = [v for v in ((prof or {}).get(key) or []) if v > SILENT_DB + 1]
    if len(xs) < 8:
        return None
    return float(np.percentile(xs, 90)) - GAP_BELOW_DB


def active_share(prof: Optional[dict], t0: float, t1: float, key: str = "voice_db") -> Optional[float]:
    """Share of hops in [t0, t1) that are not a gap (the flow density of the rap there)."""
    floor = gap_floor_db(prof, key)
    xs = _slice(prof, key, t0, t1) if prof else []
    if floor is None or not xs:
        return None
    return sum(1 for v in xs if v > floor) / len(xs)


def gaps(prof: Optional[dict], t0: float, t1: float, min_s: float = 0.5, key: str = "voice_db") -> List[Tuple[float, float]]:
    """(start, end) of the runs of gap hops in [t0, t1) that last at least min_s."""
    floor = gap_floor_db(prof, key)
    if floor is None or not prof:
        return []
    hop = prof.get("hop") or HOP_S
    i0 = max(0, int(t0 / hop))
    xs = _slice(prof, key, t0, t1)
    out, start = [], None
    for i, v in enumerate(xs + [floor + 1.0]):
        if v <= floor and start is None:
            start = i
        elif v > floor and start is not None:
            if (i - start) * hop >= min_s:
                out.append(((i0 + start) * hop, (i0 + i) * hop))
            start = None
    return out


# ------------------------------------------------------------------------- derivations
def rap_offsets(riff_db: Optional[float], riff_voice_db: Optional[float]) -> Tuple[float, float, str]:
    """(rap_under_riff_db, rap_lift_db, source) from the riff's own spectrum.

    Invariant: the rap sits (RAP_UNDER_RIFF_DB - REF_VOICE_LOSS_DB) under the riff IN THE VOICE BAND, so a riff
    that is mostly bass (its voice band far under its full level) lets the rap sit lower overall, and a bright
    riff that masks the voice gets the rap closer to it. The lift for the mashup's second half follows.
    Fallback (no measurement): the fixed 9 dB and the x1.5 lift.
    """
    if riff_db is None or riff_voice_db is None or riff_voice_db < SILENT_DB + 1:
        return RAP_UNDER_RIFF_DB, RAP_LIFT_PER_DB * RAP_UNDER_RIFF_DB, FALLBACK
    off = (RAP_UNDER_RIFF_DB - REF_VOICE_LOSS_DB) + (riff_db - riff_voice_db)
    off = min(RAP_OFFSET_CLAMP_DB[1], max(RAP_OFFSET_CLAMP_DB[0], off))
    lift = min(RAP_LIFT_CLAMP_DB[1], max(RAP_LIFT_CLAMP_DB[0], RAP_LIFT_PER_DB * off))
    return off, lift, MEASURED


def pick_hold_bar(prof: Optional[dict], line_start_s: float, bar_s: float, first: int, last: int) -> Tuple[Optional[int], str]:
    """Which bar (index counted from line_start_s) of the rap the "hold on" loops.

    The bar between `first` and `last` that is full of rap (no breath: FULL_BAR_ACTIVE of its hops voiced)
    and loudest in the voice band; a bar with a gap would loop a hole. (None, fallback) when unmeasured
    or no bar qualifies: the caller keeps its fixed bar.
    """
    if not prof or bar_s <= 0:
        return None, FALLBACK
    best = None
    for b in range(first, last + 1):
        t0, t1 = line_start_s + b * bar_s, line_start_s + (b + 1) * bar_s
        share = active_share(prof, t0, t1)
        lvl = mean_db(prof, "voice_db", t0, t1)
        if share is None or lvl is None or share < FULL_BAR_ACTIVE:
            continue
        if best is None or lvl > best[0]:
            best = (lvl, b)
    return (best[1], MEASURED) if best else (None, FALLBACK)


def pick_dropout_bar(prof: Optional[dict], line_start_s: float, bar_s: float, first: int, last: int) -> Tuple[Optional[int], str]:
    """The first of the two bars where A drops out and the rap is alone: the 2-bar span of the mashup's
    tail (first..last) in which the rap is most continuous (share of voiced hops), then loudest."""
    if not prof or bar_s <= 0:
        return None, FALLBACK
    best = None
    for b in range(first, last + 1):
        t0, t1 = line_start_s + b * bar_s, line_start_s + (b + 2) * bar_s
        share = active_share(prof, t0, t1)
        lvl = mean_db(prof, "voice_db", t0, t1)
        if share is None or lvl is None:
            continue
        cand = (share, lvl, b)
        if best is None or cand[:2] > best[:2]:
            best = cand
    return (best[2], MEASURED) if best and best[0] >= FULL_BAR_ACTIVE else (None, FALLBACK)


def guest_level(guest_voice_db: Optional[float], host_voice_db: Optional[float], fallback: float) -> Tuple[float, str]:
    """Gain of the mashup's guest vocal so its voice band sits level with the host's own voice band
    (0 dB in band: intelligible without riding above the beat; UNVERIFIED target). Clamped to LEVEL_CLAMP."""
    if guest_voice_db is None or host_voice_db is None:
        return fallback, FALLBACK
    g = 10 ** ((host_voice_db - guest_voice_db) / 20.0)
    return float(min(LEVEL_CLAMP[1], max(LEVEL_CLAMP[0], g))), MEASURED


def sub_corner_hz(owner_low_hz90: Optional[float], fallback: float = float(SUB_BASS_CROSSOVER_HZ)) -> Tuple[float, str]:
    """High-pass corner for the layer that must leave the low end to the OTHER track: where the owner's own
    low end stops (90 % of its 20-300 Hz energy), never under the KB sub crossover, capped at HP_CLAMP_HZ[1]."""
    if owner_low_hz90 is None:
        return fallback, FALLBACK
    return float(min(HP_CLAMP_HZ[1], max(HP_CLAMP_HZ[0], owner_low_hz90))), MEASURED


# ------------------------------------------------------------------------------ logging
def note(move: str, sources: Dict[str, str], **extra) -> None:
    """Log which parameters of a move came from a measurement and which fell back (the sim counts them)."""
    try:
        from app.ui import engine

        engine.current().host.log_event("derived_params", move=move, sources=dict(sources), measured=sum(
            1 for s in sources.values() if s == MEASURED), fallback=sum(1 for s in sources.values() if s != MEASURED), **extra)
    except Exception:
        pass
