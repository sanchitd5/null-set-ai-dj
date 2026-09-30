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
* Feedback loops (an echo's delay -> feedback gain -> delay) are rendered like Web Audio does:
  the loop is processed in blocks no longer than its shortest delay, and a delay inside a cycle
  is never shorter than one render quantum (128 samples).
* The master DynamicsCompressor is modelled after the spec's gain computer (threshold,
  knee, ratio, attack, release, auto make-up gain); that is an approximation, listed in
  `approximations`.

The window is rendered in chunks (every node keeps its filter / delay / envelope state across
them), so a whole-song render stays in memory. Anything on the audible path that is not
modelled (worklets, channel splitters, param modulation inputs, a cycle without a delay) is
listed in `not_rendered` instead of being faked.
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
QUANTUM = 128       # Web Audio render quantum: the shortest delay a cycle may have
CHUNK = 128 * 690   # samples per render chunk (~2 s)


# ---------------------------------------------------------------- AudioParam timelines
def _const(v: float) -> Callable[[np.ndarray], np.ndarray]:
    return lambda x: np.full(len(x), v)


class ParamEval:
    """A captured AudioParam evaluated over ascending, non-overlapping blocks of times (the
    same rules as FakeAudioParam.valueAt: the segment before each event is decided by that
    event). Keeps its place in the timeline, so a whole window costs O(events + blocks)."""

    def __init__(self, p: dict):
        self.ev = p.get("ev") or []
        self.g = _const(float(p.get("base", 0.0)))
        self.t_prev = -math.inf
        self.i = 0

    def _consume(self, e: dict, et: float) -> None:
        v_start = float(self.g(np.array([et]))[0])
        if e["type"] == "target":
            tv, tc = float(e["value"]), max(1e-9, float(e["tc"]))
            self.g = lambda x, tv=tv, tc=tc, et=et, vs=v_start: tv + (vs - tv) * np.exp(-(x - et) / tc)
        elif e["type"] == "curve":
            cv = np.asarray(e["curve"], dtype=np.float64)
            dur = max(1e-9, float(e["dur"]))
            self.g = lambda x, cv=cv, dur=dur, et=et: np.interp(
                np.clip((x - et) / dur, 0.0, 1.0) * (len(cv) - 1), np.arange(len(cv)), cv)
        else:
            self.g = _const(float(e["value"]))
        self.t_prev = et

    def __call__(self, tt: np.ndarray) -> np.ndarray:
        out = np.empty(len(tt), dtype=np.float64)
        lo = 0
        while self.i < len(self.ev):
            e = self.ev[self.i]
            et = float(e["time"])
            hi = int(np.searchsorted(tt, et, side="left"))       # t < et breaks at this event
            if hi > lo:
                seg = tt[lo:hi]
                v = self.g(seg)
                if e["type"] in ("lin", "exp"):
                    t0 = 0.0 if self.t_prev == -math.inf else self.t_prev
                    f = (seg - t0) / max(1e-9, et - t0)
                    nv = float(e["value"])
                    if e["type"] == "lin":
                        r = v + (nv - v) * f
                    else:
                        vs = np.where(np.abs(v) > 0, v, 1e-6)
                        r = v * np.power(np.maximum(nv / vs, 1e-12), f)
                    v = np.where(seg > t0, r, v)
                out[lo:hi] = v
                lo = hi
            if hi >= len(tt):
                break                                           # decides later blocks too: keep it
            self._consume(e, et)
            self.i += 1
        if lo < len(tt):
            out[lo:] = self.g(tt[lo:])
        return out


def param_curve(p: dict, t: np.ndarray) -> np.ndarray:
    """Value of a captured AudioParam at the (ascending) times `t`."""
    return ParamEval(p)(np.asarray(t, dtype=np.float64))


def param_all_zero(p: Optional[dict]) -> bool:
    """True when the param is 0 everywhere (every value it can take is 0)."""
    if not p:
        return False
    if float(p.get("base", 0.0)) != 0.0:
        return False
    for e in p.get("ev") or []:
        if float(e.get("value", 0.0)) != 0.0 or (e["type"] == "curve" and any(float(v) != 0.0 for v in e["curve"])):
            return False
    return True


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


