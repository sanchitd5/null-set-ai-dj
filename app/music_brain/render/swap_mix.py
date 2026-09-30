"""Offline "swap mix": a chain of songs, each the lead for one 16-bar window (its main drop), handed over by stem
Bass Swaps on the phrase line. Owner ask: "a swap mix, each electronic song switching every 30 seconds".

Rules (CLAUDE.md s4, ./DJ/ [[Bass Swap]], [[EQ & Frequency Management]], [[Phrasing & Structure]]):
  - one tempo for the whole mix; every song key-locked to it (Rubber Band R3 on the stems), max 8 % stretch
  - each song's window starts on a drop line (8-bar phrase line, blend.drop_lines), else its loudest 16-bar phrase
  - handover: B's drums + other enter high-passed at 120 Hz for SWAP_BARS / 2 bars before the swap downbeat,
    A's drums + other leave high-passed for SWAP_BARS / 2 bars after it: one song owns the sub at any time
  - no vocal is layered over the other song (vocals only play in the song's own lead window)
  - every song's lead window is RMS-matched, the mix is set to PEAK_DBFS
The chain itself (select_chain) comes from the pair atlas plus the live gates: Camelot >= KEY_SAFE_MIN, tempo within
6 %, no family jump, no vetoed pair.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfiltfilt

from app.music_brain.render.blend import drop_lines

LEAD_BARS = 16
SWAP_BARS = 4                 # total overlap per handover, centred on the swap downbeat
SUB_HZ = 120.0
TARGET_RMS_DBFS = -14.0
PEAK_DBFS = -1.0
MAX_STRETCH = 0.08            # = techniques.MAX_KEYLOCK_STRETCH
KEY_SAFE_MIN = 0.6
TEMPO_GATE = 0.06
RAMP_S = 0.02                 # click-free edges
DROP_MIN_SHARE = 0.9          # a drop line must reach this share of the song's strongest 16-bar groove
FULL_DB = 12.0               # QC: sub band within this of the song's own lead median = "at full level"
STEMS = ("drums", "bass", "other", "vocals")
HANDOVER_STEMS = ("drums", "other")
RUBBERBAND = shutil.which("rubberband") or "/opt/homebrew/bin/rubberband"


def _g(a, k, d=None):
    return a.get(k, d) if isinstance(a, dict) else getattr(a, k, d)


def pick_window(analysis, stem_bars: Optional[dict] = None, bars: int = LEAD_BARS, pre_bars: float = SWAP_BARS / 2,
                post_bars: float = SWAP_BARS / 2) -> Optional[dict]:
    """{start, end, how} of the song's lead window (native seconds) on an 8-bar phrase line, with room for the
    handover pre-roll and tail. Score = mean stem level (drums + bass + other) over the window from the atlas bar
    features `stem_bars` ({anchor, bar, bars: {stem: [level per bar]}}), else the analysis energy curve.
    The best-scoring drop line wins unless it scores under DROP_MIN_SHARE of the best phrase (a weak drop line,
    e.g. an early intro slam): then the strongest groove phrase wins. None when nothing fits."""
    bpm = float(_g(analysis, "bpm") or 0)
    dur = float(_g(analysis, "duration") or 0)
    if bpm <= 0 or dur <= 0:
        return None
    bar = 240.0 / bpm
    phrases = list(_g(analysis, "phrase_boundaries_8bar") or [])
    times, curve = _g(analysis, "energy_times") or [], _g(analysis, "energy_curve") or []

    def fits(t):
        return t - pre_bars * bar >= 0 and t + (bars + post_bars) * bar <= dur

    lv = None
    if stem_bars and stem_bars.get("bars"):
        B = stem_bars["bars"]
        n = min(len(B.get(s) or []) for s in ("drums", "bass", "other"))
        if n:
            lv = sum(np.asarray(B[s][:n], float) for s in ("drums", "bass", "other"))

    def score(t):
        if lv is not None:
            i = int(round((t - stem_bars["anchor"]) / stem_bars["bar"]))
            seg = lv[max(i, 0):i + bars]
            return float(seg.mean()) if len(seg) else -1.0
        v = [e for x, e in zip(times, curve) if t <= x < t + bars * bar]
        return sum(v) / len(v) if v else -1.0

    fit = [(score(t), t) for t in phrases if fits(t)]
    if not fit:
        return None
    top = max(fit)
    drops = [(score(t), t) for t, _, _ in drop_lines(phrases, times, curve, bar) if fits(t)]
    if drops and max(drops)[0] >= DROP_MIN_SHARE * top[0]:
        return {"start": max(drops)[1], "end": max(drops)[1] + bars * bar, "how": "drop line"}
    return {"start": top[1], "end": top[1] + bars * bar, "how": "strongest groove phrase"}


def target_bpm(bpms: Sequence[float], step: float = 0.5) -> float:
    """Median of the chain's tempi on the key-lock cache grid (keylock.TEMPO_STEP)."""
    return round(float(np.median(bpms)) / step) * step


