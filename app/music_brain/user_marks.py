"""The owner's own marks on songs, pairs or moves: vetoes, likes, anything the owner says about
their listening. Private: the USER DB (CACHE_DIR/user.db), never exported, never in git.

A mark is (kind, subject) -> {data, t}: kind names the list ("veto", "liked", ...), subject what
it is about (a track id, "a>b" for a pair, a move name). Setting it again replaces its data.

    from app.music_brain import user_marks as um
    um.mark("veto", "a>b", {"why": "clashing vocals"})
    um.is_marked("veto", "a>b");  um.marks("veto");  um.unmark("veto", "a>b")
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

STEPS = (
    """CREATE TABLE marks (kind TEXT NOT NULL, subject TEXT NOT NULL, data TEXT, t REAL NOT NULL,
        PRIMARY KEY (kind, subject));
    CREATE INDEX marks_subject ON marks (subject)""",
)


def _conn(cache_dir: Optional[Path] = None):
    from app.music_brain import db

    conn = db.connect(db.db_path(cache_dir, db.USER_DB))
    db.ensure(conn, "marks", STEPS)
    return conn


def _check(kind: str, subject: str) -> None:
    if not isinstance(kind, str) or not kind or len(kind) > 40:
        raise ValueError("mark kind: a short non-empty string")
    if not isinstance(subject, str) or not subject or len(subject) > 400:
        raise ValueError("mark subject: a non-empty string up to 400 chars")


def mark(kind: str, subject: str, data: Optional[dict] = None, cache_dir: Optional[Path] = None) -> dict:
    """Set (or replace) one mark. -> {kind, subject, data, t}."""
    from app.music_brain import db

    _check(kind, subject)
    row = {"kind": kind, "subject": subject, "data": data or {}, "t": time.time()}
    conn = _conn(cache_dir)
    with db.tx(conn):
        conn.execute("INSERT OR REPLACE INTO marks (kind, subject, data, t) VALUES (?, ?, ?, ?)",
                     (kind, subject, json.dumps(row["data"], ensure_ascii=False, default=str), row["t"]))
    return row


def unmark(kind: str, subject: str, cache_dir: Optional[Path] = None) -> bool:
    from app.music_brain import db

    _check(kind, subject)
    conn = _conn(cache_dir)
    with db.tx(conn):
        return conn.execute("DELETE FROM marks WHERE kind = ? AND subject = ?", (kind, subject)).rowcount > 0


def is_marked(kind: str, subject: str, cache_dir: Optional[Path] = None) -> bool:
    return _conn(cache_dir).execute("SELECT 1 FROM marks WHERE kind = ? AND subject = ?",
                                    (kind, subject)).fetchone() is not None


def marks(kind: str, cache_dir: Optional[Path] = None) -> Dict[str, dict]:
    """{subject: {data, t}} of one kind, newest first."""
    return {s: {"data": json.loads(d) if d else {}, "t": t} for s, d, t in _conn(cache_dir).execute(
        "SELECT subject, data, t FROM marks WHERE kind = ? ORDER BY t DESC, subject", (kind,))}


# ---------------------------------------------------------------- helpers for the typed stores
# vetoes.py and liked.py keep their own public APIs and store their rows here (kind "veto" /
# "liked"); these helpers give them insertion order, add-if-new, a cheap change stamp and the
# one-time migration of their old JSON file.

def rows(kind: str, cache_dir: Optional[Path] = None) -> List[Tuple[str, dict]]:
    """[(subject, data)] of one kind in insertion order (oldest first)."""
    return [(s, json.loads(d) if d else {}) for s, d in _conn(cache_dir).execute(
        "SELECT subject, data FROM marks WHERE kind = ? ORDER BY t, rowid", (kind,))]


def add_new(kind: str, subject: str, data: dict, cache_dir: Optional[Path] = None,
            keep: Optional[int] = None) -> bool:
    """Insert unless (kind, subject) exists; keep: then only the newest `keep` of this kind stay.
    One transaction. True when inserted."""
    from app.music_brain import db

    _check(kind, subject)
    conn = _conn(cache_dir)
    with db.tx(conn):
        if conn.execute("SELECT 1 FROM marks WHERE kind = ? AND subject = ?", (kind, subject)).fetchone():
            return False
        conn.execute("INSERT INTO marks (kind, subject, data, t) VALUES (?, ?, ?, ?)",
                     (kind, subject, json.dumps(data, ensure_ascii=False, default=str), time.time()))
        if keep:
            conn.execute("DELETE FROM marks WHERE kind = ? AND rowid NOT IN (SELECT rowid FROM marks "
                         "WHERE kind = ? ORDER BY t DESC, rowid DESC LIMIT ?)", (kind, kind, keep))
    return True


def stamp(kind: str, cache_dir: Optional[Path] = None) -> tuple:
    """Changes whenever a mark of this kind is added, replaced or removed (for readers' memos)."""
    return tuple(_conn(cache_dir).execute(
        "SELECT count(*), max(rowid), max(t), total(length(data)) FROM marks WHERE kind = ?", (kind,)).fetchone())


def migrate_json(kind: str, legacy: Path, parse: Callable[[Path], Optional[List[Tuple[str, dict]]]]) -> bool:
    """Once per legacy file: parse(legacy) -> [(subject, data)] (None: unreadable, the file is left
    alone and tried again), inserted in order into the user DB beside it, then the file is renamed
    <name>.migrated. A stray file written later by old code is ignored. True when it migrated."""
    from app.music_brain import db

    legacy = Path(legacy)
    marker = f"file:{legacy.name}"
    conn = _conn(legacy.parent)
    if is_marked("_migrated", marker, legacy.parent) or not legacy.is_file():
        return False
    got = parse(legacy)
    if got is None:
        return False
    with db.tx(conn):
        if conn.execute("SELECT 1 FROM marks WHERE kind = '_migrated' AND subject = ?", (marker,)).fetchone():
            return False
        t = time.time()
        for i, (s, d) in enumerate(got):
            conn.execute("INSERT OR REPLACE INTO marks (kind, subject, data, t) VALUES (?, ?, ?, ?)",
                         (kind, s, json.dumps(d, ensure_ascii=False, default=str), t + i * 1e-6))
        conn.execute("INSERT OR REPLACE INTO marks (kind, subject, data, t) VALUES ('_migrated', ?, '{}', ?)",
                     (marker, t))
        db.retire(legacy)
    return True