def run_biquad(x: np.ndarray, kind: str, f: np.ndarray, q: np.ndarray, g: np.ndarray,
               zi: Optional[list] = None):
    """x: (2, n). Params per sample; coefficients held for BLOCK samples. zi: per-channel
    filter state carried between calls (updated in place). -> y"""
    n = x.shape[1]
    idx = np.arange(0, n, BLOCK)
    cf = biquad_coeffs(kind, f[idx], q[idx], g[idx])
    y = np.empty_like(x)
    const = all(np.allclose(c, c[0]) for c in cf)
    if zi is None:
        zi = [np.zeros(2) for _ in range(x.shape[0])]
    for ch in range(x.shape[0]):
        z = zi[ch]
        if const:
            b = [cf[0][0], cf[1][0], cf[2][0]]
            a = [1.0, cf[3][0], cf[4][0]]
            y[ch], z = lfilter(b, a, x[ch], zi=z)
        else:
            for j, s in enumerate(idx):
                e = min(n, s + BLOCK)
                b = [cf[0][j], cf[1][j], cf[2][j]]
                a = [1.0, cf[3][j], cf[4][j]]
                y[ch, s:e], z = lfilter(b, a, x[ch, s:e], zi=z)
        zi[ch] = z
    return y


# ---------------------------------------------------------------- stateful nodes
class Compressor:
    """Spec-shaped DynamicsCompressor: peak detector over both channels, static curve
    (threshold / knee / ratio), attack / release smoothing, 6 ms look-ahead, auto make-up
    gain (1 / curve(1.0)) ^ 0.6. Parameters taken at the first block."""
    DET = 32

    def __init__(self):
        self.s = 0.0
        self.la = int(0.006 * SR)
        self.tail = np.zeros((2, self.la))
        self.p = None

    def __call__(self, x: np.ndarray, p: Dict[str, float]) -> np.ndarray:
        if self.p is None:
            self.p = p
        thr, knee, ratio = self.p["threshold"], self.p["knee"], max(1.0, self.p["ratio"])
        att, rel = max(1e-4, self.p["attack"]), max(1e-3, self.p["release"])

        def curve_db(lv):
            over = lv - thr
            if knee <= 0:
                return np.where(over > 0, thr + over / ratio, lv)
            k2 = knee / 2
            soft = lv + (1 / ratio - 1) * (over + k2) ** 2 / (2 * knee)
            return np.where(over <= -k2, lv, np.where(over >= k2, thr + over / ratio, soft))

        makeup = (1.0 / 10 ** (float(curve_db(np.array([0.0]))[0]) / 20)) ** 0.6
        blk, n = self.DET, x.shape[1]
        nb = (n + blk - 1) // blk
        pad = np.zeros((x.shape[0], nb * blk))
        pad[:, :n] = np.abs(x)
        peak = pad.reshape(x.shape[0], nb, blk).max(axis=2).max(axis=0)
        lvl = 20 * np.log10(np.maximum(peak, 1e-9))
        red = curve_db(lvl) - lvl                               # <= 0 dB
        ka, kr = math.exp(-blk / (att * SR)), math.exp(-blk / (rel * SR))
        sm = np.empty(nb)
        s = self.s
        for i in range(nb):
            r = red[i]
            s = ka * s + (1 - ka) * r if r < s else kr * s + (1 - kr) * r
            sm[i] = s
        self.s = s
        gain = np.repeat(10 ** (sm / 20), blk)[:n] * makeup
        buf = np.concatenate([self.tail, x], axis=1)             # look-ahead: audio delayed, gain not
        self.tail = buf[:, n:]
        return buf[:, :n] * gain


