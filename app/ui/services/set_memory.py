"""Cross-set song memory, so the next set from the same seed isn't a replay.

Every suggest request carries the current set's played history. This module
records those titles (bare, lower-cased) with a timestamp in the private user DB
(CACHE_DIR/user.db, table set_memory; the old set_memory.json migrates once), and hands back the ones played in EARLIER sets (not the
current one) so the prompt can ask for fresh picks. It is a soft avoid: the
model may still pick a remembered song when it is clearly the best fit.

Kept small: MAX_SONGS most recent titles, older than MAX_AGE_DAYS dropped.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Iterable, List

MAX_SONGS = 300
MAX_AGE_DAYS = 14
PROMPT_LIMIT = 40
FAVOURITE_MIN_SONGS = 3   # different remembered songs by one artist -> a favourite

_lock = threading.Lock()


def artist_key(name: str) -> str:
    """"Fred again..", "Fred Again", "FRED AGAIN..." -> "fredagain"."""
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


def _key(title: str) -> str:
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", str(title).lower())
    return " ".join(re.sub(r"[^a-z0-9 \-]+", " ", t).split())


# SQLite, the USER DB (app.music_brain.db.USER_DB, private, never exported): the memory at legacy
# path P lives in P.parent/user.db, keyed by P's file name. memory_files marks a file name as
# living in the DB (its old JSON was migrated and renamed .migrated, or never existed).
MEMORY_STEPS = (
    """CREATE TABLE memory_files (file TEXT PRIMARY KEY, migrated_at REAL);
    CREATE TABLE set_memory (file TEXT NOT NULL, key TEXT NOT NULL, name TEXT NOT NULL, t REAL NOT NULL,
        set_id TEXT NOT NULL DEFAULT '', PRIMARY KEY (file, key));
    CREATE INDEX set_memory_t ON set_memory (file, t DESC)""",
)


def _db(path: Path):
    """(connection, file key); the first open with the old JSON on disk migrates it once."""
    from app.music_brain import db

    path = Path(path)
    conn = db.connect(db.beside(path, db.USER_DB))
    db.ensure(conn, "set_memory", MEMORY_STEPS)
    f = path.name
    if conn.execute("SELECT 1 FROM memory_files WHERE file = ?", (f,)).fetchone() is None and path.is_file():
        with db.tx(conn):
            if conn.execute("SELECT 1 FROM memory_files WHERE file = ?", (f,)).fetchone() is None:
                try:
                    d = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    d = None                          # unreadable: left alone, the memory starts empty
                if isinstance(d, dict):
                    _write(conn, f, {}, d)
                    db.retire(path)
    return conn, f


def _write(conn, f: str, old: dict, new: dict) -> None:
    """Rows for keys whose entry changed; rows of dropped keys deleted (caller's transaction)."""
    for k, v in new.items():
        if not isinstance(v, dict):
            continue
        if old.get(k) != v:
            conn.execute("INSERT OR REPLACE INTO set_memory (file, key, name, t, set_id) VALUES (?, ?, ?, ?, ?)",
                         (f, k, str(v.get("name", ""))[:120], float(v.get("t", 0) or 0), str(v.get("set", "") or "")))
    for k in old:
        if k not in new:
            conn.execute("DELETE FROM set_memory WHERE file = ? AND key = ?", (f, k))
    conn.execute("INSERT OR IGNORE INTO memory_files (file, migrated_at) VALUES (?, ?)", (f, time.time()))


def load(path: Path) -> dict:
    """{title key: {name, t, set}}, newest first; {} when none or unreadable."""
    import sqlite3

    try:
        conn, f = _db(path)
        return {k: {"name": n, "t": t, "set": s} for k, n, t, s in conn.execute(
            "SELECT key, name, t, set_id FROM set_memory WHERE file = ? ORDER BY t DESC, key", (f,))}
    except (sqlite3.Error, OSError):
        return {}


def peek(path: Path) -> dict:
    """load() without side effects (dry runs): never creates the DB nor migrates; a memory not
    yet in the DB is read from its old JSON."""
    import sqlite3

    from app.music_brain import db

    path = Path(path)
    dbp = db.beside(path, db.USER_DB)
    if dbp.is_file():
        try:
            conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True, timeout=db.BUSY_MS / 1000)
            try:
                if conn.execute("SELECT 1 FROM memory_files WHERE file = ?", (path.name,)).fetchone():
                    return {k: {"name": n, "t": t, "set": s} for k, n, t, s in conn.execute(
                        "SELECT key, name, t, set_id FROM set_memory WHERE file = ? ORDER BY t DESC, key",
                        (path.name,))}
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


class SetMemory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict = load(self.path)

    def _save(self, old: dict) -> None:
        import sqlite3

        from app.music_brain import db

        try:
            conn, f = _db(self.path)
            with db.tx(conn):
                _write(conn, f, old, self._data)
        except (sqlite3.Error, OSError):
            pass  # memory is best-effort, never breaks a suggest call

    def record(self, played: Iterable[str], set_id: str = "") -> None:
        """Remember `played` as heard in set `set_id` ("" = unscoped caller)."""
        now = time.time()
        with _lock:
            old = dict(self._data)
            for name in played:
                k = _key(name)
                if k:
                    self._data[k] = {"name": str(name)[:120], "t": now, "set": set_id}
            cutoff = now - MAX_AGE_DAYS * 86400
            items = sorted(((k, v) for k, v in self._data.items() if v.get("t", 0) >= cutoff),
                           key=lambda kv: kv[1]["t"], reverse=True)[:MAX_SONGS]
            self._data = dict(items)
            self._save(old)

    def favourite_artists(self, min_songs: int = FAVOURITE_MIN_SONGS, limit: int = 12) -> List[str]:
        """Artists the listener keeps coming back to: >= min_songs different
        remembered songs credit them. Their songs must not be avoided just for
        having been heard (user: "biased against Fred again.. songs")."""
        from app.ui.services.track_identity import credited_artists
        counts: dict = {}
        shown: dict = {}
        with _lock:
            names = [v["name"] for v in self._data.values()]
        for name in names:
            for a in {artist_key(x): x for x in credited_artists(str(name))}.items():
                if a[0]:
                    counts[a[0]] = counts.get(a[0], 0) + 1
                    shown.setdefault(a[0], a[1].strip(" .") + (".." if a[1].rstrip().endswith("..") else ""))
        top = sorted((k for k, c in counts.items() if c >= min_songs), key=lambda k: -counts[k])
        return [shown[k] for k in top[:limit]]

    def earlier_sets(self, current: Iterable[str], limit: int = PROMPT_LIMIT,
                     set_id: str = "") -> List[str]:
        """Most recent remembered songs that are NOT in the current set.

        With a set_id, everything this set recorded counts as the current set
        too (the request's history is only its last 30 songs), so a long set's
        own early songs never come back as "heard in an earlier set"."""
        cur = {_key(c) for c in current}
        with _lock:
            items = sorted(self._data.items(), key=lambda kv: kv[1]["t"], reverse=True)
        return [v["name"] for k, v in items
                if k not in cur and not (set_id and v.get("set") == set_id)][:limit]
