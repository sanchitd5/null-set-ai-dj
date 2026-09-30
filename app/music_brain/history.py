"""SET HISTORY: every set the owner played, queryable (the private USER DB, CACHE_DIR/user.db).

Owner: "always have all sets in memory, a db will help here". The append-only logs stay the
source of truth and are never rewritten:

    CACHE_DIR/sessions/<id>/events.jsonl          session_log: transitions, moves, ...
    CACHE_DIR/sessions/<id>/songs/<NN>-*/meta.json song_log: one played song (deck, entry / exit)
    CACHE_DIR/set_logs/<id>.djset.json            saved console recordings (djset-v1)

This module is the index over them, rebuildable from them at any time (rebuild()):

    sessions     id, started, ended, songs, source file
    plays        session, nn, track id, name, deck, started / ended (epoch s), entry / exit song
                 position, play length, bpm, key, energy level, recipe in / out, the genre / era
                 label the song had when first indexed
    transitions  session, seq (byte offset of the event line), t, from / to (name + id), recipe,
                 planned, seconds, a_pos, b entry position, out / in deck, source (pick / macro /
                 follow set / fallback) and the macro or studied step behind it
    moves        session, seq, t, deck, song, pos, move, label, why, spec (the raw event as JSON)
    set_logs     id, created, duration, track a / b, events, the djset JSON

Never pruned: a session whose JSONL was deleted keeps its rows. Private: the knowledge export
never opens this DB (test_knowledge guard).

Ingest: the first open indexes every existing session once; the server then appends as it logs
(session_log calls on_event, song_log calls on_play, the set-log endpoint calls add_set_log).

Read API for the replay / time-travel feature: sessions(), timeline(session),
state_at(session, t), pair_plays(a, b), sessions_with(song).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Union

STORE = "history"
STEPS = (
    """CREATE TABLE sessions (id TEXT PRIMARY KEY, started REAL, ended REAL, songs INTEGER NOT NULL DEFAULT 0,
        source TEXT, offset INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE plays (session TEXT NOT NULL, nn INTEGER NOT NULL, track_id TEXT, name TEXT, deck TEXT,
        started REAL, ended REAL, entry_pos REAL, exit_pos REAL, length REAL, bpm REAL, camelot TEXT,
        energy_level INTEGER, recipe_in TEXT, recipe_out TEXT, genre TEXT, era TEXT, folder TEXT,
        PRIMARY KEY (session, nn));
    CREATE INDEX plays_track ON plays (track_id, session);
    CREATE INDEX plays_name ON plays (name, session);
    CREATE INDEX plays_time ON plays (session, started);
    CREATE TABLE transitions (session TEXT NOT NULL, seq INTEGER NOT NULL, t REAL NOT NULL,
        from_name TEXT, to_name TEXT, from_id TEXT, to_id TEXT, recipe TEXT, planned TEXT, seconds REAL,
        a_pos REAL, b_pos REAL, out_deck TEXT, in_deck TEXT, source TEXT, step TEXT,
        PRIMARY KEY (session, seq));
    CREATE INDEX transitions_pair_id ON transitions (from_id, to_id);
    CREATE INDEX transitions_pair_name ON transitions (from_name, to_name);
    CREATE INDEX transitions_time ON transitions (session, t);
    CREATE TABLE moves (session TEXT NOT NULL, seq INTEGER NOT NULL, t REAL NOT NULL, deck TEXT, song TEXT,
        pos REAL, move TEXT, label TEXT, why TEXT, spec TEXT, PRIMARY KEY (session, seq));
    CREATE INDEX moves_time ON moves (session, t);
    CREATE TABLE set_logs (id TEXT PRIMARY KEY, created TEXT, duration REAL, track_a TEXT, track_b TEXT,
        events INTEGER, data TEXT NOT NULL);
    CREATE TABLE history_meta (key TEXT PRIMARY KEY, value TEXT)""",
)

_ingested: set = set()                  # cache dirs whose first-open ingest ran in this process
_ingest_lock = threading.Lock()


def _cache(cache_dir) -> Path:
    if cache_dir is None:
        from app.music_brain.config import CACHE_DIR
        cache_dir = CACHE_DIR
    return Path(cache_dir)


def _conn(cache_dir=None, ingest: bool = True):
    """The user DB of `cache_dir`; its first open indexes every existing session once."""
    from app.music_brain import db

    cache = _cache(cache_dir)
    conn = db.connect(cache / db.USER_DB)
    db.ensure(conn, STORE, STEPS)
    key = str(cache.resolve())
    if ingest and key not in _ingested:
        with _ingest_lock:
            if key not in _ingested:
                _ingested.add(key)
                if conn.execute("SELECT 1 FROM history_meta WHERE key = 'ingested'").fetchone() is None:
                    ingest_all(cache)
                    with db.tx(conn):
                        conn.execute("INSERT OR REPLACE INTO history_meta (key, value) VALUES ('ingested', ?)",
                                     (str(time.time()),))
    return conn


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _text(v) -> Optional[str]:
    return None if v is None else str(v)


# ------------------------------------------------------------------------------------ ingest
def _index_event(conn, session: str, seq: int, ev: dict) -> None:
    """One events.jsonl line into its table (inside the caller's transaction). Idempotent."""
    t = _num(ev.get("t"))
    if t is None:
        return
    kind = ev.get("kind")
    if kind == "track" and ev.get("event") == "transition_start":
        conn.execute("INSERT OR IGNORE INTO transitions (session, seq, t, from_name, to_name, recipe, planned, "
                     "seconds, a_pos, b_pos, out_deck, in_deck) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (session, seq, t, _text(ev.get("from")), _text(ev.get("to")), _text(ev.get("recipe")),
                      _text(ev.get("planned")), _num(ev.get("seconds")), _num(ev.get("a_pos")),
                      _num(ev.get("b_pos", ev.get("b_entry"))), _text(ev.get("out")), _text(ev.get("in"))))
    elif kind == "move":
        conn.execute("INSERT OR IGNORE INTO moves (session, seq, t, deck, song, pos, move, label, why, spec) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (session, seq, t, _text(ev.get("deck")), _text(ev.get("song")), _num(ev.get("pos")),
                      _text(ev.get("move")), _text(ev.get("label")), _text(ev.get("why")),
                      json.dumps(ev, ensure_ascii=False, sort_keys=True, default=str)))
    conn.execute("INSERT INTO sessions (id, started, ended) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                 "started = min(coalesce(started, excluded.started), excluded.started), "
                 "ended = max(coalesce(ended, excluded.ended), excluded.ended)", (session, t, t))