class Delay:
    """DelayNode: output(t) = input(t - delayTime(t)), linear interpolation, over a ring buffer
    of the node's past input. Inside a cycle the delay is at least one render quantum."""

    def __init__(self, max_s: float):
        self.R = int((max_s + 1.0) * SR) + 2 * CHUNK + 4 * QUANTUM
        self.H = np.zeros((2, self.R), dtype=np.float64)
        self.written = 0                                        # global sample index written up to
        self.last_nz = -10 ** 12
        self.max_s = max_s

    def write(self, k0: int, x: Optional[np.ndarray], n: int) -> None:
        if k0 != self.written:
            raise RuntimeError("delay written out of order")
        idx = (k0 + np.arange(n)) % self.R
        if x is None:
            self.H[:, idx] = 0.0
        else:
            self.H[:, idx] = x
            if np.any(x):
                self.last_nz = k0 + n
        self.written = k0 + n

    def read(self, k0: int, d: np.ndarray, in_cycle: bool) -> Optional[np.ndarray]:
        n = len(d)
        dd = np.maximum(0.0, d) * SR
        if in_cycle:
            dd = np.maximum(dd, QUANTUM)
        if k0 - float(dd.max()) > self.last_nz + 2:
            return None                                          # only silence is left in the line
        p = (k0 + np.arange(n)) - dd
        i0 = np.floor(p).astype(np.int64)
        fr = p - i0
        ok0 = (i0 >= 0) & (i0 < self.written) & (i0 >= self.written - self.R)
        ok1 = (i0 + 1 >= 0) & (i0 + 1 < self.written) & (i0 + 1 >= self.written - self.R)
        a = np.where(ok0, self.H[:, i0 % self.R], 0.0)
        b = np.where(ok1, self.H[:, (i0 + 1) % self.R], 0.0)
        return a * (1 - fr) + b * fr


class Osc:
    """OscillatorNode between its start and stop, phase carried across blocks. Waveforms are
    the ideal shapes, not Web Audio's band-limited tables."""

    def __init__(self, node: dict):
        self.node = node
        self.phase = 0.0

    def __call__(self, t: np.ndarray, freq: np.ndarray) -> Optional[np.ndarray]:
        s, e = self.node.get("start"), self.node.get("stop")
        if s is None:
            return None
        on = (t >= s) & (t < (e if e is not None else math.inf))
        if not on.any():
            return None
        inc = np.where(on, freq, 0.0) / SR
        ph = self.phase + np.cumsum(inc) - inc
        self.phase = float(ph[-1] + inc[-1])
        frac = np.mod(ph, 1.0)
        kind = self.node.get("type") or "sine"
        if kind == "square":
            w = np.where(frac < 0.5, 1.0, -1.0)
        elif kind == "sawtooth":
            w = 2 * frac - 1
        elif kind == "triangle":
            w = 1 - 4 * np.abs(np.mod(frac + 0.25, 1.0) - 0.5)
        else:
            w = np.sin(2 * np.pi * frac)
        y = w * on
        return np.vstack([y, y])


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


def _pos_arrays(node: dict, pos_hz: float):
    """(times, positions with NaN where silent) of a source, built once per node."""
    if "_pt" not in node:
        pp = node.get("positions") or {}
        pos = pp.get("pos") or []
        node["_pv"] = np.array([np.nan if v is None else v for v in pos], dtype=np.float64)
        node["_pt"] = (int(pp.get("k0", 0)) + np.arange(len(pos))) / pos_hz
    return node["_pt"], node["_pv"]


def _pos_range(node: dict) -> Optional[tuple]:
    pp = node.get("positions")
    vals = [v for v in (pp or {}).get("pos") or [] if v is not None]
    return (min(vals), max(vals)) if vals else None


def render_source(node: dict, audio: np.ndarray, t: np.ndarray, pos_hz: float, a_off: float = 0.0,
                  sr_in: int = SR) -> Optional[np.ndarray]:
    """Read the song at the captured position; `audio` (at `sr_in`) starts at song second
    `a_off`. None when the source does not sound in `t`."""
    pt, pv = _pos_arrays(node, pos_hz)
    if len(pt) < 2 or t[-1] < pt[0] or t[0] > pt[-1]:
        return None
    live = ~np.isnan(pv)
    j = np.searchsorted(pt, t, side="right") - 1
    ok = (j >= 0) & (j < len(pt) - 1)
    jj = np.clip(j, 0, len(pt) - 2)
    both = ok & live[jj] & live[jj + 1]
    if not both.any():
        return None
    a0, a1 = np.nan_to_num(pv[jj]), np.nan_to_num(pv[jj + 1])
    p = a0 + (a1 - a0) * (t - pt[jj]) * pos_hz
    # a jump (seek / loop wrap) between two samples: hold the earlier segment, do not sweep
    jump = np.abs((a1 - a0) * pos_hz) > 8
    p = np.where(jump, a0 + (t - pt[jj]), p)
    idx = (p - a_off) * sr_in
    n = audio.shape[1]
    if n < 2:
        return None
    good = both & (idx >= 0) & (idx < n - 1)
    i0 = np.floor(np.where(good, idx, 0)).astype(np.int64)
    fr = np.where(good, idx - i0, 0.0)
    y = np.zeros((2, len(t)))
    for ch in range(2):
        a = audio[min(ch, audio.shape[0] - 1)]
        y[ch] = np.where(good, a[i0] * (1 - fr) + a[np.minimum(i0 + 1, n - 1)] * fr, 0.0)
    return y


