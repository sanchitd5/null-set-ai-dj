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
import math
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
    from app.music_brain.analysis.analyzer import _file_hash

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
    from app.music_brain.analysis.vibe import analyze_vibe

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
    from app.ui.services import engine

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
# Cumulative fall: per-step falls are capped, but 9 > 7 > 4 > 2 still drained a set. Set curves in
# DJ/06 - Energy & Crowd/Energy Management & Dynamics.md hold a valley to ~3 below the peak and only as a
# deliberate "Reset Valley" (8.5 -> 5-6, 60 min curve; house wave 8.5 -> 6). So without a reset context a
# song may not land 3+ levels under the peak of the last PEAK_WINDOW songs.
PEAK_WINDOW = 6
MAX_BELOW_PEAK = 2


def next_ok(cur: int, nxt: int, relaxed: bool = False, force: bool = False,
            songs: Optional[int] = None, set_pos: Optional[float] = None,
            raw_delta: Optional[float] = None, recent: Optional[list] = None,
            reset: bool = False) -> dict:
    """May `nxt` follow `cur`? {ok, step, why}. The live rule is the console's
    autopilot.js energyStepOk(); this mirrors it exactly (golden vectors in
    app/tests/fixtures/rule_vectors.json check both).
    force: last-round fallback, one more level and no arc rule. songs: songs played
    so far (the first WARMUP_SONGS build: no fall of more than 1, unless cur is
    already LOW_ENERGY_SET_MAX or below -- a sufi/relaxing set isn't "building"
    just because it's early). set_pos 0-1, used only without songs: < 0.3 builds,
    > 0.85 cools. raw_delta: raw_b - raw_a; under ENERGY_MIN_RAW the songs measure
    the same, whatever the levels say. recent: measured levels of the songs played so
    far, playing one last; a fall that lands MAX_BELOW_PEAK+1 or more under their peak
    (last PEAK_WINDOW) is refused unless reset (a deliberate dip was asked for), the
    session is relaxed, the set is in its cool-down or the peak is itself low."""
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
    if step < 0 and recent and not reset and not relaxed and arc != "cool":
        peak = max([v for v in recent[-PEAK_WINDOW:] if v is not None] + [cur])
        if peak > LOW_ENERGY_SET_MAX and peak - nxt > MAX_BELOW_PEAK:
            return {"ok": False, "step": step, "why": f"energy {cur} -> {nxt} drains the set: {peak - nxt} below its recent peak {peak}"}
    return {"ok": True, "step": step, "why": f"energy {cur} -> {nxt}"}


# "Let the song finish" (research/notes/dj-hidden-practices.md item 10, "allow a full play-through when energy is
# at target"): the set is at its target when it has left the warm-up (or is a low-energy set that never builds),
# the last TARGET_SONGS measured levels sit within TARGET_TOL of each other and the playing song is within
# TARGET_TOL of the recent peak (last PEAK_WINDOW). The console's autopilot.js energyAtTarget() mirrors it
# (golden vectors in app/tests/fixtures/rule_vectors.json). TARGET_SONGS and TARGET_TOL are GUESSES.
TARGET_SONGS = 3
TARGET_TOL = 1


def at_target(recent: Optional[list], songs: int) -> dict:
    """{ok, why}: is the set holding its energy target with the playing song (recent[-1])?"""
    lv = [v for v in (recent or []) if v is not None]
    if len(lv) < TARGET_SONGS:
        return {"ok": False, "why": f"only {len(lv)} measured songs"}
    cur = lv[-1]
    if songs < WARMUP_SONGS and cur > LOW_ENERGY_SET_MAX:
        return {"ok": False, "why": "the set is still building"}
    last = lv[-TARGET_SONGS:]
    if max(last) - min(last) > TARGET_TOL:
        return {"ok": False, "why": f"energy still moving ({min(last)}-{max(last)})"}
    peak = max(lv[-PEAK_WINDOW:])
    if peak - cur > TARGET_TOL:
        return {"ok": False, "why": f"energy {cur} under the recent peak {peak}"}
    return {"ok": True, "why": f"energy holding at {cur}"}


