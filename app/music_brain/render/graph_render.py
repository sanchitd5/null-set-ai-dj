"""Render a captured console audio graph (app/sim/stem_capture.py) on the real audio.

The capture holds what the console's Web Audio graph was told to do while it played one
transition: every node, every AudioParam's automation timeline, every connect / disconnect
with its time, and each buffer source's song position over time. This module replays that
on the real files (mix + 4 stems) at 44.1 kHz stereo:

* AudioParam values follow the same timeline rules as the sim's recording API
  (app/sim/js/webaudio.js FakeAudioParam.valueAt: set / linear / exponential / setTarget /
  setValueCurve), evaluated per sample.
* Biquads use the Web Audio spec (Audio EQ Cookbook) coefficients, updated every 64 samples.
* Buffer sources read the song at the captured position (linear interpolation), so rate
  automation and seeks are exactly what the console scheduled.
* The master DynamicsCompressor is modelled after the spec's gain computer (threshold,
  knee, ratio, attack, release, auto make-up gain); that is an approximation, listed in
  `approximations`.

Anything the graph used on the audible path that is not modelled (worklets, oscillators,
param modulation inputs, feedback loops, channel splitters) is listed in `not_rendered`
instead of being faked.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
from scipy.signal import lfilter

SR = 44100
BLOCK = 64          # biquad coefficient update period (samples)
SUPPORTED = {"gain", "biquad", "delay", "panner", "compressor", "shaper", "analyser", "source", "destination"}


# ---------------------------------------------------------------- AudioParam timelines
def param_curve(p: dict, t: np.ndarray) -> np.ndarray:
    """Value of a captured AudioParam at the (ascending) times `t`, same rules as
    FakeAudioParam.valueAt: the segment before each event is decided by that event."""
    ev = p.get("ev") or []
    out = np.empty(len(t), dtype=np.float64)
    if not ev:
        out.fill(float(p.get("base", 0.0)))
        return out

    base = float(p.get("base", 0.0))
    g: Callable[[np.ndarray], np.ndarray] = lambda x: np.full(len(x), base)
    t_prev = -math.inf
    lo_i = 0
    for i, e in enumerate(ev):
        et = float(e["time"])
        hi_i = int(np.searchsorted(t, et, side="left"))       # t < et breaks at this event
        if hi_i > lo_i:
            tt = t[lo_i:hi_i]
            v = g(tt)
            if e["type"] in ("lin", "exp"):
                t0 = 0.0 if t_prev == -math.inf else t_prev
                on = tt > t0
                f = (tt - t0) / max(1e-9, et - t0)
                nv = float(e["value"])
                if e["type"] == "lin":
                    r = v + (nv - v) * f
                else:
                    vs = np.where(np.abs(v) > 0, v, 1e-6)
                    r = v * np.power(np.maximum(nv / vs, 1e-12), f)
                v = np.where(on, r, v)
            out[lo_i:hi_i] = v
            lo_i = hi_i
        # the value from this event on, until the next one decides
        v_start = float(g(np.array([et]))[0])
        if e["type"] == "target":
            tv, tc = float(e["value"]), max(1e-9, float(e["tc"]))
            g = (lambda tv, tc, et, vs: lambda x: tv + (vs - tv) * np.exp(-(x - et) / tc))(tv, tc, et, v_start)
        elif e["type"] == "curve":
            cv = np.asarray(e["curve"], dtype=np.float64)
            dur = max(1e-9, float(e["dur"]))
            g = (lambda cv, dur, et: lambda x: np.interp(
                np.clip((x - et) / dur, 0.0, 1.0) * (len(cv) - 1), np.arange(len(cv)), cv))(cv, dur, et)
        else:
            val = float(e["value"])
            g = (lambda val: lambda x: np.full(len(x), val))(val)
        t_prev = et
    if lo_i < len(t):
        out[lo_i:] = g(t[lo_i:])
    return out


# ---------------------------------------------------------------- biquads (Web Audio spec)
def biquad_coeffs(kind: str, f: np.ndarray, q: np.ndarray, gain_db: np.ndarray, sr: int = SR):
    """(b0, b1, b2, a1, a2) normalised by a0, per element."""
    nyq = sr / 2
    f = np.clip(f, 1e-3, nyq)
    w0 = 2 * np.pi * f / sr
    cw, sw = np.cos(w0), np.sin(w0)
    A = np.power(10.0, gain_db / 40)
    one = np.ones_like(f)
    if kind in ("lowpass", "highpass"):
        alpha = sw / (2 * np.power(10.0, q / 20))
        if kind == "lowpass":
            b0, b1, b2 = (1 - cw) / 2, 1 - cw, (1 - cw) / 2
        else:
            b0, b1, b2 = (1 + cw) / 2, -(1 + cw), (1 + cw) / 2
        a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    elif kind in ("bandpass", "notch", "allpass", "peaking"):
        alpha = sw / (2 * np.maximum(q, 1e-4))
        if kind == "bandpass":
            b0, b1, b2 = alpha, 0 * one, -alpha
            a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
        elif kind == "notch":
            b0, b1, b2 = one, -2 * cw, one
            a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
        elif kind == "allpass":
            b0, b1, b2 = 1 - alpha, -2 * cw, 1 + alpha
            a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
        else:
            b0, b1, b2 = 1 + alpha * A, -2 * cw, 1 - alpha * A
            a0, a1, a2 = 1 + alpha / A, -2 * cw, 1 - alpha / A
    elif kind in ("lowshelf", "highshelf"):
        alpha = sw / 2 * np.sqrt(2.0)                       # shelf slope S = 1 (Web Audio)
        sa = 2 * np.sqrt(A) * alpha
        if kind == "lowshelf":
            b0 = A * ((A + 1) - (A - 1) * cw + sa)
            b1 = 2 * A * ((A - 1) - (A + 1) * cw)
            b2 = A * ((A + 1) - (A - 1) * cw - sa)
            a0 = (A + 1) + (A - 1) * cw + sa
            a1 = -2 * ((A - 1) + (A + 1) * cw)
            a2 = (A + 1) + (A - 1) * cw - sa
        else:
            b0 = A * ((A + 1) + (A - 1) * cw + sa)
            b1 = -2 * A * ((A - 1) + (A + 1) * cw)
            b2 = A * ((A + 1) + (A - 1) * cw - sa)
            a0 = (A + 1) - (A - 1) * cw + sa
            a1 = 2 * ((A - 1) - (A + 1) * cw)
            a2 = (A + 1) - (A - 1) * cw - sa
    else:
        raise ValueError(f"biquad type {kind!r}")
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


def run_biquad(x: np.ndarray, kind: str, f: np.ndarray, q: np.ndarray, g: np.ndarray) -> np.ndarray:
    """x: (2, n). Params per sample; coefficients held for BLOCK samples."""
    n = x.shape[1]
    idx = np.arange(0, n, BLOCK)
    cf = biquad_coeffs(kind, f[idx], q[idx], g[idx])
    y = np.empty_like(x)
    const = all(np.allclose(c, c[0]) for c in cf)
    zi = [np.zeros(2), np.zeros(2)]
    for ch in range(x.shape[0]):
        if const:
            b = [cf[0][0], cf[1][0], cf[2][0]]
            a = [1.0, cf[3][0], cf[4][0]]
            y[ch] = lfilter(b, a, x[ch])
            continue
        z = zi[ch]
        for j, s in enumerate(idx):
            e = min(n, s + BLOCK)
            b = [cf[0][j], cf[1][j], cf[2][j]]
            a = [1.0, cf[3][j], cf[4][j]]
            y[ch, s:e], z = lfilter(b, a, x[ch, s:e], zi=z)
    return y


# ---------------------------------------------------------------- other nodes
def run_compressor(x: np.ndarray, p: Dict[str, np.ndarray]) -> np.ndarray:
    """Spec-shaped DynamicsCompressor: peak detector over both channels, static curve
    (threshold / knee / ratio), attack / release smoothing, 6 ms look-ahead, auto make-up
    gain (1 / curve(1.0)) ^ 0.6. Parameters taken at the window start."""
    thr, knee, ratio = float(p["threshold"][0]), float(p["knee"][0]), max(1.0, float(p["ratio"][0]))
    att, rel = max(1e-4, float(p["attack"][0])), max(1e-3, float(p["release"][0]))

    def curve_db(lv):
        over = lv - thr
        if knee <= 0:
            return np.where(over > 0, thr + over / ratio, lv)
        k2 = knee / 2
        soft = lv + (1 / ratio - 1) * (over + k2) ** 2 / (2 * knee)
        return np.where(over <= -k2, lv, np.where(over >= k2, thr + over / ratio, soft))

    makeup = (1.0 / 10 ** (float(curve_db(np.array([0.0]))[0]) / 20)) ** 0.6
    blk = 32
    n = x.shape[1]
    nb = (n + blk - 1) // blk
    pad = np.zeros((x.shape[0], nb * blk))
    pad[:, :n] = np.abs(x)
    peak = pad.reshape(x.shape[0], nb, blk).max(axis=2).max(axis=0)
    lvl = 20 * np.log10(np.maximum(peak, 1e-9))
    red = curve_db(lvl) - lvl                               # <= 0 dB
    ka, kr = math.exp(-blk / (att * SR)), math.exp(-blk / (rel * SR))
    sm = np.empty(nb)
    s = 0.0
    for i in range(nb):
        r = red[i]
        s = ka * s + (1 - ka) * r if r < s else kr * s + (1 - kr) * r
        sm[i] = s
    gain = np.repeat(10 ** (sm / 20), blk)[:n] * makeup
    la = int(0.006 * SR)                                    # look-ahead: audio delayed, gain not
    xd = np.zeros_like(x)
    xd[:, la:] = x[:, :n - la]
    return xd * gain


def run_panner(x: np.ndarray, pan: np.ndarray) -> np.ndarray:
    """StereoPannerNode, stereo input (spec equal-power)."""
    pan = np.clip(pan, -1, 1)
    y = np.empty_like(x)
    left = pan <= 0
    xl = np.where(left, pan + 1, pan) * np.pi / 2
    gl, gr = np.cos(xl), np.sin(xl)
    y[0] = np.where(left, x[0] + x[1] * gl, x[0] * gl)
    y[1] = np.where(left, x[1] * gr, x[1] + x[0] * gr)
    return y


def run_delay(x: np.ndarray, d: np.ndarray) -> np.ndarray:
    n = x.shape[1]
    pos = np.arange(n) - np.maximum(0.0, d) * SR
    y = np.empty_like(x)
    for ch in range(x.shape[0]):
        y[ch] = np.interp(pos, np.arange(n), x[ch], left=0.0, right=0.0)
    return y


def run_shaper(x: np.ndarray, curve: Optional[list]) -> np.ndarray:
    if not curve:
        return x
    c = np.asarray(curve, dtype=np.float64)
    idx = (np.clip(x, -1, 1) + 1) / 2 * (len(c) - 1)
    return np.interp(idx, np.arange(len(c)), c)


# ---------------------------------------------------------------- audio files
def decode(path: Path, sr: int = SR, start: float = 0.0, end: Optional[float] = None) -> np.ndarray:
    """Song seconds [start, end) of any audio file -> float32 (2, n) at `sr` via ffmpeg
    (sample-accurate trim after decoding, not a keyframe seek)."""
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg is required to decode the songs")
    trim = f"atrim=start_sample={int(start * sr)}" + (f":end_sample={int(math.ceil(end * sr))}" if end else "")
    r = subprocess.run([ff, "-v", "error", "-i", str(path), "-af", f"aresample={sr},{trim}", "-f", "f32le",
                        "-acodec", "pcm_f32le", "-ar", str(sr), "-ac", "2", "pipe:1"], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg could not decode {path}: {r.stderr.decode(errors='replace')[-300:]}")
    return np.frombuffer(r.stdout, dtype=np.float32).reshape(-1, 2).T.copy()


def _pos_range(node: dict) -> Optional[tuple]:
    pp = node.get("positions")
    vals = [v for v in (pp or {}).get("pos") or [] if v is not None]
    return (min(vals), max(vals)) if vals else None


def render_osc(node: dict, t: np.ndarray, freq: np.ndarray) -> Optional[np.ndarray]:
    """OscillatorNode between its start and stop (phase integrated from the frequency curve).
    Waveforms are the ideal shapes, not Web Audio's band-limited tables."""
    s, e = node.get("start"), node.get("stop")
    if s is None:
        return None
    on = (t >= s) & (t < (e if e is not None else math.inf))
    if not on.any():
        return None
    ph = np.cumsum(np.where(on, freq, 0.0)) / SR
    ph -= ph[np.argmax(on)]
    frac = np.mod(ph, 1.0)
    kind = node.get("type") or "sine"
    if kind == "square":
        w = np.where(frac < 0.5, 1.0, -1.0)
    elif kind == "sawtooth":
        w = 2 * frac - 1
    elif kind == "triangle":
        w = 1 - 4 * np.abs(np.mod(frac + 0.25, 1.0) - 0.5)
    else:
        w = np.sin(2 * np.pi * frac)
    y = (w * on).astype(np.float32)
    return np.vstack([y, y])


