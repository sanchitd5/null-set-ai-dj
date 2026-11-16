"""LYRIC REMIX SONGS: every accepted lyric remix plan as a playable song, rendered once on request.

The plans (lyric_remix.json.gz, built by the private atlas builder, read only here) say which sung lines of a song (or of two songs)
make a new lyric. The DECISION of where each line goes lives in dj-core (`lyric_remix_plan`, the same call the live move makes, in
its on-demand form); this module only turns that plan into audio:

    intro (instrumental of A) | remixed section: A's instrumental + the plan's vocal pieces | outro (instrumental of A)

- Section: starts on the bar line at the first line of A the plan uses, so the instrumental under it is the part of the song the
  lines came from. Pieces come from the vocal stem of A ("self") or of B ("guest": played at A's tempo by rate = bpmA / bpmB, the move's
  own varispeed, high-passed at 120 Hz like vocal_swap). Every piece sits on a beat, with 12 ms edge ramps.
- Intro / outro: whole bars of A's instrumental on both sides so the result is a full track, not a 15 s clip. Each side gets just
  enough bars to bring the total to >= 60 s, at most 16 bars (so 60-110 s at normal tempos); the source song's start / end limit it
  and the other side takes the slack. The intro fades in over 1 s, the outro out over 2 s. Instrumental only: no original vocal
  competes with the new lyric.
- Loudness: ffmpeg loudnorm to -14 LUFS, true peak -1 dB, while encoding to the phone AAC format (same codec settings as ?q=phone).
- Cache: <cache>/songs/_remix/<plan_id>/{audio.m4a, meta.json}. `_`-folders are never library songs, so a remix is not picked or
  analysed as one. meta.json carries the identity (render version + plan + the lines it used + both track ids, which are content
  hashes); a changed plan or render version re-renders. LRU by last use, capped (LYRIC_REMIX_CACHE_MAX, default 60 files).
- The beat grid of the render comes from the analyser (analyze_track on the render), with the exact synthetic grid as fallback.

REST (included by server.py): see wt/REMIXSONGS_CONTRACT.txt. One render at a time (heavy slot), RAM bounded: stems are decoded in
segments by ffmpeg, never whole.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.music_brain import djcore_bridge, song_store
from app.ui.services import lyric_remix_api as lra
from app.ui.services import phone_audio
from app.ui.services.ranged_file import RangedFileResponse

log = logging.getLogger(__name__)
router = APIRouter()

RENDER_VERSION = 1
SR = 44100
MIN_TOTAL_S = 60.0
MAX_SIDE_BARS = 16
RAMP_S = 0.012
FADE_IN_S = 1.0
FADE_OUT_S = 2.0
TARGET_LUFS = -14.0
STEM_WAIT_S = 20 * 60
_PLAN_RE = re.compile(r"[0-9a-f]{6,40}")
_STEMS = ("drums", "bass", "other")


def cache_max() -> int:
    try:
        return max(2, int(os.environ.get("LYRIC_REMIX_CACHE_MAX", "60")))
    except ValueError:
        return 60


# ------------------------------------------------------------------------------------------------ host hooks


class Host:
    """What the server provides (set by server.py): library lookups and stem separation. Tests replace it."""
    track_path: Callable[[str], Optional[Path]] = staticmethod(lambda tid: None)
    stems4: Callable[[str], Optional[Dict[str, str]]] = staticmethod(lambda tid: None)
    queue_stems: Callable[[str], Any] = staticmethod(lambda tid: None)
    name_of: Callable[[str], str] = staticmethod(lambda tid: tid)
    analyze: Optional[Callable[[Path], Any]] = None


def configure(**kw) -> None:
    for k, v in kw.items():
        setattr(Host, k, staticmethod(v) if callable(v) else v)


# ------------------------------------------------------------------------------------------------ paths, state


def remix_dir(plan_id: str) -> Path:
    if not _PLAN_RE.fullmatch(plan_id or ""):
        raise ValueError("bad plan id")
    return song_store.songs_dir() / "_remix" / plan_id


def settings_path() -> Path:
    return song_store.songs_dir() / "_remix" / "settings.json"


def get_settings() -> dict:
    try:
        d = json.loads(settings_path().read_text(encoding="utf-8"))
        return {"ai_picks": bool(d.get("ai_picks"))}
    except (OSError, ValueError, AttributeError):
        return {"ai_picks": False}


def set_settings(ai_picks: bool) -> dict:
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps({"ai_picks": bool(ai_picks)}), encoding="utf-8")
    os.replace(tmp, p)
    return get_settings()


_jobs: Dict[str, dict] = {}            # plan id -> {state, progress, stage, error}; in memory, a restart forgets errors only
_jobs_lock = threading.Lock()
_render_slot = threading.Semaphore(1)


def _set_job(pid: str, **kw) -> None:
    with _jobs_lock:
        _jobs.setdefault(pid, {}).update(kw)


def identity(doc: dict, plan: dict) -> str:
    """Hash of everything the audio depends on: render version, plan, the lines it uses and both tracks (ids are content hashes)."""
    parts: List[Any] = [RENDER_VERSION, plan.get("id"), plan.get("a"), plan.get("b"), plan.get("pieces")]
    for pc in plan.get("pieces") or []:
        src = plan.get("a") if pc.get("s") == "a" else plan.get("b")
        song = doc["songs"].get(src) or {}
        lines = song.get("lines") or []
        i = pc.get("i")
        parts.append([song.get("bpm"), lines[i] if isinstance(i, int) and 0 <= i < len(lines) else None])
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _read_meta(pid: str) -> Optional[dict]:
    try:
        return json.loads((remix_dir(pid) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ready_meta(doc: dict, plan: dict) -> Optional[dict]:
    """meta of a valid cached render of this plan, else None."""
    pid = plan["id"]
    m = _read_meta(pid)
    audio = remix_dir(pid) / "audio.m4a"
    if m and m.get("identity") == identity(doc, plan) and audio.is_file() and audio.stat().st_size > 0:
        return m
    return None


# ------------------------------------------------------------------------------------------------ list / status


def _title(plan: dict) -> str:
    a = Host.name_of(plan["a"])
    return f"{a} - lyric remix" if not plan.get("b") else f"{a} x {Host.name_of(plan['b'])} - lyric remix"


def _available(plan: dict) -> bool:
    return all(Host.track_path(t) is not None for t in (plan.get("a"), plan.get("b")) if t)


def _song(doc: dict, plan: dict) -> dict:
    pid = plan["id"]
    job = dict(_jobs.get(pid) or {})
    meta = _ready_meta(doc, plan)
    if job.get("state") == "rendering":
        state, progress, stage, err = "rendering", float(job.get("progress") or 0), job.get("stage") or "", None
    elif meta:
        state, progress, stage, err = "ready", 1.0, "ready", None
    elif job.get("state") == "error":
        state, progress, stage, err = "error", 0.0, "", job.get("error") or "render failed"
    else:
        state, progress, stage, err = "none", 0.0, "", None
    return {"id": f"lr-{pid}", "plan_id": pid, "kind": "pair" if plan.get("b") else "within", "a": plan["a"],
            "b": plan.get("b"), "title": _title(plan), "text": plan.get("text") or "", "score": float(plan.get("score") or 0),
            "state": state, "progress": progress, "stage": stage, "error": err,
            "duration": (meta or {}).get("duration") if state == "ready" else None,
            "bpm": (meta or {}).get("bpm") if state == "ready" else None}


def _find(doc: dict, pid: str) -> dict:
    for p in doc["plans"]:
        if p.get("id") == pid and p.get("a"):
            return p
    raise HTTPException(status_code=404, detail="unknown lyric remix plan")


def list_songs(q: str = "", limit: int = 1000) -> dict:
    doc = lra.load()
    out = []
    needle = q.strip().lower()
    for p in doc["plans"]:
        if not p.get("id") or not p.get("a") or not _available(p):
            continue
        s = _song(doc, p)
        if needle and needle not in (s["title"] + " " + s["text"]).lower():
            continue
        out.append(s)
    out.sort(key=lambda s: (-s["score"], s["plan_id"]))
    return {"ai_picks": get_settings()["ai_picks"], "total": len(out), "songs": out[:max(1, limit)]}


def song_doc(pid: str) -> dict:
    doc = lra.load()
    plan = _find(doc, pid)
    s = _song(doc, plan)
    meta = _ready_meta(doc, plan)
    if meta and s["state"] == "ready":
        s.update(sha256=meta.get("sha256"), bytes=meta.get("bytes"), beat_times=meta.get("beat_times") or [],
                 downbeat_times=meta.get("downbeat_times") or [])
    return s


def start_render(pid: str) -> dict:
    doc = lra.load()
    plan = _find(doc, pid)
    if not _available(plan):
        raise HTTPException(status_code=404, detail="a song of this plan is not in the library")
    with _jobs_lock:
        busy = (_jobs.get(pid) or {}).get("state") == "rendering"
    if not busy and _ready_meta(doc, plan) is None:
        _set_job(pid, state="rendering", progress=0.0, stage="queued", error=None)
        threading.Thread(target=_run, args=(pid, doc, plan), name=f"lyric-remix-{pid}", daemon=True).start()
    return _song(doc, plan)


def _run(pid: str, doc: dict, plan: dict) -> None:
    try:
        with _render_slot:
            render(doc, plan, lambda frac, stage: _set_job(pid, state="rendering", progress=round(frac, 3), stage=stage))
        _set_job(pid, state="done", progress=1.0, stage="ready", error=None)
    except Exception as exc:                                  # a failed render says why; never crashes the server
        log.warning("lyric remix %s: render failed: %s", pid, exc)
        _set_job(pid, state="error", progress=0.0, stage="", error=str(exc)[:300])


# ------------------------------------------------------------------------------------------------ the dj-core plan


def build_ctx(doc: dict, plan: dict) -> dict:
    """The on-demand ctx for dj-core's planLyricRemix: the section starts at the bar line of the first line of A the plan uses."""
    a, b = plan["a"], plan.get("b")
    songs = {k: doc["songs"][k] for k in ((a, b) if b else (a,)) if k in doc["songs"]}
    sa = songs.get(a) or {}
    bpm = float(sa.get("bpm") or 0)
    alines = sa.get("lines") or []
    ts = [float(alines[pc["i"]]["t"]) for pc in plan.get("pieces") or []
          if pc.get("s") == "a" and isinstance(pc.get("i"), int) and 0 <= pc["i"] < len(alines)]
    if bpm <= 0 or not ts:
        raise RuntimeError("the plan has no usable line table of the first song")
    bar = 240.0 / bpm
    line_t = min(ts)
    ctx = {"aId": a, "bId": b, "songs": songs, "plans": [plan], "mode": "transition" if b else "song", "onDemand": True,
           "pos": line_t - 10.0, "rate": 1.0, "bpm": bpm, "bar": bar, "lineT": line_t, "stemsLive": True, "guestStems": True,
           "vocalBusy": False, "drops": [], "vocals": [], "used": [], "count": 0, "variant": 0}
    if b:
        ctx.update(bBpm=float((songs.get(b) or {}).get("bpm") or 0), winStart=line_t, winEnd=line_t + 40 * bar / 4)
    return ctx


