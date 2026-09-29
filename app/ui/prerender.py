"""Pre-render scheduler: the NEXT songs' stems and key-locked tempo stems, made ahead of the booking.

The console tells the server which pool songs are the next candidates (ranked, best first) and at
which tempi they may have to play (the tempo A will be at when B enters). The scheduler then makes
sure, in that order and one heavy job at a time:

  1. the 4 stems exist (queued on the shared separation worker: the pool head is urgent, the rest
     wait behind it and behind the LLM: server._next_stem_job),
  2. every key-locked tempo set that candidate needs exists (Rubber Band, one render at a time,
     and not while a separation is running, for at most STEMS_WAIT_S).

Everything is cached by content hash (stems) and by (hash, BPM) (tempo sets), so a candidate that is
rejected costs nothing more and one that comes back is instant. A candidate that leaves the ranked
list has its queued (not started) separation dropped; work already running is left to finish (both
outputs are cached and reusable). No Demucs or Rubber Band code lives here: the ports do the work
(`ServerIO` in production, the same class over the sim host in app/sim).

Pure scheduling logic (no threads, no I/O of its own): `step()` is called by a 1 s worker thread in
production and by the requests themselves in the sim, so both are testable with a stub io.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

TEMPO_STEP = 0.5            # keylock.TEMPO_STEP: tempo sets are cached per 0.5 BPM
MAX_ITEMS = 6               # candidates tracked at once (pool of 4 + the booked song + one spare)
MAX_BPMS = 3                # tempi per candidate (A's tempo now, A's native tempo, one spare)
STEMS_WAIT_S = 45.0         # a tempo render yields to a running separation for at most this long
BPM_LO, BPM_HI = 40.0, 240.0


def round_bpm(bpm: float) -> float:
    return round(float(bpm) / TEMPO_STEP) * TEMPO_STEP


@dataclass
class Item:
    tid: str
    bpms: List[float]
    rank: int
    asked_at: float
    stems_queued_at: Optional[float] = None
    stems_at: Optional[float] = None
    tempo_started: Dict[float, float] = field(default_factory=dict)
    tempo_at: Dict[float, float] = field(default_factory=dict)
    tempo_err: Dict[float, str] = field(default_factory=dict)

    def tempo_done(self, b: float) -> bool:
        return b in self.tempo_at

    @property
    def stems_ready(self) -> bool:
        return self.stems_at is not None

    @property
    def ready(self) -> bool:
        return self.stems_ready and all(b in self.tempo_at for b in self.bpms)

    @property
    def failed(self) -> bool:
        return any(b in self.tempo_err for b in self.bpms)


class Prerender:
    def __init__(self, io, clock) -> None:
        self.io, self.clock = io, clock
        self._lock = threading.RLock()
        self._items: Dict[str, Item] = {}
        self._running: Optional[tuple] = None          # (tid, bpm): the one tempo render in flight
        self.dropped_before_ready: List[str] = []       # candidates that left the list before their stems / tempo sets landed
        self.tempo_rendered: List[dict] = []            # {tid, bpm, seconds}: renders finished (seconds: start to seen done)
        self.stems_finished: List[dict] = []            # {tid, seconds}: from the first ask to the stems being cached
        self.heavy_max = 0                              # most heavy jobs seen at once (separations + tempo renders)

    # ---- the ranked list the console wants ready --------------------------------------------
    def set_items(self, items: List[dict]) -> List[dict]:
        """Replace the ranked candidate list. items: [{track_id, bpms: [..]}] best first. A candidate that
        is no longer listed is dropped (its queued separation is cancelled); listed ones keep their state."""
        with self._lock:
            now = self.clock()
            keep: Dict[str, Item] = {}
            for rank, raw in enumerate(items[:MAX_ITEMS]):
                tid = str(raw.get("track_id") or "")
                if not tid or tid in keep:
                    continue
                bpms = []
                for b in raw.get("bpms") or []:
                    try:
                        b = float(b)
                    except (TypeError, ValueError):
                        continue
                    if BPM_LO <= b <= BPM_HI and round_bpm(b) not in bpms:
                        bpms.append(round_bpm(b))
                bpms = bpms[:MAX_BPMS]
                it = self._items.get(tid)
                if it is None:
                    it = Item(tid=tid, bpms=bpms, rank=rank, asked_at=now)
                else:
                    it.bpms, it.rank = bpms, rank
                keep[tid] = it
            for tid, it in self._items.items():
                if tid not in keep:
                    if not it.ready:
                        self.dropped_before_ready.append(tid)
                    self.io.drop_stems(tid)             # queued, not started: gone; a running one finishes and stays cached
            self._items = keep
            return self.status()

    def status(self) -> List[dict]:
        with self._lock:
            out = []
            for it in sorted(self._items.values(), key=lambda x: x.rank):
                tempo = {f"{b:.1f}": ("done" if b in it.tempo_at else "error" if b in it.tempo_err
                                      else "running" if b in it.tempo_started else "waiting") for b in it.bpms}
                out.append({"track_id": it.tid, "rank": it.rank, "stems": it.stems_ready, "tempo": tempo,
                            "ready": it.ready, "failed": it.failed,
                            "stems_at": it.stems_at, "tempo_at": max(it.tempo_at.values()) if it.tempo_at else None,
                            "asked_at": it.asked_at})
            return out

    def stats(self) -> dict:
        with self._lock:
            return {"items": len(self._items), "dropped_before_ready": len(self.dropped_before_ready),
                    "tempo_renders": len(self.tempo_rendered),
                    "tempo_render_seconds": round(sum(r["seconds"] for r in self.tempo_rendered), 1),
                    "heavy_max": self.heavy_max}

    # ---- one unit of work ------------------------------------------------------------------------
    def step(self) -> None:
        with self._lock:
            now = self.clock()
            items = sorted(self._items.values(), key=lambda x: x.rank)
            # 1) stems: note the ones that landed, queue the ones not asked for yet
            for it in items:
                if it.stems_ready:
                    continue
                if self.io.stems_ready(it.tid):
                    it.stems_at = now
                    self.stems_finished.append({"tid": it.tid, "seconds": round(now - it.asked_at, 1)})
                elif it.stems_queued_at is None:
                    self.io.queue_stems(it.tid, it.rank == 0)     # the pool head is urgent; the rest wait for an idle LLM
                    it.stems_queued_at = now
            # 2) the tempo render in flight, if any
            if self._running is not None:
                tid, b = self._running
                it = self._items.get(tid)
                st = self.io.tempo_state(tid, b)
                if it is None or b not in it.bpms:
                    if st != "running":
                        self._running = None                    # dropped candidate: let the render finish, then move on
                elif st == "done":
                    it.tempo_at[b] = now
                    self.tempo_rendered.append({"tid": tid, "bpm": b, "seconds": round(now - it.tempo_started[b], 1)})
                    self._running = None
                elif st.startswith("error"):
                    it.tempo_err[b] = st
                    self._running = None
            # 3) start the next one: best-ranked candidate whose stems exist, one at a time
            if self._running is None:
                for it in items:
                    if not it.stems_ready:
                        continue
                    todo = [b for b in it.bpms if b not in it.tempo_at and b not in it.tempo_err]
                    if not todo:
                        continue
                    if self.io.stems_running() and now - it.stems_at < STEMS_WAIT_S:
                        break                                   # yield to the separation, do not stack CPU work
                    b = todo[0]
                    st = self.io.tempo_state(it.tid, b)
                    if st == "done":
                        it.tempo_at[b] = now                    # cache hit: nothing rendered
                        continue
                    if st.startswith("error"):
                        it.tempo_err[b] = st
                        continue
                    if st == "none":
                        self.io.start_tempo(it.tid, b)
                    it.tempo_started[b] = now
                    self._running = (it.tid, b)
                    break
            self.heavy_max = max(self.heavy_max, self.io.heavy_now())


class ServerIO:
    """The production ports: server.py's stem queue, keylock's tempo renders, the engine host."""

    def __init__(self) -> None:
        self._hash: Dict[str, str] = {}

    @staticmethod
    def _server():
        from app.ui import server

        return server

    @staticmethod
    def _host():
        from app.ui import engine

        return engine.current().host

    def stems_ready(self, tid: str) -> bool:
        try:
            return self._server()._cached_stems4(tid) is not None
        except Exception:
            return False

    def queue_stems(self, tid: str, urgent: bool) -> None:
        self._host().queue_stems(tid, urgent)

    def drop_stems(self, tid: str) -> bool:
        return self._host().drop_stems(tid)

    def stems_running(self) -> bool:
        return self._host().stems_running()

    def heavy_now(self) -> int:
        return int(self._host().stems_running()) + self._host().tempo_running()

    def _key(self, tid: str, bpm: float):
        from app.music_brain import keylock, stem_service

        srv = self._server()
        path = srv._track_path(tid)
        h = self._hash.get(tid) or self._hash.setdefault(tid, stem_service.file_hash(path))
        native = srv.analyze_track(path).bpm
        key, t = keylock.tempo_key(h, native, bpm)
        return key, native, h

    def tempo_state(self, tid: str, bpm: float) -> str:
        from app.music_brain import keylock

        try:
            key, native, _ = self._key(tid, bpm)
        except Exception as exc:
            return f"error: {type(exc).__name__}"
        if not native or abs(bpm / native - 1) > keylock.MAX_TEMPO_STRETCH:
            return "error: tempo gap too large for a key-locked stretch"
        if (keylock.KEYLOCK_DIR / key / "meta.json").exists():
            return "done" if self._host().tempo_gate(key) else "running"
        with keylock._lock:
            st = keylock._jobs.get(key)
        if st == "running":
            return "running"
        if st and st.startswith("error"):
            return st
        return "none"

    def start_tempo(self, tid: str, bpm: float) -> None:
        from app.music_brain import keylock

        srv = self._server()
        key, native, h = self._key(tid, bpm)
        stems = srv._cached_stems4(tid)
        if stems:
            keylock.ensure_tempo(h, stems, native, bpm)


_sched: Optional[Prerender] = None
_sched_lock = threading.Lock()


def current() -> Prerender:
    """The process scheduler (a new one per installed engine: the sim starts each run clean)."""
    global _sched
    from app.ui import engine

    with _sched_lock:
        eng = engine.current()
        if _sched is None or getattr(_sched, "_engine", None) is not eng:
            _sched = Prerender(ServerIO(), lambda: engine.current().host.now())
            _sched._engine = eng
        return _sched


def reset() -> None:
    global _sched
    with _sched_lock:
        _sched = None
