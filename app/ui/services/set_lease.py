"""One set at a time: the server's SET LEASE (docs/engineering/SET_LEASE.md).

The server's set state (session log, LLM gate, live ear, set memory, booking / preplan state, stem
priority, prerender list) assumes ONE running set. A client that starts a set takes the lease; a
second client gets 409 with who holds it and since when, and may take it over explicitly.

    POST   /api/set/lease                {client, device_name} -> {lease} | 409 {holder, since}
    POST   /api/set/lease/{id}/beat      every ~15 s; 60 s without one and the lease lapses
    DELETE /api/set/lease/{id}           release (POST /api/set/lease/{id}/release: same, for sendBeacon)
    POST   /api/set/lease/takeover       {client, device_name} -> {lease, previous}; the old holder's
                                         next beat answers 409 "taken over"
    GET    /api/set/lease                {active, lease (no id)}

Every route sits behind the owner auth (cookie or device token), so "owner only" holds for all of
them. The lease id is the capability: set-scoped routes (SET_SCOPED) need `X-Set-Lease: <id>` while
a lease is active, else 409. With no active lease they are open (plain local use, the sim, tests).
A stem split asked without the lease while another set runs is queued at backfill priority, never
ahead of the holder's decks (`may_lead`).

State is one small JSON file (atomic write) so a restart keeps the holder; times are wall-clock.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

TTL_S = 60.0
BEAT_S = 15.0
HEADER = "x-set-lease"
CLIENTS = ("console", "phone")
MAX_TAKEN = 20
MAX_HISTORY = 50                                 # closed sets kept for the admin page
try:                                             # one value for Dart, Python and JS (dj-config SESSION_PAUSE_CLOSE_MIN)
    from app.music_brain.generated.dj_config import SESSION_PAUSE_CLOSE_MIN as SESSION_CLOSE_MIN
except ImportError:                              # pragma: no cover - generated file missing: the documented default
    SESSION_CLOSE_MIN = 15

# (method, path regex): the routes that act on the running set's server state.
SET_SCOPED = [(m, re.compile(p)) for m, p in (
    ("POST", r"^/api/autopilot/(suggest|plan|vet)$"),
    ("POST", r"^/api/transition/preplan$"),
    ("POST", r"^/api/merge/audition$"),
    ("GET", r"^/api/learned/pick$"),
    ("POST", r"^/api/riff/plan$"),
    ("POST", r"^/api/riff/[^/]+/balance$"),
    ("POST", r"^/api/live/ear$"),
    ("POST", r"^/api/session/(event|events|steps)$"),
    ("POST", r"^/api/prerender$"),
)]


def is_set_scoped(method: str, path: str) -> bool:
    return any(method == m and rx.match(path) for m, rx in SET_SCOPED)


class LeaseStore:
    """{"current": lease|null, "taken_over": [{id, at, why?}], "history": [closed sets, newest last]}. A lease is active while now < deadline;
    a lapsed one stays as `current` until someone else takes the set (its holder's next beat
    revives it)."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time,
                 log: Optional[Callable[..., None]] = None):
        self.path = Path(path)
        self.clock = clock
        self.log = log or _session_log
        self._lock = threading.Lock()            # state only: never held across I/O
        self._io_lock = threading.Lock()         # one file writer at a time
        self._ver = 0
        self._written = 0
        self._data = self._load()

    # -- io
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError):
            data = {}                             # a corrupt file: no lease (fail open, crash-safe)
        cur = data.get("current")
        return {"current": cur if isinstance(cur, dict) and cur.get("id") else None,
                "taken_over": [t for t in data.get("taken_over") or [] if isinstance(t, dict)][-MAX_TAKEN:],
                "history": [h for h in data.get("history") or [] if isinstance(h, dict)][-MAX_HISTORY:]}

    def _snap(self):
        """(version, json text) of the state. Caller holds self._lock; memory only, no I/O."""
        self._ver += 1
        return self._ver, json.dumps(self._data, indent=1)

    def _flush(self, snap, defer: bool = False) -> None:
        """Write a `_snap()` to disk, NEVER under self._lock (Gate.allows runs on the event loop for every
        /api request and takes that lock; an fsync on mergerfs took up to 1 s). Writers serialise on
        _io_lock and a snapshot older than the last one written is dropped, so the file only moves forward.
        defer: write from a short thread, for a caller that must not wait for the disk (the 15 s beat)."""
        if defer:
            threading.Thread(target=self._flush, args=(snap,), daemon=True, name="lease-write").start()
            return
        ver, text = snap
        with self._io_lock:
            if ver <= self._written:
                return
            self._save(text)
            self._written = ver

    def _save(self, text: str) -> None:
        """Atomic: temp file + fsync + rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".set_lease-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- views
    def _active(self, now: float) -> Optional[dict]:
        cur = self._data["current"]
        return cur if cur and now < float(cur.get("deadline", 0)) else None

    @staticmethod
    def public(lease: dict) -> dict:
        h = lease.get("holder") or {}
        return {"client": lease.get("client"), "device_name": lease.get("device_name"),
                "kind": h.get("kind"), "name": h.get("name"), "since": lease.get("started"),
                "deadline": lease.get("deadline")}

    @staticmethod
    def owned(lease: dict) -> dict:
        """What the holder gets: the id plus the timing it must keep."""
        return {"id": lease["id"], "client": lease["client"], "device_name": lease["device_name"],
                "holder": {k: v for k, v in (lease.get("holder") or {}).items() if k in ("kind", "name")},
                "started": lease["started"], "deadline": lease["deadline"], "ttl_s": TTL_S, "beat_s": BEAT_S}

    def conflict(self, lease: dict, holder: Optional[dict] = None, detail: str = "a set is running") -> dict:
        same = bool(holder and holder.get("key") and holder.get("key") == (lease.get("holder") or {}).get("key"))
        return {"detail": detail, "code": "set_lease", "holder": self.public(lease),
                "since": lease.get("started"), "same_holder": same}

    def status(self) -> dict:
        with self._lock:
            cur = self._active(self.clock())
            return {"active": cur is not None, "lease": self.public(cur) if cur else None}

    # -- changes
    def _new(self, client: str, device_name: str, holder: dict, now: float) -> dict:
        return {"id": secrets.token_urlsafe(18), "client": client, "device_name": device_name,
                "holder": holder, "started": round(now, 3), "deadline": round(now + TTL_S, 3)}

    def _end(self, cur: dict, reason: str, now: float) -> None:
        """Record `cur` as a closed set (reason: released, taken_over, lapsed, paused_timeout, admin).
        Caller holds self._lock and snapshots afterwards. The lease id is stored only as an 8 hex hash."""
        h = cur.get("holder") or {}
        entry = {"id_hash": hashlib.sha256(str(cur.get("id")).encode()).hexdigest()[:8],
                 "client": cur.get("client"), "device_name": cur.get("device_name"),
                 "holder": {"kind": h.get("kind"), "name": h.get("name")},
                 "started": cur.get("started"), "ended": round(now, 3), "reason": reason}
        self._data["history"] = (self._data["history"] + [entry])[-MAX_HISTORY:]

    def acquire(self, client: str, device_name: str, holder: dict):
        """(lease, None) granted, or (None, conflict body)."""
        with self._lock:
            now = self.clock()
            cur = self._active(now)
            if cur is not None:
                return None, self.conflict(cur, holder)
            old = self._data["current"]
            if old:
                self._end(old, "lapsed", float(old.get("deadline", now)))
            lease = self._data["current"] = self._new(client, device_name, holder, now)
            snap = self._snap()
        self._flush(snap)
        _notify()
        if old:
            self.log("set_lease", action="lapsed", client=old.get("client"), device=old.get("device_name"),
                     since=old.get("started"))
        self.log("set_lease", action="grant", client=client, device=device_name, holder=holder.get("kind"))
        return lease, None

    def takeover(self, client: str, device_name: str, holder: dict):
        """(lease, previous public view | None). The old id is remembered as taken over."""
        with self._lock:
            now = self.clock()
            old = self._data["current"]
            prev = self.public(old) if old and self._active(now) else None
            if old:
                self._end(old, "taken_over", now)
                self._data["taken_over"] = (self._data["taken_over"] + [{"id": old["id"], "at": round(now, 3)}])[-MAX_TAKEN:]
            lease = self._data["current"] = self._new(client, device_name, holder, now)
            snap = self._snap()
        self._flush(snap)
        _notify()
        self.log("set_lease", action="takeover", client=client, device=device_name, holder=holder.get("kind"),
                 previous=(prev or {}).get("device_name"), previous_client=(prev or {}).get("client"))
        return lease, prev

    def beat(self, lease_id: str, defer: bool = False):
        """(lease, None) renewed, or (None, 409 body). A lapsed lease nobody else took is revived.
        defer: the file write goes to a thread, so an async route can call this without touching the disk."""
        with self._lock:
            now = self.clock()
            cur = self._data["current"]
            if cur and secrets.compare_digest(str(cur["id"]), str(lease_id)):
                revived = now >= float(cur["deadline"])
                cur["deadline"] = round(now + TTL_S, 3)
                snap = self._snap()
                lease = dict(cur)
            else:
                row = next((t for t in self._data["taken_over"]
                            if secrets.compare_digest(str(t.get("id")), str(lease_id))), None)
                detail = "lease ended" if row is None else "closed by admin" if row.get("why") == "admin" else "taken over"
                body = {"detail": detail, "code": "set_lease"}
                active = self._active(now)
                if active:
                    body.update(holder=self.public(active), since=active.get("started"))
                return None, body
        self._flush(snap, defer)
        if revived:
            self.log("set_lease", action="revived", client=lease["client"], device=lease["device_name"])
        return lease, None

    def release(self, lease_id: str) -> bool:
        with self._lock:
            cur = self._data["current"]
            if not cur or not secrets.compare_digest(str(cur["id"]), str(lease_id)):
                return False
            self._end(cur, "released", self.clock())
            self._data["current"] = None
            snap = self._snap()
        self._flush(snap)
        _notify()
        self.log("set_lease", action="release", client=cur.get("client"), device=cur.get("device_name"))
        return True

    def sweep(self, now: Optional[float] = None) -> bool:
        """Close a set whose holder sent no beat for SESSION_CLOSE_MIN minutes (a dead or long-paused phone):
        the lease is removed, so its next beat answers 409 "lease ended". True when one was closed."""
        with self._lock:
            now = self.clock() if now is None else now
            cur = self._data["current"]
            if not cur or now - float(cur.get("deadline", 0)) < SESSION_CLOSE_MIN * 60 - TTL_S:
                return False
            self._end(cur, "paused_timeout", now)
            self._data["current"] = None
            snap = self._snap()
        self._flush(snap)
        _notify()
        self.log("set_lease", action="closed", client=cur.get("client"), device=cur.get("device_name"),
                 since=cur.get("started"), idle_min=SESSION_CLOSE_MIN)
        return True

    def force_close(self, reason: str = "admin") -> Optional[dict]:
        """End the live set from the admin shell: the public view of what was closed, or None when no set is
        live. The holder's next beat answers 409 "closed by admin"."""
        with self._lock:
            now = self.clock()
            cur = self._active(now)
            if cur is None:
                return None
            view = self.public(cur)
            self._end(cur, reason, now)
            self._data["taken_over"] = (self._data["taken_over"] + [{"id": cur["id"], "at": round(now, 3), "why": reason}])[-MAX_TAKEN:]
            self._data["current"] = None
            snap = self._snap()
        self._flush(snap)
        _notify()
        self.log("set_lease", action="force_close", client=cur.get("client"), device=cur.get("device_name"), reason=reason)
        return view

    def history_view(self, n: int = 20) -> list:
        """Closed sets, newest first, without any lease id."""
        with self._lock:
            rows = list(self._data["history"][-n:]) if n > 0 else []
        return [dict(r, duration_s=round(float(r.get("ended") or 0) - float(r.get("started") or 0), 1))
                for r in reversed(rows)]

    def status_admin(self) -> dict:
        """The live set for the admin page (no id): public view plus last beat age and seconds to lapse."""
        with self._lock:
            now = self.clock()
            cur = self._active(now)
            if cur is None:
                return {"active": False, "lease": None}
            view = self.public(cur)
            view["last_beat"] = round(float(cur["deadline"]) - TTL_S, 3)
            view["lapses_in"] = round(float(cur["deadline"]) - now, 1)
            return {"active": True, "lease": view}

    def allows(self, lease_id: Optional[str]):
        """None when a set-scoped request may go ahead, else the 409 body."""
        with self._lock:
            cur = self._active(self.clock())
            if cur is None or (lease_id and secrets.compare_digest(str(cur["id"]), str(lease_id))):
                return None
            return self.conflict(cur)