def plan_via_dj_core(doc: dict, plan: dict) -> dict:
    ctx = build_ctx(doc, plan)
    try:
        res = djcore_bridge.call("lyric_remix_plan", {"ctx": ctx})
    except (djcore_bridge.DjCoreUnavailable, djcore_bridge.DjCoreError) as exc:
        raise RuntimeError(f"dj-core is not available: {exc}") from exc
    if not isinstance(res, dict) or not res.get("ok"):
        raise RuntimeError(f"dj-core refused the plan ({(res or {}).get('gate')}): {(res or {}).get('reason')}")
    res["_bar"] = ctx["bar"]
    return res


# ------------------------------------------------------------------------------------------------ audio


def decode(path: str, start: float, dur: float) -> np.ndarray:
    """dur seconds of any audio file from `start`, stereo float32 at SR, exactly dur*SR frames (zero padded past the end)."""
    n = int(round(dur * SR))
    if n <= 0:
        return np.zeros((0, 2), np.float32)
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, start):.6f}", "-t", f"{dur:.6f}",
           "-i", str(path), "-vn", "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"]
    res = subprocess.run(cmd, capture_output=True, timeout=300)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg could not read {Path(path).name}: {res.stderr.decode(errors='replace')[-200:]}")
    x = np.frombuffer(res.stdout, np.float32)
    x = x[: len(x) // 2 * 2].reshape(-1, 2)
    out = np.zeros((n, 2), np.float32)
    k = min(n, len(x))
    out[:k] = x[:k]
    return out


def probe_duration(path: str) -> float:
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, timeout=60)
    try:
        return float(res.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"cannot read the length of {Path(path).name}") from exc


