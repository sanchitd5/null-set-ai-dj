"""The ONE adapter between the public server and the dj-core decision service.

All pick / mix rules live in dj-core (private repo). The server never keeps a second implementation of them: it spawns the
compiled `dj-core-svc` executable (built and published by dj-core CI, dropped on the host by the deploy, path in the env var
DJ_CORE_SVC, default /opt/null-set-ai-dj/bin/dj-core-svc) and talks JSON lines over its stdin / stdout:

    request  {"id": n, "fn": "<name>", "args": {...}}
    response {"id": n, "ok": true, "result": ...}  or  {"id": n, "ok": false, "error": "..."}

Nothing of dj-core's source or data is in this repo, only this client. The child lives as long as the server (it exits on stdin
EOF), is started on first use, is restarted after a crash or a timeout (with a 1 s back-off), and is shared by all threads
behind one lock. When the service cannot be reached the call RAISES DjCoreUnavailable and logs loudly: there is no Python
fallback, because a silent second implementation is exactly the duplicate this bridge removes.
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import threading
import time
from typing import Any, List, Optional

PROTOCOL = 1                                  # the wire version this client speaks; the service says its own in its hello line
DEFAULT_BIN = "/opt/null-set-ai-dj/bin/dj-core-svc"
SKIP_REASON = "needs_djcore: the dj-core service executable is not installed (set DJ_CORE_SVC to a dj-core-svc build)"
CALL_TIMEOUT_S = 5.0                          # a decision is ~0.1 ms; anything near this is a hung child
START_TIMEOUT_S = 10.0
RESTART_BACKOFF_S = 1.0


class DjCoreUnavailable(RuntimeError):
    """The dj-core service is missing, crashed, timed out or speaks another protocol."""


class DjCoreError(RuntimeError):
    """The service answered {"ok": false}: the call itself was rejected (bad args, unknown fn)."""


def binary_path() -> str:
    return os.environ.get("DJ_CORE_SVC") or DEFAULT_BIN


def available() -> bool:
    return os.access(binary_path(), os.X_OK)


def _log(msg: str) -> None:
    print(f"[djcore] {msg}", flush=True)


class _Child:
    def __init__(self, path: str):
        self.proc = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1, encoding="utf-8")
        self.lines: "queue.Queue[Optional[str]]" = queue.Queue()
        threading.Thread(target=self._pump, daemon=True, name="djcore-reader").start()

    def _pump(self) -> None:
        try:
            for line in self.proc.stdout:                 # type: ignore[union-attr]
                self.lines.put(line)
        finally:
            self.lines.put(None)                          # EOF: the child is gone

    def read(self, timeout: float) -> str:
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            raise DjCoreUnavailable(f"no answer within {timeout:g} s") from None
        if line is None:
            raise DjCoreUnavailable("service exited")
        return line

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except Exception:                                 # noqa: BLE001
            pass
        for f in (self.proc.stdin, self.proc.stdout):
            try:
                f.close()                                 # type: ignore[union-attr]
            except Exception:                             # noqa: BLE001
                pass


class DjCoreBridge:
    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.Lock()
        self._child: Optional[_Child] = None
        self._hello: dict = {}
        self._next_id = 0
        self._failed_at = 0.0

    @property
    def path(self) -> str:
        return self._path or binary_path()

    def _start(self) -> None:
        if self._failed_at and time.monotonic() - self._failed_at < RESTART_BACKOFF_S:
            raise DjCoreUnavailable("service is restarting (back-off)")
        path = self.path
        if not os.access(path, os.X_OK):
            raise DjCoreUnavailable(f"dj-core service not found or not executable: {path} (set DJ_CORE_SVC)")
        child = None
        try:
            child = _Child(path)
            hello = json.loads(child.read(START_TIMEOUT_S))
            if hello.get("hello") != "dj-core-svc" or hello.get("protocol") != PROTOCOL:
                raise DjCoreUnavailable(f"service {path} speaks protocol {hello.get('protocol')!r}, this server needs {PROTOCOL}")
        except DjCoreUnavailable:
            if child:
                child.kill()
            self._failed_at = time.monotonic()
            raise
        except Exception as exc:                          # noqa: BLE001 -- OSError, bad hello JSON
            if child:
                child.kill()
            self._failed_at = time.monotonic()
            raise DjCoreUnavailable(f"cannot start {path}: {type(exc).__name__}: {exc}") from exc
        self._child, self._hello = child, hello
        _log(f"started {path} (sha {hello.get('sha')}, protocol {hello.get('protocol')}, pid {child.proc.pid})")

    def info(self) -> dict:
        """The service's hello line ({hello, protocol, sha, fns}); starts it when needed."""
        with self._lock:
            if self._child is None or self._child.proc.poll() is not None:
                self._child = None
                self._start()
            return dict(self._hello)

    def call(self, fn: str, args: Any = None, timeout: float = CALL_TIMEOUT_S) -> Any:
        """One request, one answer. Raises DjCoreUnavailable (service down: logged) or DjCoreError (call rejected)."""
        with self._lock:
            try:
                if self._child is None or self._child.proc.poll() is not None:
                    if self._child is not None:
                        _log(f"service exited (code {self._child.proc.returncode}); restarting")
                    self._child = None
                    self._start()
                child = self._child
                self._next_id += 1
                rid = self._next_id
                child.proc.stdin.write(json.dumps({"id": rid, "fn": fn, "args": args}) + "\n")      # type: ignore[union-attr]
                child.proc.stdin.flush()                                                              # type: ignore[union-attr]
                while True:
                    resp = json.loads(child.read(timeout))
                    if resp.get("id") == rid:
                        break                                 # an older answer (after a timeout we kill the child, so rare)
            except DjCoreUnavailable as exc:
                self._drop(f"{fn}: {exc}")
                raise
            except (OSError, ValueError) as exc:              # broken pipe, bad JSON from the child
                self._drop(f"{fn}: {type(exc).__name__}: {exc}")
                raise DjCoreUnavailable(f"{fn}: {type(exc).__name__}: {exc}") from exc
        if not resp.get("ok"):
            raise DjCoreError(f"{fn}: {resp.get('error')}")
        return resp.get("result")

    def _drop(self, why: str) -> None:
        _log(f"ERROR {why}; dropping the service, next call restarts it")
        if self._child is not None:
            self._child.kill()
        self._child = None
        self._failed_at = time.monotonic()

    def batch(self, calls: List[dict], timeout: float = CALL_TIMEOUT_S) -> List[dict]:
        """calls = [{"fn", "args"}] -> [{"ok", "result" | "error"}] in one round trip."""
        return self.call("batch", {"calls": calls}, timeout)

    def close(self) -> None:
        with self._lock:
            if self._child is not None:
                self._child.kill()
                self._child = None


_bridge: Optional[DjCoreBridge] = None
_bridge_lock = threading.Lock()


def get_bridge() -> DjCoreBridge:
    """The process-wide bridge (path re-read from DJ_CORE_SVC when the child is next started)."""
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = DjCoreBridge()
            atexit.register(_bridge.close)
        return _bridge


def call(fn: str, args: Any = None, timeout: float = CALL_TIMEOUT_S) -> Any:
    return get_bridge().call(fn, args, timeout)