def allowed_window(cur: int, relaxed: bool = False, songs: Optional[int] = None,
                   set_pos: Optional[float] = None, recent: Optional[list] = None,
                   reset: bool = False) -> tuple:
    """(lo, hi): the levels next_ok accepts after `cur`, for the suggest prompt and filter."""
    ok = [n for n in range(1, 11)
          if next_ok(cur, n, relaxed=relaxed, songs=songs, set_pos=set_pos, recent=recent, reset=reset)["ok"]]
    return (min(ok), max(ok)) if ok else (cur, cur)


# ---- SET ENERGY (owner spec, energy-recipe-choice): twin of autopilot.js setEnergy /
# energyRecipeChoice (parity: app/tests/py/test_set_energy_parity.py). The set's energy is the
# planned arc target and the recent songs actually played, combined. GUESS numbers (DJ/06 curves).
ARC_TARGET = {"warm-up": 4, "build": 6, "peak": 8, "cool-down": 5}
SET_RECENT = 4          # rolling window: the last 4 played levels, newest weighted most (4,3,2,1)
SET_ARC_W = 0.5         # combined = 0.5 * arc target + 0.5 * rolling played level
SET_RELAXED_MAX, SET_HIGH_MIN = 5, 7
ENERGY_MATCH_TOL = 3    # an option whose window level is > 3 off the set level is vetoed
MASHUP, BASS, BLEND = "Mashup → Transition", "Bass Swap", "Long Blend"


def arc_at(set_pos: Optional[float]) -> Optional[str]:
    if set_pos is None:
        return None
    return "warm-up" if set_pos < 0.1 else "build" if set_pos < 0.3 else "peak" if set_pos <= 0.85 else "cool-down"


def set_energy(set_pos: Optional[float], recent: Optional[list]) -> dict:
    """{level, band, arc, target, played}: the set's energy, arc target and rolling played level."""
    arc = arc_at(set_pos)
    target = ARC_TARGET.get(arc) if arc else None
    lv = [v for v in (recent or []) if v is not None][-SET_RECENT:]
    played = None
    if lv:
        s = w = 0
        for i, v in enumerate(lv):
            s += v * (i + 1)
            w += i + 1
        played = s / w
    raw = played if target is None else target if played is None else SET_ARC_W * target + (1 - SET_ARC_W) * played
    if raw is None:
        return {"level": None, "band": None, "arc": arc, "target": target, "played": played}
    level = max(1, min(10, math.floor(raw + 0.5)))   # half up, as the JS twin (not banker's)
    band = "relaxed" if level <= SET_RELAXED_MAX else "high" if level >= SET_HIGH_MIN else "middle"
    return {"level": level, "band": band, "arc": arc, "target": target, "played": played}


def recipe_choice(band: Optional[str], mashup_fits: bool, mashup_kind: Optional[str], blend_open: bool,
                  clean_both: bool, long_blend_ok: bool = False, set_level: Optional[float] = None,
                  levels: Optional[dict] = None) -> Optional[str]:
    """The recipe among Mashup / Bass Swap / Long Blend by set energy (None: keep the rules' pick)."""
    allowed = bool(mashup_fits) and (band == "relaxed" if mashup_kind == "low"
                                     else band in ("middle", "high") if mashup_kind == "beat" else False)
    order = [MASHUP] if allowed else []
    if blend_open:
        order += [BLEND, BASS] if clean_both else ([BASS, BLEND] if long_blend_ok else [BASS])
    if not order:
        return None
    key = {MASHUP: "mashup", BASS: "bass", BLEND: "blend"}
    lv = levels or {}
    for r in order:
        v = lv.get(key[r])
        if v is not None and set_level is not None and abs(v - set_level) > ENERGY_MATCH_TOL:
            continue
        return r
    return order[0]
