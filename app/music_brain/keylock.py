"""Key-locked stems and the "riff over rap" plan (techniques.riff_over_rap, live).

The live console changes tempo with playbackRate, which moves pitch: +13.9 %
would detune a riff by 2.3 semitones. So the stretch happens here, offline and
key-locked (Rubber Band R3), on only the stretch of A that the move plays: its
last 16-bar full groove and the first 8 bars of the drumless breakdown right
after it. The browser plays those stems at rate 1 into deck A's channel.

Timeline (bars at B's tempo, from the moment A reaches the groove start).
The USB002 1:06:00 structure (measured), with the user's rules on top:
   0     A's groove, as is (none of B)
  16     the FIRST HALF of A's drop, clean (8 drumless bars): the set's break
  24     that half LOOPED (one groove, never two different ones); B's RAP ONLY on
         top, ~9 dB under A, entering after B's repeated opening hook: the mashup
         (16 bars, 32 when B keeps rapping)
  24+M   8-bar crossfade: A's riff fades as B's backing fades in, bass swap at +4
  32+M   A out; B's full track plays on
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import soundfile as sf

from app.music_brain import audio_io
from app.music_brain import waveform_params as wp
from app.music_brain.config import CACHE_DIR

KEYLOCK_DIR = CACHE_DIR / "keylock"
RUBBERBAND = shutil.which("rubberband") or "/opt/homebrew/bin/rubberband"
STEMS = ("drums", "bass", "vocals", "other")
GROOVE_BARS = 16
SOLO_BARS = 8           # the set plays only the FIRST HALF of A's drop (8 bars, drumless), then loops it
MASHUP_BARS_SHORT, MASHUP_BARS_LONG = 16, 32
MASHUP_LONG_VOCAL = 0.7    # B keeps rapping through the long mashup: the vibe holds
BLEND_BARS = 8


def timeline(mashup_bars: int = MASHUP_BARS_SHORT) -> dict:
    """Bars from A's groove start: groove 0-16, first half of A's drop clean
    16-24 (the set's break), then the mashup over that half looped."""
    m = 24 + mashup_bars
    return {"break": 16, "rap": 24, "mashup": 24, "blend": m, "swap": m + BLEND_BARS // 2, "end": m + BLEND_BARS,
            "mashup_bars": mashup_bars}


def measured_lines(tl: dict, rap_profile: Optional[dict], line_start_s: float, bar_s: float) -> Tuple[dict, Dict[str, str]]:
    """The timeline with the rap moves placed from the rap's own vocal stem: `holds` (per 16-bar segment, the
    bar the "hold on" loops) and `dropout` (32-bar mashups: where A drops out under the rap). Bars are counted
    on the timeline; line_start_s is B's song time at the mashup line. Unmeasured keeps the fixed bars."""
    out, src = dict(tl), {}
    m0, n = tl["mashup"], tl["mashup_bars"]
    holds, hold_src = [], wp.MEASURED
    for k in range(n // 16):
        rel, s = wp.pick_hold_bar(rap_profile, line_start_s, bar_s, 16 * k + 8, 16 * k + 11)
        holds.append(m0 + rel if rel is not None else m0 + 16 * k + 11)
        if rel is None:
            hold_src = wp.FALLBACK
    out["holds"] = holds
    src["hold_on_bar"] = hold_src
    if n >= 32:
        rel, s = wp.pick_dropout_bar(rap_profile, line_start_s, bar_s, n - 8, n - 2)
        out["dropout"] = m0 + rel if rel is not None else tl["blend"] - 2
        src["dropout_bar"] = s
    # how much of the rap is actually there under each pick, against the old fixed bars (the sim's "vocal-gap fit")
    share = lambda bar, span: wp.active_share(rap_profile, line_start_s + (bar - m0) * bar_s, line_start_s + (bar - m0 + span) * bar_s)  # noqa: E731
    fixed_holds = [m0 + 16 * k + 11 for k in range(n // 16)]
    fit = {"hold_active": [share(b, 1) for b in holds], "hold_active_fixed": [share(b, 1) for b in fixed_holds]}
    if n >= 32:
        fit.update(dropout_active=share(out["dropout"], 2), dropout_active_fixed=share(tl["blend"] - 2, 2))
    out["fit"] = fit
    return out, src


TIMELINE = timeline()
# B enters AT its rap, on the drop (user: before VLF's 1:24 rap its backing adds nothing).
# The set's bass-intro handover (66:48) is dropped: A owns the bass until the drop line.
B_LEAD_BARS = 0     # the rap starts B, at bar 16
MIN_B_AFTER_S = 60.0                                     # B must play on after the drop
SNAP_S = 0.6                                             # groove end must meet the breakdown start


def available() -> bool:
    return Path(RUBBERBAND).exists()


def _nearest(xs: Sequence[float], t: float) -> Optional[float]:
    return min(xs, key=lambda x: abs(x - t)) if xs else None


def choose(a: dict, b: dict, grooves: List[Tuple[float, float]], breakdowns: List[Tuple[float, float]],
           b_rap_at: List[float], b_bass_db: Optional[callable] = None, not_before: float = 0.0) -> dict:
    """Pick A's groove + breakdown and B's entry. a / b: analysis dicts.
    Returns {"ok": True, ...} or {"ok": False, "reasons": [...]}."""
    a_bar, b_bar = 240.0 / a["bpm"], 240.0 / b["bpm"]
    reasons = []
    pick = None
    for s0, s1 in breakdowns:
        if s1 - s0 < 8 * a_bar * 0.95 or s0 + SOLO_BARS * a_bar > a["duration"] - 1:
            continue          # needs a drumless first phrase, and the whole drop inside the song
        g0 = s0 - GROOVE_BARS * a_bar
        if g0 < not_before:
            continue
        run = next(((x, y) for x, y in grooves if x <= g0 + SNAP_S and abs(y - s0) <= SNAP_S), None)
        if run is None:
            continue
        g0 = _nearest(a.get("downbeat_times") or [], g0) or g0
        pick = (g0, s0)
        break
    if pick is None:
        reasons.append("A has no 16-bar full groove running straight into an 8-bar drumless breakdown"
                       + (f" after {not_before:.0f} s" if not_before else ""))
    rap = None
    for r in b_rap_at:
        start = r - B_LEAD_BARS * b_bar
        if start < 0 or b["duration"] - r < MIN_B_AFTER_S:
            continue
        r = _nearest(b.get("phrase_boundaries_8bar") or [], r) or r
        if r - B_LEAD_BARS * b_bar >= 0:
            rap = r
            break
    if rap is None:
        reasons.append(f"B has no rap section with {MIN_B_AFTER_S:.0f} s of song after it")
    if reasons:
        return {"ok": False, "reasons": reasons}
    g0, s0 = pick
    b_start = rap - B_LEAD_BARS * b_bar
    return {
        "ok": True,
        "target_bpm": b["bpm"],
        "ratio": a["bpm"] / b["bpm"],                  # stretched duration / original
        "a_groove": [g0, s0],
        "a_solo": [s0, s0 + SOLO_BARS * a_bar],
        "b_entry": rap,
        "b_start": b_start,
        "b_bass_intro": False,
        "timeline": TIMELINE,
        "bar_s": b_bar,
    }


def rms_db(y: np.ndarray) -> float:
    return float(10 * np.log10(np.mean(np.square(y)) + 1e-12))


def stretched_levels(folder: Path, meta: dict, plan: dict) -> dict:
    """RMS dB of A's stretched groove (all stems) and of its breakdown riff."""
    ws, r = meta["window_start"], plan["ratio"]
    g0, g1 = (plan["a_groove"][0] - ws) * r, (plan["a_groove"][1] - ws) * r
    s0, s1 = (plan["a_solo"][0] - ws) * r, (plan["a_solo"][1] - ws) * r
    ys = {}
    for n in STEMS:
        y, sr = sf.read(audio_io.stem_file(folder, n), always_2d=True)
        ys[n] = y.mean(axis=1)
    mix = sum(ys.values())
    cut = lambda y, a, b: y[int(a * sr): int(b * sr)]  # noqa: E731
    riff = cut(ys["other"], s0, s1)
    return {"a_mix_db": rms_db(cut(mix, g0, g1)), "a_riff_db": rms_db(riff),
            "a_riff_voice_db": _voice_band_db(riff, sr),        # how much of the riff competes with a voice (300-3400 Hz)
            "a_solo_mix_db": rms_db(cut(mix, s0, s1)),
            "a_peak_db": float(20 * np.log10(np.max(np.abs(mix[int(g0 * sr): int(s1 * sr)])) + 1e-9))}


def backfill_voice_band(key: str) -> Optional[dict]:
    """meta of a render made before the voice-band level was measured, with it added (and saved). None when absent."""
    p = KEYLOCK_DIR / key
    m = meta(key)
    other = audio_io.stem_file(p, "other")
    if m is None or "a_riff_voice_db" in m or other is None:
        return m
    try:
        y, sr = sf.read(other, always_2d=True)
        ws, r = m["window_start"], m["ratio"]
        y = y.mean(axis=1)[int((m["a_solo"][0] - ws) * r * sr): int((m["a_solo"][1] - ws) * r * sr)]
        m["a_riff_voice_db"] = _voice_band_db(y, sr)
        (p / "meta.json").write_text(json.dumps(m))
    except Exception:
        pass
    return m


def _voice_band_db(y: np.ndarray, sr: int) -> Optional[float]:
    if len(y) < wp.WIN:
        return None
    return wp.mean_db(wp.measure(y, sr), "voice_db", 0.0, len(y) / sr + 1.0)


# Balance targets (user, 2026-09-27: "rap volume should be lower than Aerodynamic";
# USB002 set: bass sits ~9 dB under the riff while the rap rides the riff).
RAP_UNDER_RIFF_DB = wp.RAP_UNDER_RIFF_DB   # the fallback and the reference; a run's value is measured (waveform_params.rap_offsets). user x2: 'rap is loud... in concert vocals are low, music vibes' (3 dB still overtook the riff)
BASS_UNDER_RIFF_DB = 9.0
MAX_A_BOOST_DB = 8.0


def balance(meta: dict, b_levels: dict) -> dict:
    """Gains for the run: A level-matched to B, B's rap and bass under A's riff."""
    a_gain_db = max(0.0, min(MAX_A_BOOST_DB, b_levels["b_mix_db"] - meta["a_mix_db"]))
    if "a_peak_db" in meta:                       # A alone must stay 3 dB under full scale
        a_gain_db = max(0.0, min(a_gain_db, -3.0 - meta["a_peak_db"]))
    riff = meta["a_riff_db"] + a_gain_db
    lin = lambda db: float(10 ** (db / 20))  # noqa: E731
    # rap offset and second-half lift from THIS riff's spectrum (the fixed 9 dB / x1.5 only when it was not measured)
    rap_off, lift_db, src = wp.rap_offsets(meta["a_riff_db"], meta.get("a_riff_voice_db"))
    wp.note("riff_over_rap", {"rap_under_riff_db": src, "rap_lift": src})
    return {
        "a_gain": lin(a_gain_db),
        "b_vocals": min(1.0, max(0.1, lin(riff - rap_off - b_levels["b_vocals_db"]))),
        "b_bass": min(1.0, max(0.2, lin(riff - BASS_UNDER_RIFF_DB - b_levels["b_bass_db"]))),
        "a_gain_db": round(a_gain_db, 1),
        "rap_under_riff_db": round(rap_off, 1),
        "rap_lift": round(lin(lift_db), 3),
        "param_source": src,
    }


_touched: Dict[str, float] = {}


def touch(key: str) -> None:
    """Mark a set as just used (dir mtime = last use, for keylock_cache's LRU); at most once a minute per set."""
    now = time.monotonic()
    if now - _touched.get(key, -1e9) < 60.0:
        return
    _touched[key] = now
    try:
        os.utime(KEYLOCK_DIR / key)
    except OSError:
        pass


def _key(audio_hash: str, plan: dict) -> str:
    raw = f"{audio_hash}|{plan['ratio']:.6f}|{plan['a_groove'][0]:.3f}|{plan.get('a_end', plan['a_solo'][1]):.3f}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


_jobs: Dict[str, str] = {}          # key -> "running" | "done" | "error: ..."
_lock = threading.Lock()


def render(key: str, stems: Dict[str, str], plan: dict) -> None:
    """Stretch A's [groove start - 1 s, solo end + 1 s] of every stem, key-locked."""
    out = KEYLOCK_DIR / key
    tmp = out.with_suffix(".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    lo, hi = plan["a_groove"][0] - 1.0, plan.get("a_end", plan["a_solo"][1]) + 1.0
    for n in STEMS:
        info = sf.info(stems[n])
        a, b = max(0, int(lo * info.samplerate)), int(hi * info.samplerate)
        y, sr = sf.read(stems[n], start=a, stop=b, always_2d=True)
        src = tmp / f"{n}.in.wav"
        sf.write(src, y, sr, subtype="FLOAT")
        subprocess.run([RUBBERBAND, "-3", "-q", "-t", f"{plan['ratio']:.6f}", str(src), str(tmp / f"{n}.wav")],
                       check=True, capture_output=True, timeout=600)
        src.unlink()
    meta = {"window_start": max(0.0, lo), "ratio": plan["ratio"], "a_groove": plan["a_groove"], "a_solo": plan["a_solo"]}
    meta.update(stretched_levels(tmp, meta, plan))
    meta["format"] = _to_flac(tmp)
    (tmp / "meta.json").write_text(json.dumps(meta))
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)


def _to_flac(folder: Path) -> str:
    """Rubber Band's float WAV of every stem -> 24-bit FLAC (0.46x, residual -143 dB, see
    research/notes/audio-format-study.md). Runs inside the .tmp dir, before the rename publishes it."""
    for n in STEMS:
        w = folder / f"{n}.wav"
        audio_io.encode_file(w, folder / f"{n}.flac", audio_io.RENDER_SUBTYPE)
        w.unlink()
    return "flac"


def ensure(audio_hash: str, stems: Dict[str, str], plan: dict) -> Tuple[str, str]:
    """(key, state). Starts the render in the background the first time."""
    key = _key(audio_hash, plan)
    if (KEYLOCK_DIR / key / "meta.json").exists():
        return key, "done"
    with _lock:
        state = _jobs.get(key)
        if state == "running":
            return key, state
        _jobs[key] = "running"

    def work():
        try:
            render(key, stems, plan)
            state = "done"
        except Exception as exc:  # reported to the client; the normal transition runs instead
            state = f"error: {type(exc).__name__}: {str(exc)[:160]}"
        with _lock:
            _jobs[key] = state

    threading.Thread(target=work, daemon=True).start()
    return key, "running"


def state(key: str) -> str:
    if (KEYLOCK_DIR / key / "meta.json").exists():
        return "done"
    with _lock:
        return _jobs.get(key, "unknown")


def stem_path(key: str, name: str) -> Optional[Path]:
    if name not in STEMS or not key.replace("_", "").isalnum():
        return None
    p = audio_io.stem_file(KEYLOCK_DIR / key, name)      # .flac, else a pre-FLAC .wav set
    if p is None:
        return None
    touch(key)
    return p


def meta(key: str) -> Optional[dict]:
    p = KEYLOCK_DIR / key / "meta.json"
    return json.loads(p.read_text()) if p.exists() else None


def pitch_shift_semitones(ratio: float) -> float:
    """What a playbackRate change would have cost (for the reasons)."""
    return float(12 * np.log2(1 / ratio))


# ---------------------------------------------------------- tempo stem sets
# Full-song stems key-locked at another BPM (user: "multiple BPM separated
# stems"). A deck plays them in place of its pitched mix, so a pair up to
# up to 8 % apart in tempo is beatmatched at its original key. One set per
# (song, BPM rounded to 0.5), cached; ~30 s to render a 3-4 min song.
TEMPO_STEP = 0.5
# Client cap is 8 % (tempo-rule KEYLOCK_RANGE_PCT, techniques MAX_KEYLOCK_STRETCH);
# +0.5 % for the 0.5 BPM rounding of the target. Past it the stretch smears
# (Ben 124 played 3 min at 110): refuse, never render. Cached sets stay valid.
MAX_TEMPO_STRETCH = 0.085


def tempo_key(audio_hash: str, native_bpm: float, target_bpm: float) -> Tuple[str, float]:
    t = round(target_bpm / TEMPO_STEP) * TEMPO_STEP
    return f"t{audio_hash[:24]}_{t:.1f}".replace(".", "p"), t


def render_tempo(key: str, stems: Dict[str, str], ratio: float) -> None:
    out = KEYLOCK_DIR / key
    tmp = out.with_suffix(".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for n in STEMS:
        subprocess.run([RUBBERBAND, "-3", "-q", "-t", f"{ratio:.6f}", str(stems[n]), str(tmp / f"{n}.wav")],
                       check=True, capture_output=True, timeout=900)
    (tmp / "meta.json").write_text(json.dumps({"ratio": ratio, "format": _to_flac(tmp)}))
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)


def ensure_tempo(audio_hash: str, stems: Dict[str, str], native_bpm: float, target_bpm: float) -> dict:
    """{key, bpm, ratio, state}. ratio = stretched duration / original."""
    if not native_bpm or not target_bpm or abs(target_bpm / native_bpm - 1) > MAX_TEMPO_STRETCH:
        return {"state": "error: tempo gap too large for a key-locked stretch"}
    key, t = tempo_key(audio_hash, native_bpm, target_bpm)
    ratio = native_bpm / t
    if (KEYLOCK_DIR / key / "meta.json").exists():
        touch(key)
        return {"key": key, "bpm": t, "ratio": ratio, "state": "done"}
    with _lock:
        if _jobs.get(key) == "running":
            return {"key": key, "bpm": t, "ratio": ratio, "state": "running"}
        _jobs[key] = "running"

    def work():
        try:
            render_tempo(key, stems, ratio)
            st = "done"
        except Exception as exc:
            st = f"error: {type(exc).__name__}: {str(exc)[:160]}"
        with _lock:
            _jobs[key] = st
        if st == "done":
            from app.music_brain import keylock_cache

            keylock_cache.run_async("render")

    threading.Thread(target=work, daemon=True).start()
    return {"key": key, "bpm": t, "ratio": ratio, "state": "running"}
