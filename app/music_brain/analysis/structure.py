"""Phrase-grid structure: sections and drops (analysis v6).

v5 labelled every 1 s energy window and flickered (658 of 661 library songs had more than half
their sections at 2 s or shorter), and found drops with one mix-energy rule that missed 4 in 10
songs. Here everything sits on the 8-bar phrase grid ([[Phrasing & Structure]]): one label per
8-bar phrase, merged runs, so no section is shorter than a phrase apart from the pickup before the
first phrase line.

A drop is the phrase line where the groove slams back in after a build or breakdown ([[Double
Drop]], [[Drop Swap]]). With Demucs stems that is the drums+bass stem level: it must reach the
song's full-groove level on this line, having been clearly lower on the phrases before it. Without
stems the mix energy curve stands in. The main drop is the loudest drop; between drops that are
about as loud, the one with the deepest, longest dip before it (the breakdown the song builds its
biggest moment on).

Pure numpy apart from `stem_phrase_db` (reads FLAC stems); `refine` turns a v5 record into v6.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np

PHRASE_BARS = 8
STRUCTURE_VERSION = 1

# Stem (drums+bass) rule, in dB of the power-summed drums+bass stems per phrase.
FULL_PCT = 80            # the song's full-groove level: this percentile of its phrase levels
FULL_TOL_DB = 3.0        # a drop phrase sits within this of the full level
SPAN_TOL_DB = 4.0        # ... and the drop runs on while phrases stay within this
STEM_JUMP_DB = 6.0       # over the lowest phrase of the dip before it
STEM_SLAM_DB = 3.0       # and over the phrase right before it (it slams in on this line)
STEM_MIN_MIX = 0.6       # the mix must be at least this share of the song's loudest phrase

# Mix-only rule (no stems), on the peak-normalised energy curve.
MIX_FULL = 0.8           # share of the loudest phrase
MIX_JUMP = 0.2           # over the lowest phrase of the dip before it
MIX_SLAM = 0.1           # over the phrase right before it
MIX_SPAN = 0.85          # the drop runs on while phrases keep this share of the drop phrase

DIP_PHRASES = 4          # how far back the dip before a drop is looked for
MAIN_TIE = 0.05          # drops this close (share of the loudest phrase) count as equally loud
LOW_SHARE = 0.5          # breakdown: below this share of the loudest phrase
EDGE_LOW = 0.6           # intro / outro: the leading / trailing phrases below this share


def phrase_edges(phrases: Sequence[float], bar: float, duration: float) -> List[tuple]:
    """[(start, end)] for every 8-bar phrase line, the last one clipped to the song's end."""
    ph = [float(p) for p in phrases or [] if p < duration]
    L = PHRASE_BARS * bar
    out = []
    for i, p in enumerate(ph):
        end = ph[i + 1] if i + 1 < len(ph) else p + L
        out.append((p, min(end, duration)))
    return out


def curve_means(edges, times, curve) -> List[Optional[float]]:
    t = np.asarray(times or [], dtype=float)
    c = np.asarray(curve or [], dtype=float)
    out = []
    for a, b in edges:
        m = (t >= a) & (t < b)
        out.append(float(c[m].mean()) if m.any() else None)
    return out


def stem_phrase_db(stems: Dict[str, str], edges) -> Optional[List[float]]:
    """Power-summed drums+bass RMS (dB) per phrase, read from the Demucs stems. None when unreadable."""
    import soundfile as sf

    if not stems or not stems.get("drums") or not stems.get("bass") or not edges:
        return None
    power = np.zeros(len(edges))
    try:
        for name in ("drums", "bass"):
            y, sr = sf.read(stems[name], dtype="float32", always_2d=True)
            y = y.mean(axis=1)
            for i, (a, b) in enumerate(edges):
                seg = y[int(a * sr):int(b * sr):4]
                power[i] += float(np.mean(seg * seg)) if len(seg) else 0.0
    except (OSError, RuntimeError, ValueError):
        return None
    return [float(10 * np.log10(max(p, 1e-12))) for p in power]


def _run_before(full: List[bool], i: int) -> List[int]:
    """Indices of the dip before phrase i: the non-full phrases right before it (at most DIP_PHRASES)."""
    out = []
    j = i - 1
    while j >= 0 and not full[j] and len(out) < DIP_PHRASES:
        out.append(j)
        j -= 1
    return out


