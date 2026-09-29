"""The look of a played song, for later analysis of what the AI did to it.

Not audio: a compact waveform (peak + RMS per bin, ~10 Hz, capped) of the full mix
and of each cached stem, plus the analysis overlays (energy, phrase lines, sections,
vocal regions). `compute` builds it, `cached` stores it once per file hash, `render`
draws waveform.png (1600x900): one lane per stem, phrase grid, sections, and every
AI step marked where it happened on the song's timeline.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional

RATE_HZ = 10          # bins per second of song ...
MAX_BINS = 3000       # ... capped, so a long mix still fits the size budget
MAX_CURVE = 600       # energy curve points kept
SR = 11025
LANES = ("drums", "bass", "vocals", "other")
VERSION = 1


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def lane(y, bins: int) -> Dict[str, list]:
    """Peak |x| and RMS per bin, rounded (the json stays small)."""
    import numpy as np
    y = np.asarray(y, dtype=np.float32).ravel()
    n = len(y)
    bins = max(1, min(int(bins), n)) if n else 0
    if not bins:
        return {"peak": [], "rms": []}
    edges = np.linspace(0, n, bins + 1).astype(int)
    starts, widths = edges[:-1], np.maximum(np.diff(edges), 1)
    peak = np.maximum.reduceat(np.abs(y), starts)
    rms = np.sqrt(np.add.reduceat(y * y, starts) / widths)
    return {"peak": np.round(peak, 3).tolist(), "rms": np.round(rms, 3).tolist()}


def _thin(times: list, values: list, keep: int) -> Dict[str, list]:
    n = min(len(times), len(values))
    step = max(1, -(-n // keep))
    return {"times": [round(float(t), 2) for t in times[:n:step]],
            "curve": [round(float(v), 3) for v in values[:n:step]]}


def compute(audio_path: Path, stems: Optional[Dict[str, str]] = None,
            analysis: Optional[dict] = None) -> dict:
    """waveform.json content for one song (mix + every stem file that exists)."""
    from app.ui import engine
    a = analysis or {}
    y, _ = engine.load_audio(str(audio_path), sr=SR, mono=True)
    duration = len(y) / SR
    bins = max(1, min(int(duration * RATE_HZ), MAX_BINS))
    lanes = {"mix": lane(y, bins)}
    for name in LANES:
        p = (stems or {}).get(name)
        if p and Path(p).exists():
            ys, _ = engine.load_audio(str(p), sr=SR, mono=True)
            lanes[name] = lane(ys, bins)
    key = a.get("key")
    return {
        "version": VERSION,
        "duration": round(duration, 2),
        "bins": bins,
        "hz": round(bins / duration, 3) if duration else 0,
        "bpm": a.get("bpm"),
        "key": key.get("camelot") if isinstance(key, dict) else key,
        "lanes": lanes,
        "energy": _thin(a.get("energy_times") or [], a.get("energy_curve") or [], MAX_CURVE),
        "phrases": [round(float(t), 2) for t in a.get("phrase_boundaries_8bar") or []],
        "sections": [{"label": s.get("label"), "start": round(float(s.get("start", 0)), 2),
                      "end": round(float(s.get("end", 0)), 2)}
                     for s in a.get("sections") or [] if isinstance(s, dict)],
        "vocal_regions": [[round(float(r[0]), 2), round(float(r[1]), 2)]
                          for r in a.get("vocal_active_regions") or [] if len(r) >= 2],
    }


def cached(audio_path: Path, stems: Optional[Dict[str, str]], analysis: Optional[dict],
           cache_dir: Path) -> dict:
    """compute(), once per (file hash, lanes available): a song played twice is not redone."""
    have = [n for n in LANES if (stems or {}).get(n) and Path(stems[n]).exists()]
    key = f"{file_hash(Path(audio_path))}-v{VERSION}-{len(have)}"
    p = Path(cache_dir) / f"{key}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    wf = compute(Path(audio_path), stems, analysis)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(wf, separators=(",", ":")), encoding="utf-8")
    tmp.replace(p)
    return wf


def colour(kind: str) -> str:
    k = (kind or "").lower()
    for words, c in ((("glitch", "silence", "dropout", "clip", "refus"), "#111111"),
                     (("hook",), "#d6336c"),
                     (("drop", "cue"), "#e03131"),
                     (("merge", "mashup", "layer", "bridge", "riff"), "#7048e8"),
                     (("exit", "transition", "song_end", "song_start", "recipe"), "#f08c00"),
                     (("stem", "move", "loop", "decision", "mind"), "#1c7ed6")):
        if any(w in k for w in words):
            return c
    return "#868e96"


def render(png_path: Path, wf: dict, steps: List[dict], meta: Optional[dict] = None) -> Path:
    """waveform.png: a lane per stem, phrase grid, sections, vocals, AI steps on the timeline."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    meta = meta or {}
    names = list(wf.get("lanes", {}).keys()) or ["mix"]
    dur = float(wf.get("duration") or 1.0)
    fig, axes = plt.subplots(len(names), 1, figsize=(16, 9), dpi=100, sharex=True, squeeze=False)
    axes = axes[:, 0]
    marks = [s for s in steps if isinstance(s.get("at_song"), (int, float)) and 0 <= s["at_song"] <= dur + 5]
    for ax, name in zip(axes, names):
        ln = wf["lanes"].get(name) or {"peak": [], "rms": []}
        peak, rms = np.asarray(ln["peak"]), np.asarray(ln["rms"])
        x = np.linspace(0, dur, len(peak)) if len(peak) else np.array([])
        ax.fill_between(x, -peak, peak, color="#adb5bd", linewidth=0)
        ax.fill_between(x, -rms, rms, color="#495057", linewidth=0)
        ax.set_ylim(-1.05, 1.05)
        ax.set_yticks([])
        ax.set_ylabel(name, rotation=0, ha="right", va="center")
        for t in wf.get("phrases", []):
            ax.axvline(t, color="#dee2e6", linewidth=0.6, zorder=0)
        if name in ("mix", "vocals"):
            for a, b in wf.get("vocal_regions", []):
                ax.axvspan(a, b, color="#fcc2d7", alpha=0.35, linewidth=0, zorder=0)
        for s in marks:
            ax.axvline(s["at_song"], color=colour(s.get("kind", "")), linewidth=0.9, alpha=0.8)
    top = axes[0]
    for i, sec in enumerate(wf.get("sections", [])):
        top.axvspan(sec["start"], sec["end"], ymin=0.93, ymax=1.0,
                    color=("#e7f5ff" if i % 2 else "#d0ebff"), linewidth=0)
        top.text(sec["start"] + 0.5, 0.97, str(sec.get("label") or "")[:10], fontsize=7, va="center")
    en = wf.get("energy") or {}
    if en.get("times"):
        c = np.asarray(en["curve"], dtype=float)
        if c.size and c.max() > 0:
            top.plot(en["times"], c / c.max() * 0.9, color="#2f9e44", linewidth=0.8)
    for j, s in enumerate(marks[:120]):   # label every step (short), staggered in 6 rows
        lab = str(s.get("decision") or s.get("kind") or "")[:18]
        top.annotate(lab, (s["at_song"], 1.05), xytext=(0, 4 + 9 * (j % 6)), textcoords="offset points",
                     fontsize=6, rotation=0, color=colour(s.get("kind", "")), annotation_clip=False)
    axes[-1].set_xlim(0, dur)
    axes[-1].set_xlabel("song seconds")
    title = f"{meta.get('nn', '')} {meta.get('name') or meta.get('track_id') or ''}"
    bits = [f"{wf.get('bpm'):.1f} BPM" if isinstance(wf.get("bpm"), (int, float)) else "",
            str(wf.get("key") or ""), f"in: {meta.get('recipe_in') or '-'}",
            f"out: {meta.get('recipe_out') or '-'}", f"{len(steps)} AI steps"]
    fig.suptitle(title.strip() + "   " + "  |  ".join(b for b in bits if b), fontsize=10, y=0.995)
    fig.subplots_adjust(left=0.06, right=0.99, top=0.88, bottom=0.06, hspace=0.08)
    png_path = Path(png_path)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png_path, dpi=100)
    plt.close(fig)
    return png_path
