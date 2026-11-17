"""CLEANLIB: spoken / non-music files (interviews, commentary, podcasts, reactions, news clips) are cleaned out of
the library automatically.

One rule, here: score a song 0..1 for "this is talk, not music" from
  * the title (speech words in English / Hindi, a talk-shaped Devanagari sentence, a very long file), and
  * the audio: the separated stems when they exist (instrumental energy against vocal energy: speech has none),
    else three short windows (pauses, beat periodicity). Windows are decoded with ffmpeg at nice 19, 20 s each.
score >= REMOVE_AT  -> status "removed": the song leaves the registry (library list, picks, lyric remix plans) and
                       the phone drops its copy through GET /api/library/bad. The audio file is never touched, so a
                       restore is instant.
score >= REVIEW_AT  -> status "review": listed for the owner, still in the library.
Never removed (listed for review at most): a song the owner liked, or one remembered as played in a set, and a song
whose stems carry a real instrumental. Restore whitelists a song for good (status "restored").

State: <cache>/not_music.json {"items": {id: {...}}, "checked": {id: score}}. The daily `not_music` job scans only
songs it has not judged yet; downloads are judged before they enter the library (check_download).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

FILE = "not_music.json"
REMOVE_AT = 0.7
REVIEW_AT = 0.4
SR = 11025
WINDOW_S = 20
STEM_REAL = 0.25                  # instrumental share of the stems at/above which a song has real music in it
LONG_S = 12 * 60
_lock = threading.Lock()
router = APIRouter()

# ------------------------------------------------------------------------------------------------ title cues

_STRONG = re.compile(
    r"\b(interviews?|podcasts?|react(?:s|ion|ions)?|commentary|news|explained|explains|talks?\s+about|conversation|"
    r"speech|lecture|vlog|debate|press\s+conference|live\s?stream|episode|breaks?\s+silence|opens?\s+up|claims?|"
    r"reveals?|exclusive|q\s*&\s*a)\b"
    r"|इंटरव्यू|साक्षात्कार|पॉडकास्ट|समाचार|न्यूज़?|खबर|रिएक्शन|बयान|दावा|खुलासा|बताई|बताया|ने\s+कहा",
    re.IGNORECASE)
_DEVA_WORD = re.compile(r"(?:^|\s)(?:ने|के|को|का|की|से|में|पर|कर|क्या|और|भी|है|था|थी)(?=\s|$)")
_DEVA = re.compile(r"[ऀ-ॿ]")


def title_cues(name: str, dur: Optional[float] = None) -> Tuple[float, List[str]]:
    """(0..1, reasons): how talk-like the title (and length) is."""
    name = str(name or "")
    hits = sorted({m.group(0).lower() for m in _STRONG.finditer(name)})
    score, why = 0.0, []
    if hits:
        score += min(0.9, 0.45 + 0.2 * (len(hits) - 1))
        why.append("title words: " + ", ".join(hits[:4]))
    if len(name) > 35 and len(_DEVA_WORD.findall(name)) >= 2 and len(_DEVA.findall(name)) > 10:
        score += 0.3
        why.append("spoken-sentence title")
    if dur is not None and dur > LONG_S:
        score += 0.25
        why.append(f"very long ({dur / 60:.0f} min)")
    return min(score, 1.0), why


# ------------------------------------------------------------------------------------------------ audio cues

def feats(x) -> Optional[dict]:
    """Pause share, beat periodicity and bass share of one mono window (numpy float32, rate SR); None if too short."""
    import numpy as np

    if x is None or len(x) < SR * 5:
        return None
    n, h = 1024, 256
    fr = np.lib.stride_tricks.sliding_window_view(x, n)[::h]
    s = np.abs(np.fft.rfft(fr * np.hanning(n), axis=1)) ** 2
    fq = np.fft.rfftfreq(n, 1 / SR)
    bass = float(s[:, (fq >= 30) & (fq < 200)].sum() / (s.sum() + 1e-12))
    rms = np.sqrt((fr ** 2).mean(1))
    pause = float((rms < 0.1 * np.median(rms) + 1e-5).mean())
    flux = np.maximum(0, np.diff(np.log1p(np.sqrt(s) * 10), axis=0)).sum(1)
    flux = flux - flux.mean()
    ac = np.correlate(flux, flux, "full")[len(flux) - 1:]
    ac = ac / (ac[0] + 1e-12)
    fps = SR / h
    beat = float(ac[int(0.28 * fps):int(1.0 * fps)].max())
    return {"bass": bass, "pause": pause, "beat": beat}


def _pcm(path: Path, start: float, secs: float):
    import numpy as np

    r = subprocess.run(["nice", "-n", "19", "ffmpeg", "-v", "error", "-ss", f"{start:.1f}", "-t", str(secs), "-i", str(path),
                        "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"], capture_output=True, timeout=90)
    return np.frombuffer(r.stdout, dtype=np.float32)


def window_feats(path: Path, dur: float, pcm: Callable = _pcm) -> Optional[dict]:
    """Mean of feats() over three windows (25 %, 50 %, 75 % of the file)."""
    out = [f for f in (feats(pcm(path, max(0.0, dur * p - WINDOW_S / 2), WINDOW_S)) for p in (0.25, 0.5, 0.75)) if f]
    return {k: sum(o[k] for o in out) / len(out) for k in out[0]} if out else None


def stem_instrumental(stems: Dict[str, Path], dur: float, pcm: Callable = _pcm) -> Optional[float]:
    """Instrumental share (bass + drums + other) of the stems' energy in the middle 20 s; None without all four stems."""
    if not all(k in stems for k in ("vocals", "bass", "drums", "other")):
        return None
    e = {}
    for k in ("vocals", "bass", "drums", "other"):
        x = pcm(stems[k], max(0.0, dur * 0.5 - WINDOW_S / 2), WINDOW_S)
        if len(x) < SR:
            return None
        e[k] = float((x ** 2).mean())
    ins = e["bass"] + e["drums"] + e["other"]
    return ins / (ins + e["vocals"] + 1e-12)