def _ramp(x: np.ndarray) -> np.ndarray:
    k = min(int(RAMP_S * SR), len(x) // 2)
    if k > 0:
        r = np.linspace(0.0, 1.0, k, dtype=np.float32)[:, None]
        x[:k] *= r
        x[-k:] *= r[::-1]
    return x


def _varispeed(x: np.ndarray, rate: float, n_out: int) -> np.ndarray:
    """x played `rate` times faster (B source seconds per output second), resampled to n_out frames; vocals only, <= 8 % apart."""
    from scipy.signal import resample_poly

    if abs(rate - 1.0) > 1e-4:
        f = Fraction(1.0 / rate).limit_denominator(2000)
        x = resample_poly(x, f.numerator, f.denominator, axis=0).astype(np.float32)
    out = np.zeros((n_out, 2), np.float32)
    k = min(n_out, len(x))
    out[:k] = x[:k]
    return out


def _highpass(x: np.ndarray, hz: float = 120.0) -> np.ndarray:
    from scipy.signal import butter, sosfilt

    return sosfilt(butter(2, hz, "highpass", fs=SR, output="sos"), x, axis=0).astype(np.float32)


def layout(start: float, end: float, bar: float, src_len: float) -> Tuple[float, float, int, int]:
    """(src0, src1, intro bars, outro bars): whole bars of A's instrumental around [start, end] to reach MIN_TOTAL_S, <= MAX_SIDE_BARS a
    side; where the song starts / ends sooner, the other side takes the slack."""
    section = end - start
    want = max(0, int(np.ceil(max(0.0, MIN_TOTAL_S - section) / 2 / bar)))
    want = min(want, MAX_SIDE_BARS)
    max_in = max(0, int(np.floor(start / bar + 1e-6)))
    max_out = max(0, int(np.floor((src_len - end) / bar + 1e-6)))
    intro, outro = min(want, max_in), min(want, max_out)
    slack = 2 * want - intro - outro
    if slack > 0:
        cut_in, cut_out = intro < want, outro < want
        if cut_in:
            outro = min(max_out, MAX_SIDE_BARS, outro + slack)
        if cut_out:
            intro = min(max_in, MAX_SIDE_BARS, intro + slack)
    return start - intro * bar, end + outro * bar, intro, outro


def mix(res: dict, a_stems: Dict[str, str], a_vocals: str, b_vocals: Optional[str], src_len: float,
        progress: Callable[[float, str], None]) -> Tuple[np.ndarray, dict]:
    """The raw stereo float32 render and {intro_bars, outro_bars, section_start_s, bpm} from dj-core's plan `res`."""
    start, end, bar = float(res["start"]), float(res["end"]), float(res["_bar"])
    src0, src1, intro, outro = layout(start, end, bar, src_len)
    total = src1 - src0
    progress(0.30, "instrumental")
    inst = None
    for i, name in enumerate(_STEMS):
        seg = decode(a_stems[name], src0, total)
        inst = seg if inst is None else inst + seg
        progress(0.30 + 0.10 * (i + 1) / len(_STEMS), "instrumental")
    out = inst
    sl = res.get("slices") or []
    for i, s in enumerate(sl):
        dur, off, gain = float(s["dur"]), float(s["off"]), float(s.get("gain", 1.0))
        n = int(round(dur * SR))
        if s.get("src") == "guest":
            if not b_vocals:
                raise RuntimeError("the other song's vocal stem is missing")
            rate = float(s.get("rate") or 1.0)
            piece = _highpass(_varispeed(decode(b_vocals, float(s["from"]), dur * rate), rate, n))
        else:
            piece = decode(a_vocals, float(s["from"]), dur)
        piece = _ramp(piece * np.float32(gain))
        o = int(round((start - src0 + off) * SR))
        k = min(n, len(out) - o)
        if k > 0:
            out[o:o + k] += piece[:k]
        progress(0.40 + 0.25 * (i + 1) / max(1, len(sl)), "lyric pieces")
    return out, {"intro_bars": intro, "outro_bars": outro, "section_start_s": start - src0, "total_s": total}


def encode(raw: np.ndarray, dst: Path) -> None:
    """Float PCM -> loudness matched AAC m4a (phone_audio's codec settings), fades on both ends."""
    total = len(raw) / SR
    af = f"loudnorm=I={TARGET_LUFS}:TP=-1:LRA=11,afade=t=in:d={FADE_IN_S},afade=t=out:st={max(0.0, total - FADE_OUT_S):.3f}:d={FADE_OUT_S}"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "-",
           "-af", af, "-ar", str(SR), "-c:a", phone_audio.CODEC, "-b:a", phone_audio.BITRATE, "-use_editlist", "1",
           "-movflags", "+faststart", "-f", "mp4", str(dst)]
    res = subprocess.run(cmd, input=np.clip(raw, -1.5, 1.5).astype(np.float32).tobytes(), capture_output=True, timeout=600)
    if res.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError(f"encode failed: {res.stderr.decode(errors='replace')[-200:]}")


