"""Progress file of one set study (set_learner.learn_set), for the console panel and the CLI.

The learner writes CACHE_DIR/learn_progress/<set_id>.json (atomic tmp + rename, throttled to one
write a second, never raises); readers (server, agent_bridge learn-status) derive `state` from it.
A killed run stops updating: its heartbeat (updated_at, refreshed every HEARTBEAT_S by a daemon
thread) goes old and its pid is gone, so readers call it `stale` instead of `running`.

Stages: fetch (set + songs) | cut | separate (each song: Demucs + analysis) | analyze (each blend
clip: Demucs + stem location) | detect | lyrics | ai_review | merge | done | error.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from app.music_brain.config import CACHE_DIR

PROGRESS_DIR = CACHE_DIR / "learn_progress"
KEEP = 20                 # files kept (oldest pruned)
THROTTLE_S = 1.0          # at most one write a second (stage changes and the end are forced)
HEARTBEAT_S = 15.0
STALE_S = 120.0           # heartbeat older than this and the pid gone: the run died
MAX_WARNINGS = 20
STAGES = ("fetch", "cut", "separate", "analyze", "detect", "lyrics", "ai_review", "merge", "cleanup", "done", "error")
_WARN = re.compile(r"^failed |NOT FOUND|probably the wrong|failed \(|skipped|no artist known|could not", re.I)


class Progress:
    """Writer for one study. Every public method swallows its own errors: progress never breaks a study."""

    def __init__(self, source: str, root: Optional[Path] = None, clock: Callable[[], float] = time.time,
                 pid: Optional[int] = None, heartbeat_s: float = HEARTBEAT_S,
                 announce: Optional[Callable[[str], None]] = None):
        self.root = Path(root) if root else PROGRESS_DIR
        self._announce = announce            # one line per stage change (the CLI passes its stderr logger)
        self.clock = clock
        self._lock = threading.Lock()
        self._last = 0.0
        self._stop = threading.Event()
        now = clock()
        # the set id is known only after fetch_set: until then a stand-in named from the source
        self.id = "pending-" + hashlib.sha256(str(source).encode()).hexdigest()[:8]
        self.doc: dict = {"id": self.id, "source": str(source), "title": None, "pid": pid or os.getpid(),
                          "started_at": now, "updated_at": now, "stage": "fetch", "stage_started_at": now,
                          "tracks_total": 0, "tracks_done": 0, "current": None, "counts": {},
                          "techniques_found": {}, "warnings": [], "error": None, "finished_at": None}
        self._safe(self._flush, True)
        self._say(f"progress files in {self.root} (agent_bridge learn-status)")
        if heartbeat_s and heartbeat_s > 0:
            try:
                threading.Thread(target=self._beat, args=(heartbeat_s,), daemon=True, name="learn-progress").start()
            except Exception:
                pass

    # -- plumbing ----------------------------------------------------------------------------------
    @staticmethod
    def _safe(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            return None

    def _say(self, msg: str) -> None:
        if self._announce:
            self._safe(self._announce, msg)

    def _path(self, id_: Optional[str] = None) -> Path:
        return self.root / f"{id_ or self.id}.json"

    def _flush(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last < THROTTLE_S:
            return
        with self._lock:
            self._last = now
            self.doc["updated_at"] = now
            self.root.mkdir(parents=True, exist_ok=True)
            p = self._path()
            tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(self.doc), encoding="utf-8")
            tmp.replace(p)

    def _beat(self, every: float) -> None:
        while not self._stop.wait(every):
            if self.doc["stage"] in ("done", "error"):
                return
            self._safe(self._flush, True)

    def _prune(self) -> None:
        files = sorted((f for f in self.root.glob("*.json")), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[KEEP:]:
            self._safe(f.unlink)

    # -- writer API ------------------------------------------------------------------------------------
    def set_id(self, set_id: str, title: Optional[str] = None) -> None:
        """The real id is known: rename the stand-in file."""
        def go():
            old = self._path()
            with self._lock:
                self.id = self.doc["id"] = str(set_id)
                if title:
                    self.doc["title"] = title
            self._flush(True)
            if old != self._path():
                self._safe(old.unlink)
            self._prune()
            self._say(f"progress file {self._path()}")
        self._safe(go)

    def update(self, **fields) -> None:
        def go():
            with self._lock:
                self.doc.update(fields)
            self._flush()
        self._safe(go)

    def stage(self, name: str, **fields) -> None:
        """Enter a stage (forced write). `total=n` sets that stage's count."""
        def go():
            total = fields.pop("total", None)
            with self._lock:
                self.doc.update(fields, stage=name, stage_started_at=self.clock(), current=fields.get("current"))
                if total is not None:
                    self.doc["counts"][name] = {"done": 0, "total": int(total)}
            self._flush(True)
            self._say(f"stage {name}" + (f" ({int(total)})" if total is not None else ""))
        self._safe(go)

    def tick(self, stage: Optional[str] = None, current: Optional[str] = None, n: int = 1) -> None:
        """One unit of `stage` (default: the current one) finished; safe from worker threads."""
        def go():
            with self._lock:
                s = stage or self.doc["stage"]
                c = self.doc["counts"].setdefault(s, {"done": 0, "total": 0})
                c["done"] += n
                if c["total"] < c["done"]:
                    c["total"] = c["done"]
                if s == "separate":               # the bar: songs separated
                    self.doc["tracks_done"] = c["done"]
                if current is not None:
                    self.doc["current"] = current
            self._flush()
        self._safe(go)

    def techniques(self, observations) -> None:
        def go():
            found: Dict[str, int] = {}
            for o in observations:
                found[o.kind] = found.get(o.kind, 0) + 1
            self.update(techniques_found=found)
            self._flush(True)
        self._safe(go)

    def warn(self, msg: str) -> None:
        def go():
            with self._lock:
                w = self.doc["warnings"]
                if len(w) < MAX_WARNINGS:
                    w.append(str(msg)[:200])
            self._flush()
        self._safe(go)

    def wrap_log(self, log: Callable[[str], None]) -> Callable[[str], None]:
        """A log callback that also files warning-looking lines."""
        def out(m):
            try:
                log(m)
            finally:
                if _WARN.search(str(m)):
                    self.warn(m)
        return out

    def finish(self) -> None:
        def go():
            with self._lock:
                self.doc.update(stage="done", current=None, finished_at=self.clock())
                self.doc["tracks_done"] = self.doc["tracks_total"]
            self._flush(True)
            self._stop.set()
        self._safe(go)

    def fail(self, exc: BaseException) -> None:
        def go():
            msg = (f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)[:400]
            with self._lock:
                self.doc.update(stage="error", error=msg, current=None, finished_at=self.clock())
            self._flush(True)
            self._stop.set()
        self._safe(go)