def render_source(node: dict, audio: np.ndarray, t: np.ndarray, pos_hz: float, a_off: float = 0.0,
                  sr_in: int = SR) -> Optional[np.ndarray]:
    """Read the song at the captured position; `audio` (at `sr_in`) starts at song second
    `a_off`. None when the source never sounds in the window."""
    pp = node.get("positions")
    if not pp or not pp.get("pos") or _pos_range(node) is None:
        return None
    y = np.zeros((2, len(t)), dtype=np.float32)
    k0, pos = int(pp["k0"]), pp["pos"]
    pt = (k0 + np.arange(len(pos))) / pos_hz
    pv = np.array([np.nan if v is None else v for v in pos], dtype=np.float64)
    live = ~np.isnan(pv)
    # position per output sample: linear between samples where both neighbours sound
    j = np.searchsorted(pt, t, side="right") - 1
    ok = (j >= 0) & (j < len(pt) - 1)
    jj = np.clip(j, 0, len(pt) - 2)
    both = ok & live[jj] & live[jj + 1]
    f = (t - pt[jj]) * pos_hz
    p = pv[jj] + (pv[jj + 1] - pv[jj]) * f
    # a jump (seek / loop wrap) between two samples: hold the earlier segment, do not sweep
    jump = np.abs((pv[jj + 1] - pv[jj]) * pos_hz) > 8
    p = np.where(jump, pv[jj] + (t - pt[jj]) * 1.0, p)
    idx = (p - a_off) * sr_in
    n = audio.shape[1]
    if n < 2:
        return None
    good = both & (idx >= 0) & (idx < n - 1)
    i0 = np.floor(np.where(good, idx, 0)).astype(np.int64)
    fr = np.where(good, idx - i0, 0.0)
    for ch in range(2):
        a = audio[min(ch, audio.shape[0] - 1)]
        y[ch] = np.where(good, a[i0] * (1 - fr) + a[np.minimum(i0 + 1, n - 1)] * fr, 0.0)
    return y


