"""Every step the AI took, per song of the set, plus the song's waveform (the look, not audio).

User: "you need to log each step that ai took for song, save waveform alongside, for
future analysis to improve our algorithm".

    data/cache/sessions/<session>/songs/<NN>-<slug>/
        steps.jsonl    {t, at_song, deck, phase, kind, decision, why, inputs, result}, time order
        meta.json      track id, name, bpm, key, energy level, entry/exit, deck, recipe in/out
        waveform.json  song_waveform.compute (mix + stems peaks/RMS, energy, phrases, sections, vocals)
        waveform.png   song_waveform.render

Song folders live inside the session dir, so session_log's pruning (newest 60 runs) covers them.
Steps come from the browser (POST /api/session/steps, app/ui/static/step-log.js) and from
server endpoints (suggest, match, plan, preplan, merge audition, learned pick, hook drops).
A step is filed under its track id; a deck-only step goes to the song on that deck. Steps
before a song plays (selection, planning) open its record early; after the song ended, a
fresh selection of the same track opens a new record (a song played twice).

Best-effort throughout: nothing here raises into a caller, and waveform work runs on one
background thread, never on a request thread.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from app.ui.services import session_log

MAX_STEPS = 2000          # per song; later steps are counted, not written
MAX_BATCH = 200           # steps per POST
MAX_TEXT = 300
MAX_BLOB = 2000           # json chars for inputs / result
PHASES = ("selection", "planning", "transition-in", "playing", "transition-out")
SELECTION_KINDS = {"match", "candidate", "candidate_reject", "candidate_accept",
                   "vibe", "energy_gate", "load"}

try:
    from app.music_brain.config import CACHE_DIR as _CACHE_DIR
    WAVEFORM_CACHE = _CACHE_DIR / "waveforms"
except Exception:  # pragma: no cover - config always importable in the app
    WAVEFORM_CACHE = Path("data/cache/waveforms")

_lock = threading.RLock()
_songs: List[dict] = []                 # this run's songs, oldest first
_deck_track: Dict[str, str] = {}        # deck letter -> track id loaded on it
_set_start: Optional[float] = None
_resolve: Dict[str, Optional[Callable]] = {"path": None, "analysis": None, "stems": None,
                                           "name": None, "energy": None}


def configure(**fns: Callable) -> None:
    """Server hands over how to find a track: path(id), analysis(path)->dict, stems(id), name(id), energy(path, bpm)."""
    _resolve.update({k: v for k, v in fns.items() if k in _resolve})


def reset() -> None:
    global _set_start
    with _lock:
        _songs.clear()
        _deck_track.clear()
        _set_start = None


# ---------------------------------------------------------------- pure parts
def pick_song(songs: List[dict], track_id: str, kind: str, phase: Optional[str]) -> Optional[int]:
    """Index of the record a step belongs to, or None = open a new one."""
    for i in range(len(songs) - 1, -1, -1):
        s = songs[i]
        if s["track_id"] != track_id:
            continue
        if s.get("exit_t") is None:
            return i
        # played and gone: picking it again starts a new record, late steps stay with the old one
        if kind in SELECTION_KINDS or kind == "song_start" or phase in ("selection", "planning"):
            return None
        return i
    return None


def phase_of(song: dict, kind: str, given: Optional[str] = None) -> str:
    if given in PHASES:
        return given
    if not song.get("entry_t"):
        return "selection" if kind in SELECTION_KINDS else "planning"
    if song.get("exit_t") is not None:
        return "transition-out"
    if song.get("transition") == "in":
        return "transition-in"
    if song.get("transition") == "out":
        return "transition-out"
    return "playing"


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40] or "song"


def _text(v) -> Optional[str]:
    if v is None:
        return None
    return str(v)[:MAX_TEXT]


def _blob(v):
    if v is None:
        return None
    try:
        s = json.dumps(v, default=str)
    except (TypeError, ValueError):
        return str(v)[:MAX_BLOB]
    return v if len(s) <= MAX_BLOB else s[:MAX_BLOB] + "..."


# ---------------------------------------------------------------- records
def _session_dir(session: Optional[str] = None) -> Path:
    return session_log._path(session).parent


def _meta_of(s: dict) -> dict:
    keys = ("nn", "track_id", "name", "deck", "bpm", "key", "energy_level", "recipe_in", "recipe_out",
            "entry_t", "exit_t", "entry_set_s", "exit_set_s", "entry_song_s", "exit_song_s", "steps", "dropped")
    m = {k: s.get(k) for k in keys}
    m["session"] = session_log.SESSION_ID
    return m


def _write_meta(s: dict) -> None:
    p = s["dir"] / "meta.json"
    tmp = p.with_suffix(".tmp")
    meta = _meta_of(s)
    tmp.write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)
    from app.music_brain import history

    history.on_play(session_log.SESSION_ID, meta, s["dir"])     # the set-history index; never raises


def _name(track_id: str, given: Optional[str]) -> str:
    if given:
        return str(given)[:200]
    fn = _resolve.get("name")
    try:
        n = fn(track_id) if fn else None
    except Exception:
        n = None
    return n or track_id


def _new_song(track_id: str, name: Optional[str]) -> dict:
    nn = len(_songs) + 1
    nm = _name(track_id, name)
    d = _session_dir() / "songs" / f"{nn:02d}-{slug(nm)}"
    d.mkdir(parents=True, exist_ok=True)
    s = {"nn": nn, "track_id": track_id, "name": nm, "dir": d, "deck": None, "entry_t": None,
         "exit_t": None, "transition": None, "recipe_in": None, "recipe_out": None, "steps": 0, "dropped": 0}
    _songs.append(s)
    _write_meta(s)
    return s


def _on_deck(deck: Optional[str]) -> Optional[dict]:
    for s in reversed(_songs):
        if s.get("entry_t") and s.get("exit_t") is None and (deck is None or s.get("deck") == deck):
            return s
    return None


def _record(kind: str, track_id: Optional[str] = None, deck: Optional[str] = None, decision=None, why=None,
            inputs=None, result=None, at_song=None, phase=None, t=None, name=None) -> Optional[dict]:
    global _set_start
    kind = str(kind or "step")[:40]
    deck = _text(deck)
    now = time.time()
    if not isinstance(t, (int, float)) or abs(t - now) > 86400:
        t = now
    with _lock:
        if kind == "load" and deck and track_id:
            _deck_track[deck] = str(track_id)
        if not track_id and deck:
            s = _on_deck(deck)
            track_id = s["track_id"] if s else _deck_track.get(deck)
        if not track_id:
            s = _on_deck(None)
            if not s:
                return None
            track_id = s["track_id"]
        track_id = str(track_id)[:64]
        i = pick_song(_songs, track_id, kind, phase)
        s = _songs[i] if i is not None else _new_song(track_id, name)
        changed = False
        if kind == "song_start" and not s.get("entry_t"):
            _set_start = _set_start or t
            s.update(entry_t=round(t, 3), deck=deck or s.get("deck"), entry_set_s=round(t - _set_start, 1),
                     entry_song_s=at_song)
            if name and s["name"] == track_id:
                s["name"] = str(name)[:200]
            changed = True
        ph = phase_of(s, kind, phase)
        if kind == "song_end" and s.get("entry_t") and s.get("exit_t") is None:
            s.update(exit_t=round(t, 3), exit_set_s=round(t - (_set_start or t), 1), exit_song_s=at_song,
                     transition=None)
            ph = "transition-out"
            changed = True
        step = {"t": round(t, 3), "at_song": round(at_song, 2) if isinstance(at_song, (int, float)) else None,
                "deck": deck or s.get("deck"), "phase": ph, "kind": kind, "decision": _text(decision),
                "why": _text(why), "inputs": _blob(inputs), "result": _blob(result)}
        if s["steps"] < MAX_STEPS:
            s["steps"] += 1
            with open(s["dir"] / "steps.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(step, ensure_ascii=False, default=str) + "\n")
        else:
            s["dropped"] += 1
            changed = changed or s["dropped"] % 50 == 1  # recorded, without a write per step
        if changed:
            _write_meta(s)
        if kind == "song_end" and changed:
            render_async(s["dir"])
        return step


def step(kind: str, track_id: Optional[str] = None, **fields) -> Optional[dict]:
    """Log one AI step for a song. Never raises: logging must not break a live set."""
    try:
        return _record(kind, track_id, **fields)
    except Exception:
        return None


_FIELDS = ("deck", "decision", "why", "inputs", "result", "at_song", "phase", "t", "name")


def ingest(steps: list) -> int:
    """A browser batch; bad items are skipped. Returns how many were filed."""
    n = 0
    for raw in steps[:MAX_BATCH]:
        if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str) or not raw["kind"].strip():
            continue
        fields = {k: raw.get(k) for k in _FIELDS if raw.get(k) is not None}
        if "at_song" in fields and not isinstance(fields["at_song"], (int, float)):
            fields.pop("at_song")
        if "t" in fields and not isinstance(fields["t"], (int, float)):
            fields.pop("t")
        tid = raw.get("track_id")
        if step(raw["kind"].strip(), str(tid) if tid else None, **fields) is not None:
            n += 1
    return n


def _set_recipe(deck: Optional[str], key: str, recipe, transition: Optional[str]) -> None:
    with _lock:
        s = _on_deck(deck) if deck else None
        if s is None and deck and _deck_track.get(deck):
            i = pick_song(_songs, _deck_track[deck], "transition", "planning")
            s = _songs[i] if i is not None else _new_song(_deck_track[deck], None)
        if s is None:
            return
        if recipe:
            s[key] = str(recipe)[:120]
        s["transition"] = transition
        _write_meta(s)


def on_session_event(kind: str, fields: dict) -> None:
    """The console's session events (session_log) that belong to a song: transitions, glitches, ear flushes."""
    try:
        if kind == "track":
            ev = fields.get("event")
            out_d, in_d = fields.get("out"), fields.get("in") or fields.get("deck")
            if ev == "transition_start":
                _set_recipe(out_d, "recipe_out", fields.get("recipe"), "out")
                _set_recipe(in_d, "recipe_in", fields.get("recipe"), "in")
                for d, side in ((out_d, "out"), (in_d, "in")):
                    if d:
                        step("transition_start", deck=d, decision=fields.get("recipe"),
                             why=f"{side}: {fields.get('from')} -> {fields.get('to')}",
                             inputs={"seconds": fields.get("seconds")})
            elif ev == "transition_end":
                with _lock:
                    for s in _songs:
                        if s.get("transition") == "in":
                            s["transition"] = None
                step("transition_end", deck=in_d, decision=fields.get("now_playing"))
        elif kind == "glitch":
            decks = fields.get("decks") if isinstance(fields.get("decks"), list) else []
            what = fields.get("kind_") or fields.get("action") or "glitch"
            for d in decks or [{}]:
                d = d if isinstance(d, dict) else {}
                step("glitch", deck=d.get("deck"), decision=what, why=fields.get("where"),
                     at_song=d.get("pos_s"), inputs={"move": fields.get("move"), "stems": d.get("stems"),
                                                     "eq": d.get("eq"), "xfader": d.get("xfader")})
        elif kind == "ear_flush":
            step("ear_flush", decision=fields.get("reason"), inputs=fields)
    except Exception:
        pass


