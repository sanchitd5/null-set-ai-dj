"""Per-track sung language, from Whisper LANGUAGE DETECTION on a few short clips of the vocal stem.

Why: the autopilot's script gate (pick_identity.scripts_of) cannot see a Latin-script song in another
language jumping into the set. The vocal stem is already there for every library song, so we ask
Whisper "what language is this?" (encoder + one decoder step; no transcription) on 2-3 ~20 s clips
cut from the vocal-active regions, and average the probabilities.

  identify(vocal_path)  -> {"v", "lang", "conf", "instr", "alt", "clips", "voiced_s", ...}
  verdict(rec, profile) -> ("ok" | "unknown" | "penalty" | "block", reason)

lang is None for an instrumental ("instr": too little singing) or when no language reaches MIN_CONF;
both are never blocked. Results are stored per track id (lang_id.json in the cache dir).
The model runs on CPU int8 by default (LANGID_DEVICE=cuda to use the shared card); it is not the
lyrics model, so a backfill never evicts it.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

LANGID_V = 1
SR = 16000
CLIPS = 3                      # clips per song
CLIP_S = 20.0                  # seconds per clip
MIN_CLIP_S = 6.0               # a shorter sung span is not a clip
MIN_VOICED_S = 15.0            # less singing than this = instrumental
MIN_CONF = float(os.environ.get("LANGID_MIN_CONF") or 0.55)   # averaged top probability needed to name a language
MARGIN_MIN_CONF = 0.35         # ... or at least this with a MARGIN lead over the runner-up
MARGIN = 0.2
MODEL = os.environ.get("LANGID_MODEL") or "base"

# profile rules
RECENT_SONGS = 12              # songs looked at
DECAY = 0.8                    # weight of one song older
PURE_SHARE = 0.8               # top group at least this = a one-language set
IN_SET_SHARE = 0.1             # a language needs at least this share to count as "in the set"

# languages Whisper confuses or that share one spoken language for a set: one group each
_GROUPS = (
    ("hi", "ur", "pa", "sd"),          # Hindustani / Punjabi
    ("no", "nn", "da", "sv"),          # Scandinavian
    ("id", "ms"),
    ("sr", "hr", "bs"),
    ("zh", "yue"),
    ("nl", "af"),
    ("es", "gl"),
)
_GROUP_OF = {c: g[0] for g in _GROUPS for c in g}


def group(code: Optional[str]) -> Optional[str]:
    return _GROUP_OF.get(code, code) if code else None


# ------------------------------------------------------------------ clips + aggregation (pure)

def pick_clips(regs: Sequence[Sequence[float]], n: int = CLIPS, clip_s: float = CLIP_S) -> List[Tuple[float, float]]:
    """Up to n windows of at most clip_s, one from the longest sung region of each third of the song
    (so a rap verse and the chorus both get heard). Spans under MIN_CLIP_S are skipped."""
    regs = [(float(a), float(b)) for a, b in regs if float(b) - float(a) >= MIN_CLIP_S]
    if not regs:
        return []
    end = max(b for _, b in regs)
    out: List[Tuple[float, float]] = []
    for k in range(n):
        lo, hi = end * k / n, end * (k + 1) / n
        mine = [r for r in regs if lo <= (r[0] + r[1]) / 2 < hi + (1e-6 if k == n - 1 else 0)]
        if not mine:
            continue
        a, b = max(mine, key=lambda r: r[1] - r[0])
        if b - a > clip_s:
            mid = (a + b) / 2
            a, b = mid - clip_s / 2, mid + clip_s / 2
        out.append((round(a, 2), round(b, 2)))
    # a third without singing leaves a gap: fill from the other sung spans (longest first), then
    # a second window of an already used long span, so one odd clip cannot decide alone
    if len(out) < n:
        used = {(round(a, 2), round(b, 2)) for a, b in out}
        for a, b in sorted(regs, key=lambda r: r[0] - r[1]):
            if len(out) >= n:
                break
            if any(a < ub and b > ua for ua, ub in used):      # overlaps a window already taken
                continue
            w = (round(a, 2), round(a + min(b - a, clip_s), 2))
            out.append(w)
            used.add(w)
        grew = True
        while len(out) < n and grew:                      # unused tails of spans a window already sits in
            grew = False
            for a, b in sorted(regs, key=lambda r: r[0] - r[1]):
                for ua, ub in sorted(used):
                    if not (ua >= a and ub <= b and b - ub >= MIN_CLIP_S and len(out) < n):
                        continue
                    w = (round(ub, 2), round(ub + min(b - ub, clip_s), 2))
                    if any(w[0] < vb and w[1] > va for va, vb in used):        # that stretch is taken
                        continue
                    out.append(w)
                    used.add(w)
                    grew = True
                    break
    return sorted(out)


def aggregate(prob_sets: Iterable[Dict[str, float]]) -> Tuple[Optional[str], float, List[List]]:
    """(top language, its mean probability, next best [[lang, p]...]) over the clips."""
    sets = [p for p in prob_sets if p]
    if not sets:
        return None, 0.0, []
    tot: Dict[str, float] = {}
    for p in sets:
        for k, v in p.items():
            tot[k] = tot.get(k, 0.0) + float(v)
    mean = sorted(((k, v / len(sets)) for k, v in tot.items()), key=lambda kv: -kv[1])
    return mean[0][0], mean[0][1], [[k, round(v, 3)] for k, v in mean[1:3] if v >= 0.02]


def decide(prob_sets: Sequence[Dict[str, float]], voiced_s: float) -> dict:
    """The record for one song from its clips' probabilities and the seconds of singing."""
    rec = {"v": LANGID_V, "lang": None, "conf": 0.0, "instr": False, "alt": [], "clips": len(prob_sets),
           "voiced_s": round(voiced_s, 1), "model": MODEL}
    if voiced_s < MIN_VOICED_S or not prob_sets:
        rec["instr"] = True
        return rec
    lang, conf, alt = aggregate(prob_sets)
    rec["conf"], rec["alt"], rec["guess"] = round(conf, 3), alt, lang
    second = alt[0][1] if alt else 0.0
    # sung vocals rarely give a 0.9: take a clear winner too (a third of the mass and 0.2 ahead of the runner-up)
    if conf >= MIN_CONF or (conf >= MARGIN_MIN_CONF and conf - second >= MARGIN):
        rec["lang"] = lang
    return rec