def _session_log(kind: str, **fields) -> None:
    try:
        from app.ui.services import session_log
        session_log.log(kind, **fields)
    except Exception:                                # the log never blocks the lease
        pass


_listeners: list = []


def add_listener(fn: Callable[[], None]) -> None:
    """Call fn() after the lease changes hands (acquire, takeover, release, sweep, force close). Runs outside the
    store lock, on the caller's thread; an exception is logged and swallowed. Process-wide: it follows use()."""
    if fn not in _listeners:
        _listeners.append(fn)


def remove_listener(fn: Callable[[], None]) -> None:
    if fn in _listeners:
        _listeners.remove(fn)


def _notify() -> None:
    for fn in list(_listeners):
        try:
            fn()
        except Exception as e:                   # a listener never breaks a lease change
            print(f"[set_lease] listener failed: {type(e).__name__}: {e}", flush=True)


_store: Optional[LeaseStore] = None
_store_lock = threading.Lock()


def default_path() -> Path:
    from app.music_brain.config import CACHE_DIR
    return Path(CACHE_DIR) / "set_lease.json"


def store() -> LeaseStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = LeaseStore(default_path())
        return _store


def use(s: Optional[LeaseStore]) -> None:
    """Swap the process-wide store (tests; None = rebuild from the default path on next use)."""
    global _store
    with _store_lock:
        _store = s