def stretch_ok(native: float, target: float) -> bool:
    return native > 0 and abs(target / native - 1) <= MAX_STRETCH


def plan(songs: List[dict], target: float) -> List[dict]:
    """Output timeline: song i leads [i * LEAD_BARS, (i + 1) * LEAD_BARS) bars at `target`.
    Each song needs `window` (native start/end) and `bpm`."""
    bar = 240.0 / target
    half = SWAP_BARS / 2 * bar
    out = []
    for i, s in enumerate(songs):
        if not stretch_ok(s["bpm"], target):
            raise ValueError(f"{s.get('name')}: {s['bpm']} -> {target} BPM is past the {MAX_STRETCH:.0%} key-lock cap")
        lead = i * LEAD_BARS * bar
        out.append({"i": i, "lead": lead, "lead_end": lead + LEAD_BARS * bar,
                    "pre": lead - half if i else lead, "post": lead + LEAD_BARS * bar + half,
                    "ratio": s["bpm"] / target,    # stretched duration / native
                    "stretch_pct": round((target / s["bpm"] - 1) * 100, 2)})
    return out


def _hp(x: np.ndarray, sr: int) -> np.ndarray:
    return sosfiltfilt(butter(8, SUB_HZ, "highpass", fs=sr, output="sos"), x, axis=0)


def _env(n: int, sr: int, pts: Sequence[tuple]) -> np.ndarray:
    """Piecewise-linear gain over n samples from (seconds, gain) points."""
    t = np.arange(n) / sr
    xs, ys = zip(*pts)
    return np.interp(t, xs, ys).astype(np.float32)


def _stretch(stems: Dict[str, np.ndarray], sr: int, ratio: float, tmp: Path) -> Dict[str, np.ndarray]:
    if abs(ratio - 1) < 1e-6:
        return stems
    out = {}
    for n, x in stems.items():
        src, dst = tmp / f"{n}.in.wav", tmp / f"{n}.out.wav"
        sf.write(src, x, sr, subtype="FLOAT")
        subprocess.run([RUBBERBAND, "-3", "-q", "-t", f"{ratio:.6f}", str(src), str(dst)],
                       check=True, capture_output=True, timeout=900)
        out[n], _ = sf.read(dst, dtype="float32", always_2d=True)
    return out


