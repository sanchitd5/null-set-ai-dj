"""The op log: sync.db in CACHE_DIR. Every accepted op gets a server `seq` (monotonic, never reused) and stays until
compaction. The log is the transport and the history; the materialised rows (graph.db, written by appliers the private
builder registers through atlas_bridge.register_appliers) are a view of it.

    ingest(ops, device_id, user_scope)  one transaction, idempotent by op_id
    since(scopes, seq, limit)           visible ops with seq > N, in seq order
    subscribe(cb)                       cb() runs after every commit that added ops (any thread)
    register_applier(tbl, fn)           fn(op) materialises one op; meta.applied.<tbl> is the replay cursor
    compact(keep_days)                  drop old, already materialised ops; raises the floor

Graph snapshots record the head seq they include (graph.db meta.seq; base_seq 0 = the build itself): a client that
loads a snapshot applies only ops with seq > that value.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

from app.music_brain import db
from app.music_brain.sync import merge

STORE = "sync"
FILE = "sync.db"
ITEM_ID = re.compile(r"[A-Za-z0-9_.:-]{1,120}")
TBL = re.compile(r"[a-z][a-z0-9_]{0,31}")
MAX_BATCH = 200
MAX_KEY = 400
SKEW_MS = 24 * 3600 * 1000                   # an op's origin clock is clamped to server time +/- 1 day

# (tbl, kind) -> which scope a write may carry. "global" ops reach every device, so only listed pairs may be global.
# Learned graph edges are global (owner decision); per-user tables are user scope. Anything unlisted is rejected.
WRITE_POLICY: Dict[tuple, str] = {
    ("edge", "incr"): "global",
    ("edge", "max"): "global",
    ("edge", "lww"): "global",
    ("node", "lww"): "global",
    ("node", "union"): "global",
    ("pref", "lww"): "user",
    ("pref", "union"): "user",
    ("play", "incr"): "user",
    ("play", "lww"): "user",
}

STEPS = (
    """CREATE TABLE op (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, op_id TEXT UNIQUE NOT NULL, scope TEXT NOT NULL,
        device TEXT, tbl TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL, delta TEXT NOT NULL,
        ts INTEGER NOT NULL, recv REAL NOT NULL);
    CREATE INDEX op_scope_seq ON op (scope, seq);
    CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)""",
)


# Device ids only server code may stamp on an op. The appliers trust ops from these (e.g. the graph's yt edges), so a
# client body or a learn-outbox item can never carry one: `ingest` rejects every op of such a device unless the
# caller passes server=True (OpLog.ingest_server).
SERVER_DEVICE = "server"
RESERVED_DEVICES = frozenset({SERVER_DEVICE})

# Held for a whole apply pass over every table, and by anything that must copy the materialised rows (the graph
# snapshot): a copy never sees one table applied further than another.
PASS_LOCK = threading.RLock()


def is_reserved_device(device_id) -> bool:
    return isinstance(device_id, str) and device_id.strip().lower() in RESERVED_DEVICES


class OpLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._listeners: List[Callable[[], None]] = []
        self._appliers: Dict[str, Callable[[dict], None]] = {}
        self._lock = threading.Lock()
        self._apply_lock = threading.Lock()

    # ------------------------------------------------------------------ plumbing
    def _conn(self):
        conn = db.connect(self.path)
        db.ensure(conn, STORE, STEPS)
        return conn

    def subscribe(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Register cb; returns the function that removes it."""
        with self._lock:
            self._listeners.append(cb)

        def off() -> None:
            with self._lock:
                if cb in self._listeners:
                    self._listeners.remove(cb)
        return off

    def register_applier(self, tbl: str, fn: Callable[[dict], None]) -> None:
        self._appliers[tbl] = fn

    def _notify(self) -> None:
        with self._lock:
            cbs = list(self._listeners)
        for cb in cbs:
            try:
                cb()
            except Exception as exc:  # noqa: BLE001 -- a dead listener never fails an ingest
                print(f"[sync] listener failed: {type(exc).__name__}: {exc}", flush=True)

    # ------------------------------------------------------------------ reads
    def head(self) -> int:
        row = self._conn().execute("SELECT seq FROM sqlite_sequence WHERE name = 'op'").fetchone()
        return int(row[0]) if row else 0

    def floor(self) -> int:
        row = self._conn().execute("SELECT v FROM meta WHERE k = 'floor'").fetchone()
        return int(row[0]) if row else 0

    def since(self, scopes: Iterable[str], seq: int, limit: int = 500, exclude_device: str = "") -> List[dict]:
        """Ops with seq > `seq` whose scope is in `scopes`, in seq order (at most `limit`)."""
        scopes = sorted(set(scopes))
        if not scopes:
            return []
        q = ",".join("?" * len(scopes))
        sql = f"SELECT {_COLS} FROM op WHERE seq > ? AND scope IN ({q})"
        args: list = [int(seq), *scopes]
        if exclude_device:
            sql += " AND COALESCE(device, '') != ?"
            args.append(exclude_device)
        sql += " ORDER BY seq LIMIT ?"
        args.append(max(1, int(limit)))
        return [_row(r) for r in self._conn().execute(sql, args).fetchall()]

    # ------------------------------------------------------------------ write
    def ingest_server(self, ops, now: Optional[float] = None) -> dict:
        """Ingest ops written by server code itself: device SERVER_DEVICE, global or owner scope as the ops say."""
        return self.ingest(ops, SERVER_DEVICE, "user:owner", now, server=True)

    def ingest(self, ops, device_id: str, user_scope: str, now: Optional[float] = None, server: bool = False) -> dict:
        """Validate and store a batch. -> {"accepted": [ids], "duplicate": [ids], "rejected": [{op_id, why}],
        "head": N}. One bad op never fails the batch. The scope comes from the auth layer (`user_scope`), the
        op's own scope field only chooses between that and global, and only where WRITE_POLICY allows."""
        from app.music_brain.matching import knowledge as kn

        now = time.time() if now is None else now
        out: dict = {"accepted": [], "duplicate": [], "rejected": []}
        if not isinstance(ops, list) or len(ops) > MAX_BATCH:
            raise ValueError(f"ops: a list of at most {MAX_BATCH}")
        if not server and is_reserved_device(device_id):
            for raw in ops:
                oid = raw.get("op_id") if isinstance(raw, dict) else None
                out["rejected"].append({"op_id": str(oid)[:120], "why": "reserved device id"})
            out["head"] = self.head()
            return out
        todo = []
        seen: Set[str] = set()
        for raw in ops:
            oid = raw.get("op_id") if isinstance(raw, dict) else None
            if not isinstance(oid, str) or not ITEM_ID.fullmatch(oid):
                out["rejected"].append({"op_id": str(oid)[:120], "why": "bad op_id"})
                continue
            if oid in seen:
                out["duplicate"].append(oid)
                continue
            seen.add(oid)
            try:
                todo.append(self._check(raw, user_scope, now, kn))
            except ValueError as exc:
                out["rejected"].append({"op_id": oid, "why": str(exc)[:200]})
        conn = self._conn()
        with db.tx(conn):
            for o in todo:
                if conn.execute("SELECT 1 FROM op WHERE op_id = ?", (o["op_id"],)).fetchone():
                    out["duplicate"].append(o["op_id"])             # checked first: an ignored insert still burns a seq
                    continue
                conn.execute(
                    "INSERT INTO op (op_id, scope, device, tbl, key, kind, delta, ts, recv) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (o["op_id"], o["scope"], device_id, o["tbl"], o["key"], o["kind"],
                     json.dumps(o["delta"], separators=(",", ":"), ensure_ascii=False), o["ts"], now))
                out["accepted"].append(o["op_id"])
        out["head"] = self.head()
        if out["accepted"]:
            self._notify()
            self.apply_pending()
        return out

    @staticmethod
    def _check(raw: dict, user_scope: str, now: float, kn) -> dict:
        tbl, key, kind, delta = raw.get("tbl"), raw.get("key"), raw.get("kind"), raw.get("delta")
        if not isinstance(tbl, str) or not TBL.fullmatch(tbl):
            raise ValueError("bad tbl")
        if not isinstance(key, str) or not 0 < len(key) <= MAX_KEY or any(ord(c) < 32 for c in key):
            raise ValueError("bad key")
        if kind not in merge.KINDS:
            raise ValueError(f"kind must be one of {merge.KINDS}")
        want = raw.get("scope", "user")
        if want not in ("user", "global"):
            raise ValueError("scope must be user or global")
        allowed = WRITE_POLICY.get((tbl, kind))
        if allowed is None:
            raise ValueError("unknown table or kind")
        if allowed != want:
            raise ValueError(f"{tbl}/{kind} ops must have scope {allowed}")
        ts = raw.get("ts")
        if not isinstance(ts, int) or isinstance(ts, bool):
            raise ValueError("ts must be integer epoch milliseconds")
        now_ms = int(now * 1000)
        ts = max(now_ms - SKEW_MS, min(now_ms + SKEW_MS, ts))
        merge.validate_delta(kind, delta)
        if kn.privacy_hits(delta, "delta") or kn.privacy_hits({"key": key}, "op"):
            raise ValueError("private string in the op (path or e-mail)")
        return {"op_id": raw["op_id"], "tbl": tbl, "key": key, "kind": kind, "delta": delta, "ts": ts,
                "scope": "global" if want == "global" else user_scope}

    # ------------------------------------------------------------------ materialising
    def applied(self, tbl: str) -> int:
        row = self._conn().execute("SELECT v FROM meta WHERE k = ?", (f"applied.{tbl}",)).fetchone()
        return int(row[0]) if row else 0

    def apply_pending(self, batch: int = 500) -> int:
        """Replay ops after each table's applied cursor through its applier, in seq order. A failing applier stops
        that table (its cursor stays) and is retried on the next call. -> ops applied.
        One pass is bounded by the head at its start and runs under PASS_LOCK, so when it ends every table has every op
        up to that head and a snapshot taken under the lock (graph.db meta.seq) skips no op of any table."""
        n = 0
        with PASS_LOCK, self._apply_lock:
            conn = self._conn()
            top = self.head()
            for tbl, fn in list(self._appliers.items()):
                while True:
                    cur = self.applied(tbl)
                    rows = conn.execute(f"SELECT {_COLS} FROM op WHERE tbl = ? AND seq > ? AND seq <= ? "
                                        "ORDER BY seq LIMIT ?", (tbl, cur, top, batch)).fetchall()
                    if not rows:
                        break
                    try:
                        for r in rows:
                            fn(_row(r))
                            conn.execute("INSERT INTO meta (k, v) VALUES (?, ?) "
                                         "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (f"applied.{tbl}", str(r[0])))
                            n += 1
                    except Exception as exc:  # noqa: BLE001 -- the log keeps the op; the cursor replays it later
                        print(f"[sync] applier {tbl} failed at seq {r[0]}: {type(exc).__name__}: {exc}", flush=True)
                        break
        return n

    def compact(self, keep_days: float = 30, now: Optional[float] = None) -> dict:
        """Delete ops older than keep_days that their table's applier already materialised; raise the floor to the
        newest deleted seq. A table with no applier is never compacted (nothing would hold its rows)."""
        now = time.time() if now is None else now
        self.apply_pending()
        conn = self._conn()
        cutoff = now - keep_days * 86400
        deleted, top = 0, 0
        with db.tx(conn):
            for tbl in self._appliers:
                cur_applied = self.applied(tbl)
                r = conn.execute("SELECT COUNT(*), COALESCE(MAX(seq), 0) FROM op WHERE tbl = ? AND recv < ? AND seq <= ?",
                                 (tbl, cutoff, cur_applied)).fetchone()
                if r[0]:
                    conn.execute("DELETE FROM op WHERE tbl = ? AND recv < ? AND seq <= ?", (tbl, cutoff, cur_applied))
                    deleted += r[0]
                    top = max(top, r[1])
            if top:
                conn.execute("INSERT INTO meta (k, v) VALUES ('floor', ?) "
                             "ON CONFLICT(k) DO UPDATE SET v = MAX(CAST(v AS INTEGER), CAST(excluded.v AS INTEGER))",
                             (str(top),))
        return {"deleted": deleted, "floor": self.floor()}


_COLS = "seq, op_id, scope, device, tbl, key, kind, delta, ts"


def _row(r) -> dict:
    seq, op_id, scope, device, tbl, key, kind, delta, ts = r
    return {"seq": seq, "op_id": op_id, "tbl": tbl, "key": key, "kind": kind, "delta": json.loads(delta), "ts": ts,
            "scope": scope, "device": device or ""}
