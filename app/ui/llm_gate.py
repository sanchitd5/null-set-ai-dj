"""Priority gate in front of every LLM call.

The browser fires look-ahead suggest calls (autopilot.js topUpPool) as well as
the per-pair transition plan (/api/autopilot/plan). With no ordering the plan
queued behind look-aheads and missed its deadline ("AI plan unavailable -
rules only").

The gate lets exactly ONE text / offline call run at a time and, when the slot
frees, hands it to the most urgent waiter: PLAN before EAR before SUGGEST
before LOOKAHEAD, first-come first-served within a level. A call already
running is never interrupted (the server cannot cancel a generation).

The live ear (LIVE, app/ui/live_ear.py) shares the model with everything else
under `start.sh --single-omni` and must answer inside one 8-bar phrase of a
hold loop. Measured in the omni server log (2026-09-28, Qwen3-Omni 4-bit,
continuous batching): a short audio call takes ~1.1 s on an idle server and
3-6 s when the server is busy, and a suggest took 17-26 s (plus a redo when cut
off), so queueing the ear behind one would miss the phrase. Policy:

* LIVE never queues. It waits at most LIVE_WAIT_S for the call in flight (the
  tail of a short call), then runs BESIDE it (the server batches both) rather
  than behind it. While a LIVE call waits or runs, nothing else starts.
* Hold: each live call (note_live) holds the model for LIVE_HOLD_S, one phrase
  ahead. During the hold LOOKAHEAD is refused outright (it is optional
  pre-fetching), and EAR does not START, except in the BEHIND_EAR_S window
  just after a live answer: the next phrase is then most of a phrase away,
  the longest gap a call gets before the ear asks again. SUGGEST (song
  selection) is NOT held: it was missing its selection window waiting on the
  live ear, so it now queues normally and starts as soon as the model is
  free, same as PLAN.
* No starvation: an EAR held this way starts after SUGGEST_MAX_HOLD_S
  whatever (the hold loop may be waiting for exactly that song). EAR clip
  ratings are advisory and give up on their own wait_timeout. PLAN is never
  held: it is short (~4 s) and the transition depends on it.
"""
from __future__ import annotations

import itertools
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

LIVE, PLAN, EAR, SUGGEST, LOOKAHEAD = -1, 0, 1, 2, 3   # EAR: the silent ear rating pre-planned transitions
NAMES = {LIVE: "live", PLAN: "plan", EAR: "ear", SUGGEST: "suggest", LOOKAHEAD: "lookahead"}
EAR_HOLD_S = 45.0          # a hold loop asks the ear every 8-bar phrase: cover the next one
LIVE_WAIT_S = 1.0          # the live ear waits this long for the call in flight, then runs beside it
LIVE_HOLD_S = 25.0         # one 8-bar phrase (~15 s at 128 BPM, ~21 s at 90) plus margin
BEHIND_EAR_S = 3.0         # right after a live answer a held EAR / SUGGEST may start
SUGGEST_MAX_HOLD_S = 20.0  # never hold a needed suggestion longer than this
_HELD = (EAR,)  # SUGGEST no longer paused for the live ear: selection was missing its window


def _skip(why: str) -> None:
    try:
        from app.ui import session_log

        session_log.log("gate_skip", priority="lookahead", why=why)
    except Exception:
        pass


class GateTimeout(TimeoutError):
    """Waited too long for the LLM slot."""