def song_layer(stems: Dict[str, np.ndarray], sr: int, slot: dict, first: bool, last: bool) -> tuple:
    """(offset_s, audio, low_owned) of one song on the output clock. `stems` are already stretched and start at
    slot['pre'] on the output clock. low_owned: the sub-band part this song plays at full level (its lead)."""
    n = min(len(x) for x in stems.values())
    pre, lead, end, post = 0.0, slot["lead"] - slot["pre"], slot["lead_end"] - slot["pre"], slot["post"] - slot["pre"]
    r = RAMP_S
    n = min(n, int(round(post * sr)))
    lead_g = _env(n, sr, [(pre, 0), (lead - r, 0), (lead, 1), (end, 1), (end + r, 0), (post, 0)] if not first
                  else [(0, 0), (r, 1), (end, 1), (end + r, 0), (post, 0)])
    if last:   # no next song: the tail keeps the full mix and fades out
        lead_g = _env(n, sr, [(pre, 0), (lead - r, 0), (lead, 1), (end, 1), (post, 0)] if not first
                      else [(0, 0), (r, 1), (end, 1), (post, 0)])
    ch = next(iter(stems.values())).shape[1]
    mix = np.zeros((n, ch), np.float32)
    for name, x in stems.items():
        mix += x[:n] * lead_g[:, None]
    hand = np.zeros((n, ch), np.float32)
    for name in HANDOVER_STEMS:
        if name in stems:
            hand += stems[name][:n]
    pts = []
    if not first:
        pts += [(pre, 0), (lead - r, 1), (lead, 0)]
    else:
        pts += [(0, 0)]
    if not last:
        pts += [(end, 0), (end + r, 1), (post, 0)]
    hand_g = _env(n, sr, pts)
    if hand_g.any():
        mix += _hp(hand, sr).astype(np.float32) * hand_g[:, None]
    return slot["pre"], mix, lead_g


def render(songs: List[dict], target: float, out_wav: Path, tmp: Path) -> dict:
    """songs: [{name, bpm, cam, stems (dir), window}]. Writes out_wav, returns the QC report."""
    slots = plan(songs, target)
    sr = None
    layers = []
    for s, slot in zip(songs, slots):
        w, ratio = s["window"], slot["ratio"]
        pad = 0.25
        t0 = w["start"] - (slot["lead"] - slot["pre"]) * ratio - pad
        t1 = w["end"] + (slot["post"] - slot["lead_end"]) * ratio + pad
        raw = {}
        for n in STEMS:
            f = Path(s["stems"]) / f"{n}.flac"
            if not f.exists():
                f = f.with_suffix(".wav")
            info = sf.info(str(f))
            sr = sr or info.samplerate
            if info.samplerate != sr:
                raise ValueError(f"{f}: sample rate {info.samplerate} != {sr}")
            a, b = int(max(t0, 0) * sr), int(t1 * sr)
            x, _ = sf.read(str(f), start=a, stop=b, dtype="float32", always_2d=True)
            if t0 < 0:
                x = np.vstack([np.zeros((int(-t0 * sr), x.shape[1]), np.float32), x])
            raw[n] = x
        d = tmp / f"s{slot['i']:02d}"
        d.mkdir(parents=True, exist_ok=True)
        st = _stretch(raw, sr, ratio, d)
        cut = int(round(pad * ratio * sr))
        st = {n: x[cut:] for n, x in st.items()}
        off, audio, lead_g = song_layer(st, sr, slot, slot["i"] == 0, slot["i"] == len(songs) - 1)
        a, b = int((slot["lead"] - off) * sr), int((slot["lead_end"] - off) * sr)
        rms = float(np.sqrt(np.mean(audio[a:b] ** 2)) + 1e-12)
        g = 10 ** (TARGET_RMS_DBFS / 20) / rms
        layers.append((off, audio * g, lead_g, round(20 * np.log10(g), 2)))
    total = int(max(off * sr + len(a_) for off, a_, *_ in layers)) + 1
    out = np.zeros((total, layers[0][1].shape[1]), np.float32)
    for off, audio, *_ in layers:
        o = int(round(off * sr))
        out[o:o + len(audio)] += audio
    peak = float(np.max(np.abs(out)))
    norm = 10 ** (PEAK_DBFS / 20) / peak
    out *= norm
    sf.write(str(out_wav), out, sr, subtype="PCM_24")
    return qc(out, sr, layers, norm, slots)


