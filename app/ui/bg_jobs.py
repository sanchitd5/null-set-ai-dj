"""Background jobs for slow audio work off the request threads.

POST /api/transition/preplan and /api/merge/audition render clips from stems
and ask the silent ear, which took up to ~27 s inside a FastAPI sync handler
(a thread-pool thread held for the whole call). Now the POST starts (or joins)
a job keyed by its inputs and returns at once; a GET on the job id returns the
result when it is done.

* Dedupe: a request whose key matches a pending, running or fresh finished job
  joins it instead of starting the same work twice.
* Bounded: `workers` jobs run at a time (1 for the ear: one clip rating at a
  time on the shared model) and at most `max_pending` wait; past that submit()
  returns None and the caller answers "busy".
* Expiry: finished jobs are kept `ttl_s`, then forgotten. A job still queued
  `queue_ttl_s` after it was asked for is dropped unrun (nobody waits for it).
"""
from __future__ import annotations

import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

PENDING, RUNNING, DONE, ERROR, EXPIRED = "pending", "running", "done", "error", "expired"


@dataclass
class Job:
    id: str
    key: str
    status: str = PENDING
    result: Any = None
    error: str = ""
    created: float = 0.0
    started: Optional[float] = None
    finished: Optional[float] = None
    fn: Optional[Callable[[], Any]] = field(default=None, repr=False)

    @property
    def active(self) -> bool:
        return self.status in (PENDING, RUNNING)

    def pending_body(self) -> dict:
        return {"status": "pending", "job": self.id}


class JobRunner:
    def __init__(self, name: str, workers: int = 1, ttl_s: float = 300.0, queue_ttl_s: float = 120.0,
                 max_pending: int = 8, clock: Callable[[], float] = time.monotonic) -> None:
        self.name, self.ttl_s, self.queue_ttl_s, self.max_pending = name, ttl_s, queue_ttl_s, max_pending
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: Dict[str, Job] = {}
        self._by_key: Dict[str, str] = {}
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix=f"job-{name}")

    def submit(self, key: str, fn: Callable[[], Any]) -> Optional[Job]:
        """The job for `key`: an active or fresh finished one, else a new one. None when full."""
        with self._lock:
            self._purge()
            jid = self._by_key.get(key)
            j = self._jobs.get(jid) if jid else None
            if j and (j.active or j.status == DONE):
                return j
            if sum(1 for x in self._jobs.values() if x.status == PENDING) >= self.max_pending:
                return None
            j = Job(id=secrets.token_hex(8), key=key, created=self._clock(), fn=fn)
            self._jobs[j.id] = j
            self._by_key[key] = j.id
        self._pool.submit(self._run, j)
        return j

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            self._purge()
            return self._jobs.get(job_id)

    def forget(self, job_id: str) -> None:
        """Drop a finished job (its result went stale) so the next submit reruns the work."""
        with self._lock:
            j = self._jobs.get(job_id)
            if j and not j.active:
                self._drop(j)

    def _run(self, j: Job) -> None:
        with self._lock:
            if self._clock() - j.created > self.queue_ttl_s:
                j.status, j.finished, j.fn = EXPIRED, self._clock(), None
                return
            j.status, j.started = RUNNING, self._clock()
        try:
            res, err, status = j.fn(), "", DONE
        except Exception as exc:                 # reported on GET, never kills the worker
            res, err, status = None, f"{type(exc).__name__}: {exc}", ERROR
        with self._lock:
            j.result, j.error, j.status, j.finished, j.fn = res, err, status, self._clock(), None

    def _drop(self, j: Job) -> None:
        self._jobs.pop(j.id, None)
        if self._by_key.get(j.key) == j.id:
            self._by_key.pop(j.key, None)

    def _purge(self) -> None:
        now = self._clock()
        for j in [x for x in self._jobs.values() if x.finished is not None and now - x.finished > self.ttl_s]:
            self._drop(j)