def beat_grid(path: Path, bpm: float, section_offset: float, total: float) -> dict:
    """Beat + downbeat times of the render: the analyser's when it can read it, else the plan's exact grid (the stems share the
    song's grid and every cut is on a beat, so the grid runs from the first bar line of the render)."""
    if Host.analyze is not None:
        try:
            a = Host.analyze(path)
            d = a.to_dict() if hasattr(a, "to_dict") else dict(a)
            if d.get("beat_times"):
                return {"bpm": float(d["bpm"]), "beat_times": [round(float(t), 4) for t in d["beat_times"]],
                        "downbeat_times": [round(float(t), 4) for t in d.get("downbeat_times") or []], "grid": "analyser"}
        except Exception as exc:                              # the analyser is best effort, the synthetic grid is exact
            log.info("lyric remix: analyser failed on the render (%s); using the plan grid", exc)
    beat = 60.0 / bpm
    p0 = section_offset % beat                                # ~0: the render starts a whole number of bars before the section
    if beat - p0 < 1e-3:
        p0 = 0.0
    beats = [round(p0 + k * beat, 4) for k in range(int((total - p0) / beat))]
    downs = beats[::4]
    return {"bpm": float(bpm), "beat_times": beats, "downbeat_times": downs, "grid": "plan"}