def qc(out: np.ndarray, sr: int, layers, norm: float, slots) -> dict:
    """Per-second RMS, peak, and sub-band ownership (seconds where two songs play < SUB_HZ at full level)."""
    secs = len(out) // sr
    rms = [round(float(20 * np.log10(np.sqrt(np.mean(out[i * sr:(i + 1) * sr] ** 2)) + 1e-12)), 1) for i in range(secs)]
    # measured sub owner per 100 ms block: a song "owns" the sub where its own < SUB_HZ band is within
    # FULL_DB of its median sub level over its lead window
    blk = sr // 10
    owners = np.zeros(len(out) // blk + 2, int)
    for (off, audio, lead_g, _), sl in zip(layers, slots):
        low = sosfiltfilt(butter(8, SUB_HZ, "lowpass", fs=sr, output="sos"), audio[:, 0])
        m = len(low) // blk
        db = 20 * np.log10(np.sqrt(np.mean(low[:m * blk].reshape(m, blk) ** 2, axis=1)) + 1e-9)
        a, b = int((sl["lead"] - off) * 10), int((sl["lead_end"] - off) * 10)
        ref = float(np.median(db[a:b]))
        o = int(round(off * 10))
        owners[o:o + m] += db > ref - FULL_DB
    for sl in slots[1:]:      # the swap downbeat itself: a block straddling it holds both halves, not a clash
        k = int(sl["lead"] * 10)
        owners[max(k - 1, 0):k + 2] = 0
    return {"sr": sr, "duration": round(len(out) / sr, 2),
            "peak_dbfs": round(float(20 * np.log10(np.max(np.abs(out)))), 2),
            "rms_min_dbfs": min(rms), "rms_max_dbfs": max(rms), "rms_per_second": rms,
            "silent_seconds": [i for i, v in enumerate(rms) if v < -40],
            "sub_clash_blocks": int((owners > 1).sum()),
            "norm_db": round(20 * np.log10(norm), 2), "gains_db": [l[3] for l in layers]}


def select_chain(cache_dir: str, n: int = 12, lanes: Optional[Dict[str, int]] = None,
                 bpm_lo: float = 124.5, bpm_hi: float = 129.5) -> List[dict]:
    """Best-`works` path of n songs through the pair atlas that passes the live gates (Bass / Drop Swap only)."""
    import sqlite3
    from app.music_brain.analysis.genre import family_jump
    from app.music_brain.analysis.genre_labels import name_key, title_key
    from app.music_brain.atlas import vetoes as V
    from app.music_brain.matching.techniques import camelot_score
    lanes = lanes or {"melodic house": 0, "progressive house": 0, "melodic techno": 1, "bass house": 1, "house": 2}
    db = sqlite3.connect(f"file:{cache_dir}/app.db?mode=ro", uri=True)
    labels = dict(db.execute("select key, genre from labels where genre is not null"))
    cand = {}
    for tid, name, bpm, cam, feat in db.execute("select id, name, bpm, camelot, features from atlas_tracks"):
        g = labels.get(name_key(name)) or labels.get(title_key(name))
        if g not in lanes or not bpm or not bpm_lo <= bpm <= bpm_hi or not cam:
            continue
        if any(w in name.lower() for w in ("unreleased", "set cut")):
            continue
        st = [s for s in glob.glob(f"{cache_dir}/stems/{tid}*_htdemucs_ft") if os.path.exists(s + "/bass.flac")]
        an = glob.glob(f"{cache_dir}/analysis/{tid}*.v5.json")
        if st and an:
            f = json.loads(feat or "{}")
            cand[tid] = dict(id=tid, name=name, bpm=bpm, cam=cam, genre=g, stems=st[0], analysis=an[0],
                             stem_bars={k: f.get(k) for k in ("anchor", "bar", "bars")} if f.get("bars") else None)
    vet = V.load(cache_dir=Path(cache_dir))
    ids = list(cand)
    marks = ",".join("?" * len(ids))
    nxt: Dict[str, list] = {}
    for a, b, works, data in db.execute(
            f"select a, b, works, data from atlas_pairs where a in ({marks}) and b in ({marks})", ids + ids):
        A, B = cand[a], cand[b]
        key = camelot_score(A["cam"], B["cam"])
        if key < KEY_SAFE_MIN or abs(A["bpm"] / B["bpm"] - 1) > TEMPO_GATE or family_jump(A["genre"], B["genre"]):
            continue
        if V.blocked(vet, A["name"], B["name"], A["genre"]):
            continue
        mv = json.loads(data).get("moves", {})
        bs, ds = mv.get("bass_swap", [0, 0]), mv.get("drop_swap", [0, 0])
        if not (bs[0] or ds[0]):
            continue
        move = "Drop Swap" if ds[0] and ds[1] > bs[1] else "Bass Swap"
        nxt.setdefault(a, []).append((works + 10 * (lanes[B["genre"]] == 0), b, works, move, key))
    for v in nxt.values():
        v.sort(reverse=True)
    best = [0.0, None]
    budget = [0]

    def dfs(path, steps, score):
        budget[0] += 1
        if budget[0] > 2_000_000:
            return
        if len(path) == n:
            if score > best[0]:
                best[0], best[1] = score, (list(path), list(steps))
            return
        for w, b, works, move, key in nxt.get(path[-1], [])[:5]:
            if b not in path:
                dfs(path + [b], steps + [(works, move, key)], score + w)

    for s in sorted(ids, key=lambda i: lanes[cand[i]["genre"]]):
        dfs([s], [], 0.0)
    if not best[1]:
        return []
    path, steps = best[1]
    return [dict(cand[t], works=steps[i][0] if i < len(steps) else None, move=steps[i][1] if i < len(steps) else None,
                 key_score=steps[i][2] if i < len(steps) else None) for i, t in enumerate(path)]


def _mmss(s: float) -> str:
    return f"{int(s // 60)}:{s % 60:04.1f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", required=True, help="data/cache dir (read only)")
    ap.add_argument("--out", required=True, help="output stem path, no extension")
    ap.add_argument("--songs", type=int, default=12)
    ap.add_argument("--tmp", default=None)
    args = ap.parse_args(argv)
    try:
        chain = select_chain(args.cache, args.songs)
        if len(chain) < args.songs:
            raise ValueError(f"only {len(chain)} songs chain through the gates")
        for s in chain:
            s["window"] = pick_window(json.loads(Path(s["analysis"]).read_text()), s.get("stem_bars"))
            if not s["window"]:
                raise ValueError(f"{s['name']}: no 16-bar window with room for the handover")
        tgt = target_bpm([s["bpm"] for s in chain])
        tmp = Path(args.tmp or tempfile.mkdtemp())
        out = Path(args.out)
        report = render(chain, tgt, out.with_suffix(".wav"), tmp)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(out.with_suffix(".wav")),
                        "-b:a", "320k", str(out.with_suffix(".mp3"))], check=True, timeout=600)
        slots = plan(chain, tgt)
        lines = [f"Swap mix: {len(chain)} songs, {tgt} BPM (median, key-locked), {LEAD_BARS} bars each, "
                 f"{SWAP_BARS}-bar Bass Swap handovers, {_mmss(report['duration'])} total",
                 "start | song | genre | key | BPM | stretch % | window in source (s) | swap into next"]
        for s, sl in zip(chain, slots):
            w = s["window"]
            lines.append(f"{_mmss(sl['lead'])} | {s['name']} | {s['genre']} | {s['cam']} | {s['bpm']} | "
                         f"{sl['stretch_pct']:+.2f} | {w['start']:.1f}-{w['end']:.1f} ({w['how']}) | "
                         f"{s['move'] or 'end (fade out)'}" + (f" (atlas works {s['works']:.0f}, key {s['key_score']})"
                                                              if s["move"] else ""))
        lines.append(f"QC: peak {report['peak_dbfs']} dBFS, per-second RMS {report['rms_min_dbfs']} to "
                     f"{report['rms_max_dbfs']} dBFS, seconds under -40 dBFS: {len(report['silent_seconds'])}, "
                     f"100 ms blocks with two full-level sub owners: {report['sub_clash_blocks']}")
        out.with_suffix(".txt").write_text("\n".join(lines) + "\n")
        report.pop("rms_per_second")
        print(json.dumps({"tracklist": str(out.with_suffix(".txt")), "target_bpm": tgt, **report}))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
