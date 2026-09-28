"""Priority gate in front of every LLM call.

mlx_lm.server handles one request at a time. The browser now fires look-ahead
suggest calls (autopilot.js topUpPool) as well as the per-pair transition plan
(/api/autopilot/plan). With no ordering the plan queued behind look-aheads and
missed its deadline ("AI plan unavailable - rules only").

The gate lets exactly ONE LLM call run at a time and, when the slot frees,
hands it to the most urgent waiter: PLAN before SUGGEST before LOOKAHEAD,
first-come first-served within a level. A call already running is never
interrupted (the server cannot cancel a generation), so a plan waits at most
for the one call in flight.
"""
from __future__ import annotations

import heapq
import itertools
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

PLAN, EAR, SUGGEST, LOOKAHEAD = 0, 1, 2, 3   # EAR: the silent ear rating pre-planned transitions
EAR_HOLD_S = 45.0          # a hold loop asks the ear every 8-bar phrase: cover the next one


def _skip(why: str) -> None:
    try:
        from app.ui import session_log

        session_log.log("gate_skip", priority="lookahead", why=why)
    except Exception:
        pass
NAMES = {PLAN: "plan", EAR: "ear", SUGGEST: "suggest", LOOKAHEAD: "lookahead"}


class GateTimeout(TimeoutError):
    """Waited too long for the LLM slot."""


class PriorityGate:
    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._busy: Optional[int] = None          # priority of the call in flight
        self._queue: list[tuple[int, int]] = []   # heap of (priority, seq)
        self._seq = itertools.count()
        self._ear_until = 0.0                    # monotonic time the live ear holds the model until

    @contextmanager
    def slot(self, priority: int, wait_timeout: Optional[float] = None) -> Iterator[float]:
        """Block until this call may use the LLM. Yields the seconds waited."""
        if priority not in NAMES:
            raise ValueError(f"unknown LLM priority {priority!r}")
        t0 = time.monotonic()
        deadline = None if wait_timeout is None else t0 + wait_timeout
        with self._cv:
            # Look-ahead is optional pre-fetching: never let it pile up. Refuse it
            # at once when another look-ahead is queued / running or anything more
            # urgent is waiting (a live set stalled behind 3 queued look-aheads).
            if priority == LOOKAHEAD and (
                self._busy == LOOKAHEAD or any(p <= LOOKAHEAD for p, _ in self._queue)
            ):
                _skip("busy")
                raise GateTimeout("lookahead skipped: the LLM is busy with more urgent work")
            # The live ear shares the one model (start.sh --single-omni) and answers
            # within seconds or not at all: a 20-40 s look-ahead decode would starve it.
            if priority == LOOKAHEAD and time.monotonic() < self._ear_until:
                _skip("ear")
                raise GateTimeout("lookahead skipped: the live ear is using the model")
            ticket = (priority, next(self._seq))
            heapq.heappush(self._queue, ticket)
            try:
                while self._busy is not None or self._queue[0] != ticket:
                    left = None if deadline is None else deadline - time.monotonic()
                    if left is not None and left <= 0:
                        raise GateTimeout(f"{NAMES[priority]} waited {wait_timeout:.0f}s for the LLM")
                    self._cv.wait(left)
            except BaseException:
                self._queue.remove(ticket)
                heapq.heapify(self._queue)
                self._cv.notify_all()
                raise
            heapq.heappop(self._queue)
            self._busy = priority
        try:
            yield time.monotonic() - t0
        finally:
            with self._cv:
                self._busy = None
                self._cv.notify_all()

    def note_ear(self, hold_s: float = EAR_HOLD_S) -> None:
        """The live ear is about to (or keeps) asking the model: hold look-aheads off."""
        with self._cv:
            self._ear_until = max(self._ear_until, time.monotonic() + hold_s)

    def snapshot(self) -> dict:
        with self._cv:
            queued = [NAMES[p] for p, _ in sorted(self._queue)]
            return {"in_flight": NAMES.get(self._busy), "queued": queued}


gate = PriorityGate()