# ------------------------------------------------------------------------------------------ readers

def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True                 # exists, not ours
    except (OSError, TypeError, ValueError, OverflowError):
        return False
    return True


def eta_s(doc: dict, now: float) -> Optional[float]:
    """Rough seconds left in the current counted stage, from its rate so far. None when unknowable."""
    c = (doc.get("counts") or {}).get(doc.get("stage")) or {}
    done, total = c.get("done", 0), c.get("total", 0)
    if done < 1:
        return None
    spent = now - float(doc.get("stage_started_at") or doc.get("started_at") or now)
    return max(0.0, spent / done * (total - done))


def derive(doc: dict, now: Optional[float] = None, alive: Callable[[object], bool] = pid_alive) -> dict:
    """doc + state (running|done|error|stale), elapsed_s, eta_s (running only)."""
    now = time.time() if now is None else now
    out = dict(doc)
    stage = doc.get("stage")
    if stage == "done":
        state = "done"
    elif stage == "error":
        state = "error"
    elif (doc.get("pid") is not None and not alive(doc.get("pid"))) \
            or (now - float(doc.get("updated_at") or 0) > STALE_S and not alive(doc.get("pid"))):
        # a run killed mid-stage (its pid gone) is stale at once, however fresh its last write
        state = "stale"
    else:
        state = "running"
    end = doc.get("finished_at") if state in ("done", "error") else (now if state == "running" else doc.get("updated_at"))
    out["state"] = state
    out["elapsed_s"] = round(max(0.0, float(end or now) - float(doc.get("started_at", now))), 1)
    eta = eta_s(doc, now) if state == "running" else None
    out["eta_s"] = None if eta is None else round(eta)
    return out


def read_all(root: Optional[Path] = None, now: Optional[float] = None, alive: Callable[[object], bool] = pid_alive) -> List[dict]:
    """Every progress file, newest first (by updated_at), each derived. Unreadable files are skipped."""
    out = []
    for f in Path(root or PROGRESS_DIR).glob("*.json"):
        try:
            out.append(derive(json.loads(f.read_text(encoding="utf-8")), now, alive))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return sorted(out, key=lambda d: d.get("updated_at") or 0, reverse=True)


def read_one(set_id: str, root: Optional[Path] = None, now: Optional[float] = None,
             alive: Callable[[object], bool] = pid_alive) -> Optional[dict]:
    if not re.fullmatch(r"[\w-]{1,64}", set_id or ""):
        return None
    try:
        return derive(json.loads((Path(root or PROGRESS_DIR) / f"{set_id}.json").read_text(encoding="utf-8")), now, alive)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def summary(d: dict) -> str:
    """One human line for a derived doc."""
    c = (d.get("counts") or {}).get(d.get("stage")) or {}
    n = f" {c.get('done', 0)}/{c.get('total', 0)}" if c else ""
    if (d.get("parts") or 0) > 1:
        n += f" (part {d.get('part')}/{d['parts']})"
    eta = f", ~{int(d['eta_s'] // 60)} min left" if d.get("eta_s") else ""
    found = ", ".join(f"{k} {v}" for k, v in sorted((d.get("techniques_found") or {}).items()))
    err = f" ERROR {d['error']}" if d.get("error") else ""
    return (f"{d.get('title') or d.get('id')} [{d['state']}] {d.get('stage')}{n} "
            f"({int(d['elapsed_s'] // 60)} min{eta}){' | ' + found if found else ''}{err}")
