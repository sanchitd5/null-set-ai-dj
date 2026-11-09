"""Generic background-job registry: named jobs, an idle signal, and pause / resume / cancel / run now.

A job is a JobSpec. Its kind is one of
    periodic     runs every interval_s (and at start when run_at_start)
    idle_worker  runs whenever the server is idle; the function loops until ctx.should_stop()
    manual       runs only on "run now"
    external     owned by another module (its own thread); the registry shows its status and can pause / resume it

`idle_only` jobs never start while a set is live (or inside the DJ_IDLE_GRACE_S window after it ended) and are asked to
stop the moment a set starts: the set_lease listener flips the flag that ctx.should_stop() reads, so the stop is event
driven. Stopping is cooperative (a thread cannot be killed): a job that ignores it for DJ_JOB_CANCEL_GRACE_S is shown
as "stuck".

The scheduler is `tick(now)`; `start()` runs it on one daemon thread (`jobs-scheduler`), woken by an Event. Each run has
its own daemon thread `job-<name>`. Tests drive tick() with a fake clock and a `spawn` that runs the job inline.
This module knows nothing about atlas data and never decides what to play; it only schedules.

State and options persist in user.db (store "jobs"): job_state (paused flag, settings, checkpoint, last run) and
job_run (the newest RUN_KEEP runs per job). Progress and the log ring are memory only.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

LOG_KEEP = 200
RUN_KEEP = 200
KINDS = ("periodic", "idle_worker", "manual", "external")
STEPS = (
    """CREATE TABLE job_state (name TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0, settings TEXT, checkpoint TEXT,
        last_start REAL, last_end REAL, last_status TEXT, last_error TEXT, last_result TEXT);
    CREATE TABLE job_run (id INTEGER PRIMARY KEY, name TEXT NOT NULL, started REAL NOT NULL, ended REAL,
        status TEXT, summary TEXT);
    CREATE INDEX job_run_name ON job_run (name, id)""",
)


class JobStop(Exception):
    """A job may raise this instead of returning once ctx.should_stop() is true."""


class JobError(Exception):
    """A refused control action: http status + machine code (the API answers with both)."""

    def __init__(self, status: int, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.status, self.code, self.detail = status, code, detail or code


@dataclass
class SettingSpec:
    type: str                                   # "int" | "float" | "bool" | "str"
    default: Any
    min: Optional[float] = None
    max: Optional[float] = None


@dataclass
class JobSpec:
    name: str
    title: str
    fn: Optional[Callable[["JobContext"], Any]] = None
    kind: str = "manual"
    interval_s: float = 0.0
    idle_only: bool = False
    run_at_start: bool = False
    settings: Dict[str, SettingSpec] = field(default_factory=dict)
    status_fn: Optional[Callable[[], dict]] = None          # external jobs: their own status
    pause_fn: Optional[Callable[[], Any]] = None
    resume_fn: Optional[Callable[[], Any]] = None


@dataclass
class IdleState:
    idle: bool
    since: float
    reason: str                                 # "set_live" | "grace" | "idle"


def _coerce(spec: SettingSpec, key: str, value: Any) -> Any:
    """The validated value, or ValueError."""
    t = spec.type
    if t == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{key}: expected true or false")
        return value
    if t == "str":
        if not isinstance(value, str):
            raise ValueError(f"{key}: expected text")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key}: expected a number")
    if t == "int":
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"{key}: expected a whole number")
        value = int(value)
    else:
        value = float(value)
    if spec.min is not None and value < spec.min:
        raise ValueError(f"{key}: below the minimum {spec.min}")
    if spec.max is not None and value > spec.max:
        raise ValueError(f"{key}: above the maximum {spec.max}")
    return value


class JobContext:
    """What a running job gets. Cheap and thread-safe; every call is safe from the job's own thread."""

    def __init__(self, reg: "Registry", job: "_Job", checkpoint: dict, settings: dict):
        self._reg, self._job = reg, job
        self.checkpoint: dict = checkpoint
        self.settings: dict = settings

    def should_stop(self) -> bool:
        return self._reg._stop_wanted(self._job)

    def progress(self, done: float, total: float, note: str = "") -> None:
        self._job.progress = {"done": done, "total": total, "note": str(note)[:200]}

    def log(self, msg: str) -> None:
        self._job.logs.append({"t": self._reg.clock(), "msg": str(msg)[:500]})

    def setting(self, key: str) -> Any:
        return self._reg._setting(self._job, key)

    def save_checkpoint(self) -> None:
        self._reg._save_checkpoint(self._job, self.checkpoint)


