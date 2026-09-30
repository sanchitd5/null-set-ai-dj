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
from typing import Dict, Optional

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