# ------------------------------------------------------------------ model (CPU int8 by default)

_LOCK = threading.Lock()
_ENGINE: Dict[str, object] = {}


class _Detector:
    def __init__(self, model: str, device: str):
        from faster_whisper import WhisperModel

        self.m = WhisperModel(model, device=device, compute_type="float16" if device == "cuda" else "int8",
                              cpu_threads=int(os.environ.get("LANGID_THREADS") or 2))

    def probs(self, audio: np.ndarray) -> Dict[str, float]:
        _, _, allp = self.m.detect_language(audio=audio)
        return {k: float(v) for k, v in allp}


def available() -> bool:
    if (os.environ.get("LANGID") or "").strip().lower() in ("off", "0", "false", "no"):
        return False
    try:
        import importlib.util

        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


def _detector(model: str = MODEL):
    device = (os.environ.get("LANGID_DEVICE") or "cpu").strip().lower()
    key = (model, device)
    if _ENGINE.get("key") != key:
        release()
        _ENGINE["engine"] = _Detector(model, device)
        _ENGINE["key"] = key
    return _ENGINE["engine"]


def release() -> None:
    _ENGINE.pop("engine", None)
    _ENGINE.pop("key", None)


def identify(vocal, engine=None, regions: Optional[Sequence[Sequence[float]]] = None) -> dict:
    """Record for one vocal stem (path or 16 kHz array). engine: object with probs(audio) (tests mock it).
    Raises when there is no engine at all (the caller keeps the song unanalysed)."""
    from app.music_brain.analysis import lyrics_asr as la

    y = la._load16k(vocal, SR)
    dur = len(y) / SR
    if regions is None:
        _, sung = la.vocal_gate(y)
        regions = la.regions(sung, duration=dur)
    voiced = float(sum(b - a for a, b in regions))
    if voiced < MIN_VOICED_S:
        return decide([], voiced)
    clips = pick_clips(regions)
    if not clips:
        return decide([], voiced)
    t0 = time.time()
    with _LOCK:
        eng = engine if engine is not None else _detector()
        sets = [eng.probs(y[int(a * SR): int(b * SR)]) for a, b in clips]
    rec = decide(sets, voiced)
    rec["secs"] = round(time.time() - t0, 2)
    return rec


