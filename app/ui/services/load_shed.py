"""Keeps the request thread pool for work that needs it (docs: PERF_PROD_REPORT finding 3).

Starlette runs every plain `def` route on anyio's default pool of 40 threads. On prod the heavy disk routes
filled all 40, so `/api/build` and the status routes (no I/O at all) queued behind them for 1 s. Two tools:

* cheap routes are `async def` and never touch the pool; the ones that need blocking data read a `Snapshot`
  (a cached value a background thread refreshes, never the caller);
* `bounded` puts a semaphore in front of a heavy `def` route. The wait happens on the event loop (no thread
  held), and only a request that holds a slot takes a pool thread. The slot is released when the handler
  returns, not when the response body is sent, so a slow download does not hold one.

HEAVY_SLOTS = 6 of the 40 threads (the report's figure): enough that 6 disk-bound handlers overlap, small
enough that 34 threads stay free for the medium routes (list/analysis hits, plans, writes) even when all 6
are stuck on a slow mergerfs read. More than 6 concurrent heavy handlers do not finish sooner: they share
one disk and 8 cores.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import threading
import time
import weakref
from typing import Any, Callable, Optional

from starlette.concurrency import run_in_threadpool

HEAVY_SLOTS = 6

_sems: "weakref.WeakKeyDictionary[Any, asyncio.Semaphore]" = weakref.WeakKeyDictionary()
_stats_lock = threading.Lock()
stats = {"active": 0, "peak": 0}


def _sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    s = _sems.get(loop)
    if s is None:
        s = _sems[loop] = asyncio.Semaphore(HEAVY_SLOTS)
    return s


def bounded(fn: Callable) -> Callable:
    """Wrap a sync route handler: wait for one of HEAVY_SLOTS on the event loop, then run it in the pool."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        async with _sem():
            with _stats_lock:
                stats["active"] += 1
                stats["peak"] = max(stats["peak"], stats["active"])
            try:
                return await run_in_threadpool(fn, *args, **kwargs)
            finally:
                with _stats_lock:
                    stats["active"] -= 1

    # FastAPI resolves string annotations (server.py has `from __future__ import annotations`) with the
    # WRAPPER's globals, i.e. this module, where e.g. RiffRequest doesn't exist: the pydantic body was taken
    # for a query param and POST /api/riff/plan + /api/prerender answered 422. Hand it the resolved signature.
    try:
        wrapper.__signature__ = inspect.signature(fn, eval_str=True)
    except NameError:   # a name defined later in the module: leave it to FastAPI as before
        pass
    return wrapper


class Snapshot:
    """A value computed by a blocking `fn`, served without blocking. `get()` returns the last value at once;
    when it is older than `ttl` a daemon thread (not the request pool) refreshes it, one at a time."""

    def __init__(self, fn: Callable[[], Any], ttl: float, fallback: Any = None):
        self.fn, self.ttl, self.value = fn, ttl, fallback
        self.at = 0.0
        self._busy = False
        self._lock = threading.Lock()

    def refresh(self) -> Any:
        """Blocking: call from a thread (startup, tests), never from the event loop."""
        try:
            self.value = self.fn()
        finally:
            self.at = time.monotonic()
        return self.value

    def _bg(self) -> None:
        try:
            self.refresh()
        except Exception:                         # keep the last good value
            pass
        finally:
            with self._lock:
                self._busy = False

    def get(self) -> Any:
        if time.monotonic() - self.at > self.ttl:
            with self._lock:
                start, self._busy = not self._busy, True
            if start:
                threading.Thread(target=self._bg, daemon=True, name="status-snapshot").start()
        return self.value
