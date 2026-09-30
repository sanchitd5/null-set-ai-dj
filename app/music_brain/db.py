"""The local SQLite stores: TWO files in CACHE_DIR (gitignored, like all of data/).

* APP DB, CACHE_DIR/app.db: the player's shared knowledge. Pair atlas, macros (every kind),
  learned techniques, genre / era labels. This is what `knowledge export` carries to git, as the
  deterministic JSON in app/music_brain/knowledge/ (a multi-hundred-MB binary DB cannot go in
  git: GitHub refuses files over 100 MB and binary diffs are unreadable); `knowledge seed` fills
  the app DB back from that JSON. "Git linked" means via the export, never the .db file.
* USER DB, CACHE_DIR/user.db: private, never exported, never in git. Set history (sessions,
  plays, transitions, moves, set logs), set memory, vetoes / liked marks: anything tied to the
  owner's own listening. The knowledge export never opens it (guard test).

A store at a legacy JSON path P lives in P's folder's database (P.parent / APP_DB or USER_DB), so
every store keeps taking the same path argument it always did and a test's tmp_path gets its own.

Concurrency: WAL (readers never block the one writer), busy_timeout, one connection per
process + thread + file, short transactions (`tx` = BEGIN IMMEDIATE ... COMMIT). Crash safety is
the transaction: no tmp-file + rename.

Schema versions: per DB file, each store registers its migrations with
`ensure(conn, store, steps)`: steps[i] takes the store from version i to i + 1, run once, in
order, inside one transaction, recorded in that file's `schema_version` table.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, Iterator, Sequence, Tuple, Union

APP_DB = "app.db"
USER_DB = "user.db"
BUSY_MS = 30000

Step = Union[str, Callable[[sqlite3.Connection], None]]

_local = threading.local()


def db_path(cache_dir: Union[str, Path, None] = None, name: str = APP_DB) -> Path:
    """CACHE_DIR/app.db (name=USER_DB: CACHE_DIR/user.db)."""
    if cache_dir is None:
        from app.music_brain.config import CACHE_DIR
        cache_dir = CACHE_DIR
    return Path(cache_dir) / name


def beside(legacy: Union[str, Path], name: str = APP_DB) -> Path:
    """The database a store whose legacy JSON file is `legacy` lives in."""
    return Path(legacy).expanduser().resolve().parent / name


def connect(path: Union[str, Path]) -> sqlite3.Connection:
    """This process + thread's connection to `path` (created, WAL, busy_timeout, on first use).
    Autocommit mode: reads see the latest commit; writes go through `tx`."""
    p = str(Path(path).expanduser().resolve())
    cache: Dict[Tuple[int, str], sqlite3.Connection] = getattr(_local, "conns", None)
    if cache is None:
        cache = _local.conns = {}
    key = (os.getpid(), p)                      # a forked child never reuses its parent's handle
    conn = cache.get(key)
    if conn is not None:
        return conn
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=BUSY_MS / 1000, isolation_level=None, check_same_thread=True)
    conn.execute(f"PRAGMA busy_timeout={BUSY_MS}")
    conn.execute("PRAGMA page_size=16384")       # only takes on a new file: atlas rows are ~1.3 KB, 16 KB pages
                                                  # pack them ~13 % smaller than 4 KB ones (measured)
    # two processes creating the same new file race on the WAL switch, which ignores busy_timeout
    _retry(lambda: conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal" or _raise_locked())
    conn.execute("PRAGMA journal_size_limit=67108864")   # the WAL file shrinks back to 64 MB after a big write
    conn.execute("PRAGMA synchronous=NORMAL")    # WAL + NORMAL: a commit survives a process crash
    conn.execute("PRAGMA foreign_keys=ON")
    _retry(lambda: conn.execute("CREATE TABLE IF NOT EXISTS schema_version "
                                "(store TEXT PRIMARY KEY, version INTEGER NOT NULL)"))
    cache[key] = conn
    return conn


def _raise_locked():
    raise sqlite3.OperationalError("database is locked")


def _retry(fn: Callable[[], object]) -> None:
    """fn() until it stops raising "database is locked", up to BUSY_MS."""
    import time

    end = time.monotonic() + BUSY_MS / 1000
    while True:
        try:
            fn()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() > end:
                raise
            time.sleep(0.02)


def close_all() -> None:
    """Close this thread's connections (tests that delete a DB file, forked workers)."""
    for c in (getattr(_local, "conns", None) or {}).values():
        try:
            c.close()
        except sqlite3.Error:
            pass
    _local.conns = {}


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One write transaction (BEGIN IMMEDIATE: takes the write lock up front, so a load -> modify
    -> save inside it cannot lose another writer's update). Nested use joins the outer one."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@contextmanager
def read(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One consistent snapshot for several SELECTs (a writer committing between them is not
    seen half way). Nested use joins the outer transaction."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")


def version(conn: sqlite3.Connection, store: str) -> int:
    row = conn.execute("SELECT version FROM schema_version WHERE store = ?", (store,)).fetchone()
    return int(row[0]) if row else 0


def ensure(conn: sqlite3.Connection, store: str, steps: Sequence[Step]) -> int:
    """Bring `store` up to len(steps): run the missing steps (SQL script or fn(conn)) in one
    transaction. Returns the version now. A DB newer than this code is left alone (ValueError)."""
    have = version(conn, store)
    if have == len(steps):
        return have
    if have > len(steps):
        raise ValueError(f"{store}: database schema v{have} is newer than this code (v{len(steps)})")
    with tx(conn):
        have = version(conn, store)                  # another process may have just done it
        for step in steps[have:]:
            if callable(step):
                step(conn)
            else:
                for stmt in [s for s in step.split(";") if s.strip()]:
                    conn.execute(stmt)
        conn.execute("INSERT INTO schema_version (store, version) VALUES (?, ?) "
                     "ON CONFLICT(store) DO UPDATE SET version = excluded.version", (store, len(steps)))
    return len(steps)


def checkpoint(conn: sqlite3.Connection) -> None:
    """Fold the WAL into the main file and truncate it (after a migration's one big write)."""
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def retire(legacy: Path) -> Path:
    """Rename a migrated legacy JSON file / folder to <name>.migrated (never deleted); an existing
    .migrated gets a timestamped name instead of being overwritten."""
    import time

    dest = legacy.with_name(legacy.name + ".migrated")
    if dest.exists():
        dest = dest.with_name(f"{dest.name}.{int(time.time())}")
    legacy.replace(dest)
    return dest