def detect_drops(edges, energy: List[Optional[float]], groove: Optional[List[float]] = None,
                 pickup_energy: Optional[float] = None, pickup_groove: Optional[float] = None) -> List[dict]:
    """Drops on the phrase grid: [{start, end, energy, jump, confidence, source}] in time order.

    `pickup_*` describe the audio before the first phrase line (an intro that starts mid-grid), so a
    drop on the first line can still be found.
    """
    n = len(edges)
    if n == 0:
        return []
    e = [(x if x is not None else 0.0) for x in energy]
    top = max(e) if e else 0.0
    if top <= 0:
        return []
    stems = groove is not None and len(groove) == n
    if stems:
        g = list(groove)
        G = float(np.percentile(g, FULL_PCT))
        full = [g[i] >= G - FULL_TOL_DB and e[i] >= STEM_MIN_MIX * top for i in range(n)]
    else:
        full = [e[i] >= MIX_FULL * top for i in range(n)]

    drops: List[dict] = []
    i = 0
    while i < n:
        if not full[i]:
            i += 1
            continue
        run = _run_before(full, i)
        if run:
            prev_e, low_e = e[i - 1], min(e[j] for j in run)
            prev_g = g[i - 1] if stems else None
            low_g = min(g[j] for j in run) if stems else None
        elif i == 0 and pickup_energy is not None:
            prev_e = low_e = pickup_energy
            prev_g = low_g = pickup_groove
        else:
            prev_e = None
        ok = False
        jump = conf = 0.0
        if prev_e is not None:
            jump = e[i] - low_e
            if stems and prev_g is not None:
                gj = g[i] - low_g
                ok = gj >= STEM_JUMP_DB and g[i] - prev_g >= STEM_SLAM_DB and e[i] > prev_e
                conf = 0.5 * min(1.0, gj / 12.0) + 0.5 * min(1.0, max(0.0, jump) / 0.4)
            elif not stems:
                ok = jump >= MIX_JUMP - 1e-9 and e[i] - prev_e >= MIX_SLAM - 1e-9
                conf = 0.7 * min(1.0, jump / 0.4)
        # the drop runs on while the groove holds
        k = i + 1
        while k < n and (
            (g[k] >= G - SPAN_TOL_DB and e[k] >= 0.7 * e[i]) if stems else e[k] >= MIX_SPAN * e[i]
        ):
            k += 1
        if ok:
            span_e = float(np.mean(e[i:k]))
            contrast = sum(e[i] - e[j] for j in run) if run else max(0.0, e[i] - (prev_e or 0.0))
            drops.append({
                "start": round(edges[i][0], 3), "end": round(edges[k - 1][1], 3),
                "energy": round(span_e, 4), "line_energy": round(e[i], 4),
                "prev_energy": round(prev_e, 4), "jump": round(jump, 4),
                "contrast": round(contrast, 4), "confidence": round(conf, 2),
                "source": "stems" if stems else "mix",
            })
        i = k
    return drops


def main_drop(drops: List[dict]) -> Optional[dict]:
    """Loudest drop; among ones about as loud, the deepest dip before it; then the earlier one."""
    if not drops:
        return None
    top = max(d["energy"] for d in drops)
    band = [d for d in drops if d["energy"] >= top - MAIN_TIE * max(top, 1e-6)]
    return max(band, key=lambda d: (round(d["contrast"], 2), -d["start"]))


