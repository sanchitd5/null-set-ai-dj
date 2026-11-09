"""Language-of-the-set service: background language id of the library + the one gate every pick source uses.

  start(...)            worker thread: identifies the vocal stem of queued tracks (downloads first, then the
                        library backfill, only while the LLM is idle), rescans for tracks that gained stems
  queue(track_id, urgent)
  profile_for(history, playing_id)  -> lang_id profile of the songs played in this set (newest weighted most)
  check(profile, track_id=None, name=None) -> (verdict, reason)   verdict: ok | unknown | penalty | block
  language_of(track_id)  -> {lang, conf, instr} | None   (what the clients see)

Owner / hand picks never go through check(): they always play and extend the profile (they are in the history).
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from app.music_brain.analysis import lang_id

RESCAN_S = 600.0
IDLE_POLL_S = 5.0

_cv = threading.Condition()
_urgent: List[str] = []
_backlog: List[str] = []
_queued: set = set()
_state = {"done": 0, "failed": 0, "busy": None, "last_secs": None, "started": False, "paused": False}

_names_idx: Dict[str, str] = {}
_names_len = -1


def status() -> dict:
    with _cv:
        return {**{k: v for k, v in _state.items() if k != "started"}, "queued": len(_urgent) + len(_backlog)}


def queue(track_id: str, urgent: bool = False) -> bool:
    """Queue a track for language id (no-op when it already has a record or is queued)."""
    if not lang_id.available() or lang_id.get(track_id) is not None:
        return False
    with _cv:
        if track_id in _queued:
            if urgent and track_id in _backlog:
                _backlog.remove(track_id)
                _urgent.append(track_id)
            return False
        _queued.add(track_id)
        (_urgent if urgent else _backlog).append(track_id)
        _cv.notify()
    return True


def set_paused(paused: bool) -> None:
    """Pause / resume the worker (the admin job page). A paused worker finishes the song it is on and takes no more."""
    with _cv:
        _state["paused"] = bool(paused)
        _cv.notify_all()


def _next() -> Tuple[Optional[str], bool]:
    """(track id, came from the urgent queue); (None, False) when nothing is queued or the worker is paused."""
    with _cv:
        if _state["paused"]:
            return None, False
        if _urgent:
            return _urgent.pop(0), True
        if _backlog:
            return _backlog.pop(0), False
    return None, False


def start(tracks: Callable[[], Dict], names: Callable[[], Dict], vocal_of: Callable[[str], Optional[str]],
          llm_busy: Callable[[], bool], is_song: Callable[[str], bool]) -> None:
    """Start the worker once. tracks() -> {track_id: path}; vocal_of(track_id) -> cached vocal stem path or None
    (never separates); llm_busy() -> the LLM is in use (the backfill waits, a fresh download does not)."""
    if _state["started"] or not lang_id.available():
        return
    _state["started"] = True

    def rescan() -> None:
        for tid in list(tracks()):
            try:
                if lang_id.get(tid) is None and is_song(tid) and vocal_of(tid):
                    queue(tid)
            except Exception:
                continue

    def run() -> None:
        last_scan = 0.0
        while True:
            if time.time() - last_scan > RESCAN_S:
                last_scan = time.time()
                rescan()
            tid, urgent = _next()
            if tid is None:
                with _cv:
                    _cv.wait(timeout=IDLE_POLL_S)
                continue
            while not urgent and llm_busy():            # the backfill waits for an idle LLM; a fresh download does not
                time.sleep(IDLE_POLL_S)
            vocal = None
            try:
                vocal = vocal_of(tid)
                if vocal is None:                       # stems not there (yet / any more): the rescan finds it later
                    continue
                with _cv:
                    _state["busy"] = tid
                rec = lang_id.identify(vocal)
                lang_id.put(tid, rec)
                with _cv:
                    _state["done"] += 1
                    _state["last_secs"] = rec.get("secs")
            except Exception as exc:                    # one bad file never stops the worker
                with _cv:
                    _state["failed"] += 1
                print(f"[langid] {tid}: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
            finally:
                with _cv:
                    _queued.discard(tid)
                    _state["busy"] = None

    threading.Thread(target=run, daemon=True, name="langid").start()


# ------------------------------------------------------------------ lookups

def language_of(track_id: str) -> Optional[dict]:
    return lang_id.public(lang_id.get(track_id))


def _key(name: str) -> str:
    from app.ui.services import pick_pool
    from app.ui.services.track_identity import clean_identity

    artist, title = clean_identity(str(name or ""))
    return pick_pool.key_of(artist, title)


def _index(names: Dict[str, str]) -> Dict[str, str]:
    global _names_idx, _names_len
    if len(names) != _names_len:
        idx: Dict[str, str] = {}
        for tid, nm in list(names.items()):
            try:
                idx.setdefault(_key(nm), tid)
            except Exception:
                continue
        _names_idx, _names_len = idx, len(names)
    return _names_idx


def record_for(names: Dict[str, str], track_id: Optional[str] = None, name: Optional[str] = None) -> Optional[dict]:
    """The stored record of a library track by id, else by its cleaned artist - title."""
    if track_id:
        rec = lang_id.get(track_id)
        if rec is not None:
            return rec
    if name:
        tid = _index(names).get(_key(name))
        if tid:
            return lang_id.get(tid)
    return None


def profile_for(names: Dict[str, str], history: Sequence[str], playing_id: Optional[str] = None) -> dict:
    """Profile of the set: the history (oldest first) then the playing song as the newest."""
    recs = [record_for(names, name=h) for h in history]
    if playing_id:
        recs.append(lang_id.get(playing_id))
    return lang_id.profile_of(recs)


def check(names: Dict[str, str], profile: dict, track_id: Optional[str] = None,
          name: Optional[str] = None) -> Tuple[str, str]:
    return lang_id.verdict(record_for(names, track_id, name), profile)