def render(doc: dict, plan: dict, progress: Callable[[float, str], None] = lambda f, s: None) -> dict:
    """Render the plan to songs/_remix/<id>/audio.m4a (+ meta.json). Returns the meta. Raises RuntimeError with the reason."""
    pid = plan["id"]
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed on the server")
    progress(0.02, "planning")
    res = plan_via_dj_core(doc, plan)
    need = [plan["a"]] + ([plan["b"]] if plan.get("b") else [])
    stems: Dict[str, Dict[str, str]] = {}
    t0 = time.time()
    for tid in need:
        queued = False
        while True:
            st = Host.stems4(tid)
            if st:
                stems[tid] = st
                break
            if time.time() - t0 > STEM_WAIT_S:
                raise RuntimeError("stems are not ready (separation timed out)")
            if not queued:
                Host.queue_stems(tid)
                queued = True
            progress(0.05, "separating stems")
            time.sleep(3.0)
    progress(0.25, "reading stems")
    src_len = probe_duration(stems[plan["a"]]["vocals"])
    raw, info = mix(res, stems[plan["a"]], stems[plan["a"]]["vocals"],
                    stems[plan["b"]]["vocals"] if plan.get("b") else None, src_len, progress)
    d = remix_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".audio.m4a.tmp{os.getpid()}.{threading.get_ident()}"
    try:
        progress(0.70, "encoding")
        encode(raw, tmp)
        out = d / "audio.m4a"
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    total = info["total_s"]
    progress(0.90, "beat grid")
    grid = beat_grid(out, float(doc["songs"][plan["a"]]["bpm"]), info["section_start_s"], total)
    meta = {"identity": identity(doc, plan), "plan_id": pid, "a": plan["a"], "b": plan.get("b"), "version": RENDER_VERSION,
            "text": res.get("text"), "duration": round(total, 3), "bytes": out.stat().st_size,
            "sha256": phone_audio.sha256_of(out), "built": int(time.time()), **info, **grid}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    prune(keep=pid)
    return meta