class PriorityGate:
    def __init__(self, suggest_max_hold_s: float = SUGGEST_MAX_HOLD_S, behind_ear_s: float = BEHIND_EAR_S,
                 poll_s: float = 0.25) -> None:
        self._cv = threading.Condition()
        self._busy: Optional[int] = None          # priority of the gated call in flight
        self._queue: list[tuple[int, int]] = []   # (priority, seq) waiting
        self._asked: dict[tuple[int, int], float] = {}   # ticket -> monotonic time it asked
        self._seq = itertools.count()
        self._ear_until = 0.0                    # look-aheads refused until (live or offline ear)
        self._live_until = 0.0                   # EAR / SUGGEST held until (live ear mid-loop)
        self._live_busy = False                  # a LIVE call is running
        self._live_waiting = 0                   # LIVE calls waiting LIVE_WAIT_S for the call in flight
        self._live_done = float("-inf")          # when the last LIVE call finished
        self.suggest_max_hold_s = suggest_max_hold_s
        self.behind_ear_s = behind_ear_s
        self.poll_s = poll_s

    # -- policy -------------------------------------------------------------
    def _held(self, ticket: tuple[int, int], now: float) -> bool:
        """True while the live ear's hold keeps this waiter from STARTING."""
        prio = ticket[0]
        if prio not in _HELD or now >= self._live_until:
            return False
        if now - self._live_done <= self.behind_ear_s:
            return False                         # just behind a live answer: the gap is ours
        if prio == SUGGEST and now - self._asked.get(ticket, now) >= self.suggest_max_hold_s:
            return False                         # bounded: a needed suggestion is never starved
        return True

    def _may_start(self, ticket: tuple[int, int], now: float) -> bool:
        if self._busy is not None or self._live_busy or self._live_waiting:
            return False
        for t in sorted(self._queue):            # most urgent waiter that is not held
            if not self._held(t, now):
                return t == ticket
        return False

    # -- slots --------------------------------------------------------------
    @contextmanager
    def slot(self, priority: int, wait_timeout: Optional[float] = None) -> Iterator[float]:
        """Block until this call may use the LLM. Yields the seconds waited.

        LIVE: `wait_timeout` (default LIVE_WAIT_S) caps the wait for the call in
        flight; past it the live call runs beside that call instead of failing.
        """
        if priority not in NAMES:
            raise ValueError(f"unknown LLM priority {priority!r}")
        if priority == LIVE:
            with self._live_slot(LIVE_WAIT_S if wait_timeout is None else wait_timeout) as waited:
                yield waited
            return
        t0 = time.monotonic()
        deadline = None if wait_timeout is None else t0 + wait_timeout
        with self._cv:
            # Look-ahead is optional pre-fetching: never let it pile up. Refuse it
            # at once when another look-ahead is queued / running or anything more
            # urgent is waiting (a live set stalled behind 3 queued look-aheads).
            if priority == LOOKAHEAD and (self._busy == LOOKAHEAD or self._queue):
                _skip("busy")
                raise GateTimeout("lookahead skipped: the LLM is busy with more urgent work")
            # The live ear shares the one model (start.sh --single-omni) and answers
            # within seconds or not at all: a 20-40 s look-ahead decode would starve it.
            if priority == LOOKAHEAD and t0 < self._ear_until:
                _skip("ear")
                raise GateTimeout("lookahead skipped: the live ear is using the model")
            ticket = (priority, next(self._seq))
            self._queue.append(ticket)
            self._asked[ticket] = t0
            try:
                while True:
                    now = time.monotonic()
                    if self._may_start(ticket, now):
                        break
                    left = None if deadline is None else deadline - now
                    if left is not None and left <= 0:
                        raise GateTimeout(f"{NAMES[priority]} waited {wait_timeout:.0f}s for the LLM")
                    # a hold ends on the clock, not on a notify: re-check it
                    if now < self._live_until:
                        left = self.poll_s if left is None else min(left, self.poll_s)
                    self._cv.wait(left)
            except BaseException:
                self._queue.remove(ticket)
                self._asked.pop(ticket, None)
                self._cv.notify_all()
                raise
            self._queue.remove(ticket)
            self._asked.pop(ticket, None)
            self._busy = priority
        try:
            yield time.monotonic() - t0
        finally:
            with self._cv:
                self._busy = None
                self._cv.notify_all()

    @contextmanager
    def _live_slot(self, wait_s: float) -> Iterator[float]:
        t0 = time.monotonic()
        with self._cv:
            self._live_waiting += 1
            try:
                while self._busy is not None or self._live_busy:
                    left = t0 + wait_s - time.monotonic()
                    if left <= 0:
                        break
                    self._cv.wait(left)
                if self._live_busy:              # live_ear.py refuses a second call itself
                    raise GateTimeout("live ear: the previous phrase is still being heard")
            finally:
                self._live_waiting -= 1
            self._live_busy = True
        try:
            yield time.monotonic() - t0
        finally:
            with self._cv:
                self._live_busy = False
                self._live_done = time.monotonic()
                self._cv.notify_all()

    # -- holds --------------------------------------------------------------
    def note_ear(self, hold_s: float = EAR_HOLD_S) -> None:
        """The (live or offline) ear is about to ask the model: hold look-aheads off."""
        with self._cv:
            self._ear_until = max(self._ear_until, time.monotonic() + hold_s)

    def note_live(self, hold_s: float = LIVE_HOLD_S) -> None:
        """A hold loop is asking the live ear: look-aheads off for EAR_HOLD_S, and no
        EAR / SUGGEST starts mid-phrase for `hold_s` (see the module docstring)."""
        with self._cv:
            now = time.monotonic()
            self._ear_until = max(self._ear_until, now + EAR_HOLD_S)
            self._live_until = max(self._live_until, now + hold_s)
            self._cv.notify_all()

    def snapshot(self) -> dict:
        with self._cv:
            queued = [NAMES[p] for p, _ in sorted(self._queue)]
            return {"in_flight": NAMES.get(self._busy), "queued": queued,
                    **({"live": True} if self._live_busy else {})}


gate = PriorityGate()