# ---------------------------------------------------------------- the graph
def render(cap: dict, files: Dict[str, Path], sr: int = SR) -> dict:
    """cap: the capture JSON. files: decoded-file key ("<track id>:mix" / ":drums" ...) -> path.
    -> {"audio": (2, n) float64, "not_rendered": [...], "approximations": [...], "window": [w0, w1]}"""
    if sr != SR:
        raise ValueError("render runs at 44.1 kHz")
    w0, w1 = cap["window"]
    n = int(round((w1 - w0) * sr))
    t = w0 + np.arange(n) / sr
    nodes = {nd["id"]: nd for nd in cap["nodes"]}
    labels = {int(k): v for k, v in (cap.get("labels") or {}).items()}
    name = lambda i: labels.get(i) or f"{nodes[i]['kind'] if i in nodes else '?'}#{i}"
    dest = cap["destination"]
    not_rendered: List[str] = []
    approx: List[str] = []

    # live edges in the window; param inputs (to: null) cannot be followed to a param
    ins: Dict[int, List[dict]] = {}
    for e in cap["edges"]:
        off = e.get("off")
        if off is not None and off <= w0:
            continue
        if e.get("on", 0) >= w1:
            continue
        if e.get("to") is None:
            ins.setdefault(-1, []).append(e)
            continue
        ins.setdefault(e["to"], []).append(e)

    # what reaches the destination
    reach, stack = set(), [dest]
    while stack:
        v = stack.pop()
        if v in reach:
            continue
        reach.add(v)
        stack.extend(e["from"] for e in ins.get(v, []))
    for e in ins.get(-1, []):
        if e["from"] in reach and _source_alive(nodes.get(e["from"]), w0, w1):
            not_rendered.append(f"{name(e['from'])} modulates an AudioParam (param input not followed)")

    # consumers per node on the audible path: a node's output is freed once all have read it
    uses: Dict[int, int] = {}
    for v in reach:
        for e in ins.get(v, []):
            uses[e["from"]] = uses.get(e["from"], 0) + 1
    # song range each decoded file is read over (decode only that, plus a margin)
    ranges: Dict[str, list] = {}
    for i in reach:
        nd = nodes.get(i)
        if nd and nd["kind"] == "source" and nd.get("file") in files and _source_alive(nd, w0, w1):
            r = _pos_range(nd)
            if r:
                cur = ranges.setdefault(nd["file"], [r[0], r[1]])
                cur[0], cur[1] = min(cur[0], r[0]), max(cur[1], r[1])

    cache: Dict[int, Optional[np.ndarray]] = {}
    decoded: Dict[str, tuple] = {}
    visiting: set = set()

    def pv(nd, key, dflt=0.0):
        p = (nd.get("params") or {}).get(key)
        return param_curve(p, t) if p else np.full(n, dflt)

    def take(i: int) -> Optional[np.ndarray]:
        y = out_of(i)
        uses[i] = uses.get(i, 1) - 1
        if uses[i] <= 0:
            cache.pop(i, None)
        return y

    def out_of(i: int) -> Optional[np.ndarray]:       # None = silence
        if i in cache:
            return cache[i]
        if i in visiting:
            not_rendered.append(f"feedback loop through {name(i)} (the loop's return path is left out)")
            return None
        nd = nodes.get(i)
        if nd is None:
            return None
        if nd["kind"] == "gain" and not np.any(pv(nd, "gain", 1.0)):
            cache[i] = None                               # muted all window: its inputs are never heard
            return None
        visiting.add(i)
        x = None
        for e in ins.get(i, []):
            y = take(e["from"])
            if y is None:
                continue
            on, off = e.get("on"), e.get("off")
            on = -math.inf if on is None else on
            if on > w0 or off is not None:
                y = y * ((t >= on) & (t < (off if off is not None else math.inf)))
            x = y.copy() if x is None else x + y
        k = nd["kind"]
        y = None
        if k == "source":
            key = nd.get("file")
            gen = (cap.get("buffers") or {}).get(str(nd.get("buf")))
            if not _source_alive(nd, w0, w1):
                y = None
            elif key is None and gen:                      # a buffer the console generated (noise, impulse)
                a = np.asarray(gen["channels"], dtype=np.float32)
                y = render_source(nd, a, t, cap.get("pos_hz", 1000), sr_in=int(gen["sr"]))
            elif key not in files:
                not_rendered.append(f"{name(i)}: buffer {key or nd.get('buf')!r} was not captured")
            else:
                if key not in decoded:
                    lo, hi = ranges.get(key, [0.0, None])
                    lo = max(0.0, lo - 1.0)
                    decoded[key] = (decode(files[key], start=lo, end=None if hi is None else hi + 1.0), lo)
                a, lo = decoded[key]
                y = render_source(nd, a, t, cap.get("pos_hz", 1000), a_off=lo)
        elif k == "osc":
            y = render_osc(nd, t, pv(nd, "frequency", 440) * np.power(2.0, pv(nd, "detune", 0) / 1200))
            if y is not None and (nd.get("type") or "sine") != "sine":
                approx.append(f"{nd.get('type')} oscillators drawn as ideal waveforms (Web Audio band-limits them)")
        elif k == "const":
            not_rendered.append(f"{name(i)}: ConstantSource not modelled (silent here)")
        elif x is None:
            y = None                                      # every node below is silent on silence
        elif k == "gain":
            gv = pv(nd, "gain", 1.0)
            y = x * gv if np.any(gv) else None
        elif k == "biquad":
            f = pv(nd, "frequency", 350) * np.power(2.0, pv(nd, "detune", 0) / 1200)
            y = run_biquad(x, nd.get("type") or "lowpass", f, pv(nd, "Q", 1), pv(nd, "gain", 0))
        elif k == "delay":
            y = run_delay(x, pv(nd, "delayTime", 0))
        elif k == "panner":
            y = run_panner(x, pv(nd, "pan", 0))
        elif k == "compressor":
            y = run_compressor(x, {p: pv(nd, p) for p in ("threshold", "knee", "ratio", "attack", "release")})
            approx.append(f"{name(i)}: DynamicsCompressor modelled from the spec's gain computer, not Chrome's exact detector")
        elif k == "shaper":
            y = run_shaper(x, nd.get("curve"))
            if nd.get("oversample") not in (None, "none"):
                approx.append(f"{name(i)}: waveshaper oversampling not modelled")
        elif k in ("analyser", "destination"):
            y = x
        elif np.any(x):
            not_rendered.append(f"{name(i)}: node kind {k!r} is not modelled (silent here)")
        visiting.discard(i)
        cache[i] = y
        return y

    audio = out_of(dest)
    if audio is None:
        audio = np.zeros((2, n), dtype=np.float32)
    return {"audio": audio, "not_rendered": sorted(set(not_rendered)), "approximations": sorted(set(approx)),
            "window": [w0, w1]}