# ------------------------------------------------------------------ store (one JSON file, by track id)

_STORE_LOCK = threading.Lock()
_STORE: Dict[str, dict] = {}
_STORE_PATH: Optional[Path] = None


def _path() -> Path:
    from app.music_brain.config import CACHE_DIR

    return _STORE_PATH or (CACHE_DIR / "lang_id.json")


def _load() -> None:
    global _STORE_PATH
    p = _path()
    if _STORE_PATH == p and _STORE:
        return
    _STORE_PATH = p
    _STORE.clear()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        if doc.get("v") == LANGID_V:
            _STORE.update(doc.get("tracks") or {})
    except (OSError, ValueError):
        pass


def get(track_id: str) -> Optional[dict]:
    with _STORE_LOCK:
        _load()
        return _STORE.get(track_id)


def put(track_id: str, rec: dict) -> None:
    with _STORE_LOCK:
        _load()
        _STORE[track_id] = rec
        p = _path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({"v": LANGID_V, "tracks": _STORE}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            pass


def public(rec: Optional[dict]) -> Optional[dict]:
    """The part the clients see: {lang, conf, instr}; None = not analysed yet."""
    if not rec:
        return None
    return {"lang": rec.get("lang"), "conf": rec.get("conf", 0.0), "instr": bool(rec.get("instr"))}


# ------------------------------------------------------------------ set profile + verdict (pure)

def usable(rec: Optional[dict]) -> Optional[str]:
    """The language group a record names, or None (unknown / instrumental / unsure)."""
    if not rec or rec.get("instr") or not rec.get("lang"):
        return None
    return group(rec["lang"])


def profile_of(recs_oldest_first: Sequence[Optional[dict]]) -> dict:
    """{"n", "share": {group: share}, "top", "pure"} from the languages of the songs played in this
    set, newest weighted most. Songs without a usable language are skipped. n == 0: no profile."""
    w: Dict[str, float] = {}
    n = 0
    for age, rec in enumerate(reversed(list(recs_oldest_first)[-RECENT_SONGS:])):
        g = usable(rec)
        if g is None:
            # keep the age counting only for real songs: skipping must not make an old song count as new
            continue
        w[g] = w.get(g, 0.0) + DECAY ** n
        n += 1
    tot = sum(w.values())
    share = {k: v / tot for k, v in w.items()} if tot else {}
    top = max(share, key=share.get) if share else None
    return {"n": n, "share": share, "top": top, "pure": bool(top) and share[top] >= PURE_SHARE}


def profile_text(profile: dict) -> str:
    return ", ".join(f"{k} {v:.0%}" for k, v in sorted((profile.get("share") or {}).items(), key=lambda kv: -kv[1]))


def verdict(rec: Optional[dict], profile: Optional[dict]) -> Tuple[str, str]:
    """("ok" | "unknown" | "penalty" | "block", reason). unknown = not analysed yet (the caller lets the
    script gate / catalogue metadata decide); a song with no usable language (instrumental, unsure) is ok."""
    if not profile or not profile.get("n"):
        return "ok", ""
    if rec is None:
        return "unknown", ""
    g = usable(rec)
    if g is None or profile["share"].get(g, 0.0) >= IN_SET_SHARE:
        return "ok", ""
    why = f"language jump: {rec['lang']} not in set ({profile_text(profile)})"
    return ("block" if profile["pure"] else "penalty"), why