def _label(name: str, labels) -> tuple:
    if labels is None:
        return None, None
    from app.music_brain.analysis import genre_labels as gl

    try:
        k = gl.name_key(name or "")
    except Exception:  # noqa: BLE001 -- a label is a nicety, never blocks indexing
        return None, None
    return labels[0].get(k), labels[1].get(k)


def _index_play(conn, session: str, meta: dict, folder: str = "", labels=None) -> None:
    nn = meta.get("nn")
    if not isinstance(nn, int):
        return
    start, end = _num(meta.get("entry_t")), _num(meta.get("exit_t"))
    g, e = _label(meta.get("name") or "", labels)
    conn.execute(
        "INSERT INTO plays (session, nn, track_id, name, deck, started, ended, entry_pos, exit_pos, length, bpm, "
        "camelot, energy_level, recipe_in, recipe_out, genre, era, folder) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session, nn) DO UPDATE SET "
        "track_id = excluded.track_id, name = excluded.name, deck = excluded.deck, started = excluded.started, "
        "ended = excluded.ended, entry_pos = excluded.entry_pos, exit_pos = excluded.exit_pos, "
        "length = excluded.length, bpm = excluded.bpm, camelot = excluded.camelot, "
        "energy_level = excluded.energy_level, recipe_in = excluded.recipe_in, recipe_out = excluded.recipe_out, "
        "genre = coalesce(plays.genre, excluded.genre), era = coalesce(plays.era, excluded.era), "
        "folder = excluded.folder",
        (session, nn, _text(meta.get("track_id")), _text(meta.get("name")), _text(meta.get("deck")), start, end,
         _num(meta.get("entry_song_s")), _num(meta.get("exit_song_s")),
         (end - start) if start is not None and end is not None else None, _num(meta.get("bpm")),
         _text(meta.get("key")), meta.get("energy_level") if isinstance(meta.get("energy_level"), int) else None,
         _text(meta.get("recipe_in")), _text(meta.get("recipe_out")), g, e, folder))
    if start is not None:
        conn.execute("INSERT INTO sessions (id, started, ended) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                     "started = min(coalesce(started, excluded.started), excluded.started), "
                     "ended = max(coalesce(ended, excluded.ended), excluded.ended)", (session, start, end or start))