def _source_alive(nd: Optional[dict], w0: float, w1: float) -> bool:
    if not nd or nd.get("kind") != "source":
        return bool(nd)
    s, e = nd.get("start"), nd.get("stop")
    return s is not None and s < w1 and (e is None or e > w0)


SUMMARY_PARAMS = ("mixGain.gain", "stemGain.drums.gain", "stemGain.bass.gain", "stemGain.vocals.gain",
                  "stemGain.other.gain", "vocalBusGain.gain", "lowFilter.gain", "midFilter.gain",
                  "highFilter.gain", "volumeGain.gain", "crossfaderGain.gain", "source.playbackRate")


def automation_summary(cap: dict, step_s: float = 0.5, rel_tol: float = 0.02) -> dict:
    """Each deck's main controls over the window as keyframes [file second, value]: sampled
    every `step_s`, kept where the value moved (plus both ends). Seconds are file seconds."""
    w0, w1 = cap["window"]
    t = np.arange(w0, w1 + 1e-9, step_s)
    by_label = {v: int(k) for k, v in (cap.get("labels") or {}).items()}
    nodes = {nd["id"]: nd for nd in cap["nodes"]}
    out: dict = {}
    for deck in ("a", "b"):
        d = {}
        for key in SUMMARY_PARAMS:
            path, param = key.rsplit(".", 1)
            nid = by_label.get(f"{deck}.{path}")
            p = nid is not None and (nodes.get(nid, {}).get("params") or {}).get(param)
            if not p:
                continue
            v = param_curve(p, t)
            keep = [0]
            for i in range(1, len(v)):
                ref = v[keep[-1]]
                if abs(v[i] - ref) > rel_tol * max(1.0, abs(ref)) or i == len(v) - 1:
                    if keep[-1] != i - 1:
                        keep.append(i - 1)          # where the move starts
                    keep.append(i)
            kf = [[round(float(t[i] - w0), 2), round(float(v[i]), 3)] for i in sorted(set(keep))]
            if len({k[1] for k in kf}) == 1:
                kf = [kf[0]]
            d[key] = kf
        out[deck] = d
    return out


def normalise(x: np.ndarray, peak_dbfs: float = -1.0) -> np.ndarray:
    pk = float(np.max(np.abs(x))) if x.size else 0.0
    return x if pk <= 0 else x * (10 ** (peak_dbfs / 20) / pk)


def write_wav(path: Path, x: np.ndarray, sr: int = SR) -> None:
    import soundfile as sf
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.clip(x.T, -1, 1).astype(np.float32), sr, subtype="PCM_16")