# ---------------------------------------------------------------- waveform worker
_jobs: "queue.Queue[Path]" = queue.Queue()
_queued: set = set()
_worker: Optional[threading.Thread] = None


def render_async(song_dir: Path) -> None:
    """Queue waveform.json + waveform.png for a song folder; one at a time, off the request thread."""
    global _worker
    song_dir = Path(song_dir)
    with _lock:
        if song_dir in _queued:
            return
        _queued.add(song_dir)
        _jobs.put(song_dir)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name="song-waveform", daemon=True)
            _worker.start()


def _work() -> None:
    while True:
        try:
            d = _jobs.get(timeout=30)
        except queue.Empty:
            return
        try:
            render_song(d)
        except Exception as exc:
            print(f"[song_log] waveform failed for {d.name}: {exc}", flush=True)
        finally:
            with _lock:
                _queued.discard(d)


def read_steps(song_dir: Path) -> List[dict]:
    out = []
    try:
        for ln in (Path(song_dir) / "steps.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def render_song(song_dir: Path) -> Path:
    """waveform.json + waveform.png for one song folder (synchronous; the worker calls this)."""
    from app.ui.services import song_waveform as sw
    song_dir = Path(song_dir)
    meta = json.loads((song_dir / "meta.json").read_text(encoding="utf-8"))
    tid = meta.get("track_id")
    path = _resolve["path"](tid) if _resolve.get("path") else None
    if path is None or not Path(path).exists():
        raise FileNotFoundError(f"no audio for track {tid}")
    analysis = {}
    if _resolve.get("analysis"):
        try:
            analysis = _resolve["analysis"](Path(path)) or {}
        except Exception:
            analysis = {}
    stems = None
    if _resolve.get("stems"):
        try:
            stems = _resolve["stems"](tid)
        except Exception:
            stems = None
    wf = sw.cached(Path(path), stems, analysis, WAVEFORM_CACHE)
    (song_dir / "waveform.json").write_text(json.dumps(wf, separators=(",", ":")), encoding="utf-8")
    key = analysis.get("key")
    meta.update(bpm=analysis.get("bpm", meta.get("bpm")),
                key=key.get("camelot") if isinstance(key, dict) else meta.get("key"))
    if _resolve.get("energy") and meta.get("energy_level") is None and meta.get("bpm"):
        try:
            meta["energy_level"] = (_resolve["energy"](Path(path), float(meta["bpm"])) or {}).get("level")
        except Exception:
            pass
    (song_dir / "meta.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    with _lock:
        for s in _songs:
            if s["dir"] == song_dir:
                s.update(bpm=meta.get("bpm"), key=meta.get("key"), energy_level=meta.get("energy_level"))
    return sw.render(song_dir / "waveform.png", wf, read_steps(song_dir), meta)


# ---------------------------------------------------------------- retrieval
def _songs_dir(session: Optional[str]) -> Path:
    return _session_dir(session) / "songs"


def songs(session: Optional[str] = None) -> List[dict]:
    """Songs of a session with step counts (ValueError on a bad session id)."""
    base = _songs_dir(session)
    if not base.is_dir():
        return []
    out = []
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        steps_ = read_steps(d)
        by_phase: Dict[str, int] = {}
        by_kind: Dict[str, int] = {}
        for s in steps_:
            by_phase[s.get("phase") or "?"] = by_phase.get(s.get("phase") or "?", 0) + 1
            by_kind[s.get("kind") or "?"] = by_kind.get(s.get("kind") or "?", 0) + 1
        out.append({"nn": int(d.name.split("-", 1)[0]) if d.name[:2].isdigit() else None, "folder": d.name,
                    "track_id": meta.get("track_id"), "name": meta.get("name"),
                    "recipe_in": meta.get("recipe_in"), "recipe_out": meta.get("recipe_out"),
                    "entry_set_s": meta.get("entry_set_s"), "exit_set_s": meta.get("exit_set_s"),
                    "steps": len(steps_), "by_phase": by_phase, "by_kind": by_kind,
                    "waveform": (d / "waveform.json").exists(), "png": (d / "waveform.png").exists()})
    return out


def song_dir(session: Optional[str], nn: int) -> Optional[Path]:
    base = _songs_dir(session)
    if not base.is_dir():
        return None
    for d in base.iterdir():
        if d.is_dir() and d.name.split("-", 1)[0] == f"{int(nn):02d}":
            return d
    return None


def song(session: Optional[str], nn: int, limit: int = MAX_STEPS) -> Optional[dict]:
    d = song_dir(session, nn)
    if d is None:
        return None
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    return {"folder": d.name, "meta": meta, "steps": read_steps(d)[:max(1, limit)],
            "waveform": (d / "waveform.json").exists(), "png": (d / "waveform.png").exists()}


def _default_resolvers() -> None:
    """Outside the server (the CLI): uploads by content id, cached analysis and stems."""
    def path(tid: str) -> Optional[Path]:
        from app.music_brain.config import CACHE_DIR
        for p in (CACHE_DIR / "uploads").glob(f"{tid}.*"):
            return p
        return None

    def analysis(p: Path) -> dict:
        from app.music_brain.analysis.analyzer import analyze
        return analyze(p).to_dict()

    def stems(tid: str):
        from app.music_brain.audio.stem_service import cached_four_stems
        p = path(tid)
        return cached_four_stems(p) if p else None

    for k, fn in (("path", path), ("analysis", analysis), ("stems", stems)):
        if _resolve.get(k) is None:
            _resolve[k] = fn


def render_all(session: str) -> List[dict]:
    """Render every song of a session now (CLI --render). Errors are reported per song."""
    _default_resolvers()
    out = []
    for s in songs(session):
        d = song_dir(session, s["nn"]) if s.get("nn") else None
        if d is None:
            continue
        try:
            out.append({"nn": s["nn"], "png": str(render_song(d))})
        except Exception as exc:
            out.append({"nn": s["nn"], "error": f"{type(exc).__name__}: {exc}"[:300]})
    return out


def report(session: Optional[str] = None) -> dict:
    """For the CLI: the newest session unless one is named."""
    all_ = session_log.sessions()
    sid = session or (all_[0] if all_ else None)
    if sid is None:
        return {"session": None, "sessions": [], "songs": []}
    return {"session": sid, "sessions": all_[:20], "songs": songs(sid)}