def _source_of(s: dict) -> Optional[str]:
    k, d = s.get("kind"), str(s.get("decision") or "")
    if k == "macro" and d.startswith("PLAY MACRO"):
        return "macro"
    if k == "studied" and d == "follow":
        return "follow set"
    if k in ("deadline_fallback", "atlas_fallback"):
        return "fallback"
    return None


def _selection(steps_path: Path) -> tuple:
    """(source, step text) of the choice of the song that comes AFTER this one: song_log files
    the selection steps taken while a song plays under that (outgoing) song. The last decisive
    one wins (a macro step played, a studied set followed, a deadline fallback); else "pick"."""
    try:
        lines = steps_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, None
    rows = []
    for ln in lines:
        try:
            rows.append(json.loads(ln))
        except ValueError:
            continue
    start = next((i for i, s in enumerate(rows) if s.get("kind") == "song_start"), -1)
    src, step = "pick", None
    for s in rows[start + 1:]:
        if s.get("phase") == "selection" and _source_of(s):
            src = _source_of(s)
            step = " ".join(str(x) for x in (s.get("decision"), s.get("why")) if x)[:300] or None
    return src, step


def _link(conn, session: str, sources: Dict[int, tuple]) -> None:
    """Transitions -> ids (by name within the session's plays) and the source of the choice,
    read from the outgoing song's play."""
    plays = conn.execute("SELECT nn, name, track_id, started FROM plays WHERE session = ? ORDER BY nn",
                         (session,)).fetchall()
    ids = {n: tid for _, n, tid, _ in plays if n and tid}
    for seq, t, fn, tn in conn.execute("SELECT seq, t, from_name, to_name FROM transitions WHERE session = ?",
                                       (session,)).fetchall():
        nn = next((p[0] for p in reversed(plays) if p[1] == fn and (p[3] is None or p[3] <= t)), None)
        src, step = sources.get(nn, (None, None)) if nn is not None else (None, None)
        conn.execute("UPDATE transitions SET from_id = coalesce(?, from_id), to_id = coalesce(?, to_id), "
                     "source = coalesce(?, source), "
                     "step = coalesce(?, step) WHERE session = ? AND seq = ?",
                     (ids.get(fn), ids.get(tn), src, step, session, seq))
    conn.execute("UPDATE sessions SET songs = (SELECT count(*) FROM plays WHERE session = ?) WHERE id = ?",
                 (session, session))


def sync_session(session: str, cache_dir=None, labels=None, _conn_=None) -> dict:
    """Index what is new in one session: events past the stored byte offset, every song meta
    (upsert), then the transition links. Idempotent; returns row counts added / seen."""
    from app.music_brain import db

    cache = _cache(cache_dir)
    conn = _conn_ or _conn(cache, ingest=False)
    sdir = cache / "sessions" / session
    p = sdir / "events.jsonl"
    rep = {"events": 0, "plays": 0}
    with db.tx(conn):
        if p.exists():
            rep["events"] = _sync_events(conn, session, p, cache)
        sources = {}
        for m in sorted((sdir / "songs").glob("*/meta.json")) if (sdir / "songs").is_dir() else []:
            try:
                meta = json.loads(m.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict):
                _index_play(conn, session, meta, m.parent.name, labels)
                sources[meta.get("nn")] = _selection(m.parent / "steps.jsonl")
                rep["plays"] += 1
        _link(conn, session, sources)
    return rep


def ingest_all(cache_dir=None) -> dict:
    """Index every session and set log under `cache_dir` (incremental: only what is new)."""
    from app.music_brain.analysis import genre_labels as gl

    cache = _cache(cache_dir)
    conn = _conn(cache, ingest=False)
    t0 = time.time()
    labels = gl.load(gl.path(cache))
    rep = {"sessions": 0, "events": 0, "plays": 0, "set_logs": 0}
    sroot = cache / "sessions"
    for d in sorted(sroot.iterdir()) if sroot.is_dir() else []:
        if (d / "events.jsonl").exists() or (d / "songs").is_dir():
            r = sync_session(d.name, cache, labels, conn)
            rep["sessions"] += 1
            rep["events"] += r["events"]
            rep["plays"] += r["plays"]
    for p in sorted((cache / "set_logs").glob("*.djset.json")) if (cache / "set_logs").is_dir() else []:
        rep["set_logs"] += add_set_log(p, cache, _conn_=conn)
    rep["seconds"] = round(time.time() - t0, 2)
    return rep