def prune(keep: Optional[str] = None) -> int:
    """LRU cap: remove the least recently used renders beyond cache_max() (mtime of meta.json = last use)."""
    root = song_store.songs_dir() / "_remix"
    rows = []
    try:
        for e in os.scandir(root):
            if e.is_dir() and _PLAN_RE.fullmatch(e.name):
                try:
                    rows.append((os.stat(os.path.join(e.path, "meta.json")).st_mtime, e.name, e.path))
                except OSError:
                    rows.append((0.0, e.name, e.path))
    except OSError:
        return 0
    rows.sort()
    n = 0
    for _, name, path in rows[:max(0, len(rows) - cache_max())]:
        if name != keep and (_jobs.get(name) or {}).get("state") != "rendering":
            shutil.rmtree(path, ignore_errors=True)
            n += 1
    return n


def touch(pid: str) -> None:
    try:
        os.utime(remix_dir(pid) / "meta.json")
    except OSError:
        pass


# ------------------------------------------------------------------------------------------------ REST


class SettingsBody(BaseModel):
    ai_picks: bool


@router.get("/api/lyric_remix/songs")
def api_songs(q: str = "", limit: int = 1000):
    return list_songs(q, limit)


@router.get("/api/lyric_remix/pickable")
def api_pickable():
    """The rendered remixes the AI DJ may pick: none unless the owner turned ai_picks on (default off = on request only)."""
    if not get_settings()["ai_picks"]:
        return {"ai_picks": False, "songs": []}
    return {"ai_picks": True, "songs": [s for s in list_songs()["songs"] if s["state"] == "ready"]}


@router.get("/api/lyric_remix/songs/{plan_id}")
def api_song(plan_id: str):
    if not _PLAN_RE.fullmatch(plan_id):
        raise HTTPException(status_code=422, detail="bad plan id")
    return song_doc(plan_id)


@router.post("/api/lyric_remix/songs/{plan_id}/render")
def api_render(plan_id: str):
    if not _PLAN_RE.fullmatch(plan_id):
        raise HTTPException(status_code=422, detail="bad plan id")
    return start_render(plan_id)


@router.get("/api/lyric_remix/settings")
def api_get_settings():
    return get_settings()


@router.put("/api/lyric_remix/settings")
def api_put_settings(body: SettingsBody):
    return set_settings(body.ai_picks)


@router.get("/api/audio/remix/{plan_id}")
def api_audio(plan_id: str, q: str = ""):
    """The rendered m4a (already the phone AAC format, so q=phone and no q are the same bytes); 409 until rendered."""
    if not _PLAN_RE.fullmatch(plan_id):
        raise HTTPException(status_code=422, detail="bad plan id")
    if q not in ("", "phone"):
        raise HTTPException(status_code=400, detail="q must be 'phone' or empty")
    doc = lra.load()
    plan = _find(doc, plan_id)
    meta = _ready_meta(doc, plan)
    if not meta:
        raise HTTPException(status_code=409, detail="this lyric remix is not rendered yet")
    touch(plan_id)
    return RangedFileResponse(remix_dir(plan_id) / "audio.m4a", media_type=phone_audio.MEDIA_TYPE,
                              headers={"X-Content-SHA256": str(meta.get("sha256") or ""), "X-Source-Id": f"lr-{plan_id}"})