# ---------------------------------------------------------------- the graph
def _sccs(nodes: List[int], succ: Dict[int, List[int]]) -> List[List[int]]:
    """Tarjan (iterative). Returned in topological order (sources first)."""
    index, low, on, stack, out = {}, {}, set(), [], []
    counter = [0]
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(succ.get(root, [])))]
        index[root] = low[root] = counter[0]; counter[0] += 1
        stack.append(root); on.add(root)
        while work:
            v, it = work[-1]
            nxt = next(it, None)
            if nxt is not None:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]; counter[0] += 1
                    stack.append(nxt); on.add(nxt)
                    work.append((nxt, iter(succ.get(nxt, []))))
                elif nxt in on:
                    low[v] = min(low[v], index[nxt])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop(); on.discard(w); comp.append(w)
                    if w == v:
                        break
                out.append(comp)
    return out[::-1]


def render(cap: dict, files: Dict[str, Path], sr: int = SR, chunk: int = CHUNK) -> dict:
    """cap: the capture JSON. files: decoded-file key ("<track id>:mix" / ":drums" ...) -> path.
    -> {"audio": (2, n) float32, "not_rendered": [...], "approximations": [...], "window": [w0, w1]}"""
    if sr != SR:
        raise ValueError("render runs at 44.1 kHz")
    if chunk % QUANTUM:
        raise ValueError("chunk must be a multiple of 128 samples")
    w0, w1 = cap["window"]
    n = int(round((w1 - w0) * sr))
    nodes = {nd["id"]: nd for nd in cap["nodes"]}
    labels = {int(k): v for k, v in (cap.get("labels") or {}).items()}
    name = lambda i: labels.get(i) or f"{nodes[i]['kind'] if i in nodes else '?'}#{i}"
    dest = cap["destination"]
    pos_hz = cap.get("pos_hz", 1000)
    not_rendered: List[str] = []
    approx: List[str] = []

    def muted(i):
        nd = nodes.get(i)
        return nd is not None and nd["kind"] == "gain" and param_all_zero((nd.get("params") or {}).get("gain"))

    # live edges in the window; param inputs (to: null) cannot be followed to a param
    ins: Dict[int, List[dict]] = {}
    param_ins = []
    for e in cap["edges"]:
        off, on = e.get("off"), e.get("on")
        if (off is not None and off <= w0) or (on is not None and on >= w1):
            continue
        if e.get("to") is None:
            param_ins.append(e)
        else:
            ins.setdefault(e["to"], []).append(e)

    # what reaches the destination (a gain at 0 the whole window passes nothing)
    reach, stack = set(), [dest]
    while stack:
        v = stack.pop()
        if v in reach or v not in nodes:
            continue
        reach.add(v)
        if not muted(v):
            stack.extend(e["from"] for e in ins.get(v, []))
    for e in param_ins:
        if e["from"] in reach and _source_alive(nodes.get(e["from"]), w0, w1):
            not_rendered.append(f"{name(e['from'])} modulates an AudioParam (param input not followed)")

    live_in = {v: ([] if muted(v) else [e for e in ins.get(v, []) if e["from"] in reach]) for v in reach}
    succ: Dict[int, List[int]] = {}
    for v, es in live_in.items():
        for e in es:
            succ.setdefault(e["from"], []).append(v)
    order = _sccs(sorted(reach), succ)
    comp_of = {v: ci for ci, comp in enumerate(order) for v in comp}
    cyclic = [len(c) > 1 or any(e["from"] == c[0] for e in live_in[c[0]]) for c in order]
    for ci, comp in enumerate(order):
        if cyclic[ci] and not any(nodes[v]["kind"] == "delay" for v in comp):
            not_rendered.append(f"cycle without a delay through {name(comp[0])} (Web Audio mutes it)")
            cyclic[ci] = None
    uses = {}
    for v, es in live_in.items():
        for e in es:
            if comp_of[e["from"]] != comp_of[v]:
                uses[e["from"]] = uses.get(e["from"], 0) + 1

    # song range each decoded file is read over (decode only that, plus a margin)
    ranges: Dict[str, list] = {}
    for i in reach:
        nd = nodes[i]
        if nd["kind"] == "source" and nd.get("file") in files and _source_alive(nd, w0, w1):
            r = _pos_range(nd)
            if r:
                cur = ranges.setdefault(nd["file"], [r[0], r[1]])
                cur[0], cur[1] = min(cur[0], r[0]), max(cur[1], r[1])
    decoded: Dict[str, tuple] = {}
    pe: Dict[tuple, ParamEval] = {}
    state: Dict[int, object] = {}

    def pv(nd, key, t, dflt=0.0):
        p = (nd.get("params") or {}).get(key)
        if not p:
            return np.full(len(t), dflt)
        k = (nd["id"], key)
        if k not in pe:
            pe[k] = ParamEval(p)
        return pe[k](t)

    def delay_of(nd) -> Delay:
        if nd["id"] not in state:
            p = (nd.get("params") or {}).get("delayTime") or {}
            vals = [float(p.get("base", 0.0))] + [float(e.get("value", 0.0)) for e in p.get("ev") or []]
            state[nd["id"]] = Delay(max(0.0, max(vals)))
        return state[nd["id"]]

    def sum_in(v, t, get):
        x = None
        for e in live_in[v]:
            y = get(e["from"])
            if y is None:
                continue
            on, off = e.get("on"), e.get("off")
            if (on is not None and on > t[0]) or (off is not None and off <= t[-1]):
                y = y * ((t >= (on if on is not None else -math.inf)) & (t < (off if off is not None else math.inf)))
            x = y.copy() if x is None else x + y
        return x

    def proc(v, x, t, k0):
        """One node over the block t (global first sample k0); x = summed input or None."""
        nd = nodes[v]
        k = nd["kind"]
        if k == "source":
            key = nd.get("file")
            gen = (cap.get("buffers") or {}).get(str(nd.get("buf")))
            if not _source_alive(nd, w0, w1):
                return None
            if key is None and gen:                           # a buffer the console generated (noise, impulse)
                if ("gen", v) not in decoded:
                    decoded[("gen", v)] = (np.asarray(gen["channels"], dtype=np.float32), 0.0)
                return render_source(nd, decoded[("gen", v)][0], t, pos_hz, sr_in=int(gen["sr"]))
            if key not in files:
                not_rendered.append(f"{name(v)}: buffer {key or nd.get('buf')!r} was not captured")
                return None
            if key not in decoded:
                lo, hi = ranges.get(key, [0.0, None])
                lo = max(0.0, lo - 1.0)
                decoded[key] = (decode(files[key], start=lo, end=None if hi is None else hi + 1.0), lo)
            a, lo = decoded[key]
            return render_source(nd, a, t, pos_hz, a_off=lo)
        if k == "osc":
            if v not in state:
                state[v] = Osc(nd)
            y = state[v](t, pv(nd, "frequency", t, 440) * np.power(2.0, pv(nd, "detune", t, 0) / 1200))
            if y is not None and (nd.get("type") or "sine") != "sine":
                approx.append(f"{nd.get('type')} oscillators drawn as ideal waveforms (Web Audio band-limits them)")
            return y
        if k == "const":
            not_rendered.append(f"{name(v)}: ConstantSource not modelled (silent here)")
            return None
        if k == "delay":                                      # outside a cycle: input first, then read
            dl = delay_of(nd)
            dl.write(k0, x, len(t))
            return dl.read(k0, pv(nd, "delayTime", t), in_cycle=False)
        if k == "biquad":
            zi = state.get(v)
            if x is None and (zi is None or max(float(np.abs(z).max()) for z in zi) < 1e-10):
                return None
            if zi is None:
                zi = state[v] = [np.zeros(2), np.zeros(2)]
            f = pv(nd, "frequency", t, 350) * np.power(2.0, pv(nd, "detune", t, 0) / 1200)
            return run_biquad(x if x is not None else np.zeros((2, len(t))), nd.get("type") or "lowpass",
                              f, pv(nd, "Q", t, 1), pv(nd, "gain", t, 0), zi)
        if k == "compressor":
            if v not in state:
                state[v] = Compressor()
                approx.append(f"{name(v)}: DynamicsCompressor modelled from the spec's gain computer, not Chrome's exact detector")
            c = state[v]
            if x is None and not np.any(c.tail):
                return None
            return c(x if x is not None else np.zeros((2, len(t))),
                     {p: float(pv(nd, p, t[:1])[0]) for p in ("threshold", "knee", "ratio", "attack", "release")})
        if x is None:
            return None
        if k == "gain":
            g = pv(nd, "gain", t, 1.0)
            return x * g if np.any(g) else None
        if k == "panner":
            return run_panner(x, pv(nd, "pan", t, 0))
        if k == "shaper":
            if nd.get("oversample") not in (None, "none"):
                approx.append(f"{name(v)}: waveshaper oversampling not modelled")
            return run_shaper(x, nd.get("curve"))
        if k in ("analyser", "destination"):
            return x
        if np.any(x):
            not_rendered.append(f"{name(v)}: node kind {k!r} is not modelled (silent here)")
        return None

    audio = np.zeros((2, n), dtype=np.float32)
    for c0 in range(0, n, chunk):
        c1 = min(n, c0 + chunk)
        t = w0 + np.arange(c0, c1) / sr
        outs: Dict[int, Optional[np.ndarray]] = {}
        left = dict(uses)

        def release(v):
            for e in live_in[v]:
                f = e["from"]
                if comp_of[f] != comp_of[v] and f in left:
                    left[f] -= 1
                    if left[f] <= 0 and f != dest:
                        outs.pop(f, None)

        for ci, comp in enumerate(order):
            if cyclic[ci] is None:
                for v in comp:
                    outs[v] = None
                continue
            if not cyclic[ci]:
                v = comp[0]
                outs[v] = proc(v, sum_in(v, t, outs.get), t, c0)
                release(v)
                continue
            # a feedback loop: blocks no longer than its shortest delay (>= one render quantum)
            dels = [v for v in comp if nodes[v]["kind"] == "delay"]
            inner = set(comp)
            dmin = min(float(np.min(ParamEval((nodes[d].get("params") or {}).get("delayTime") or {})(t))) for d in dels)
            B = max(QUANTUM, int(dmin * sr) // QUANTUM * QUANTUM)
            rest = [v for v in comp if v not in dels]
            # order the rest: edges into a delay are cut, so what remains is acyclic
            indeg = {v: sum(1 for e in live_in[v] if e["from"] in inner and e["from"] not in dels) for v in rest}
            seq, ready = [], [v for v in rest if indeg[v] == 0]
            while ready:
                v = ready.pop()
                seq.append(v)
                for s in succ.get(v, []):
                    if s in indeg:
                        indeg[s] -= 1
                        if indeg[s] == 0:
                            ready.append(s)
            full = {v: None for v in comp}
            for b0 in range(0, c1 - c0, B):
                b1 = min(c1 - c0, b0 + B)
                tb = t[b0:b1]
                blk: Dict[int, Optional[np.ndarray]] = {}

                def get(f, b0=b0, b1=b1, blk=blk):
                    if f in inner:
                        return blk.get(f)
                    y = outs.get(f)
                    return None if y is None else y[:, b0:b1]

                for d in dels:
                    blk[d] = delay_of(nodes[d]).read(c0 + b0, pv(nodes[d], "delayTime", tb), in_cycle=True)
                for v in seq:
                    blk[v] = proc(v, sum_in(v, tb, get), tb, c0 + b0)
                for d in dels:
                    delay_of(nodes[d]).write(c0 + b0, sum_in(d, tb, get), b1 - b0)
                for v in comp:
                    if blk.get(v) is not None:
                        if full[v] is None:
                            full[v] = np.zeros((2, c1 - c0))
                        full[v][:, b0:b1] = blk[v]
            outs.update(full)
            for v in comp:
                release(v)
        y = outs.get(dest)
        if y is not None:
            audio[:, c0:c1] = y
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