def rebuild(cache_dir=None) -> dict:
    """Drop and re-index every session whose logs still exist (and every set log) from the
    JSONL / meta files. Sessions whose logs are gone keep their rows (never pruned)."""
    from app.music_brain import db

    cache = _cache(cache_dir)
    conn = _conn(cache, ingest=False)
    sroot = cache / "sessions"
    live = [d.name for d in sroot.iterdir()] if sroot.is_dir() else []
    with db.tx(conn):
        for s in live:
            for tbl in ("plays", "transitions", "moves"):
                conn.execute(f"DELETE FROM {tbl} WHERE session = ?", (s,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (s,))
        conn.execute("DELETE FROM set_logs")
    return ingest_all(cache)


def add_set_log(path: Union[str, Path], cache_dir=None, _conn_=None) -> int:
    """One saved djset-v1 recording (idempotent). 1 when indexed."""
    from app.music_brain import db

    path = Path(path)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(d, dict):
        return 0
    meta = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
    conn = _conn_ or _conn(cache_dir)
    with db.tx(conn):
        conn.execute("INSERT OR REPLACE INTO set_logs (id, created, duration, track_a, track_b, events, data) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (path.name.split(".")[0], _text(meta.get("created_at")), _num(meta.get("duration_seconds")),
                      _text(meta.get("track_a_id")), _text(meta.get("track_b_id")),
                      len(d.get("control_event_stream") or []), json.dumps(d, ensure_ascii=False)))
    return 1


# ---------------------------------------------------------------------- live hooks (never raise)
def on_event(session: str, events_path: Path) -> None:
    """session_log appended a line to CACHE/<sessions>/<session>/events.jsonl: index what is new
    in it (the stored offset makes that one line, normally)."""
    try:
        from app.music_brain import db

        p = Path(events_path)
        cache = p.parents[2]
        conn = _conn(cache)
        with db.tx(conn):
            _sync_events(conn, session, p, cache)
    except Exception:  # noqa: BLE001 -- logging must not break a live set
        pass


def _sync_events(conn, session: str, p: Path, cache: Path) -> int:
    """Index the events.jsonl lines past the session's stored byte offset (caller's
    transaction). A half-written last line waits for the next call. -> events indexed."""
    row = conn.execute("SELECT offset FROM sessions WHERE id = ?", (session,)).fetchone()
    off = row[0] if row else 0
    with open(p, "rb") as f:
        f.seek(off)
        data = f.read()
    end = data.rfind(b"\n") + 1
    pos, n = off, 0
    for raw in data[:end].split(b"\n")[:-1] if end else []:
        try:
            ev = json.loads(raw)
            if isinstance(ev, dict):
                _index_event(conn, session, pos, ev)
                n += 1
        except ValueError:
            pass
        pos += len(raw) + 1
    conn.execute("INSERT INTO sessions (id, source, offset) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                 "source = excluded.source, offset = excluded.offset",
                 (session, str(p.relative_to(cache)) if p.is_relative_to(cache) else str(p), off + end))
    if end and b'"transition_start"' in data[:end]:
        _link(conn, session, {})
    return n


_last_play: Dict[tuple, tuple] = {}


def on_play(session: str, meta: dict, folder: Path) -> None:
    """song_log wrote a song's meta.json in CACHE/<sessions>/<session>/songs/<folder>: upsert its
    play row when something in it changed (song_log rewrites meta on every step: unchanged ones
    are skipped here)."""
    try:
        cache_dir = Path(folder).parents[3]            # <cache>/<sessions>/<session>/songs/<folder>
        key = (str(cache_dir), session, meta.get("nn"))
        sig = tuple(sorted((k, str(v)) for k, v in meta.items() if k not in ("steps", "dropped")))
        if _last_play.get(key) == sig or meta.get("entry_t") is None:
            return
        _last_play[key] = sig
        from app.music_brain import db
        from app.music_brain.analysis import genre_labels as gl

        conn = _conn(cache_dir)
        labels = gl.load(gl.path(_cache(cache_dir)))
        with db.tx(conn):
            _index_play(conn, session, meta, Path(folder).name, labels)
            _link(conn, session, {meta.get("nn"): _selection(Path(folder) / "steps.jsonl")})
    except Exception:  # noqa: BLE001 -- logging must not break a live set
        pass