class _Job:
    def __init__(self, spec: JobSpec):
        self.spec = spec
        self.state = "idle"
        self.paused = False
        self.running = False
        self.stop_at: Optional[float] = None       # when a stop was asked of the current run
        self.stop_reason = ""                      # "cancel" | "pause" | "busy" | "shutdown"
        self.stuck = False
        self.next_run: Optional[float] = None
        self.progress: Dict[str, Any] = {"done": 0, "total": 0, "note": ""}
        self.logs: deque = deque(maxlen=LOG_KEEP)
        self.overrides: Dict[str, Any] = {}
        self.checkpoint: dict = {}
        self.thread: Optional[threading.Thread] = None
        self.last: Dict[str, Any] = {"status": None, "started": None, "ended": None, "error": None, "summary": None}


def _json(obj: Any, limit: int = 4000) -> str:
    try:
        return json.dumps(obj, default=str)[:limit]
    except (TypeError, ValueError):
        return "{}"


class Registry:
    def __init__(self, cache_dir: Optional[Path] = None, clock: Callable[[], float] = time.time,
                 spawn: Optional[Callable[[str, Callable[[], None]], Optional[threading.Thread]]] = None,
                 lease_active: Optional[Callable[[], bool]] = None, environ=None):
        env = os.environ if environ is None else environ
        self.cache_dir = cache_dir
        self.clock = clock
        self._spawn = spawn or self._thread_spawn
        self._lease_active = lease_active or self._lease_active_default
        self.enabled = str(env.get("DJ_JOBS", "1")).strip().lower() not in ("0", "false", "off", "no")
        self.grace_s = self._num(env, "DJ_IDLE_GRACE_S", 30.0)
        self.tick_s = self._num(env, "DJ_JOBS_TICK_S", 2.0)
        self.cancel_grace_s = self._num(env, "DJ_JOB_CANCEL_GRACE_S", 30.0)
        self._jobs: Dict[str, _Job] = {}
        self._lock = threading.RLock()              # in-memory state; never held across a job's fn
        self._db_lock = threading.Lock()
        self._wake = threading.Event()
        self._halt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._busy = False                          # a set is live (kept fresh by the lease listener and tick)
        self._free_since: Optional[float] = clock()
        self._listening = False

    @staticmethod
    def _num(env, name: str, default: float) -> float:
        try:
            return float(env.get(name) or default)
        except ValueError:
            return default

    # ---- persistence -------------------------------------------------------------------------
    def _conn(self):
        from app.music_brain import db
        conn = db.connect(db.db_path(self.cache_dir, db.USER_DB))
        db.ensure(conn, "jobs", STEPS)
        return conn

    def _db(self, fn: Callable[[Any], Any]) -> Any:
        from app.music_brain import db
        with self._db_lock:
            conn = self._conn()
            with db.tx(conn):
                return fn(conn)

    def _load(self, job: _Job) -> None:
        try:
            with self._db_lock:
                row = self._conn().execute(
                    "SELECT paused, settings, checkpoint, last_start, last_end, last_status, last_error, last_result "
                    "FROM job_state WHERE name = ?", (job.spec.name,)).fetchone()
        except Exception as e:                      # a broken db never stops the server
            print(f"[jobs] load {job.spec.name} failed: {type(e).__name__}: {e}", flush=True)
            return
        if not row:
            return
        job.paused = bool(row[0])
        for col, attr in ((row[1], "overrides"), (row[2], "checkpoint")):
            try:
                v = json.loads(col) if col else {}
                if isinstance(v, dict):
                    setattr(job, attr, v)
            except ValueError:
                pass
        try:
            summary = json.loads(row[7]) if row[7] else None
        except ValueError:
            summary = None
        job.last = {"status": row[5], "started": row[3], "ended": row[4], "error": row[6], "summary": summary}

    def _save(self, job: _Job, **cols: Any) -> None:
        names = ("paused", "settings", "checkpoint", "last_start", "last_end", "last_status", "last_error", "last_result")
        cols.setdefault("paused", int(job.paused))

        def w(conn):
            conn.execute("INSERT OR IGNORE INTO job_state (name) VALUES (?)", (job.spec.name,))
            for k, v in cols.items():
                assert k in names
                conn.execute(f"UPDATE job_state SET {k} = ? WHERE name = ?", (v, job.spec.name))
        try:
            self._db(w)
        except Exception as e:
            print(f"[jobs] save {job.spec.name} failed: {type(e).__name__}: {e}", flush=True)

    def _record_run(self, job: _Job, started: float, ended: float, status: str, summary: str) -> None:
        def w(conn):
            conn.execute("INSERT INTO job_run (name, started, ended, status, summary) VALUES (?, ?, ?, ?, ?)",
                         (job.spec.name, started, ended, status, summary[:500]))
            conn.execute("DELETE FROM job_run WHERE name = ? AND id NOT IN "
                         "(SELECT id FROM job_run WHERE name = ? ORDER BY id DESC LIMIT ?)", (job.spec.name, job.spec.name, RUN_KEEP))
        try:
            self._db(w)
        except Exception as e:
            print(f"[jobs] run record {job.spec.name} failed: {type(e).__name__}: {e}", flush=True)

    def runs(self, name: str, n: int = 20) -> List[dict]:
        self._get(name)
        with self._db_lock:
            rows = self._conn().execute("SELECT started, ended, status, summary FROM job_run WHERE name = ? "
                                        "ORDER BY id DESC LIMIT ?", (name, int(n))).fetchall()
        return [{"started": r[0], "ended": r[1], "status": r[2], "summary": r[3]} for r in rows]

    # ---- registration ------------------------------------------------------------------------
    def register(self, spec: JobSpec) -> None:
        if spec.kind not in KINDS:
            raise ValueError(f"{spec.name}: unknown kind {spec.kind!r}")
        if spec.kind != "external" and spec.fn is None:
            raise ValueError(f"{spec.name}: a {spec.kind} job needs fn")
        if spec.kind == "periodic" and spec.interval_s <= 0:
            raise ValueError(f"{spec.name}: a periodic job needs interval_s > 0")
        job = _Job(spec)
        self._load(job)
        now = self.clock()
        if spec.kind == "periodic":
            last = job.last.get("started")
            job.next_run = now if spec.run_at_start else (float(last) if last else now) + spec.interval_s
        if job.paused and spec.kind != "external":
            job.state = "paused"
        if spec.kind == "external" and job.paused and spec.pause_fn:
            self._call(spec.pause_fn, spec.name)    # a pause outlives a restart
        with self._lock:
            self._jobs[spec.name] = job
        self._wake.set()

    @staticmethod
    def _call(fn: Callable, name: str) -> None:
        try:
            fn()
        except Exception as e:
            print(f"[jobs] {name} control failed: {type(e).__name__}: {e}", flush=True)

    def _get(self, name: str) -> _Job:
        job = self._jobs.get(name)
        if job is None:
            raise JobError(404, "unknown_job", f"no job {name}")
        return job

    # ---- idle signal -------------------------------------------------------------------------
    @staticmethod
    def _lease_active_default() -> bool:
        from app.ui.services import set_lease
        return bool(set_lease.store().status()["active"])

    def _on_lease(self) -> None:
        """set_lease listener: a set started or ended. Flips _busy now, so running idle_only jobs stop within a tick."""
        try:
            self._observe(self.clock())
        finally:
            self._wake.set()

    def _observe(self, now: float) -> bool:
        active = bool(self._lease_active())
        with self._lock:
            if active:
                self._busy, self._free_since = True, None
            else:
                if self._busy or self._free_since is None:
                    self._free_since = now
                self._busy = False
        return active

    def idle(self, now: Optional[float] = None) -> IdleState:
        now = self.clock() if now is None else now
        if self._observe(now):
            return IdleState(False, now, "set_live")
        since = self._free_since if self._free_since is not None else now
        if now - since >= self.grace_s:
            return IdleState(True, since, "idle")
        return IdleState(False, since, "grace")

    def _stop_wanted(self, job: _Job) -> bool:
        return job.stop_at is not None or (job.spec.idle_only and self._busy)

    # ---- scheduler ---------------------------------------------------------------------------
    def _blocked(self, job: _Job) -> bool:
        return not self.enabled and job.spec.kind != "external"

    def tick(self, now: Optional[float] = None) -> None:
        """One scheduler pass: stop what must stop, mark stuck runs, start what is due."""
        now = self.clock() if now is None else now
        idle = self.idle(now)
        for job in list(self._jobs.values()):
            spec = job.spec
            if spec.kind == "external" or self._blocked(job):
                continue
            if job.running:
                if spec.idle_only and not idle.idle and job.stop_at is None and self._busy:
                    self._ask_stop(job, "busy", now)
                if job.stop_at is not None and not job.stuck and now - job.stop_at >= self.cancel_grace_s:
                    job.stuck = True
                    job.state = "stuck"
                continue
            if job.paused or spec.kind == "manual":
                continue
            if spec.idle_only and not idle.idle:
                continue
            if spec.kind == "periodic" and (job.next_run is None or now < job.next_run):
                continue
            if spec.kind == "idle_worker" and job.next_run is not None and now < job.next_run:
                continue
            self._start(job, now)

    def _ask_stop(self, job: _Job, reason: str, now: float) -> None:
        job.stop_at, job.stop_reason = now, reason
        if job.state == "running":
            job.state = "cancelling"

    def _start(self, job: _Job, now: float) -> None:
        with self._lock:
            if job.running:
                raise JobError(409, "running", "already running")
            job.running, job.stuck, job.stop_at, job.stop_reason = True, False, None, ""
            job.state = "running"
            job.progress = {"done": 0, "total": 0, "note": ""}
            job.last = dict(job.last, started=now)
        self._save(job, last_start=now)
        spec = job.spec
        job.thread = self._spawn(f"job-{spec.name}", lambda: self._run(job, now))

    def _thread_spawn(self, name: str, fn: Callable[[], None]) -> threading.Thread:
        t = threading.Thread(target=fn, daemon=True, name=name)
        t.start()
        return t

    def _run(self, job: _Job, started: float) -> None:
        spec = job.spec
        ctx = JobContext(self, job, dict(job.checkpoint), self._effective(job))
        status, error, result = "ok", None, None
        try:
            result = spec.fn(ctx)
            if self._stop_wanted(job):
                status = "stopped"
        except JobStop:
            status = "stopped"
        except BaseException as e:                  # a job error is stored and shown, never raised into the runner
            status, error = "error", f"{type(e).__name__}: {str(e)[:300]}"
            ctx.log(f"error: {error}")
            print(f"[jobs] {spec.name} failed: {error}", flush=True)
        ended = self.clock()
        with self._lock:
            reason = job.stop_reason
            if status == "stopped":
                status = {"cancel": "cancelled", "pause": "paused"}.get(reason, "stopped")
            job.checkpoint = ctx.checkpoint if isinstance(ctx.checkpoint, dict) else {}
            summary = result if isinstance(result, (dict, str, int, float, bool, list)) else None
            job.last = {"status": status, "started": started, "ended": ended, "error": error, "summary": summary}
            job.running, job.stop_at, job.stop_reason, job.stuck = False, None, "", False
            job.thread = None
            job.state = "paused" if job.paused else "error" if status == "error" else "idle"
            if spec.kind == "periodic":
                job.next_run = ended if status == "stopped" else ended + spec.interval_s   # pushed out by a set: retry when idle
            elif spec.kind == "idle_worker":
                job.next_run = ended + max(spec.interval_s, self.tick_s)
        self._save(job, checkpoint=_json(job.checkpoint, 20000), last_end=ended, last_status=status, last_error=error,
                   last_result=_json(summary, 1000) if summary is not None else None)
        self._record_run(job, started, ended, status, error or (_json(summary, 300) if summary is not None else ""))
        self._wake.set()

    # ---- controls ----------------------------------------------------------------------------
    def run_now(self, name: str) -> dict:
        job = self._get(name)
        spec = job.spec
        if spec.kind == "external" or spec.fn is None:
            raise JobError(409, "not_supported", "this job has no run now")
        if not self.enabled:
            raise JobError(409, "disabled", "the job runner is off (DJ_JOBS=0)")
        if job.running:
            raise JobError(409, "running", "already running")
        if job.paused:
            raise JobError(409, "paused", "paused: resume it first")
        if spec.idle_only and not self.idle().idle:
            raise JobError(409, "not_idle", "a set is live (or just ended)")
        self._start(job, self.clock())
        return self.view(name)

    def pause(self, name: str) -> dict:
        job = self._get(name)
        spec = job.spec
        if spec.kind == "external":
            if spec.pause_fn is None:
                raise JobError(409, "not_supported", "this job cannot be paused")
            self._call(spec.pause_fn, name)
        with self._lock:
            job.paused = True
            if job.running:
                self._ask_stop(job, "pause", self.clock())
            elif spec.kind != "external":
                job.state = "paused"
        self._save(job, paused=1)
        self._wake.set()
        return self.view(name)

    def resume(self, name: str) -> dict:
        job = self._get(name)
        spec = job.spec
        if spec.kind == "external":
            if spec.resume_fn is None:
                raise JobError(409, "not_supported", "this job cannot be resumed")
            self._call(spec.resume_fn, name)
        with self._lock:
            job.paused = False
            if job.running and job.stop_reason == "pause":
                job.stop_at, job.stop_reason = None, ""         # resumed before it stopped: let it carry on
                job.state = "running"
            elif not job.running and spec.kind != "external":
                job.state = "idle"
        self._save(job, paused=0)
        self._wake.set()
        return self.view(name)

    def cancel(self, name: str) -> dict:
        job = self._get(name)
        if job.spec.kind == "external":
            raise JobError(409, "not_supported", "this job cannot be cancelled")
        if not job.running:
            raise JobError(409, "not_running", "not running")
        with self._lock:
            self._ask_stop(job, "cancel", self.clock())
        self._wake.set()
        return self.view(name)

    # ---- settings ----------------------------------------------------------------------------
    def _effective(self, job: _Job) -> dict:
        return {k: job.overrides.get(k, s.default) for k, s in job.spec.settings.items()}

    def _setting(self, job: _Job, key: str) -> Any:
        if key not in job.spec.settings:
            raise KeyError(key)
        return job.overrides.get(key, job.spec.settings[key].default)

    def set_settings(self, name: str, values: dict) -> dict:
        job = self._get(name)
        if not isinstance(values, dict):
            raise ValueError("settings must be an object")
        clean = {}
        for k, v in values.items():
            spec = job.spec.settings.get(k)
            if spec is None:
                raise ValueError(f"unknown setting {k!r}")
            clean[k] = _coerce(spec, k, v)
        with self._lock:
            job.overrides.update(clean)
            merged = dict(job.overrides)
        self._save(job, settings=_json(merged))
        return self.view(name)

    def _save_checkpoint(self, job: _Job, checkpoint: dict) -> None:
        job.checkpoint = dict(checkpoint)
        self._save(job, checkpoint=_json(job.checkpoint, 20000))

    # ---- views -------------------------------------------------------------------------------
    def log_tail(self, name: str, n: int = 100) -> List[dict]:
        job = self._get(name)
        rows = list(job.logs)
        return rows[-max(0, int(n)):] if n else []

    def view(self, name: str) -> dict:
        job = self._get(name)
        spec = job.spec
        ext = None
        state, progress = job.state, dict(job.progress)
        if spec.kind == "external":
            try:
                ext = spec.status_fn() if spec.status_fn else {}
            except Exception as e:
                ext = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
            state = "paused" if job.paused else "running" if ext.get("busy") else "idle"
            if "done" in ext and "queued" in ext:
                progress = {"done": ext["done"], "total": ext["done"] + ext["queued"], "note": str(ext.get("busy") or "")}
        elif self._blocked(job):
            state = "disabled"
        running = job.running
        can_run = (spec.kind != "external" and spec.fn is not None and self.enabled and not running and not job.paused)
        view = {
            "name": spec.name, "title": spec.title, "kind": spec.kind, "state": state, "idle_only": spec.idle_only,
            "paused": job.paused, "progress": progress,
            "last": dict(job.last), "next_run": job.next_run if spec.kind == "periodic" and not job.paused else None,
            "settings": {k: {"value": job.overrides.get(k, s.default), "default": s.default, "min": s.min, "max": s.max,
                             "type": s.type} for k, s in spec.settings.items()},
            "controls": {"pause": not job.paused and (spec.kind != "external" or spec.pause_fn is not None),
                         "resume": job.paused and (spec.kind != "external" or spec.resume_fn is not None),
                         "cancel": running and spec.kind != "external" and job.stop_at is None,
                         "run": can_run},
        }
        if ext is not None:
            view["external"] = ext
        return view

    def views(self) -> List[dict]:
        return [self.view(n) for n in list(self._jobs)]

    # ---- lifecycle ---------------------------------------------------------------------------
    def start(self) -> None:
        """Attach to the lease and start the scheduler thread (a no-op when DJ_JOBS=0 or already started)."""
        from app.ui.services import set_lease
        set_lease.add_listener(self._on_lease)
        self._listening = True
        if not self.enabled or self._thread is not None:
            return
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="jobs-scheduler")
        self._thread.start()

    def _loop(self) -> None:
        while not self._halt.is_set():
            try:
                self.tick()
            except Exception as e:                  # the scheduler never dies on a bad pass
                print(f"[jobs] tick failed: {type(e).__name__}: {e}", flush=True)
            self._wake.wait(self.tick_s)
            self._wake.clear()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the scheduler and ask every running job to stop, then join the threads (no thread outlives this)."""
        from app.ui.services import set_lease
        if self._listening:
            set_lease.remove_listener(self._on_lease)
            self._listening = False
        self._halt.set()
        self._wake.set()
        # Join the scheduler first: until it has exited it can still start a job thread, which a snapshot taken
        # earlier would miss (that thread then outlives shutdown and never sees a stop request).
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        with self._lock:
            threads = [j.thread for j in self._jobs.values() if j.thread is not None]
            for j in self._jobs.values():
                if j.running and j.stop_at is None:
                    self._ask_stop(j, "shutdown", self.clock())
        for t in threads:
            if t is not threading.current_thread():
                t.join(timeout)


registry = Registry()           # process singleton; cache_dir None = the configured CACHE_DIR


def use(r: Optional[Registry]) -> None:
    """Swap the process-wide registry (tests; None = a fresh default one)."""
    global registry
    registry = r if r is not None else Registry()