def _clip(v: float) -> float:
    return max(0.0, min(1.0, v))


def speech_from_feats(f: Optional[dict]) -> float:
    """0..1 from the window features alone: long pauses and no steady beat read as speech."""
    if not f:
        return 0.0
    return _clip(f["pause"] / 0.1) * 0.6 + _clip((0.35 - f["beat"]) / 0.2) * 0.4


def speech_from_stems(ratio: Optional[float]) -> float:
    return 0.0 if ratio is None else _clip((0.15 - ratio) / 0.15)


def audio_cues(path: Path, stems: Optional[Dict[str, Path]] = None, dur: Optional[float] = None,
               pcm: Callable = _pcm) -> dict:
    """{"score", "why", "instr", "feats"}: how speech-like the audio is, 0 when nothing could be measured."""
    dur = dur or _duration(path)
    if not dur:
        return {"score": 0.0, "why": [], "instr": None, "feats": None}
    instr = stem_instrumental(stems or {}, dur, pcm) if stems else None
    if instr is not None and instr >= STEM_REAL:            # real instrumental under the voice: music, no window decode needed
        return {"score": 0.0, "why": [], "instr": instr, "feats": None}
    f = window_feats(path, dur, pcm)
    a = speech_from_feats(f)
    score = _clip(0.7 * speech_from_stems(instr) + 0.3 * a) if instr is not None else a
    why = []
    if instr is not None and instr < 0.15:
        why.append(f"stems: instrumental {instr:.0%} of the energy")
    if f and f["pause"] > 0.05:
        why.append(f"{f['pause']:.0%} pauses")
    if f and f["beat"] < 0.25:
        why.append("no steady beat")
    return {"score": score, "why": why, "instr": instr, "feats": f}


def _duration(path: Path) -> Optional[float]:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return None


# ------------------------------------------------------------------------------------------------ the verdict

def judge(name: str, dur: Optional[float], audio: Optional[dict], protected: bool = False) -> dict:
    """{"score", "reasons", "action"}: action is "remove", "review" or "keep". Audio None = title only (a title
    alone never removes: it is capped under REMOVE_AT)."""
    m, mwhy = title_cues(name, dur)
    if audio is None:
        score = min(m * 0.8, REMOVE_AT - 0.01)
        why = mwhy
    else:
        score = 0.55 * audio["score"] + 0.45 * m
        why = mwhy + audio["why"]
        if audio.get("instr") is not None and audio["instr"] >= STEM_REAL:
            score, why = min(score, REVIEW_AT - 0.01), why + ["stems carry a real instrumental"]
    score = round(score, 2)
    action = "remove" if score >= REMOVE_AT else "review" if score >= REVIEW_AT else "keep"
    if action == "remove" and protected:
        action = "review"
        why = why + ["liked or played in a set: kept for review"]
    return {"score": score, "reasons": why, "action": action}