def segment(edges, energy: List[Optional[float]], drops: List[dict], duration: float, bar: float) -> List[dict]:
    """One label per phrase, merged: intro / build / drop / breakdown / verse / outro.

    Sections start and end on phrase lines (plus the pickup before the first line and the tail
    after the last one, folded into their neighbour when shorter than 4 bars).
    """
    n = len(edges)
    if n == 0:
        return [{"label": "verse", "start": 0.0, "end": float(duration), "energy": 0.0}] if duration > 0 else []
    e = [(x if x is not None else 0.0) for x in energy]
    top = max(max(e), 1e-6)
    rel = [x / top for x in e]
    lab = ["verse"] * n
    starts = {d["start"]: d["end"] for d in drops}
    i = 0
    while i < n:
        s = round(edges[i][0], 3)
        if s in starts:
            while i < n and edges[i][1] <= starts[s] + 1e-3:
                lab[i] = "drop"
                i += 1
        else:
            i += 1
    for i in range(n):
        if lab[i] != "drop" and rel[i] < LOW_SHARE:
            lab[i] = "breakdown"
    # build: the last phrase of a dip that rises into a drop
    for i in range(1, n):
        if lab[i] == "drop" and lab[i - 1] != "drop" and i >= 2 and (e[i - 1] >= e[i - 2] or rel[i - 1] >= LOW_SHARE):
            lab[i - 1] = "build"
    first_drop = next((i for i in range(n) if lab[i] == "drop"), n)
    for i in range(n):
        if i >= first_drop or rel[i] >= EDGE_LOW or lab[i] == "build":
            break
        lab[i] = "intro"
    last_drop = max((i for i in range(n) if lab[i] == "drop"), default=-1)
    for i in range(n - 1, -1, -1):
        if i <= last_drop or rel[i] >= EDGE_LOW:
            break
        lab[i] = "outro"

    secs = []
    for i in range(n):
        a, b = edges[i]
        if secs and secs[-1]["label"] == lab[i]:
            s = secs[-1]
            w0, w1 = s["end"] - s["start"], b - a
            s["energy"] = (s["energy"] * w0 + e[i] * w1) / max(1e-6, w0 + w1)
            s["end"] = b
        else:
            secs.append({"label": lab[i], "start": a, "end": b, "energy": e[i]})
    # pickup before the first line, tail after the last one
    if secs[0]["start"] > 0:
        if secs[0]["start"] < 4 * bar or secs[0]["label"] == "intro":
            secs[0]["start"] = 0.0
        else:
            secs.insert(0, {"label": "intro", "start": 0.0, "end": secs[0]["start"], "energy": 0.0})
    if secs[-1]["end"] < duration:
        if duration - secs[-1]["end"] < 4 * bar or secs[-1]["label"] == "outro":
            secs[-1]["end"] = float(duration)
        else:
            secs.append({"label": "outro", "start": secs[-1]["end"], "end": float(duration), "energy": 0.0})
    for s in secs:
        s["start"], s["end"], s["energy"] = round(float(s["start"]), 3), round(float(s["end"]), 3), round(float(s["energy"]), 4)
    return secs


def _pickup(times, curve, first: float, bar: float) -> Optional[float]:
    if first < 2 * bar:
        return None
    m = curve_means([(0.0, first)], times, curve)[0]
    return m


def refine(record: dict, stems: Optional[Dict[str, str]] = None) -> dict:
    """Sections, drops and main drop of an analysis record (dict), in place and returned.

    Needs only the stored phrase grid and energy curve (no audio decode of the mix); drums and bass
    stems sharpen the drop rule when given ({name: path}, as audio_io.read_manifest returns).
    """
    duration = float(record.get("duration") or 0.0)
    bpm = float(record.get("bpm") or 0.0)
    bar = 240.0 / bpm if bpm > 0 else 2.0
    times, curve = record.get("energy_times") or [], record.get("energy_curve") or []
    edges = phrase_edges(record.get("phrase_boundaries_8bar") or [], bar, duration)
    energy = curve_means(edges, times, curve)
    groove = pickup_g = None
    if stems and edges:
        allg = stem_phrase_db(stems, ([(0.0, edges[0][0])] if edges[0][0] >= 2 * bar else []) + edges)
        if allg is not None:
            if edges[0][0] >= 2 * bar:
                pickup_g, groove = allg[0], allg[1:]
            else:
                groove = allg
    pickup_e = _pickup(times, curve, edges[0][0], bar) if edges else None
    drops = detect_drops(edges, energy, groove, pickup_e, pickup_g)
    secs = segment(edges, energy, drops, duration, bar)
    # section energy = mean of the curve over the span (as v5 did), pickup and tail included
    for s, m in zip(secs, curve_means([(s["start"], s["end"]) for s in secs], times, curve)):
        if m is not None:
            s["energy"] = round(float(m), 4)
    record["sections"] = secs
    record["drops"] = drops
    record["main_drop"] = main_drop(drops)
    record["structure"] = {"version": STRUCTURE_VERSION, "stems": groove is not None}
    return record
