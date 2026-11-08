"""PAIR ATLAS: pre-knowledge of which library songs work together, and how.

Offline, deterministic, no LLM, no network. Every ordered pair (A -> B) of library
tracks with a cached analysis is judged by the console's OWN rules:

* Camelot score: techniques.camelot_score (the dj-mind.js camelotScore table,
  parity-tested), KEY_SAFE_MIN 0.6 gate (autopilot.js keySafeRecipe).
* tempo: gap with half / double folding (tempo-rule.js lockRate), 8 % key-lock cap.
* energy: energy.next_ok (golden-vector twin of autopilot.js energyStepOk).
* vocals / entry and exit points: blend.plan_blend (the /api/blend/plan answer).
* the recipe: autopilot.js decideRecipe + learnedRecipe, run in node unmodified
  (pair_atlas_rules.js), fed techniques.learned_pick.
* merge -> hold -> transition: stem-moves.js holdPlan in node on per-bar stem RMS.
* riff over rap, full mashup, learned moves: techniques.rank (PairFeatures).
* mashup-layer fit: a port of autopilot.js mashupFits (runtime closure, not exported).
* supermoves: dj-mind.js peakTransition (double drop / drop swap) plus the moves
  mascot.js CUE_MOVES announce as supermoves (MERGE, MASHUP, RIFF OVER RAP, ...).

Played evidence (sessions, set logs, learned_techniques sightings) is mined and
attached on every build, and so is STUDIED evidence (the transitions of the famous sets
the set learner studied, studied_combos.py): a studied pair is a combo. Incremental: a pair is
rescored only when either track's inputs or the rules change.

Stored in SQLite, CACHE_DIR/app.db (app.music_brain.db; tables atlas_meta, atlas_tracks,
atlas_pairs, see write_atlas). A build upserts only the rows whose JSON changed, in one transaction,
under _atlas_lock. load() still returns the whole atlas as one dict; request paths use pairs_for /
track / load_for / cached_index (indexed queries). The old segmented folder CACHE_DIR/pair_atlas/
(or an older single pair_atlas.json) is migrated once into the database on first open and renamed
<name>.migrated; migrate() reads that folder format for one more release.

    python3 -m app.music_brain.atlas.pair_atlas build [--cache-dir D] [--out DIR] [--full]
    python3 -m app.music_brain.atlas.pair_atlas show <track id|name> [--move merge] [-n 10]
    python3 -m app.music_brain.atlas.pair_atlas best [--move supermove] [-n 15] [--chains 3]
    python3 -m app.music_brain.atlas.pair_atlas studied [--missing] [--json]
    python3 -m app.music_brain.atlas.pair_atlas import-set <set_id> [--dry-run]   # studied set's songs -> library
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

HERE = Path(__file__).resolve().parent
STATIC = HERE.parents[1] / "ui" / "static"
RULES_JS = HERE.parent / "pair_atlas_rules.js"  # kept at old path (rules_hash reads its bytes)
SCHEMA = 1
ATLAS_NAME = "pair_atlas"            # the old folder name (migration source; its parent holds the DB)
META = "meta.json"
_META_ONLY = ("track_index", "shards")          # meta.json keys that are not part of the atlas dict
LIGHT_FIELDS = ("name", "artist", "bpm", "key", "duration", "level", "stems", "knowledge")
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
STEM_NAMES = ("drums", "bass", "vocals", "other")

# the files whose rules the atlas applies: any edit invalidates every pair
RULE_FILES = (
    HERE / "pair_atlas.py", RULES_JS, HERE.parent / "render" / "blend.py", HERE.parent / "analysis" / "energy.py", HERE.parent / "matching" / "techniques.py",
    STATIC / "autopilot.js", STATIC / "tempo-rule.js", STATIC / "stem-moves.js", STATIC / "dj-mind.js",
    HERE.parent / "render" / "drop_line.py", STATIC / "drop-line.js", HERE.parent / "render" / "entry_lines.py",
)

KEY_SAFE_MIN = 0.6          # autopilot.js KEY_SAFE_MIN (parity-tested)
KEYLOCK_CAP = 0.08          # tempo-rule.js KEYLOCK_RANGE_PCT / 100 (parity-tested)
PITCH_LOCK = 0.02           # autopilot.js READY_MIN_GAP: above it key-locked stems are needed
MASHUP_KEY_OK = 0.8         # autopilot.js mashupFits keyOk
FOLD = (1.0, 2.0, 0.5)      # tempo-rule.js lockRate ratios
BLEND_BARS = 16
MERGE_TRIES = 6             # A exit phrase lines tried for a merge -> hold per pair
COMBO_MIN_WORKS = 65        # a pair is a COMBO when it works this well AND a combo move fits
COMBO_MOVES = ("merge", "riff", "mashup", "double_drop", "drop_swap")
COMBO_LABEL = {"merge": "MERGE", "riff": "RIFF x RAP", "mashup": "MASHUP",
               "double_drop": "DOUBLE DROP", "drop_swap": "DROP SWAP",
               "studied": "STUDIED COMBO"}      # a pair a studied famous set played (studied_combos.py)
MOVES = ("merge", "riff", "mashup", "stem_bridge", "stem_intro", "bass_swap", "echo_out",
         "hook_drop", "learned", "artist", "double_drop", "drop_swap", "supermove")
# columns the atlas cannot judge offline (same answer for every pair, not stored per pair)
UNJUDGED = {"hook_drop": "judged live only (needs synced-lyrics hooks of A)",
            "artist": "artist moves are judged live only (not wired into the atlas yet)"}
MOVE_ALIASES = {"riff_over_rap": "riff", "rap": "riff", "hold": "merge", "super": "supermove",
                "peak": "supermove", "mashup_layer": "mashup"}
# the works score: weights of the 0-1 sub-scores (sum 1)
WEIGHTS = {"key": 0.30, "tempo": 0.20, "energy": 0.15, "vocal": 0.15, "stems": 0.10, "move": 0.10}
KEY_CLASH_CAP = 45          # a key clash (< KEY_SAFE_MIN) never "works" above this
PLAYED_GOOD, PLAYED_BAD = 4, 10   # works points per good / bad played transition (capped)
OWNER_VETO_BAD = 3                # bad transitions an owner veto counts as (3 x PLAYED_BAD = the -30 cap)


# ---------------------------------------------------------------- small pure helpers

def camelot_of(analysis: dict) -> Optional[str]:
    k = analysis.get("key")
    if isinstance(k, dict):
        return k.get("camelot")
    return k if isinstance(k, str) and k else None


def fold_gap(a_bpm: float, b_bpm: float) -> Tuple[float, float]:
    """(gap, rate) with half / double folding: tempo-rule.js lockRate."""
    if not (a_bpm and a_bpm > 0 and b_bpm and b_bpm > 0):
        return 1.0, 1.0
    r = min((a_bpm / (b_bpm * m) for m in FOLD), key=lambda x: abs(x - 1))
    return abs(r - 1), r


def share(regions: Optional[Sequence], lo: float, hi: float) -> float:
    if not regions or hi <= lo:
        return 0.0
    return sum(max(0.0, min(e, hi) - max(s, lo)) for s, e in regions) / (hi - lo)


def artist_of(name: str) -> str:
    n = (name or "").lower()
    for sep in (" - ", " – ", ": "):
        if sep in n:
            n = n.split(sep, 1)[0]
            break
    for sep in (" & ", " x ", ", ", " feat", " ft.", " ft "):
        n = n.split(sep, 1)[0]
    return n.strip()


def rules_hash() -> str:
    h = hashlib.sha256(f"schema{SCHEMA}".encode())
    for p in RULE_FILES:
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"missing:" + p.name.encode())
    return h.hexdigest()[:16]


def normalize_move(move: Optional[str]) -> Optional[str]:
    if not move:
        return None
    m = move.strip().lower().replace("-", "_").replace(" ", "_")
    m = MOVE_ALIASES.get(m, m)
    if m not in MOVES:
        raise ValueError(f"unknown move {move!r}: one of {', '.join(MOVES)}")
    return m


def mashup_fit(stems_both: bool, a_bpm: float, b_bpm: float, key: Optional[float],
               ve: Optional[dict], a_left_s: float) -> dict:
    """autopilot.js mashupFits (runtime closure, so ported): B's sung phrase over A's
    instrumental. {ok, M, gate}. Offline: key-locked tempo stems are assumed renderable."""
    if not stems_both:
        return {"ok": False, "M": 0, "gate": "stems"}
    if not ve or ve.get("entry") is None:
        return {"ok": False, "M": 0, "gate": "no B vocal entry"}
    gap = abs(a_bpm / b_bpm - 1) if a_bpm and b_bpm else 1.0
    if gap > KEYLOCK_CAP:
        return {"ok": False, "M": 0, "gate": f"tempo gap {gap:.1%} over the key-lock cap"}
    key_ok = key is None or key >= MASHUP_KEY_OK
    if not key_ok and not ve.get("rap"):
        return {"ok": False, "M": 0, "gate": f"keys clash (camelot {key:.2f}) under a sung vocal"}
    bar_s = 240.0 / a_bpm
    v16, v32 = ve.get("vocal16") or 0, ve.get("vocal32") or 0
    m = 32 if v32 >= 0.7 and a_left_s >= 44 * bar_s else 16 if a_left_s >= 26 * bar_s and v16 >= 0.5 else 0
    if not m:
        return {"ok": False, "M": 0, "gate": "B's vocal phrase too short or no room left in A"}
    return {"ok": True, "M": m, "gate": None}


def mashup_bars(m: int, ve: Optional[dict], busy_at) -> dict:
    """autopilot.js mashupBars: a 32-bar mashup whose vocal would sing over A's drop window keeps its
    existing 16-bar variant when that one is clear, else the refusal stands. -> {M, busy}"""
    busy = busy_at(m)
    if busy and m == 32 and ve and (ve.get("vocal16") or 0) >= 0.5 and not busy_at(16):
        return {"M": 16, "busy": None}
    return {"M": m, "busy": busy}


def works_score(key: Optional[float], gap: float, energy_ok: Optional[bool], vocal_clean: Optional[bool],
                stems: Tuple[bool, bool], merge_ok: bool, combo_ok: bool) -> Tuple[int, dict]:
    """0-100: how well A -> B works, before played evidence. -> (works, sub-scores)."""
    k = 0.5 if key is None else float(key)
    if gap <= PITCH_LOCK:
        t = 1.0
    elif gap <= KEYLOCK_CAP:
        t = 1.0 - 0.5 * (gap - PITCH_LOCK) / (KEYLOCK_CAP - PITCH_LOCK)
    else:
        t = 0.15
    e = 0.7 if energy_ok is None else (1.0 if energy_ok else 0.3)
    v = 0.7 if vocal_clean is None else (1.0 if vocal_clean else 0.5)
    s = 1.0 if all(stems) else 0.5 if any(stems) else 0.0
    mv = 1.0 if merge_ok else 0.8 if combo_ok else 0.4
    sub = {"key": round(k, 3), "tempo": round(t, 3), "energy": e, "vocal": v, "stems": s, "move": mv}
    w = 100 * sum(WEIGHTS[n] * sub[n] for n in WEIGHTS)
    if key is not None and key < KEY_SAFE_MIN:
        w = min(w, KEY_CLASH_CAP)
    return int(round(w)), sub


def played_adjust(played: Optional[dict]) -> int:
    if not played:
        return 0
    good = played.get("good", 0)
    bad = played.get("bad", 0)
    return min(12, PLAYED_GOOD * good) - min(30, PLAYED_BAD * bad)


# ---------------------------------------------------------------- library + per-track features

class Library:
    """Read-only view of a cache dir (uploads, analysis, stems, energy, fame, learned)."""

    def __init__(self, cache_dir: Path):
        self.dir = Path(cache_dir)

    def _json(self, rel: str, default):
        try:
            return json.loads((self.dir / rel).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def names(self) -> Dict[str, str]:
        d = self._json("uploads/_names.json", {})
        return {k: v for k, v in d.items() if isinstance(v, str)} if isinstance(d, dict) else {}

    def fame(self) -> dict:
        d = self._json("fame.json", {})
        return d if isinstance(d, dict) else {}

    def learned(self) -> dict:
        from app.music_brain.learning.set_learner import load_learned

        return load_learned(self.dir / "learned_techniques.json")

    def analysis_path(self, tid: str) -> Optional[Path]:
        for v in ("v6", "v5"):      # v6 = phrase-grid drops; v5 until the library is re-analysed
            hits = sorted((self.dir / "analysis").glob(f"{tid}*.{v}.json"))
            if hits:
                return hits[0]
        return None

    def stems(self, digest: str) -> Optional[Dict[str, str]]:
        from app.music_brain.audio.audio_io import read_manifest

        for model in ("htdemucs_ft", "htdemucs"):       # stem_service.cached_four_stems order
            m = read_manifest(self.dir / "stems" / f"{digest}_{model}")   # v1 or v2, FLAC or WAV
            if m and all(m.get(n) for n in STEM_NAMES):
                return {n: m[n] for n in STEM_NAMES}
        return None

    def tracks(self) -> List[dict]:
        """Every upload with a v5 / v6 analysis: [{id, name, path, analysis, digest, stems, energy}]"""
        from app.ui.services.download_service import _is_live, _is_mix

        names = self.names()
        out = []
        up = self.dir / "uploads"
        audio = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aiff", ".aif", ".opus"}
        for p in sorted(up.glob("*")) if up.is_dir() else []:
            if not p.is_file() or p.name.startswith("_") or p.suffix.lower() not in audio:
                continue
            tid = p.stem
            ap = self.analysis_path(tid)
            if ap is None:
                continue
            digest = ap.name.split(".", 1)[0]
            name = names.get(tid) or tid
            if _is_live(name) or _is_mix(name):
                continue        # not a song: the autopilot skips live recordings and mixes
            ep = self.dir / "analysis" / f"{digest}.energy.json"
            out.append({"id": tid, "name": name, "path": str(p), "analysis": str(ap), "digest": digest,
                        "stems": self.stems(digest), "energy": str(ep) if ep.exists() else None})
        return out


def _stat_sig(*paths: Optional[str]) -> str:
    h = hashlib.sha256()
    for p in paths:
        try:
            st = os.stat(p) if p else None
            h.update(f"{p}:{st.st_size}:{st.st_mtime_ns};".encode() if st else b"-;")
        except OSError:
            h.update(b"x;")
    return h.hexdigest()[:16]


def track_sig(t: dict) -> str:
    return _stat_sig(t["analysis"], t["energy"], *((t["stems"] or {}).get(n) for n in STEM_NAMES))


def _bar_rms(ch: np.ndarray, sr: int, anchor: float, bar: float, n: int) -> List[float]:
    """stem-moves.js stemEnergyBars at native tempo: per bar, RMS of every 64th sample."""
    out = []
    for i in range(n):
        s0 = max(0, int(math.floor((anchor + i * bar) * sr)))
        s1 = min(len(ch), int(math.floor((anchor + (i + 1) * bar) * sr)))
        seg = ch[s0:s1:64]
        out.append(round(float(np.sqrt(np.mean(seg * seg))), 5) if len(seg) else 0.0)
    return out


def _rap_at(vocals: np.ndarray, sr: int, vox: List[list]) -> List[float]:
    """server.py _pair_features' style scan: up to 8 chunks of 20 s of voice, the starts that rap."""
    import librosa
    from app.music_brain.matching import techniques

    chunks = [(s, min(e, s + 20)) for s, e in vox if e - s >= 12]
    out = []
    for s, e in chunks[::max(1, len(chunks) // 8)][:8]:
        y = librosa.resample(vocals[int(s * sr):int(e * sr)], orig_sr=sr, target_sr=16000)
        if techniques.vocal_style(y, 16000)["rap"]:
            out.append(round(s, 1))
    return out


def track_features(t: dict) -> dict:
    """The expensive per-track part (audio reads), cached in the atlas by track_sig."""
    from app.music_brain.matching import techniques

    a = json.loads(Path(t["analysis"]).read_text(encoding="utf-8"))
    bpm = float(a.get("bpm") or 0)
    dur = float(a.get("duration") or 0)
    phrases = [float(x) for x in a.get("phrase_boundaries_8bar") or []]
    bar = 240.0 / bpm if bpm > 0 else 2.0
    anchor = (phrases[0] % bar) if phrases else 0.0
    f = {"sig": track_sig(t), "bpm": bpm, "key": camelot_of(a), "duration": dur, "bar": bar, "anchor": anchor,
         "stems": bool(t["stems"]), "vox": None, "bars": None, "grooves": [], "breakdowns": [],
         "vocal_entry": None, "raw": None}
    if t["energy"]:
        try:
            e = json.loads(Path(t["energy"]).read_text(encoding="utf-8"))
            from app.music_brain.analysis import energy as en
            if e.get("version") == en.VERSION:
                f["raw"] = en._raw(e)
        except (OSError, ValueError, KeyError):
            pass
    if not t["stems"]:
        return f
    import soundfile as sf
    from app.music_brain.analysis.analyzer import vocal_presence_map

    try:
        f["vox"] = [[round(s, 2), round(e, 2)] for s, e in vocal_presence_map(Path(t["stems"]["vocals"]))]
    except Exception as exc:  # noqa: BLE001 -- one unreadable stem must not kill the build
        f["error"] = f"vocals: {exc}"
        return f
    n = int(max(0.0, dur - anchor) / bar) + 1
    bars, low = {}, {}
    for name in STEM_NAMES:
        try:
            y, sr = sf.read(t["stems"][name], dtype="float32", always_2d=True)
        except Exception as exc:  # noqa: BLE001
            f["error"] = f"{name}: {exc}"
            return f
        bars[name] = _bar_rms(y[:, 0], sr, anchor, bar, n)
        step = max(1, int(round(sr / 11025)))
        low[name] = y.mean(axis=1)[::step]
        if name == "vocals":
            f["rap_at"] = _rap_at(y.mean(axis=1), sr, f["vox"])
    f["bars"] = bars
    smap = techniques.stem_map(low, 11025, phrases)
    f["grooves"] = [list(x) for x in techniques.full_groove_runs(smap)]
    f["breakdowns"] = [list(x) for x in techniques.breakdowns(smap)]
    vox = f["vox"]
    first = next((p for p in phrases if share(vox, p, p + 16 * bar) >= 0.5), None)
    if first is not None:
        f["vocal_entry"] = {"entry": first, "rap": None,
                            "vocal16": round(share(vox, first, first + 16 * bar), 2),
                            "vocal32": round(share(vox, first, first + 32 * bar), 2)}
    return f


# ---------------------------------------------------------------- node bridge

def node_run(tracks: dict, jobs: List[dict], chunk: int = 4000, procs: int = 4) -> List[dict]:
    """Run jobs through the console's JS rules (pair_atlas_rules.js), a few node processes at once."""
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node is not installed: the atlas runs the console's own JS rules")
    if not jobs:
        return []
    chunks = [jobs[i:i + chunk] for i in range(0, len(jobs), chunk)]
    need = {}
    for c in chunks:
        ids = {j.get("a") for j in c} | {j.get("b") for j in c}
        need[id(c)] = {k: tracks[k] for k in ids if k in tracks}
    payloads = [json.dumps({"tracks": need[id(c)], "jobs": c}) for c in chunks]

    def run(payload: str) -> List[dict]:
        # subprocess.run feeds stdin through communicate(); closing stdin by hand first makes
        # communicate() flush a closed file (ValueError on Python < 3.12)
        r = subprocess.run([node, str(RULES_JS)], input=payload, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"pair_atlas_rules.js failed: {r.stderr[-500:]}")
        return json.loads(r.stdout)["results"]

    with ThreadPoolExecutor(max_workers=procs) as ex:
        return [r for part in ex.map(run, payloads) for r in part]


# ---------------------------------------------------------------- pair scoring

_W: dict = {}   # worker globals (process pool initializer)


def _init_worker(state: dict) -> None:
    _W.clear()
    _W.update(state)
    _W["ta"] = {}


def _ta(tid: str):
    from app.music_brain.analysis.analyzer import _from_dict

    if tid not in _W["ta"]:
        d = json.loads(Path(_W["meta"][tid]["analysis"]).read_text(encoding="utf-8"))
        if isinstance(d.get("key"), str):       # older records: key as a bare Camelot string
            d["key"] = {"camelot": d["key"], "key_name": d["key"], "is_major": d["key"].upper().endswith("B"), "confidence": 0.0}
        _W["ta"][tid] = _from_dict(d)
    return _W["ta"][tid]


def _play_window(f: dict, level: Optional[int]) -> Tuple[float, float]:
    """The console's hybrid play window for a good match (autopilot.js playWindowFor +
    exitBounds, entered on A's first phrase line). Values mirrored from WINDOWS."""
    dur = f["duration"]
    w = {"quick": (40, 100, 8), "medium": (120, 240, 16), "long": (180, 360, 24)}
    key = "medium" if level is None else "quick" if level >= 7 else "medium" if level <= 3 else "long" if level <= 5 else "medium"
    lo, hi, xf = w[key]
    entry = f["anchor"]
    end = dur - xf - 2
    return min(entry + lo, end), min(entry + hi, end)


def _entry_lines_b(tb, fb: dict, vb, level: Optional[int], include: float) -> List[float]:
    """B's strong-downbeat entry lines across the whole song (autopilot.js entryLines, band-free).
    Room = B's play window minimum + crossfade + 2 s (autopilot.js entryRoomS)."""
    from app.music_brain.render import blend, entry_lines

    if not fb.get("bpm") or fb["bpm"] <= 0:
        return []
    key = "medium" if level is None else "quick" if level >= 7 else "medium" if level <= 3 else "long" if level <= 5 else "medium"
    lo, xf = {"quick": (40, 8), "medium": (120, 16), "long": (180, 24)}[key]
    drops = [{"t": t, "energy": e} for t, e, _ in
             blend.drop_lines(tb.phrase_boundaries_8bar, tb.energy_times, tb.energy_curve, fb["bar"])]
    db = (fb.get("bars") or {}).get("drums")      # drum-stem bar RMS on B's phrase grid (track_features)
    drums = (lambda t: entry_lines.low_level_at(db, fb["anchor"], fb["bar"], t)) if db else None
    out = entry_lines.entry_lines(
        lines=tb.phrase_boundaries_8bar, include=include, energy_times=tb.energy_times,
        energy_curve=tb.energy_curve, vocals=[tuple(x) for x in vb] if vb is not None else None,
        drops=drops, bar=fb["bar"], end=fb["duration"], room_s=lo + xf + 2, band=None, drums=drums)
    return [round(t, 3) for t in out["lines"]]


def _score_a(a: str) -> Tuple[str, Dict[str, dict], List[dict]]:
    """All B partners of one A: the Python half of each record plus the node jobs it needs."""
    from app.music_brain.render import blend, drop_line
    from app.music_brain.matching import techniques
    from app.music_brain.analysis import energy

    feats, meta, levels, learned = _W["feats"], _W["meta"], _W["levels"], _W["learned"]
    todo_b = _W["todo"].get(a, [])
    fa = feats[a]
    ta = _ta(a)
    lo, hi = _play_window(fa, levels.get(a))
    va = fa["vox"]
    recs, jobs = {}, []
    a_drops = blend.track_drop_lines(ta, fa["bar"]) \
        if fa["bpm"] > 0 else []
    for b in todo_b:
        fb = feats[b]
        tb = _ta(b)
        # the console's key gate: dj-mind.js camelotScore (computed in node), unknown key -> None
        key = _W["keytab"].get(f"{fa['key']}>{fb['key']}") if fa["key"] and fb["key"] else None
        gap, rate = fold_gap(fa["bpm"], fb["bpm"])
        raw_gap = abs(fa["bpm"] / fb["bpm"] - 1) if fa["bpm"] and fb["bpm"] else 1.0
        la, lb = levels.get(a), levels.get(b)
        en = None
        if la is not None and lb is not None:
            raw_delta = None
            if fa["raw"] is not None and fb["raw"] is not None:
                raw_delta = fb["raw"] - fa["raw"]
            en = energy.next_ok(la, lb, raw_delta=raw_delta)
        stems = (fa["stems"] and fa["bars"] is not None, fb["stems"] and fb["bars"] is not None)
        vb = fb["vox"]
        bl = None
        if gap <= KEYLOCK_CAP and hi > lo:
            bl = blend.plan_blend(ta, tb, lo, hi, a_vocals=[tuple(x) for x in va] if va is not None else None,
                                  b_vocals=[tuple(x) for x in vb] if vb is not None else None, bars=BLEND_BARS)
            if not bl.get("ok"):
                bl = {"ok": False, "reasons": bl.get("reasons", [])}
        exit_t = bl["exit"] if bl and bl.get("ok") else (hi if hi > 0 else None)
        entry_t = bl["entry"] if bl and bl.get("ok") else (tb.phrase_boundaries_8bar[0] if tb.phrase_boundaries_8bar else 0.0)
        # techniques: riff over rap, full mashup, learned moves; learned_pick for the recipe
        xlo, xhi = max(0.0, fa["duration"] - 90), fa["duration"] - 10
        rap_at = fb.get("rap_at") or []
        pf = techniques.PairFeatures(
            bpm_a=fa["bpm"], bpm_b=fb["bpm"], key_a=fa["key"], key_b=fb["key"],
            stems_a=stems[0], stems_b=stems[1], famous_a=bool(_W["fame"].get(a, {}).get("famous")),
            vocal_a_exit=share(va, xlo, xhi),
            vocal_b_entry=max([share(vb, t, t + 30) for t in rap_at] or [share(vb, 0, min(fb["duration"], 60))]),
            b_rap=bool(rap_at) if stems[1] else None, b_rap_at=rap_at, a_grooves=[tuple(x) for x in fa["grooves"]],
            a_breakdowns=[tuple(x) for x in fa["breakdowns"]], keylock=_W["keylock"], exit_window=(xlo, xhi))
        ranked = techniques.rank(pf, learned)
        # /api/learned/pick's own key score (server side: techniques.camelot_score), as the console gets it
        pick = techniques.learned_pick(ranked, learned, key_score=techniques.camelot_score(fa["key"], fb["key"])
                                       if fa["key"] and fb["key"] else None)
        mash = mashup_fit(all(stems), fa["bpm"], fb["bpm"], key, fb["vocal_entry"],
                          (fa["duration"] - exit_t) if exit_t is not None else 0.0)
        if mash["ok"] and exit_t is not None:   # autopilot.js mashupGate: B's vocal over A's drop window
            ve_b = fb["vocal_entry"] or {}
            sings = None if vb is None else drop_line.mapped_sings(vb, exit_t, ve_b.get("entry") or 0.0, fb["bar"] / fa["bar"])
            mb = mashup_bars(mash["M"], ve_b, lambda m: drop_line.song_busy(
                ta, exit_t, exit_t + m * 240.0 / fa["bpm"], sings=sings))
            mash = {"ok": False, "M": 0, "gate": mb["busy"]["gate"]} if mb["busy"] else dict(mash, M=mb["M"])
        rec = {"a": a, "b": b, "key": key, "gap": round(gap, 4), "raw_gap": round(raw_gap, 4),
               "stems": list(stems),
               "energy": {"a": la, "b": lb, "ok": None if en is None else en["ok"],
                          **({"why": en["why"]} if en is not None and not en["ok"] else {})},
               "exit": None if exit_t is None else round(exit_t, 3), "entry": round(entry_t, 3),
               "blend": None if bl is None else {k: bl.get(k) for k in (
                   "ok", "instrumental", "b_vocal_coverage", "b_vocal_in_bars", "exit_label", "entry_label")},
               "techniques": [{"name": r["name"], "fits": r["fits"], "live": r["live"],
                               "gate": next((x[4:] for x in r["reasons"] if x.startswith("no: ")), None),
                               "score": int(round(100 * sum(x.startswith("ok: ") for x in r["reasons"])
                                                  / max(1, len(r["reasons"]))))} for r in ranked],
               "learned_pick": pick, "mashup": mash,
               # late entry: every strong-downbeat line B may enter on (band-free: the live
               # pick applies the main-drop-by-energy rule and the seeded uniform choice)
               "entry_lines": _entry_lines_b(tb, fb, vb, lb, entry_t)}
        recs[b] = rec
        # node: the console's recipe choice
        o = {"recipe": "Long Blend", "blend": bl if bl and bl.get("ok") else None, "layer": False,
             "aStems": stems[0], "bStems": stems[1], "aEff": fa["bpm"], "bBpm": fb["bpm"],
             # key-locked B stems at A's tempo: the console prerenders them (readinessNeeds "tempo stems")
             "tempoStemsBpm": fb["bpm"] * rate if all(stems) and gap <= KEYLOCK_CAP else None,
             "keyScore": key, "mashupFits": mash["ok"]}
        jobs.append({"kind": "recipe", "a": a, "b": b, "o": o, "pick": pick})
        # node: merge -> hold (only when the cheap gates can pass: holdPlan re-checks them all)
        if all(stems) and raw_gap <= KEYLOCK_CAP and (key is None or key >= 0.8):
            lines = [x for x in ta.phrase_boundaries_8bar if lo - 0.01 <= x <= hi + 0.01]
            if exit_t is not None and exit_t not in lines:
                lines.append(exit_t)
            lines = sorted(set(lines), key=lambda x: (abs(x - (exit_t or x)), -x))[:MERGE_TRIES]
            # autopilot.js planHold: B's vocal entry line, else bFallback (the blend plan's entry)
            bT = fb["vocal_entry"]["entry"] if fb["vocal_entry"] else entry_t
            for aT in lines:
                room = (fa["duration"] - aT) / fa["bar"]
                jobs.append({"kind": "hold", "a": a, "b": b, "aT": aT, "bT": bT, "gap": raw_gap, "keyScore": key,
                             "roomBars": room, "barS": 240.0 / fa["bpm"], "barA": fa["bar"], "barB": fb["bar"],
                             "bRap": False, "aVox": va, "bVox": vb})
        # node: supermoves (double drop / drop swap land B's drop on A's drop downbeat)
        if all(stems) and gap <= KEYLOCK_CAP and a_drops:
            dp = blend.plan_blend(ta, tb, lo, hi, a_vocals=[tuple(x) for x in va] if va is not None else None,
                                  b_vocals=[tuple(x) for x in vb] if vb is not None else None,
                                  bars=8, entry_mode="drop")
            if dp.get("ok"):
                jobs.append({"kind": "peak", "a": a, "b": b, "p": {
                    "peakOn": True, "peak": True, "drop": dp,
                    "aDrops": [{"t": t, "energy": e, "prevEnergy": pe} for t, e, pe in a_drops],
                    "aVocal": va or [], "lo": lo, "hi": hi, "plannedExit": exit_t or hi,
                    "entryPos": fa["anchor"], "bar": fa["bar"], "keyScore": key,
                    "bDropEnergy": dp.get("entry_energy"),
                    "brakesUsed": 0, "lastSwapBraked": False}})
    return a, recs, jobs


def _finish(rec: dict, recipe: Optional[dict], holds: List[dict], peak: Optional[dict]) -> dict:
    """Fold the node answers in: best plan, per-move compatibility, works score."""
    key, gap, stems = rec["key"], rec["gap"], rec["stems"]
    both = all(stems)
    lock = "pitched" if gap <= PITCH_LOCK else "keylock" if gap <= KEYLOCK_CAP else "none"
    # merge -> hold: the best feasible hold over the tried exits (longest, then best pick score)
    ok_holds = [h for h in holds if h.get("ok")]
    merge: dict
    if ok_holds:
        h = max(ok_holds, key=lambda x: (x["holdPhrases"], (x.get("pick") or {}).get("score", 0), x["aT"]))
        merge = {"ok": True, "gate": None, "aT": round(h["aT"], 3), "bT": round(h["bT"], 3), "M": h["M"],
                 "hold_bars": h["holdBars"], "hold_phrases": h["holdPhrases"], "pick": h.get("pick"),
                 "phases": h.get("phases"),
                 "score": int(min(100, 55 + 10 * h["holdPhrases"] + 5 * ((h.get("pick") or {}).get("score") or 0)))}
    elif holds:
        h = holds[0]
        merge = {"ok": False, "gate": h.get("gate"), "reason": h.get("reason"), "score": 0}
    else:
        why = ("stems" if not both else "tempo" if rec["raw_gap"] > KEYLOCK_CAP else
               "key" if key is not None and key < 0.8 else "room")
        merge = {"ok": False, "gate": why, "reason": {"stems": "needs 4 stems on both songs",
                                                      "tempo": f"gap {rec['raw_gap']:.1%} over the key-lock cap",
                                                      "key": f"camelot {key} < 0.8",
                                                      "room": "no exit line in A's play window"}[why], "score": 0}
    tech = {t["name"]: t for t in rec["techniques"]}
    riff = tech.get("riff_over_rap") or {}
    mash = rec["mashup"]
    bl = rec["blend"] or {}
    beat = bool(recipe and recipe.get("beat"))
    moves = {
        "merge": {"ok": merge["ok"], "score": merge["score"], "gate": merge.get("gate")},
        "riff": {"ok": bool(riff.get("fits")), "score": riff.get("score", 0), "gate": riff.get("gate")},
        "mashup": {"ok": mash["ok"], "score": (60 + mash["M"]) if mash["ok"] else 0, "gate": mash["gate"]},
        "stem_bridge": {"ok": both, "score": (70 if lock == "none" else 40) if both else 0,
                        "gate": None if both else "needs stems on both decks"},
        "stem_intro": {"ok": both and beat and (key is None or key >= KEY_SAFE_MIN),
                       "score": int(60 * (key or 0.5)) if both and beat else 0,
                       "gate": None if both and beat and (key is None or key >= KEY_SAFE_MIN) else
                       ("needs stems on both decks" if not both else "tempo does not lock" if not beat
                        else f"camelot {key} < {KEY_SAFE_MIN}")},
        "bass_swap": {"ok": beat and (key is None or key >= KEY_SAFE_MIN),
                      "score": int(100 * (key if key is not None else 0.5) * (1 if gap <= PITCH_LOCK else 0.8)) if beat else 0,
                      "gate": None if beat and (key is None or key >= KEY_SAFE_MIN) else
                      ("tempo does not lock" if not beat else f"camelot {key} < {KEY_SAFE_MIN} (Echo Out)")},
        "echo_out": {"ok": True, "score": 70 if (lock == "none" or (key is not None and key < KEY_SAFE_MIN)) else 30,
                     "gate": None},
        "hook_drop": {"ok": None, "score": None, "gate": "needs synced lyrics hooks (judged live only)"},
        "learned": {"ok": bool(rec["learned_pick"]) or any(t["fits"] and t["name"].startswith("learned:")
                                                          for t in rec["techniques"]),
                    "score": (rec["learned_pick"] or {}).get("seen", 0),
                    "gate": None if rec["learned_pick"] else "no learned move fits this pair",
                    "moves": [t["name"] for t in rec["techniques"] if t["fits"] and t["name"].startswith("learned:")]},
        "artist": {"ok": None, "score": None, "gate": "no artist-move planner on main"},
        "double_drop": {"ok": bool(peak and peak.get("kind") == "double_drop"), "score": 80 if peak and peak.get("kind") == "double_drop" else 0,
                        "gate": None if peak and peak.get("kind") == "double_drop" else "no shared drop downbeat / vocal or key rule"},
        "drop_swap": {"ok": bool(peak and peak.get("kind") == "drop_swap"), "score": 75 if peak and peak.get("kind") == "drop_swap" else 0,
                      "gate": None if peak and peak.get("kind") == "drop_swap" else "no drop swap (B's drop weaker or no drop)"},
    }
    sup = [m for m in ("merge", "mashup", "riff", "double_drop", "drop_swap") if moves[m]["ok"]]
    moves["supermove"] = {"ok": bool(sup), "score": max((moves[m]["score"] or 0) for m in sup) if sup else 0,
                          "gate": None if sup else "no supermove fits", "kinds": sup}
    combo_ok = any(moves[m]["ok"] for m in COMBO_MOVES)
    vclean = bl.get("instrumental") if bl.get("ok") else None
    works, sub = works_score(key, gap, rec["energy"]["ok"], vclean, (stems[0], stems[1]), merge["ok"], combo_ok)
    plan_recipe = (recipe or {}).get("recipe") or "Echo Out"
    learned_recipe = (recipe or {}).get("learned")
    if learned_recipe:
        plan_recipe = learned_recipe
    best = "Merge → Hold" if merge["ok"] else plan_recipe
    combo = None
    if works >= COMBO_MIN_WORKS:
        combo = max((m for m in COMBO_MOVES if moves[m]["ok"]), key=lambda m: moves[m]["score"] or 0, default=None)
    out = dict(rec)
    out.pop("techniques", None)
    lp = rec["learned_pick"]
    out["learned_pick"] = {k: lp[k] for k in ("kind", "recipe", "seen", "source")} if lp else None
    if out.get("blend"):
        out["blend"].pop("reasons", None)
    out["mashup"] = {"ok": mash["ok"], "M": mash["M"]}
    out.update({"lock": lock, "recipe": plan_recipe, "best": best,
                "recipe_why": (recipe or {}).get("learnedWhy") or (recipe or {}).get("vocalCut") or None,
                "merge": merge, "moves": {m: pack_move(v) for m, v in moves.items() if m not in UNJUDGED},
                "combo": combo,
                "works_base": works, "works": works, "sub": sub})
    if merge["ok"]:
        out["exit"], out["entry"] = merge["aT"], merge["bT"]
    return out


def pack_move(v: dict) -> list:
    """stored compactly: [ok (1/0/null), score, gate, extra]"""
    ok = v.get("ok")
    extra = v.get("kinds") or v.get("moves") or None
    gate = v.get("gate")
    row = [None if ok is None else int(bool(ok)), v.get("score"), gate[:60] if isinstance(gate, str) else gate]
    return row + [extra] if extra else row


def move_of(p: dict, m: str) -> dict:
    r = (p.get("moves") or {}).get(m)
    if r is None:
        return {"ok": None, "score": None, "gate": UNJUDGED.get(m, "not judged")}
    if isinstance(r, dict):
        return r
    d = {"ok": None if r[0] is None else bool(r[0]), "score": r[1], "gate": r[2]}
    if len(r) > 3:
        d["kinds"] = r[3]
    return d


# ---------------------------------------------------------------- played evidence

def _norm(s: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in (s or "").lower()).split())


class _NameIndex:
    def __init__(self, names: Dict[str, str]):
        self.exact = {}
        self.items = [(tid, _norm(n)) for tid, n in names.items()]
        for tid, n in self.items:
            self.exact.setdefault(n, tid)

    def find(self, name: str) -> Optional[str]:
        n = _norm(name)
        if not n:
            return None
        if n in self.exact:
            return self.exact[n]
        hits = [tid for tid, full in self.items if n in full or (len(full) > 6 and full in n)]
        return hits[0] if len(set(hits)) == 1 else None


def _read_jsonl(p: Path) -> List[dict]:
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def mine_history(cache_dir: Path, names: Dict[str, str]) -> Dict[str, dict]:
    """{"A>B": {count, good, bad, recipes, executed, glitches, dead_air, ear, merge, sources,
    sessions, learned}} from sessions (songs/*/meta.json + steps.jsonl, else events.jsonl),
    set logs (djset.json track ids) and learned_techniques sightings naming both tracks."""
    idx = _NameIndex(names)
    ev: Dict[str, dict] = {}

    def slot(a, b):
        return ev.setdefault(f"{a}>{b}", {"count": 0, "good": 0, "bad": 0, "recipes": Counter(), "executed": Counter(),
                                          "glitches": 0, "dead_air": 0, "ear": Counter(), "merge": Counter(),
                                          "sources": Counter(), "sessions": [], "learned": []})

    def outcome(e, glitches, dead, dropped):
        if dead or glitches >= 2 or dropped:
            e["bad"] += 1
        elif glitches == 0:
            e["good"] += 1

    sdir = Path(cache_dir) / "sessions"
    for s in sorted(sdir.iterdir()) if sdir.is_dir() else []:
        songs = sorted((s / "songs").glob("*/meta.json")) if (s / "songs").is_dir() else []
        if songs:
            metas = []
            for m in songs:
                try:
                    metas.append((json.loads(m.read_text(encoding="utf-8")), _read_jsonl(m.parent / "steps.jsonl")))
                except (OSError, ValueError):
                    continue
            metas.sort(key=lambda x: x[0].get("nn") or 0)
            for (pa, sa), (pb, sb) in zip(metas, metas[1:]):
                a, b = pa.get("track_id"), pb.get("track_id")
                if not a or not b or a == b:
                    continue
                e = slot(a, b)
                e["count"] += 1
                e["sources"]["session"] += 1
                e["sessions"].append(s.name)
                if pb.get("recipe_in"):
                    e["recipes"][pb["recipe_in"]] += 1
                seam = [x for x in sa if x.get("phase") == "transition-out"] + \
                       [x for x in sb if x.get("phase") == "transition-in"]
                g = [x for x in seam if x.get("kind") == "glitch"]
                dead = sum(1 for x in g if x.get("decision") == "silence")
                for x in sa + sb:
                    k = x.get("kind")
                    if k == "recipe_executed" and x.get("decision"):
                        e["executed"][x["decision"]] += 1
                    elif k == "silent-ear":
                        e["ear"][((x.get("result") or {}).get("verdict")) or "?"] += 1
                    elif k in ("merge_gate", "merge_audition", "merge_deferred"):
                        e["merge"][f"{k}:{x.get('decision')}"] += 1
                e["glitches"] += len(g)
                e["dead_air"] += dead
                outcome(e, len(g), dead, bool(pb.get("dropped")))
            continue
        cur = None
        for x in _read_jsonl(s / "events.jsonl"):
            if x.get("kind") == "track" and x.get("event") == "transition_start":
                a, b = idx.find(x.get("from", "")), idx.find(x.get("to", ""))
                cur = None
                if a and b and a != b:
                    cur = slot(a, b)
                    cur["count"] += 1
                    cur["sources"]["session"] += 1
                    cur["sessions"].append(s.name)
                    if x.get("recipe"):
                        cur["recipes"][x["recipe"]] += 1
                    cur["_g"], cur["_d"] = 0, 0
            elif cur is not None and x.get("kind") == "glitch":
                cur["_g"] += 1
                cur["glitches"] += 1
                if x.get("kind_") == "silence":
                    cur["_d"] += 1
                    cur["dead_air"] += 1
            elif cur is not None and x.get("kind") == "track" and x.get("event") == "transition_end":
                outcome(cur, cur.pop("_g", 0), cur.pop("_d", 0), False)
                cur = None
        if cur is not None:
            outcome(cur, cur.pop("_g", 0), cur.pop("_d", 0), False)
    for p in sorted((Path(cache_dir) / "set_logs").glob("*.djset.json")):
        try:
            md = json.loads(p.read_text(encoding="utf-8")).get("metadata") or {}
        except (OSError, ValueError):
            continue
        a, b = md.get("track_a_id"), md.get("track_b_id")
        if a and b and a != b:
            e = slot(a, b)
            e["count"] += 1
            e["sources"]["set_log"] += 1
    from app.music_brain.learning import liked as _liked   # the owner's liked transitions: PLAYED_GOOD evidence
    for a, b in _liked.pairs(cache_dir):
        if a != b:
            e = slot(a, b)
            e["good"] += 1
            e["sources"]["liked"] += 1
    from app.music_brain.learning.set_learner import load_learned

    store = load_learned(Path(cache_dir) / "learned_techniques.json")
    for kind, entry in (store.items() if isinstance(store, dict) else []):
        for o in (entry or {}).get("observations") or []:
            a, b = idx.find(o.get("track_a", "")), idx.find(o.get("track_b", ""))
            if a and b and a != b:
                slot(a, b)["learned"].append({"kind": kind, "set_id": o.get("set_id"), "at": o.get("at")})
    # OWNER VETO (atlas/vetoes.py, console "bad pair"): a vetoed pair is PLAYED_BAD evidence, heavy
    # enough (OWNER_VETO_BAD) that bad outweighs any good the pair ever had
    try:
        from app.music_brain.atlas import vetoes as _vetoes
        bad = _vetoes.bad_pairs(_vetoes.load(cache_dir))
    except Exception as exc:  # noqa: BLE001 -- a bad veto file must not stop a build
        print(f"[atlas] vetoes not read: {type(exc).__name__}: {exc}", flush=True)
        bad = []
    for an, bn in bad:
        a, b = idx.find(an), idx.find(bn)
        if a and b and a != b:
            e = slot(a, b)
            e["bad"] = max(e["bad"], e["good"]) + OWNER_VETO_BAD
            e["sources"]["owner_veto"] += 1
    for e in ev.values():
        for k in ("_g", "_d"):
            e.pop(k, None)
        for k in ("recipes", "executed", "ear", "merge", "sources"):
            e[k] = dict(e[k])
        e["sessions"] = sorted(set(e["sessions"]))
    return ev


# ---------------------------------------------------------------- build

# storage lives in atlas_store.py (not a rule file: a storage change never forces a rescore)
from app.music_brain.atlas.atlas_store import (  # noqa: E402,F401
    atlas_path, _root, _safe_id, _read_json, _dumps, _write_if_changed, _legacy, _meta, _load_file, _load_folder,
    ATLAS_STEPS, _db, _has_atlas, _db_meta, _REV, _objects, migrate, load, load_meta, pairs_for, track, load_for,
    write_atlas,
)


def library_levels(feats: Dict[str, dict], cache_dir: Path) -> Dict[str, Optional[int]]:
    """Energy level 1-10 per track: energy.level_from_raw against the library's raw scores."""
    from app.music_brain.analysis import energy

    raws = []
    for p in (Path(cache_dir) / "analysis").glob("*.energy.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("version") == energy.VERSION:
                raws.append(energy._raw(d))
        except (OSError, ValueError, KeyError):
            continue
    return {t: (None if f.get("raw") is None else energy.level_from_raw(f["raw"], raws)) for t, f in feats.items()}


def build(cache_dir: Path, out: Optional[Path] = None, full: bool = False, workers: Optional[int] = None,
          log=print, only: Optional[Sequence[str]] = None, seed_macros_to: Optional[Path] = None) -> dict:
    """Score every ordered pair; only: restrict to these track ids (tests, trial runs). Runs under
    _atlas_lock (re-entrant in one process, so callers that already hold it are fine)."""
    from app.music_brain.learning.set_import import _atlas_lock

    with _atlas_lock(Path(cache_dir)):
        return _build(cache_dir, out, full, workers, log, only, seed_macros_to)


def _build(cache_dir: Path, out: Optional[Path], full: bool, workers: Optional[int], log,
           only: Optional[Sequence[str]], seed_macros_to: Optional[Path]) -> dict:
    from app.music_brain.audio import keylock

    t0 = time.time()
    cache_dir = Path(cache_dir)
    out = Path(out) if out else atlas_path(cache_dir)
    migrate(out, log)
    lib = Library(cache_dir)
    rh = rules_hash()
    old = None if full else load(path=out)
    if old and old.get("rules") != rh:
        log(f"rules changed ({old.get('rules')} -> {rh}): rescoring every pair")
        old_pairs = {}
    else:
        old_pairs = (old or {}).get("pairs", {})
    old_tracks = (old or {}).get("tracks", {})
    tracks = lib.tracks()
    if only:
        tracks = [t for t in tracks if t["id"] in set(only)]
    meta = {t["id"]: t for t in tracks}
    workers = workers or max(1, min(8, (os.cpu_count() or 2) - 1))
    # per-track features: reuse when the inputs' signature is unchanged
    feats, redo = {}, []
    for t in tracks:
        f = old_tracks.get(t["id"])
        if f and f.get("sig") == track_sig(t) and "bars" in f and (f["bars"] is None or "rap_at" in f):
            feats[t["id"]] = f
        else:
            redo.append(t)
    t1 = time.time()
    if redo:
        log(f"features: {len(redo)} of {len(tracks)} tracks to (re)measure")
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for t, f in zip(redo, ex.map(track_features, redo, chunksize=4)):
                feats[t["id"]] = f
    t_feat = time.time() - t1
    levels = library_levels(feats, cache_dir)
    for tid, f in feats.items():
        f["level"] = levels.get(tid)
    ids = sorted(feats)
    todo: Dict[str, List[str]] = {}
    kept = {}
    for a in ids:
        for b in ids:
            if a == b:
                continue
            k = f"{a}>{b}"
            p = old_pairs.get(k)
            if p and p.get("sig") == [feats[a]["sig"], feats[b]["sig"], feats[a].get("level"), feats[b].get("level")]:
                kept[k] = p
            else:
                todo.setdefault(a, []).append(b)
    n_todo = sum(len(v) for v in todo.values())
    log(f"pairs: {len(ids) * (len(ids) - 1)} total, {n_todo} to score, {len(kept)} unchanged")
    keys = sorted({f["key"] for f in feats.values() if f.get("key")})
    kj = [{"kind": "camelot", "a": x, "b": y} for x in keys for y in keys]
    keytab = {f"{j['a']}>{j['b']}": r for j, r in zip(kj, node_run({}, kj))}
    state = {"keytab": keytab, "feats": feats, "meta": {t: {"analysis": meta[t]["analysis"]} for t in ids}, "levels": levels,
             "learned": lib.learned(), "fame": lib.fame(), "keylock": keylock.available(), "todo": todo}
    t2 = time.time()
    recs: Dict[str, dict] = {}
    jobs: List[dict] = []
    if todo:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(state,)) as ex:
            for a, r, j in ex.map(_score_a, list(todo), chunksize=2):
                for b, rec in r.items():
                    recs[f"{a}>{b}"] = rec
                jobs.extend(j)
    t_py = time.time() - t2
    t3 = time.time()
    node_tracks = {t: {"bar": f["bar"], "anchor": f["anchor"], "bars": f["bars"]} for t, f in feats.items()}
    res = node_run(node_tracks, jobs)
    t_node = time.time() - t3
    by: Dict[str, dict] = {}
    for j, r in zip(jobs, res):
        e = by.setdefault(f"{j['a']}>{j['b']}", {"recipe": None, "holds": [], "peak": None})
        if j["kind"] == "recipe":
            e["recipe"] = r
        elif j["kind"] == "hold":
            e["holds"].append(dict(r, aT=j["aT"], bT=j["bT"]))
        elif j["kind"] == "peak":
            e["peak"] = r
    pairs = dict(kept)
    for k, rec in recs.items():
        e = by.get(k, {})
        p = _finish(rec, e.get("recipe"), e.get("holds", []), e.get("peak"))
        a, b = rec["a"], rec["b"]
        p["sig"] = [feats[a]["sig"], feats[b]["sig"], feats[a].get("level"), feats[b].get("level")]
        pairs[k] = p
    # played evidence: recomputed every build (cheap; history grows without the tracks changing)
    names = {t["id"]: t["name"] for t in tracks}
    ev = mine_history(cache_dir, {**lib.names(), **names})
    for k, p in pairs.items():
        pl = ev.get(k)
        p["played"] = pl
        p["works"] = int(max(0, min(100, p["works_base"] + played_adjust(pl))))
        p.pop("seed", None)
    # the owner's seed combos: a merge that holdPlan accepts is a combo whatever its works score
    for s in load_seeds():
        p = pairs.get(f"{s['a']}>{s['b']}")
        if p is not None:
            p["seed"] = True
            if s.get("move") == "merge" and p["merge"]["ok"]:
                p["combo"] = "merge"
    # studied combos: transitions the famous studied sets played (recomputed every build)
    from app.music_brain.atlas import studied_combos as sc

    studied = sc.load(cache_dir, {**lib.names(), **names})
    n_studied = sc.attach(pairs, sc.evidence(studied))
    doc = {"schema": SCHEMA, "rules": rh, "built_at": time.time(), "cache_dir": str(cache_dir),
           "tracks": {t: dict(feats[t], name=names.get(t, t), artist=artist_of(names.get(t, t))) for t in ids},
           "pairs": pairs,
           "stats": {"tracks": len(ids), "pairs": len(pairs), "scored": len(recs), "unchanged": len(kept),
                     "feature_tracks": len(redo), "played_pairs": sum(1 for p in pairs.values() if p.get("played")),
                     "merge_ok": sum(1 for p in pairs.values() if p["merge"]["ok"]),
                     "combos": sum(1 for p in pairs.values() if p.get("combo")),
                     "studied_transitions": len(studied), "studied_pairs": n_studied,
                     "seconds": {"features": round(t_feat, 1), "python": round(t_py, 1), "node": round(t_node, 1),
                                 "total": round(time.time() - t0, 1)}}}
    doc["written"] = write_atlas(doc, out)          # changed rows only, one transaction (not stored)
    log(json.dumps(doc["stats"]) + f" written {json.dumps(doc['written'])}")
    if seed_macros_to is not None:
        doc["seeded"] = seed_macros(doc, seed_macros_to) + sc.write_macros(doc, studied, seed_macros_to)
        log(f"seeded macros: {len(doc['seeded'])} ({', '.join(m['name'] for m in doc['seeded'][:4])} ...)")
    return doc


SEED_FILE = HERE.parent / "seed_combos.json"


def load_seeds(path: Path = SEED_FILE) -> List[dict]:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [c for c in d.get("combos", []) if isinstance(c, dict) and c.get("a") and c.get("b")]


def _short(name: str, n: int = 18) -> str:
    from app.music_brain.atlas.macros import slug

    try:
        return slug(name)[:n].strip("-") or "x"
    except ValueError:
        return "x"


def seed_macros(doc: dict, cache_dir: Path, top: int = 20, n_chains: int = 4) -> List[dict]:
    """Ready-made macros from the atlas (owner): the seed combos (combo-seed-*, merge -> hold with
    their holdPlan answer, or the refusing gate noted), the top `top` combo pairs by works for
    each combo move (combo-<A>-<B>) and `n_chains` chain macros (5-8 songs, chain-<k>).
    Only macros this function wrote before (source atlas*) are overwritten."""
    from app.music_brain.atlas import macros as mc

    tr, out = doc["tracks"], []

    def step_of(p: dict, why: str) -> dict:
        pl = plan_of(p)
        return {"a": p["a"], "b": p["b"], "a_name": tr[p["a"]]["name"], "b_name": tr[p["b"]]["name"],
                "recipe": pl["recipe"], "a_time": pl["a_time"], "b_time": pl["b_time"], "merge": pl["merge"],
                "tempo": {"lock": p["lock"]}, "combo": p.get("combo"), "works": p["works"], "why": why}

    def write(m: dict) -> None:
        try:
            out.append(mc.write_seed(m, cache_dir))
        except ValueError:
            pass

    for s in load_seeds():
        p = doc["pairs"].get(f"{s['a']}>{s['b']}")
        if p is None:
            continue
        m = p["merge"]
        why = (f"seed combo: merge -> hold {m.get('hold_bars')} bars ({m.get('hold_phrases')} phrases)" if m["ok"]
               else f"seed combo: merge -> hold refused ({m.get('gate')}: {m.get('reason')}), plays {p['recipe']}")
        write({"name": f"combo-seed-{_short(tr[p['a']]['name'])}-{_short(tr[p['b']]['name'])}", "source": "atlas:seed",
               "steps": [step_of(p, why)], "note": why})
    # studied combos get their own macros (studied_combos.write_macros)
    combos = sorted((p for p in doc["pairs"].values() if p.get("combo") and p["combo"] != "studied"),
                    key=lambda p: (-p["works"], p["a"], p["b"]))
    per_move: Counter = Counter()      # top `top` per combo move: one MACRO section each (owner, 2026-10-11)
    for p in combos:
        per_move[p["combo"]] += 1
        if per_move[p["combo"]] > top:
            continue
        lab = COMBO_LABEL.get(p["combo"], p["combo"])
        write({"name": f"combo-{_short(tr[p['a']]['name'])}-{_short(tr[p['b']]['name'])}", "source": "atlas:combo",
               "steps": [step_of(p, f"atlas combo {lab} (works {p['works']})")]})
    for k, c in enumerate(chains(doc, length=8, k=n_chains, min_works=55), 1):
        if len(c["tracks"]) < 5:
            continue
        steps = [step_of(doc["pairs"][f"{x}>{y}"], f"chain {k}: works {doc['pairs'][f'{x}>{y}']['works']}")
                 for x, y in zip(c["tracks"], c["tracks"][1:])]
        write({"name": f"chain-{k}-{_short(tr[c['tracks'][0]]['name'])}", "source": "atlas:chain", "steps": steps,
               "note": f"mean works {c['mean_works']}"})
    return out


# ---------------------------------------------------------------- queries

def _move_score(p: dict, move: Optional[str]) -> Tuple[bool, float]:
    if move is None:
        return True, p["works"]
    m = move_of(p, move)
    return bool(m.get("ok")), (m.get("score") or 0) + p["works"] / 100.0


def resolve(atlas: dict, q: str) -> Optional[str]:
    if q in atlas["tracks"]:
        return q
    hits = [t for t, f in atlas["tracks"].items() if t.startswith(q)] or \
           [t for t, f in atlas["tracks"].items() if _norm(q) in _norm(f.get("name", ""))]
    return hits[0] if hits else None


def pair(atlas: dict, a: str, b: str) -> Optional[dict]:
    return atlas["pairs"].get(f"{a}>{b}")


def partners(atlas: dict, a: str, move: Optional[str] = None, n: int = 10) -> List[dict]:
    move = normalize_move(move)
    rows = []
    for b in atlas["tracks"]:
        p = atlas["pairs"].get(f"{a}>{b}")
        if not p:
            continue
        ok, s = _move_score(p, move)
        if ok:
            rows.append((s, p))
    rows.sort(key=lambda x: (-x[0], x[1]["b"]))
    return [summary(atlas, p) for _, p in rows[:n]]


def best_pairs(atlas: dict, move: Optional[str] = None, n: int = 15) -> List[dict]:
    move = normalize_move(move)
    rows = []
    for p in atlas["pairs"].values():
        ok, s = _move_score(p, move)
        if ok:
            rows.append((s, p))
    rows.sort(key=lambda x: (-x[0], x[1]["a"], x[1]["b"]))
    return [summary(atlas, p) for _, p in rows[:n]]


def _studied_brief(st: Optional[dict]) -> Optional[dict]:
    """What the console shows of a studied pair: count, sets, DJs, techniques, the move it plays."""
    if not st:
        return None
    return {k: st.get(k) for k in ("count", "sets", "djs", "techniques", "move", "recipe", "why")}


def summary(atlas: dict, p: dict) -> dict:
    tr = atlas["tracks"]
    tb = tr.get(p["b"], {})
    return {"a": p["a"], "b": p["b"], "a_name": tr.get(p["a"], {}).get("name"), "b_name": tb.get("name"),
            "b_bpm": tb.get("bpm"), "b_duration": tb.get("duration"), "b_level": tb.get("level"),
            "works": p["works"], "best": p["best"], "recipe": p["recipe"], "combo": p.get("combo"),
            "combo_label": COMBO_LABEL.get(p.get("combo")), "key": p["key"], "gap": p["gap"], "lock": p["lock"],
            "exit": p["exit"], "entry": p["entry"], "merge_ok": p["merge"]["ok"],
            "merge": {k: p["merge"].get(k) for k in ("ok", "gate", "reason", "hold_bars", "hold_phrases", "M", "aT", "bT")},
            "moves": {m: move_of(p, m) for m in MOVES},
            "energy": p["energy"], "played": bool(p.get("played")), "seed": bool(p.get("seed")),
            "studied": _studied_brief(p.get("studied")),
            "played_good": (p.get("played") or {}).get("good", 0), "played_bad": (p.get("played") or {}).get("bad", 0)}


def _arc_ok(levels: List[Optional[int]], nxt: Optional[int]) -> bool:
    """Chain energy arc: the pair rule (energy.next_ok with the set position), and no second
    fall in a row while building. Unknown levels pass."""
    from app.music_brain.analysis import energy

    cur = levels[-1] if levels else None
    if cur is None or nxt is None:
        return True
    r = energy.next_ok(cur, nxt, songs=len(levels), recent=[v for v in levels if v is not None])
    return bool(r["ok"])


def chains(atlas: dict, length: int = 8, k: int = 3, beam: int = 40, min_works: int = 50,
           spacing: int = 3, start: Optional[str] = None, pool: Optional[Sequence[str]] = None) -> List[dict]:
    """Good song chains by beam search: sum of works scores, energy arc respected, no artist
    within `spacing` songs of itself, no repeats. pool: restrict to these tracks."""
    tr = atlas["tracks"]
    ids = list(pool) if pool is not None else list(tr)
    idset = set(ids)
    by_a: Dict[str, List[Tuple[str, dict]]] = {}
    for p in atlas["pairs"].values():
        if p["a"] in idset and p["b"] in idset and p["works"] >= min_works:
            by_a.setdefault(p["a"], []).append((p["b"], p))
    for a in by_a:
        by_a[a].sort(key=lambda x: -x[1]["works"])
    starts = [start] if start else ids
    states = [(0.0, [s]) for s in starts if s in idset]
    for _ in range(length - 1):
        nxt = []
        for score, path in states:
            last = path[-1]
            recent_art = {tr.get(x, {}).get("artist") for x in path[-spacing:]}
            levels = [tr.get(x, {}).get("level") for x in path]
            for b, p in by_a.get(last, [])[:25]:
                if b in path or (tr.get(b, {}).get("artist") and tr[b]["artist"] in recent_art):
                    continue
                if not _arc_ok(levels, tr.get(b, {}).get("level")):
                    continue
                nxt.append((score + p["works"], path + [b]))
        if not nxt:
            break
        nxt.sort(key=lambda x: (-x[0], x[1]))
        states = nxt[:beam]
    states.sort(key=lambda x: (-x[0] / max(1, len(x[1]) - 1), x[1]))
    out, seen = [], set()
    for score, path in states:
        if len(path) < 2 or path[0] in seen:
            continue
        seen.add(path[0])
        steps = [summary(atlas, atlas["pairs"][f"{x}>{y}"]) for x, y in zip(path, path[1:])]
        out.append({"tracks": path, "names": [tr.get(x, {}).get("name") for x in path],
                    "mean_works": round(score / max(1, len(path) - 1), 1), "steps": steps})
        if len(out) >= k:
            break
    return out


def order_picks(atlas: dict, ids: Sequence[str], locked: bool = False) -> dict:
    """PLAN FROM PICKS: the best flow over the user's picks (or their order if locked),
    with each transition filled from the atlas. Unknown pairs keep a plain Echo Out."""
    ids = [i for i in dict.fromkeys(ids)]
    if not locked and len(ids) > 2:
        best = None
        for s in ids:
            c = chains(atlas, length=len(ids), k=1, beam=60, min_works=0, spacing=0, start=s, pool=ids)
            if c and len(c[0]["tracks"]) == len(ids) and (best is None or c[0]["mean_works"] > best["mean_works"]):
                best = c[0]
        order = best["tracks"] if best else ids
    else:
        order = ids
    steps = []
    for x, y in zip(order, order[1:]):
        p = atlas["pairs"].get(f"{x}>{y}")
        steps.append(summary(atlas, p) if p else {"a": x, "b": y, "works": None, "best": "Echo Out",
                                                   "recipe": "Echo Out", "exit": None, "entry": None,
                                                   "merge_ok": False, "merge": {"ok": False}})
    return {"order": order, "locked": locked, "steps": steps}


class Index:
    """What the server keeps in memory: per A, the partner summaries worth serving
    (top `keep` by works, top `per_move` per move, every combo and played pair).
    Index(atlas): from a whole atlas dict, every A up front. Index.lazy(root, meta): from the DB,
    meta now and one A's pair rows the first time A is asked for, so a request never reads
    every row."""

    def __init__(self, atlas: Optional[dict] = None, keep: int = 60, per_move: int = 20, *,
                 root: Optional[Path] = None, meta: Optional[dict] = None):
        head = atlas if atlas is not None else (meta or {})
        self.rules, self.built_at, self.stats = head.get("rules"), head.get("built_at"), head.get("stats")
        self.keep, self.per_move, self.root = keep, per_move, root
        tracks = atlas["tracks"] if atlas is not None else (meta or {}).get("track_index") or {}
        self._light = {"tracks": tracks}                # all summary() reads: name, bpm, duration
        self.names = {t: f.get("name") for t, f in tracks.items()}
        self.by_a: Dict[str, List[dict]] = {}
        self.pairs: Dict[str, dict] = {}
        if atlas is not None:
            by_a: Dict[str, List[dict]] = {}
            for p in atlas["pairs"].values():
                by_a.setdefault(p["a"], []).append(p)
            for a, ps in by_a.items():
                self._fill(a, ps)

    @classmethod
    def lazy(cls, root: Path, meta: dict, keep: int = 60, per_move: int = 20) -> "Index":
        return cls(None, keep, per_move, root=Path(root), meta=meta)

    def _fill(self, a: str, ps: List[dict]) -> List[dict]:
        ps.sort(key=lambda p: (-p["works"], p["b"]))
        pick = {p["b"]: p for p in ps[:self.keep]}
        for p in ps:
            if p.get("combo") or p.get("played") or p.get("seed"):
                pick[p["b"]] = p
        for m in MOVES:
            if m in UNJUDGED:
                continue
            ok = [p for p in ps if move_of(p, m).get("ok")]
            ok.sort(key=lambda p: (-(move_of(p, m).get("score") or 0), -p["works"], p["b"]))
            for p in ok[:self.per_move]:
                pick[p["b"]] = p
        rows = []
        for p in pick.values():
            s = summary(self._light, p)
            s["plan"] = plan_of(p)
            rows.append(s)
            self.pairs[f"{a}>{p['b']}"] = s
        rows.sort(key=lambda s: (not s.get("studied"), -s["works"], s["b"]))   # studied combos first
        self.by_a[a] = rows
        return rows

    def rows(self, a: str) -> List[dict]:
        """A's served partner summaries (lazy mode: reads A's rows the first time)."""
        if a in self.by_a or self.root is None:
            return self.by_a.get(a, [])
        if a not in self.names:
            return []
        return self._fill(a, list(pairs_for(a, path=self.root).values()))

    def partners(self, a: str, move: Optional[str] = None, n: int = 10) -> List[dict]:
        move = normalize_move(move)
        rows = self.rows(a)
        if move:
            rows = [r for r in rows if r["moves"].get(move, {}).get("ok")]
            rows = sorted(rows, key=lambda r: (-((r["moves"][move].get("score") or 0) + r["works"] / 100), r["b"]))
        return rows[:max(1, min(100, n))]

    def pair(self, a: str, b: str) -> Optional[dict]:
        self.rows(a)
        return self.pairs.get(f"{a}>{b}")


def plan_of(p: dict) -> dict:
    """The transition plan the console can use as its default for this pair (re-validated live).
    A studied pair plays the move its studied technique maps to (studied_combos.pick_move)."""
    m = p["merge"]
    recipe = (p.get("studied") or {}).get("recipe") or ("Stem Merge" if m["ok"] else p["recipe"])
    return {"recipe": recipe, "a_time": p["exit"], "b_time": p["entry"],
            "merge": {k: m.get(k) for k in ("hold_bars", "hold_phrases", "M", "aT", "bT", "pick", "phases")}
            if m["ok"] and recipe == "Stem Merge" else None,
            "lock": p["lock"], "combo": p.get("combo")}


_INDEX: Dict[str, tuple] = {}


def cached_index(cache_dir: Optional[Path] = None, path: Optional[Path] = None) -> Optional[Index]:
    """Lazy Index of the atlas, rebuilt when the database's atlas revision changes (every write
    that changes something bumps it). None: no atlas. An old single-file path is still read whole."""
    p = _root(cache_dir, path)
    if p.is_file():
        try:
            st = p.stat()
        except OSError:
            return None
        stamp = (st.st_mtime_ns, st.st_size)
    else:
        migrate(p)
        row = _db(p).execute("SELECT value FROM atlas_meta WHERE key = ?", (_REV,)).fetchone()
        if row is None:
            return None
        stamp = row[0]
    hit = _INDEX.get(str(p))
    if hit and hit[0] == stamp:
        return hit[1]
    if p.is_file():
        atlas = _load_file(p)
        idx = Index(atlas) if atlas is not None else None
    else:
        meta = load_meta(path=p)
        idx = Index.lazy(p, meta) if meta is not None else None
    if idx is None:
        return None
    _INDEX[str(p)] = (stamp, idx)
    return idx


# ---------------------------------------------------------------- CLI

def _fmt(r: dict) -> str:
    m = "merge-hold OK" if r["merge_ok"] else f"merge refused ({r['merge'].get('gate')})"
    combo = f" COMBO {r['combo_label']}" if r.get("combo") else ""
    pl = f" played +{r['played_good']}/-{r['played_bad']}" if r.get("played") else ""
    return (f"{r['works']:3d}  {r['a_name']}  ->  {r['b_name']}  | {r['best']} ({r['recipe']}), key {r['key']}, "
            f"gap {r['gap']:.1%}, {m}{combo}{pl}")


def _studied_cli(cache_dir: Path, missing: bool, as_json: bool) -> int:
    """List the studied combos (resolved / missing / skipped, technique, source set); offline."""
    from app.music_brain.atlas import studied_combos as sc

    rows = sc.load(cache_dir)
    st = sc.status(rows)
    if as_json:
        print(json.dumps(st if missing else dict(st, transitions=rows), indent=1, default=str))
        return 0
    if not missing:
        for r in rows:
            print(sc.row_line(r))
        print()
        for s in st["sets"]:
            print(f"{s['set_id']:<12} {s['dj'][:40]:<40} transitions {s['transitions']:3d}  resolved {s['resolved']:3d}  "
                  f"missing {s['missing']:3d}  skipped {s['skipped']:3d}")
    print(f"\nsongs to download ({len(st['missing_songs'])}):")
    for m in st["missing_songs"]:
        print(f"  {m['blocks']:2d}  {m['name']}  [{', '.join(m['sets'])}]" + ("  (set download was the wrong song)" if m["wrong_download"] else ""))
    return 0


def _import_set_cli(cache_dir: Path, args: Sequence[str], dry_run: bool, as_json: bool) -> int:
    """Register a studied set's downloaded songs as library tracks (set_import.py); offline."""
    from app.music_brain.learning import set_import as si

    if len(args) != 1 or not (cache_dir / "sets" / args[0] / "study.json").is_file():
        print(json.dumps({"error": f"import-set needs one studied set id with CACHE_DIR/sets/<id>/study.json (got {list(args)})"}))
        return 1
    rows = si.plan(cache_dir, args[0])
    if not dry_run:
        rows = si.apply(rows)
    out = dict(si.summary(rows), set_id=args[0], dry_run=dry_run)
    if as_json:
        print(json.dumps(dict(out, rows=rows), indent=1, default=str))
        return 0 if not out["errors"] else 1
    for r in rows:
        err = f"  ERROR {r['error']}" if r.get("error") else ""
        inf = r.get("info") or {}
        inf = f"  [{inf.get('bpm') or 0:.1f} BPM, key {inf.get('key')}, {inf.get('duration') or 0:.0f} s]" if inf else ""
        print(f"#{r['position']:<3} {r['action']:<6} {r['title'][:52]:<52} {r['id'] or '':<16}  {r['why'] or ''}{inf}{err}")
    print(f"\n{args[0]}: {out['entries']} entries, {out['playable']} playable ({out['imported']} "
          f"{'to import' if dry_run else 'imported'}, {out['cut']} ID(s) {'to cut' if dry_run else 'cut'} from the set, "
          f"{out['reused']} already in the library), {len(out['skipped'])} skipped"
          + (f", {len(out['errors'])} errors" if out["errors"] else ""))
    if not dry_run and (out["imported"] or out["cut"]):
        print("next: python3 -m app.music_brain.atlas.pair_atlas build   (atlas + studied macros with the new songs)")
    return 0 if not out["errors"] else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    from app.music_brain.config import CACHE_DIR

    ap = argparse.ArgumentParser(prog="pair_atlas")
    ap.add_argument("cmd", choices=("build", "show", "best", "chains", "picks", "studied", "import-set"))
    ap.add_argument("arg", nargs="*")
    ap.add_argument("--cache-dir", default=str(CACHE_DIR))
    ap.add_argument("--out", default=None, help="atlas path; the DB lives in its parent (default CACHE_DIR/pair_atlas)")
    ap.add_argument("--full", action="store_true", help="rescore everything")
    ap.add_argument("--no-macros", action="store_true", help="build: do not write the ready-made macros")
    ap.add_argument("--macros-dir", default=None, help="build: cache dir whose macros/ gets them (default --cache-dir)")
    ap.add_argument("--move", default=None)
    ap.add_argument("-n", type=int, default=15)
    ap.add_argument("--chains", type=int, default=3)
    ap.add_argument("--length", type=int, default=8)
    ap.add_argument("--locked", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--missing", action="store_true", help="studied: only the songs to download to complete the chains")
    ap.add_argument("--dry-run", action="store_true", help="import-set: show the plan, register nothing")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        build(Path(a.cache_dir), Path(a.out) if a.out else None, full=a.full, only=a.arg or None,
              seed_macros_to=None if a.no_macros else Path(a.macros_dir or a.cache_dir))
        return 0
    if a.cmd == "studied":
        return _studied_cli(Path(a.cache_dir), a.missing, a.json)
    if a.cmd == "import-set":
        return _import_set_cli(Path(a.cache_dir), a.arg, a.dry_run, a.json)
    atlas = load(Path(a.cache_dir), Path(a.out) if a.out else None)
    if atlas is None:
        print(json.dumps({"error": "no atlas: run `python3 -m app.music_brain.atlas.pair_atlas build`"}))
        return 1
    try:
        if a.cmd == "show":
            if not a.arg:
                raise ValueError("show needs a track id or name")
            tid = resolve(atlas, " ".join(a.arg))
            if tid is None:
                raise ValueError(f"unknown track {' '.join(a.arg)!r}")
            rows = partners(atlas, tid, a.move, a.n)
            out = {"track": tid, "name": atlas["tracks"][tid]["name"], "move": a.move, "partners": rows}
        elif a.cmd == "best":
            out = {"move": a.move, "pairs": best_pairs(atlas, a.move, a.n),
                   "chains": chains(atlas, length=a.length, k=a.chains) if a.move is None else []}
        elif a.cmd == "chains":
            out = {"chains": chains(atlas, length=a.length, k=a.chains)}
        else:
            ids = [resolve(atlas, x) for x in a.arg]
            if None in ids:
                raise ValueError("unknown track in picks")
            out = order_picks(atlas, ids, locked=a.locked)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    if a.json:
        print(json.dumps(out, indent=1))
        return 0
    rows = out.get("partners") or out.get("pairs") or out.get("steps") or []
    if "name" in out:
        print(f"partners for {out['name']}" + (f" (move {a.move})" if a.move else ""))
    for r in rows:
        print(_fmt(r))
    for c in out.get("chains", []):
        print(f"\nchain (mean works {c['mean_works']}):")
        for nm in c["names"]:
            print(f"  {nm}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
