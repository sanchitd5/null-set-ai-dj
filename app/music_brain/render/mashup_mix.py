"""Offline multi-song stem mashup: a chain of songs layered by stems with rolling hand-offs, so 2 (sometimes 3)
songs sound together most of the time. Owner, after hearing swap-mix-30s: "time shift was not restricted to 30
i wanted multiple songs mashup currently there is a lot of energy shift".

Per song (./DJ/ [[Stems Transition]], [[Live Mashup]], [[Bass Swap]], [[EQ & Frequency Management]]):
  enter  ENTER_BARS before its core: its `other` stem only (lead / pads), high-passed at 120 Hz, fading in
  core   16 bars, 32 for a song the owner liked: all four stems; the bass lands on the core's first downbeat
  exit   EXIT_BARS after its core: its drums only, high-passed, under the next song's core
A song enters by one stem and leaves by another; only a core owns the sub (< 120 Hz) and only a core sings (never
two vocals). A core vocal is muted wherever it would sit over the layered song's drop window or its sung line into
it (drop_line.song_busy: "never vocal mix a drop line"). An entering stem with no energy is not layered (CLAUDE.md
s5, stem-moves.js:pickIntro). Energy: every 8-bar block of a core reaches BAND of the song's own strongest block
(no dips to a breakdown), cores are RMS-matched, a slow per-8-bar bus leveller (max LEVEL_MAX_DB) evens the rest.
Layered pairs need Camelot >= mashup.MIN_KEY_SCORE (0.8) plus the live gates (swap_mix.select_chain: genre family,
tempo, vetoes, atlas moves).

Two halves, so a live autopilot option can reuse the first one later:
  build_plan(chain, bpm) -> pure JSON plan (songs, parts = stem + gain envelope + high-pass on the output clock,
                            vocal mutes, 8-bar sections), no audio
  render_plan(plan, ...) -> reads the stems, key-locks, applies the parts, levels, QC
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

import numpy as np
import soundfile as sf

from app.music_brain.render import drop_line
from app.music_brain.render import swap_mix as sm
from app.music_brain.render.blend import drop_lines
from app.music_brain.render.mashup import MIN_KEY_SCORE

ENTER_BARS = 16
EXIT_BARS = 8
BLOCK_BARS = 8
ENTER_GAIN = 0.7          # the entering lead sits under the core's own lead
BAND = 0.85               # every 8-bar block of a core >= this share of the song's strongest 8-bar block
LEVEL_MAX_DB = 3.0        # bus leveller cap per 8-bar block
LOW_HZ = 150.0            # energy QC also measures the band under this (kick + bass)
QUIET_SHARE = 0.1        # entering `other` level under this share of its core level: no energy, not layered


def _bar_levels(stem_bars: Optional[dict], stems=("drums", "bass", "other")):
    if not stem_bars or not stem_bars.get("bars"):
        return None
    B = stem_bars["bars"]
    n = min(len(B.get(s) or []) for s in stems)
    return sum(np.asarray(B[s][:n], float) for s in stems) if n else None


def _level_fn(analysis, stem_bars: Optional[dict], stems=("drums", "bass", "other")):
    """mean(t, n_bars) -> level of the song from t (native s): atlas stem bars, else the analysis energy curve."""
    bar = 240.0 / float(sm._g(analysis, "bpm"))
    lv = _bar_levels(stem_bars, stems)
    if lv is not None:
        def mean(t, k):
            i = int(round((t - stem_bars["anchor"]) / stem_bars["bar"]))
            seg = lv[max(i, 0):i + k] if i >= 0 else []
            return float(np.mean(seg)) if len(seg) == k else -1.0
        return mean
    times = np.asarray(sm._g(analysis, "energy_times") or [], float)
    curve = np.asarray(sm._g(analysis, "energy_curve") or [], float)

    def mean(t, k):
        m = (times >= t) & (times < t + k * bar)
        return float(curve[m].mean()) if m.any() else -1.0
    return mean


def pick_core(analysis, stem_bars: Optional[dict] = None, long: bool = False, enter_bars: int = ENTER_BARS,
              exit_bars: int = EXIT_BARS) -> Optional[dict]:
    """{start, end, bars, how, share} of the song's core on an 8-bar phrase line: 32 bars when `long` and one fits,
    else 16, whose weakest 8-bar block reaches BAND of the song's strongest block; drop lines first, then the highest
    weakest block. Falls back to swap_mix.pick_window (flagged "below band"). None if nothing fits."""
    bpm = float(sm._g(analysis, "bpm") or 0)
    dur = float(sm._g(analysis, "duration") or 0)
    if bpm <= 0 or dur <= 0:
        return None
    bar = 240.0 / bpm
    phrases = list(sm._g(analysis, "phrase_boundaries_8bar") or [])
    mean = _level_fn(analysis, stem_bars)
    top = max([mean(t, BLOCK_BARS) for t in phrases] or [-1.0])
    if top <= 0:
        return None
    drops = [t for t, _, _ in drop_lines(phrases, sm._g(analysis, "energy_times") or [],
                                         sm._g(analysis, "energy_curve") or [], bar)]
    for k in ((32, 16) if long else (16,)):
        cand = []
        for t in phrases:
            if t - enter_bars * bar < 0 or t + (k + exit_bars) * bar > dur:
                continue
            m = min(mean(t + j * BLOCK_BARS * bar, BLOCK_BARS) for j in range(k // BLOCK_BARS))
            if m >= BAND * top:
                cand.append((any(abs(t - d) < 1e-3 for d in drops), m, t))
        if cand:
            is_drop, m, t = max(cand)
            return {"start": t, "end": t + k * bar, "bars": k, "share": round(m / top, 2),
                    "how": "drop line" if is_drop else "full-groove phrase"}
    w = sm.pick_window(analysis, stem_bars, 16, enter_bars, exit_bars)
    if not w:
        return None
    m = min(mean(w["start"] + j * BLOCK_BARS * bar, BLOCK_BARS) for j in range(2))
    return dict(w, bars=16, share=round(m / top, 2), how=w["how"] + " (below band)")


def enter_ok(analysis, stem_bars: Optional[dict], core_start: float, enter_bars: int) -> bool:
    """False when the song's `other` stem has no energy in the enter window (under QUIET_SHARE of its core level).
    Unknown (no stem bars) passes."""
    if not enter_bars or _bar_levels(stem_bars, ("other",)) is None:
        return True
    mean = _level_fn(analysis, stem_bars, ("other",))
    bar = 240.0 / float(sm._g(analysis, "bpm"))
    e, c = mean(core_start - enter_bars * bar, enter_bars), mean(core_start, BLOCK_BARS)
    return c <= 0 or e >= QUIET_SHARE * c


def slots(chain: List[dict], target: float) -> List[dict]:
    """Output clock: cores back to back; song i's segment runs its enter bars before its core (none for the first)
    to EXIT_BARS after it. Each song needs `core` and `bpm`; `enter_bars` (optional) overrides ENTER_BARS."""
    bar = 240.0 / target
    out, t = [], 0.0
    for i, s in enumerate(chain):
        if not sm.stretch_ok(s["bpm"], target):
            raise ValueError(f"{s.get('name')}: {s['bpm']} -> {target} BPM is past the {sm.MAX_STRETCH:.0%} key-lock cap")
        e = 0 if i == 0 else s.get("enter_bars", ENTER_BARS)
        k = s["core"]["bars"]
        out.append({"i": i, "enter_bars": e, "core_bars": k, "exit_bars": EXIT_BARS,
                    "pre": t - e * bar, "lead": t, "lead_end": t + k * bar, "post": t + (k + EXIT_BARS) * bar,
                    "ratio": s["bpm"] / target, "stretch_pct": round((target / s["bpm"] - 1) * 100, 2),
                    "vocal_mute": []})
        t += k * bar
    return out


def vocal_mutes(chain: List[dict], sl: List[dict], target: float) -> None:
    """Fill sl[i]['vocal_mute'] (output seconds) where a core's vocal would ride a layered song's drop window or its
    sung line into it (drop_line.song_busy on the layered song's own clock)."""
    bar = 240.0 / target
    for i, (s, c) in enumerate(zip(chain, sl)):
        regions = drop_line.vocal_regions(s.get("an"))
        if not regions:
            continue
        if i > 0:        # song i-1's drums play on under the first EXIT_BARS of this core
            p = chain[i - 1]
            t0 = p["core"]["end"]
            t1 = t0 + EXIT_BARS * 240.0 / p["bpm"]
            if drop_line.song_busy(p.get("an"), t0, t1, p["bpm"],
                                   drop_line.mapped_sings(regions, t0, s["core"]["start"], p["bpm"] / s["bpm"])):
                c["vocal_mute"].append((c["lead"], c["lead"] + EXIT_BARS * bar))
        if i + 1 < len(chain) and sl[i + 1]["enter_bars"]:   # the next song's other stem enters over this core
            q, e = chain[i + 1], min(sl[i + 1]["enter_bars"], c["core_bars"])
            t1 = q["core"]["start"]
            t0 = t1 - e * 240.0 / q["bpm"]
            at0 = s["core"]["end"] - e * 240.0 / s["bpm"]
            if drop_line.song_busy(q.get("an"), t0, t1, q["bpm"],
                                   drop_line.mapped_sings(regions, t0, at0, q["bpm"] / s["bpm"])):
                c["vocal_mute"].append((c["lead_end"] - e * bar, c["lead_end"]))


def _parts(c: dict, first: bool, last: bool, bar: float) -> List[dict]:
    """Stem parts of one song: {stem, role, env [[t_out, gain]], hp_hz, mute [[a, b]]} on the output clock."""
    r = sm.RAMP_S
    pre, L, E, P = c["pre"], c["lead"], c["lead_end"], c["post"]
    head = [[pre, 1]] if first else [[L - r, 0], [L, 1]]
    tail = [[E, 1], [P - r, 0.12], [P, 0]] if last else [[E, 1], [E + r, 0]]   # the fade never dips under -40 dBFS
    out = [{"stem": n, "role": "core", "env": head + tail, "hp_hz": None,
            "mute": [list(m) for m in c["vocal_mute"]] if n == "vocals" else []} for n in sm.STEMS]
    if c["enter_bars"]:
        e = c["enter_bars"] * bar
        out.append({"stem": "other", "role": "enter", "hp_hz": sm.SUB_HZ, "mute": [],
                    "env": [[pre, 0], [pre + e / 2, ENTER_GAIN], [L - r, ENTER_GAIN], [L, 0]]})
    if not last:
        out.append({"stem": "drums", "role": "exit", "hp_hz": sm.SUB_HZ, "mute": [],
                    "env": [[E - r, 0], [E, 1], [E + (P - E) / 2, 1], [P, 0]]})
    return out


def sections(chain: List[dict], sl: List[dict], bar: float, total: float) -> List[dict]:
    """One entry per 8-bar block: {start, songs: [{i, name, stems, role}]}."""
    out = []
    for j in range(int(np.ceil(total / (BLOCK_BARS * bar) - 1e-9))):
        t = j * BLOCK_BARS * bar
        mid = t + BLOCK_BARS * bar / 2
        on = []
        for s, c in zip(chain, sl):
            if c["enter_bars"] and c["pre"] <= mid < c["lead"]:
                on.append({"i": c["i"], "name": s["name"], "stems": ["other"], "role": "enter"})
            elif c["lead"] <= mid < c["lead_end"]:
                muted = any(a <= mid < b for a, b in c["vocal_mute"])
                on.append({"i": c["i"], "name": s["name"], "role": "core",
                           "stems": ["drums", "bass", "other"] + ([] if muted else ["vocals"]),
                           **({"vocals_muted": "drop line"} if muted else {})})
            elif c["lead_end"] <= mid < c["post"]:
                last = c["i"] == len(sl) - 1
                on.append({"i": c["i"], "name": s["name"], "role": "fade" if last else "exit",
                           "stems": list(sm.STEMS) if last else ["drums"]})
        out.append({"start": round(t, 3), "songs": on})
    return out


def build_plan(chain: List[dict], target: float) -> dict:
    """Pure plan (JSON-safe, no audio) for a chain from swap_mix.select_chain with `an` (analysis), `core`
    (pick_core) and optional `stem_bars` per song."""
    bar = 240.0 / target
    for i, s in enumerate(chain):
        if i and not enter_ok(s.get("an"), s.get("stem_bars"), s["core"]["start"], ENTER_BARS):
            s["enter_bars"], s["enter_refused"] = 0, "entering other stem has no energy"
    sl = slots(chain, target)
    vocal_mutes(chain, sl, target)
    songs = []
    for s, c in zip(chain, sl):
        bar_n = 240.0 / s["bpm"]
        songs.append({
            "i": c["i"], "id": s.get("id"), "name": s["name"], "genre": s.get("genre"), "key": s.get("cam"),
            "bpm": s["bpm"], "ratio": c["ratio"], "stretch_pct": c["stretch_pct"], "stems_dir": s.get("stems"),
            "core": s["core"], "enter_bars": c["enter_bars"], "exit_bars": c["exit_bars"],
            "enter_refused": s.get("enter_refused"),
            "src": [s["core"]["start"] - c["enter_bars"] * bar_n, s["core"]["end"] + c["exit_bars"] * bar_n],
            "out": [c["pre"], c["post"]], "core_out": [c["lead"], c["lead_end"]],
            "gain": {"rms_match_dbfs": sm.TARGET_RMS_DBFS, "over": "core"},
            "next": {"key_score": s.get("key_score"), "works": s.get("works"), "atlas_move": s.get("move")}
            if s.get("move") else None,
            "parts": _parts(c, c["i"] == 0, c["i"] == len(sl) - 1, bar),
        })
    total = sl[-1]["post"]
    return {"kind": "stem_mashup", "bpm": target, "bar_s": bar, "duration": total, "songs": songs,
            "sections": sections(chain, sl, bar, total),
            "rules": {"key_min": MIN_KEY_SCORE, "sub_hz": sm.SUB_HZ, "max_stretch": sm.MAX_STRETCH, "band": BAND,
                      "enter_bars": ENTER_BARS, "exit_bars": EXIT_BARS, "enter_gain": ENTER_GAIN}}


# ------------------------------------------------------------------------------------------------ renderer

def block_db(x: np.ndarray, sr: int, block_s: float) -> List[float]:
    """RMS dBFS of every full block of block_s seconds."""
    k = int(round(block_s * sr))
    return [round(float(20 * np.log10(np.sqrt(np.mean(x[i * k:(i + 1) * k] ** 2)) + 1e-12)), 2)
            for i in range(len(x) // k)]


def energy_steps(x: np.ndarray, sr: int, bpm: float, drop_last: bool = True, lowpass_hz: Optional[float] = None) -> dict:
    """Per-8-bar RMS of a finished mix: blocks, max step between neighbours, overall range (dB).
    drop_last: the final block is the fade-out, left out of the numbers. lowpass_hz: measure only the band under it
    (the kick + bass drive, where a drop-to-groove change shows most)."""
    if lowpass_hz:
        from scipy.signal import butter, sosfiltfilt
        x = sosfiltfilt(butter(4, lowpass_hz, "lowpass", fs=sr, output="sos"), x, axis=0)
    b = block_db(x, sr, BLOCK_BARS * 240.0 / bpm)
    if drop_last and len(b) > 2:
        b = b[:-1]
    steps = [abs(q - p) for p, q in zip(b, b[1:])]
    return {"blocks_db": b, "max_step_db": round(max(steps or [0.0]), 2), "range_db": round(max(b) - min(b), 2)}


def level(out: np.ndarray, sr: int, block_s: float) -> List[float]:
    """Slow bus leveller: each block moves toward the median block level (capped at LEVEL_MAX_DB), gains
    interpolated between block centres so nothing steps. Returns the block gains (dB)."""
    k = int(round(block_s * sr))
    b = np.asarray(block_db(out, sr, block_s))
    if len(b) < 2:
        return []
    g = np.clip(float(np.median(b)) - b, -LEVEL_MAX_DB, LEVEL_MAX_DB)
    gain = 10 ** (np.interp(np.arange(len(out)), (np.arange(len(b)) + 0.5) * k, g) / 20)
    out *= gain.astype(np.float32)[:, None]
    return [round(float(v), 2) for v in g]


def _env(n: int, sr: int, pre: float, pts, mutes) -> np.ndarray:
    g = sm._env(n, sr, [(t - pre, v) for t, v in pts])
    r = sm.RAMP_S
    for a, b in mutes:
        g *= sm._env(n, sr, [(0, 1), (a - pre - r, 1), (a - pre, 0), (b - pre, 0), (b - pre + r, 1), (n / sr + 1, 1)])
    return g


def song_audio(stems, sr: int, song: dict) -> np.ndarray:
    """One song's parts on its own segment (starts at song['out'][0])."""
    pre, post = song["out"]
    n = min(min(len(x) for x in stems.values()), int(round((post - pre) * sr)))
    ch = next(iter(stems.values())).shape[1]
    mix = np.zeros((n, ch), np.float32)
    for p in song["parts"]:
        x = stems.get(p["stem"])
        if x is None:
            continue
        x = x[:n]
        if p["hp_hz"]:
            x = sm._hp(x, sr).astype(np.float32)
        mix += x * _env(n, sr, pre, p["env"], p["mute"])[:, None]
    return mix


def render_plan(plan: dict, out_wav: Path, tmp: Path) -> dict:
    """Render a build_plan plan to out_wav; returns the QC report."""
    sr, layers, pad = None, [], 0.25
    for s in plan["songs"]:
        t0, t1 = s["src"][0] - pad, s["src"][1] + pad
        raw = {}
        for n in sm.STEMS:
            f = Path(s["stems_dir"]) / f"{n}.flac"
            if not f.exists():
                f = f.with_suffix(".wav")
            info = sf.info(str(f))
            sr = sr or info.samplerate
            if info.samplerate != sr:
                raise ValueError(f"{f}: sample rate {info.samplerate} != {sr}")
            x, _ = sf.read(str(f), start=int(max(t0, 0) * sr), stop=int(t1 * sr), dtype="float32", always_2d=True)
            if t0 < 0:
                x = np.vstack([np.zeros((int(-t0 * sr), x.shape[1]), np.float32), x])
            raw[n] = x
        d = tmp / f"m{s['i']:02d}"
        d.mkdir(parents=True, exist_ok=True)
        st = sm._stretch(raw, sr, s["ratio"], d)
        cut = int(round(pad * s["ratio"] * sr))
        audio = song_audio({n: x[cut:] for n, x in st.items()}, sr, s)
        a, b = (int((t - s["out"][0]) * sr) for t in s["core_out"])
        g = 10 ** (sm.TARGET_RMS_DBFS / 20) / (float(np.sqrt(np.mean(audio[a:b] ** 2))) + 1e-12)
        layers.append((s["out"][0], audio * g, None, round(20 * np.log10(g), 2)))
    total = int(max(off * sr + len(x) for off, x, *_ in layers)) + 1
    out = np.zeros((total, layers[0][1].shape[1]), np.float32)
    for off, audio, *_ in layers:
        o = int(round(off * sr))
        out[o:o + len(audio)] += audio
    lev = level(out, sr, BLOCK_BARS * plan["bar_s"])
    norm = 10 ** (sm.PEAK_DBFS / 20) / float(np.max(np.abs(out)))
    out *= norm
    sf.write(str(out_wav), out, sr, subtype="PCM_24")
    qslots = [{"lead": s["core_out"][0], "lead_end": s["core_out"][1]} for s in plan["songs"]]
    rep = sm.qc(out, sr, layers, norm, qslots)
    rep["leveller_db"] = lev
    rep["energy"] = energy_steps(out, sr, plan["bpm"])
    rep["energy_low"] = energy_steps(out, sr, plan["bpm"], lowpass_hz=LOW_HZ)
    return rep


def timeline_lines(plan: dict) -> List[str]:
    lines = []
    for sec in plan["sections"]:
        parts = []
        for x in sec["songs"]:
            what = {"enter": "other (entering, HP 120)", "exit": "drums (leaving, HP 120)",
                    "fade": "full mix fading out"}.get(x["role"]) or "+".join(x["stems"]) + (
                " (vocals muted: drop line)" if x.get("vocals_muted") else "")
            parts.append(f"{x['name'][:42]}: {what}")
        lines.append(f"{sm._mmss(sec['start'])} [{len(parts)}] " + " | ".join(parts))
    return lines


def liked_names(cache_dir: str) -> set:
    """Song names in the owner's liked pairs (user.db marks, read only)."""
    try:
        db = sqlite3.connect(f"file:{cache_dir}/user.db?mode=ro", uri=True)
        rows = db.execute("select data from marks where kind = 'liked'").fetchall()
    except sqlite3.Error:
        return set()
    out = set()
    for (d,) in rows:
        try:
            e = json.loads(d or "{}")
        except ValueError:
            continue
        out.update(x for x in (e.get("a_name"), e.get("b_name")) if x)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", required=True, help="data/cache dir (read only)")
    ap.add_argument("--out", required=True, help="output stem path, no extension")
    ap.add_argument("--before", default=None, help="an earlier mix .wav to measure the same energy numbers on")
    ap.add_argument("--songs", type=int, default=12)
    ap.add_argument("--tmp", default=None)
    ap.add_argument("--plan-only", action="store_true", help="print the JSON plan, render nothing")
    args = ap.parse_args(argv)
    try:
        if args.songs < 8:
            raise ValueError("a mashup needs at least 8 songs")
        chain = sm.select_chain(args.cache, args.songs, key_min=MIN_KEY_SCORE)
        if len(chain) < args.songs:
            raise ValueError(f"only {len(chain)} songs chain through the gates")
        liked = liked_names(args.cache)
        for s in chain:
            s["an"] = json.loads(Path(s["analysis"]).read_text())
            s["core"] = pick_core(s["an"], s.get("stem_bars"), long=s["name"] in liked)
            if not s["core"]:
                raise ValueError(f"{s['name']}: no core window with room for the enter / exit layers")
        plan = build_plan(chain, sm.target_bpm([s["bpm"] for s in chain]))
        out = Path(args.out)
        if args.plan_only:
            print(json.dumps(plan))
            return 0
        out.with_suffix(".plan.json").write_text(json.dumps(plan, indent=1))
        rep = render_plan(plan, out.with_suffix(".wav"), Path(args.tmp or tempfile.mkdtemp()))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(out.with_suffix(".wav")),
                        "-b:a", "320k", str(out.with_suffix(".mp3"))], check=True, timeout=600)
        before = None
        if args.before and Path(args.before).exists():
            x, bsr = sf.read(args.before, dtype="float32", always_2d=True)
            before = {"rms": energy_steps(x, bsr, plan["bpm"]),
                      "low": energy_steps(x, bsr, plan["bpm"], lowpass_hz=LOW_HZ)}
        lines = [f"Stem mashup mix: {len(chain)} songs, {plan['bpm']} BPM (median, key-locked), cores of 16 bars "
                 f"(32 for liked songs); each song enters by its other stem ({ENTER_BARS} bars) and leaves by its "
                 f"drums ({EXIT_BARS} bars); {sm._mmss(rep['duration'])} total",
                 "core start | song | genre | key | BPM | stretch % | core in source (s) | key score into next"]
        for s in plan["songs"]:
            c = s["core"]
            lines.append(f"{sm._mmss(s['core_out'][0])} | {s['name']} | {s['genre']} | {s['key']} | {s['bpm']} | "
                         f"{s['stretch_pct']:+.2f} | {c['start']:.1f}-{c['end']:.1f} ({c['bars']} bars, {c['how']}, "
                         f"weakest block {c['share']:.0%} of top) | "
                         + (str(s["next"]["key_score"]) if s["next"] else "end")
                         + (f" | {s['enter_refused']}" if s["enter_refused"] else ""))
        e, lo = rep["energy"], rep["energy_low"]
        lines +=["", "TIMELINE (8-bar blocks, [songs audible]):"] + timeline_lines(plan) + [
            "", f"QC: peak {rep['peak_dbfs']} dBFS, per-second RMS {rep['rms_min_dbfs']} to {rep['rms_max_dbfs']} "
                f"dBFS, seconds under -40: {len(rep['silent_seconds'])}, 100 ms blocks with two full-level sub "
                f"owners: {rep['sub_clash_blocks']}",
            f"ENERGY per 8 bars (RMS dB, fade-out block left out): max step {e['max_step_db']} dB, "
            f"range {e['range_db']} dB; band < {LOW_HZ:.0f} Hz: max step {lo['max_step_db']} dB, "
            f"range {lo['range_db']} dB"]
        if before:
            lines.append(f"ENERGY before ({Path(args.before).name}): max step {before['rms']['max_step_db']} dB, "
                         f"range {before['rms']['range_db']} dB; band < {LOW_HZ:.0f} Hz: max step "
                         f"{before['low']['max_step_db']} dB, range {before['low']['range_db']} dB")
        out.with_suffix(".txt").write_text("\n".join(lines) + "\n")
        rep.pop("rms_per_second")
        print(json.dumps({"tracklist": str(out.with_suffix(".txt")), "target_bpm": plan["bpm"], "before": before,
                          **rep}))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