# ------------------------------------------------------------------------------------------------ state

def _path(cache: Path) -> Path:
    return Path(cache) / FILE


def load(cache: Path) -> dict:
    try:
        d = json.loads(_path(cache).read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return {"items": d.get("items") or {}, "checked": d.get("checked") or {}}
    except (OSError, ValueError):
        pass
    return {"items": {}, "checked": {}}


def _save(cache: Path, data: dict) -> None:
    p = _path(cache)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def removed_ids(cache: Path) -> List[str]:
    return sorted(t for t, i in load(cache)["items"].items() if i.get("status") == "removed")


def items(cache: Path) -> List[dict]:
    """The owner's list: removed + review + restored, newest first."""
    rows = [{"id": t, **i} for t, i in load(cache)["items"].items()]
    return sorted(rows, key=lambda r: -float(r.get("at") or 0))


def set_status(cache: Path, track_id: str, status: str, now: Optional[float] = None) -> Optional[dict]:
    """Owner action: "restored" whitelists the song for good, "removed" removes a review item. None if unknown."""
    with _lock:
        d = load(cache)
        it = d["items"].get(track_id)
        if it is None:
            return None
        it["status"] = status
        it["at"] = now or time.time()
        _save(cache, d)
        return {"id": track_id, **it}


def _liked_ids(cache: Path) -> set:
    try:
        from app.music_brain.learning import liked as lk

        return {x for v in lk.load(cache).values() for x in (v.get("a"), v.get("b")) if x}
    except Exception:
        return set()


def _played_keys(cache: Path) -> set:
    try:
        from app.ui.services import set_memory as sm

        return {k for k in (sm._key(v.get("name", "")) for v in sm.load(Path(cache) / "set_memory.json").values()) if k}
    except Exception:
        return set()


def protected_ids(cache: Path, names: Dict[str, str]) -> set:
    """Songs the owner liked or played in a set."""
    from app.ui.services import set_memory as sm

    liked, played = _liked_ids(cache), _played_keys(cache)
    return {t for t, n in names.items() if t in liked or (sm._key(n) and sm._key(n) in played)}


def _stems_of(track_id: str) -> Dict[str, Path]:
    from app.music_brain import song_store

    for model in ("htdemucs_ft", "htdemucs"):
        try:
            s = song_store.stem_files(track_id, model)
        except Exception:
            s = {}
        if s:
            return s
    return {}


def scan(tracks: Dict[str, Path], names: Dict[str, str], cache: Path, now: float,
         log: Callable[[str], None] = lambda s: None, should_stop: Callable[[], bool] = lambda: False,
         audio_fn: Optional[Callable[[str, Path], dict]] = None, limit: int = 0, redo: bool = False) -> dict:
    """Judge every library song not judged yet (redo: all). Writes the state; returns counts and the new items."""
    audio_fn = audio_fn or (lambda tid, p: audio_cues(p, _stems_of(tid)))
    prot = protected_ids(cache, names)
    d = load(cache)
    new: List[dict] = []
    done = 0
    for tid, path in list(tracks.items()):
        if should_stop() or (limit and done >= limit):
            break
        it = d["items"].get(tid)
        if (tid in d["checked"] and not redo) or (it and it.get("status") == "restored"):
            continue
        if not Path(path).exists():
            continue
        done += 1
        name = names.get(tid) or tid
        dur = _duration(path)
        # audio is measured for every song (stems make it cheap); the verdict needs both to remove
        try:
            audio = audio_fn(tid, path)
        except Exception as exc:                          # noqa: BLE001 - one bad file never stops the scan
            log(f"{tid}: audio check failed: {exc!r}")
            audio = None
        v = judge(name, dur, audio, protected=tid in prot)
        d["checked"][tid] = v["score"]
        if v["action"] != "keep":
            row = {"title": name, "score": v["score"], "reasons": v["reasons"], "at": now,
                   "status": "removed" if v["action"] == "remove" else "review"}
            if not (it and it.get("status") == "removed" and row["status"] == "review"):
                d["items"][tid] = row
                new.append({"id": tid, **row})
        if done % 25 == 0:
            with _lock:
                _save(cache, d)
    with _lock:
        _save(cache, d)
    n_rm = sum(1 for r in new if r["status"] == "removed")
    log(f"judged {done}: {n_rm} removed, {len(new) - n_rm} for review")
    return {"judged": done, "removed": n_rm, "review": len(new) - n_rm, "new": new}


def check_download(path: Path, title: str, dur: Optional[float] = None, cache: Optional[Path] = None,
                   audio_fn: Optional[Callable[[Path], dict]] = None) -> Optional[str]:
    """A reason to reject a fresh download before it enters the library, else None. Only a confident verdict
    rejects (title cues and the window features, no stems yet); a doubtful file is left to the scan's review."""
    dur = dur or _duration(path)
    try:
        audio = (audio_fn or (lambda p: audio_cues(p, None, dur)))(path)
    except Exception:                                   # noqa: BLE001
        audio = None
    v = judge(title, dur, audio)
    return "not music: " + "; ".join(v["reasons"]) if v["action"] == "remove" else None


# ------------------------------------------------------------------------------------------------ registry

def apply_removed(tracks: dict, cache: Optional[Path] = None) -> int:
    """Take every removed song out of the server's track registry (library list, picks, lyric remix plans read it)."""
    from app.music_brain.config import CACHE_DIR

    n = 0
    for tid in removed_ids(cache or CACHE_DIR):
        if tracks.pop(tid, None) is not None:
            n += 1
    return n


def is_removed(track_id: str, cache: Optional[Path] = None) -> bool:
    from app.music_brain.config import CACHE_DIR

    it = load(cache or CACHE_DIR)["items"].get(track_id)
    return bool(it and it.get("status") == "removed")


def restore(tracks: dict, track_id: str, cache: Optional[Path] = None) -> Optional[dict]:
    """Whitelist a song and put it back into the registry."""
    from app.music_brain import song_store
    from app.music_brain.config import CACHE_DIR

    row = set_status(cache or CACHE_DIR, track_id, "restored")
    if row is not None and track_id not in tracks:
        p = song_store.audio_path(track_id)
        if p is not None:
            tracks[track_id] = Path(p)
    return row


def remove(tracks: dict, track_id: str, cache: Optional[Path] = None) -> Optional[dict]:
    from app.music_brain.config import CACHE_DIR

    row = set_status(cache or CACHE_DIR, track_id, "removed")
    apply_removed(tracks, cache)
    return row


# ------------------------------------------------------------------------------------------------ routes

def _view(r: dict) -> dict:
    return {"id": r["id"], "title": r.get("title") or r["id"], "reason": "; ".join(r.get("reasons") or []),
            "score": r.get("score"), "status": r.get("status"), "at": r.get("at")}


@router.get("/api/library/not-music")
def get_not_music():
    """The owner's review list ("Removed as not music"): status removed (out of the library), review (still in,
    flagged) or restored (whitelisted). The phone shows it with a restore button."""
    from app.music_brain.config import CACHE_DIR

    rows = items(CACHE_DIR)
    return {"items": [_view(r) for r in rows],
            "removed": sum(1 for r in rows if r.get("status") == "removed")}


@router.post("/api/library/not-music/{track_id}/restore")
def post_not_music_restore(track_id: str, request: Request):
    from app.ui import server

    if not re.fullmatch(r"[\w-]{1,64}", track_id):
        raise HTTPException(status_code=400, detail="bad id")
    row = restore(server._tracks, track_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not in the list")
    if "text/html" in request.headers.get("accept", ""):          # the admin page's form: back to the list
        return RedirectResponse("/admin/not-music", status_code=303)
    return {"item": _view(row)}


@router.post("/api/library/not-music/{track_id}/remove")
def post_not_music_remove(track_id: str, request: Request):
    from app.ui import server

    if not re.fullmatch(r"[\w-]{1,64}", track_id):
        raise HTTPException(status_code=400, detail="bad id")
    row = remove(server._tracks, track_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not in the list")
    if "text/html" in request.headers.get("accept", ""):          # the admin page's form: back to the list
        return RedirectResponse("/admin/not-music", status_code=303)
    return {"item": _view(row)}