# ---------------------------------------------------------------------------------- read API
def _rows(cur) -> List[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def sessions(cache_dir=None) -> List[dict]:
    """Every indexed session, newest first: id, started, ended, songs, transitions, moves."""
    conn = _conn(cache_dir)
    return _rows(conn.execute(
        "SELECT s.id, s.started, s.ended, s.songs, s.source, "
        "(SELECT count(*) FROM transitions t WHERE t.session = s.id) AS transitions, "
        "(SELECT count(*) FROM moves m WHERE m.session = s.id) AS moves "
        "FROM sessions s ORDER BY s.started DESC, s.id DESC"))


def timeline(session: str, cache_dir=None) -> List[dict]:
    """Everything that happened in one session, in time order: {"type": play_start | play_end |
    transition | move, "t", ...row}."""
    conn = _conn(cache_dir)
    out = []
    for p in _rows(conn.execute("SELECT * FROM plays WHERE session = ? ORDER BY nn", (session,))):
        if p["started"] is not None:
            out.append(dict(p, type="play_start", t=p["started"]))
        if p["ended"] is not None:
            out.append(dict(p, type="play_end", t=p["ended"]))
    out += [dict(r, type="transition") for r in
            _rows(conn.execute("SELECT * FROM transitions WHERE session = ? ORDER BY t", (session,)))]
    out += [dict(r, type="move") for r in
            _rows(conn.execute("SELECT * FROM moves WHERE session = ? ORDER BY t", (session,)))]
    order = {"play_end": 0, "transition": 1, "play_start": 2, "move": 3}
    out.sort(key=lambda r: (r["t"], order[r["type"]], r.get("seq") or r.get("nn") or 0))
    return out


def state_at(session: str, t: float, cache_dir=None) -> dict:
    """What each deck was playing at epoch time t and where in the song: {"t", "decks": {deck:
    {nn, track_id, name, pos, since}}, "transition": {from, to, recipe, progress 0-1, ...} or None}.
    pos: the last logged position on that deck (a move's pos) or the song's entry point, plus the
    wall time since (tempo changes and pauses are not modelled)."""
    conn = _conn(cache_dir)
    decks: Dict[str, dict] = {}
    for p in _rows(conn.execute(
            "SELECT * FROM plays WHERE session = ? AND started <= ? AND (ended IS NULL OR ended >= ?) "
            "ORDER BY started", (session, t, t))):
        base_t, base_pos = p["started"], p["entry_pos"] or 0.0
        mv = conn.execute("SELECT t, pos FROM moves WHERE session = ? AND deck = ? AND song = ? AND t <= ? "
                          "AND t >= ? AND pos IS NOT NULL ORDER BY t DESC LIMIT 1",
                          (session, p["deck"], p["name"], t, p["started"])).fetchone()
        if mv:
            base_t, base_pos = mv
        decks[p["deck"] or "?"] = {"nn": p["nn"], "track_id": p["track_id"], "name": p["name"],
                                   "pos": round(base_pos + (t - base_t), 3), "since": p["started"]}
    tr = conn.execute("SELECT * FROM transitions WHERE session = ? AND t <= ? AND t + coalesce(seconds, 0) >= ? "
                      "ORDER BY t DESC LIMIT 1", (session, t, t))
    rows = _rows(tr)
    trans = None
    if rows:
        trans = rows[0]
        trans["progress"] = round((t - trans["t"]) / trans["seconds"], 3) if trans.get("seconds") else None
    return {"t": t, "decks": decks, "transition": trans}


def pair_plays(a: str, b: str, cache_dir=None) -> List[dict]:
    """Every time A -> B was played (ids or names), oldest first."""
    conn = _conn(cache_dir)
    return _rows(conn.execute(
        "SELECT * FROM transitions WHERE (from_id = ? AND to_id = ?) OR (from_name = ? AND to_name = ?) "
        "ORDER BY t", (a, b, a, b)))


def sessions_with(song: str, cache_dir=None) -> List[dict]:
    """Every session containing a song (track id or name), with its plays there."""
    conn = _conn(cache_dir)
    return _rows(conn.execute(
        "SELECT session, nn, track_id, name, deck, started, ended FROM plays WHERE track_id = ? OR name = ? "
        "ORDER BY started", (song, song)))