# ---- request gate ------------------------------------------------------------------------------

_may_lead: contextvars.ContextVar[bool] = contextvars.ContextVar("set_lease_may_lead", default=True)


def may_lead() -> bool:
    """False inside a request from a client that does not hold the active lease: its stem splits go
    to backfill, never ahead of the running set's decks. True everywhere else (background threads)."""
    return _may_lead.get()


def _header(scope) -> Optional[str]:
    for k, v in scope.get("headers") or []:
        if k.decode("latin-1").lower() == HEADER:
            return v.decode("latin-1").strip() or None
    return None


class Gate:
    """Pure ASGI. Installed inside the auth middleware, so only an authenticated request ever
    learns who holds the set."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            return await self.app(scope, receive, send)
        refusal = store().allows(_header(scope))
        if refusal is not None and is_set_scoped(scope.get("method", "GET"), scope.get("path", "")):
            body = json.dumps(refusal).encode()
            await send({"type": "http.response.start", "status": 409, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store")]})
            return await send({"type": "http.response.body", "body": body})
        token = _may_lead.set(refusal is None)
        try:
            return await self.app(scope, receive, send)
        finally:
            _may_lead.reset(token)


# ---- routes ------------------------------------------------------------------------------------

router = APIRouter()


class LeaseRequest(BaseModel):
    client: str = Field(default="console", max_length=16)
    device_name: str = Field(default="", max_length=200)


def holder_of(request: Request) -> dict:
    """Who asks: the device token (id, name) or the browser session the auth middleware saw."""
    a = request.scope.get("dj_auth") or {"kind": "local", "key": "local", "name": "this machine"}
    return {"kind": a.get("kind"), "key": a.get("key"), "name": a.get("name")}


def _clean(req: LeaseRequest, holder: dict):
    client = (req.client or "").strip().lower()
    if client not in CLIENTS:
        raise HTTPException(status_code=422, detail=f"client: one of {', '.join(CLIENTS)}")
    name = " ".join((req.device_name or "").split())[:64] or str(holder.get("name") or client)[:64]
    return client, name


SWEEP_EVERY_S = 60.0


def sweep_once() -> bool:
    """One sweep of the process store; a closed set publishes what it taught the server, like a release."""
    closed = store().sweep()
    if closed:
        from app.music_brain.matching import knowledge_publish
        knowledge_publish.on_set_end(log=lambda m: print(m, flush=True))
    return closed


def force_close_once(reason: str = "admin") -> Optional[dict]:
    """Admin close of the process store's live set: same end-of-set publish as the sweep. The closed set's public view, or None."""
    view = store().force_close(reason)
    if view:
        from app.music_brain.matching import knowledge_publish
        knowledge_publish.on_set_end(log=lambda m: print(m, flush=True))
    return view


def start_sweeper(every_s: float = SWEEP_EVERY_S) -> threading.Thread:
    """Daemon thread: sweep_once() every [every_s] seconds, never dies on an error."""
    def run() -> None:
        while True:
            time.sleep(every_s)
            try:
                sweep_once()
            except Exception as e:               # keep sweeping
                print(f"[set_lease] sweep failed: {e}", flush=True)
    t = threading.Thread(target=run, daemon=True, name="lease-sweep")
    t.start()
    return t


@router.get("/api/set/lease")
async def get_lease():                         # memory only (the lock is never held across I/O)
    return store().status()


@router.post("/api/set/lease")
def post_lease(req: LeaseRequest, request: Request):
    holder = holder_of(request)
    client, name = _clean(req, holder)
    lease, conflict = store().acquire(client, name, holder)
    if conflict:
        return JSONResponse(conflict, status_code=409)
    return {"lease": LeaseStore.owned(lease)}


@router.post("/api/set/lease/takeover")
def post_takeover(req: LeaseRequest, request: Request):
    holder = holder_of(request)
    client, name = _clean(req, holder)
    lease, prev = store().takeover(client, name, holder)
    return {"lease": LeaseStore.owned(lease), "previous": prev}


@router.post("/api/set/lease/{lease_id}/beat")
async def post_beat(lease_id: str):            # no pool thread: the state moves in memory, the write is deferred
    lease, refusal = store().beat(lease_id, defer=True)
    if refusal:
        return JSONResponse(refusal, status_code=409)
    return {"lease": LeaseStore.owned(lease)}


@router.delete("/api/set/lease/{lease_id}")
@router.post("/api/set/lease/{lease_id}/release")
def delete_lease(lease_id: str):
    released = store().release(lease_id)
    if released:                                # the set ended: publish what it taught the server (opt-in, background)
        from app.music_brain.matching import knowledge_publish
        knowledge_publish.on_set_end(log=lambda m: print(m, flush=True))
    return {"ok": True, "released": released}
