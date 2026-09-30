"""Genre / era labels the model gave songs, kept across server restarts.

The suggestion filters and the library fallback (/api/library/lockable) read these labels.
They used to live only in server memory, so after a restart a Punjabi set's library
fallback found no labelled song and the set sat in HOLD LOOP (session 2026-09-30_102327).

    CACHE_DIR/app.db, table labels (file, key, genre, era): one row per title key.
    (Before: CACHE_DIR/genre_labels.json {"version": 1, "labels": {"<title key>": {...}}},
    migrated once on first open and kept as genre_labels.json.migrated.)

Keyed by the normalised clean title (server._genre_key), the same key the readers use:
the model labels songs by title, often before the song is downloaded and has a track id.
The knowledge export carries them per track id (knowledge/genre_labels.json), resolved
through each id's name, so another machine's library gets them by name.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, Tuple

FILE = "genre_labels.json"
VERSION = 1
MAX_LABELS = 5000              # the model labels ~5 songs a call; a year of sets stays far below
MAX_FIELD = 80


def path(cache_dir: Optional[Path] = None) -> Path:
    from app.music_brain.config import CACHE_DIR

    return Path(cache_dir or CACHE_DIR) / FILE


def title_key(title: str) -> str:
    from app.ui.services.track_identity import clean_title

    return " ".join(clean_title(str(title or "")).lower().split())


def name_key(name: str) -> str:
    """ "Artist - Title (Official Video)" -> the title key."""
    from app.ui.services.track_identity import clean_identity

    return title_key(clean_identity(str(name or ""))[1])


def _clean(v) -> str:
    return str(v or "").strip()[:MAX_FIELD]


# SQLite (app.music_brain.db, the APP DB): the labels at legacy path P live in P.parent/app.db,
# keyed by P's file name. labels_files marks a file name as living in the DB (its old JSON was
# migrated and renamed .migrated, or never existed); a stray JSON written later is ignored.
LABEL_STEPS = (
    """CREATE TABLE labels_files (file TEXT PRIMARY KEY, migrated_at REAL);
    CREATE TABLE labels (file TEXT NOT NULL, key TEXT NOT NULL, genre TEXT, era TEXT,
        PRIMARY KEY (file, key))""",
)


def _parse(d) -> Tuple[Dict[str, str], Dict[str, str]]:
    labels = d.get("labels") if isinstance(d, dict) else None
    genres, eras = {}, {}
    for k, v in (labels or {}).items():
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        if _clean(v.get("genre")):
            genres[k] = _clean(v.get("genre"))
        if _clean(v.get("era")):
            eras[k] = _clean(v.get("era"))
    return genres, eras


def _db(p: Path):
    """(connection, file key); the first open with the old JSON on disk migrates it once."""
    from app.music_brain import db

    p = Path(p)
    conn = db.connect(db.beside(p))
    db.ensure(conn, "labels", LABEL_STEPS)
    f = p.name
    if conn.execute("SELECT 1 FROM labels_files WHERE file = ?", (f,)).fetchone() is None and p.is_file():
        with db.tx(conn):
            if conn.execute("SELECT 1 FROM labels_files WHERE file = ?", (f,)).fetchone() is None:
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    d = None                          # unreadable: left alone, the store starts empty
                if isinstance(d, dict):
                    _write(conn, f, *_parse(d))
                    db.retire(p)
    return conn, f


def _read(conn, f: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    genres, eras = {}, {}
    for k, g, e in conn.execute("SELECT key, genre, era FROM labels WHERE file = ? ORDER BY key", (f,)):
        if g:
            genres[k] = g
        if e:
            eras[k] = e
    return genres, eras


def _write(conn, f: str, genres: Dict[str, str], eras: Dict[str, str]) -> int:
    """Make the DB hold exactly these labels (the newest MAX_LABELS keys: dicts keep insertion
    order, the server re-inserts a relabelled key). Only changed rows are written. Row count."""
    import time as _time

    want: Dict[str, tuple] = {}
    for k in list(dict.fromkeys(list(genres) + list(eras)))[-MAX_LABELS:]:
        g, e = _clean(genres.get(k)) or None, _clean(eras.get(k)) or None
        if g or e:
            want[k] = (g, e)
    have = {k: (g, e) for k, g, e in conn.execute("SELECT key, genre, era FROM labels WHERE file = ?", (f,))}
    n = 0
    for k, v in want.items():
        if have.pop(k, None) != v:
            conn.execute("INSERT OR REPLACE INTO labels (file, key, genre, era) VALUES (?, ?, ?, ?)", (f, k, *v))
            n += 1
    for k in have:
        conn.execute("DELETE FROM labels WHERE file = ? AND key = ?", (f, k))
        n += 1
    conn.execute("INSERT OR IGNORE INTO labels_files (file, migrated_at) VALUES (?, ?)", (f, _time.time()))
    return n


def load(p: Optional[Path] = None) -> Tuple[Dict[str, str], Dict[str, str]]:
    """(genres, eras) by title key; empty when there are none or the store is unreadable."""
    import sqlite3

    from app.music_brain import db

    try:
        conn, f = _db(Path(p or path()))
        with db.read(conn):
            return _read(conn, f)
    except sqlite3.DatabaseError:
        return {}, {}


def save(genres: Dict[str, str], eras: Dict[str, str], p: Optional[Path] = None) -> bool:
    """Replace the stored labels with these, in one transaction. The newest MAX_LABELS keys win
    (dicts keep insertion order, the server re-inserts a relabelled key). False on a DB error:
    never raises."""
    import sqlite3

    from app.music_brain import db

    try:
        conn, f = _db(Path(p or path()))
        with db.tx(conn):
            _write(conn, f, genres, eras)
        return True
    except (sqlite3.Error, OSError):
        return False


def merge_save(genres: Dict[str, str], eras: Dict[str, str], p: Optional[Path] = None) -> bool:
    """save() that never drops labels another process wrote: in one write transaction, stored
    labels that `genres` / `eras` lack are added in place (oldest first, so the caller's own stay
    the newest), then only the changed rows are upserted. The live server and the `label` command
    both write the store; a plain save() from the server's startup copy wiped a whole labelling
    run. False on a DB error: never raises."""
    import sqlite3

    from app.music_brain import db

    try:
        conn, f = _db(Path(p or path()))
        with db.tx(conn):
            dg, de = _read(conn, f)
            for mine, disk in ((genres, dg), (eras, de)):
                extra = {k: v for k, v in disk.items() if k not in mine}
                if extra:
                    keep = dict(mine)
                    mine.clear()
                    mine.update(extra)
                    mine.update(keep)
            _write(conn, f, genres, eras)
        return True
    except (sqlite3.Error, OSError):
        return False


def backfill(genres: Dict[str, str], eras: Dict[str, str], tracked: dict, names: Dict[str, str]) -> int:
    """Labels from the knowledge export ({track id: {genre, era}} + its names.json) for songs
    that have none yet, matched by name, so the local label always wins. No model call.
    Returns how many labels were added."""
    added = 0
    for tid, lab in (tracked or {}).items():
        if not isinstance(lab, dict) or not names.get(tid):
            continue
        k = name_key(names[tid])
        if not k:
            continue
        if _clean(lab.get("genre")) and k not in genres:
            genres[k] = _clean(lab.get("genre"))
            added += 1
        if _clean(lab.get("era")) and k not in eras:
            eras[k] = _clean(lab.get("era"))
    return added


def for_export(names: Dict[str, str], genres: Dict[str, str], eras: Dict[str, str]) -> Dict[str, dict]:
    """{track id: {genre, era}} for every named song that has a label (knowledge export)."""
    out = {}
    for tid, n in names.items():
        k = name_key(n)
        e = {x: src[k] for x, src in (("genre", genres), ("era", eras)) if src.get(k)}
        if e:
            out[tid] = e
    return dict(sorted(out.items()))


# ------------------------------------------------------------------ offline labelling
# python -m app.music_brain.analysis.genre_labels label [--missing-only|--all]
#     [--backend local|claudecode] [--overwrite] [--dry-run]
# Labels library songs by name only (nothing else leaves the machine). The backend is
# AI_REVIEW_BACKEND (default local); claudecode is the owner's own `claude` CLI login,
# see app/music_brain/llm/claudecode.py.

LABEL_BATCH = 5                # local model, same as the server's suggest calls
CLAUDECODE_LABEL_BATCH = 40
LABEL_SCHEMA = {"type": "object", "required": ["labels"], "properties": {
    "labels": {"type": "array", "items": {"type": "object", "required": ["title_key", "genre", "era"],
               "properties": {"title_key": {"type": "string"}, "genre": {"type": "string"},
                              "era": {"type": "string"}}}}}}


def _label_system() -> str:
    from app.music_brain.analysis.genre import GENRE_FAMILIES, SCENES
    vocab = sorted({w for words in GENRE_FAMILIES.values() for w in words} | set(SCENES))
    return ("You label songs for a DJ's library by name only. For each song give its genre and "
            "its era. Genre: prefer one of these words, or a short phrase built from them: "
            + ", ".join(vocab) + ". Use another short genre name only when none fits. Era: the "
            "decade the recording came out, written like \"1990s\" or \"2020s\"; \"\" when unsure. "
            "Copy each title_key exactly. Answer JSON: {\"labels\": [{\"title_key\": \"<as given>\", "
            "\"genre\": \"<genre>\", \"era\": \"<decade>\"}]}")


def library_titles(cache_dir: Optional[Path] = None) -> Dict[str, str]:
    """{title key: display name} for every library song: uploads/_names.json, a duplicate
    id resolved to its canonical id (track_aliases.json) so a song counts once."""
    from app.music_brain.config import CACHE_DIR
    from app.ui.services.dedup_songs import load_aliases, resolve_alias
    cache = Path(cache_dir or CACHE_DIR)
    try:
        names = json.loads((cache / "uploads" / "_names.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(names, dict):
        return {}
    aliases = load_aliases(cache)
    out: Dict[str, str] = {}
    for tid in names:
        n = names.get(resolve_alias(tid, cache, aliases)) or names[tid]
        if isinstance(n, str) and (k := name_key(n)):
            out.setdefault(k, n)
    return out


def _era(label) -> str:
    from app.music_brain.analysis.genre import decade_of
    d = decade_of(label)
    return f"{d}s" if d is not None else ""


def label_library(missing_only: bool = True, overwrite: bool = False, backend: Optional[str] = None,
                  dry_run: bool = False, cache_dir: Optional[Path] = None, chat=None,
                  log=lambda m: None) -> dict:
    """Ask the model for {genre, era} of library songs; local labels win unless overwrite.
    dry_run: counts and calls only, no model call. `chat` is for tests (skips the backend)."""
    from app.music_brain.learning import set_ai
    p = path(cache_dir)
    genres, eras = load(p)
    titles = library_titles(cache_dir)
    todo = [(k, n) for k, n in sorted(titles.items())
            if not missing_only or not (genres.get(k) and eras.get(k))]
    from app.music_brain.llm import claudecode as cc
    b = cc.backend(backend)
    batch = CLAUDECODE_LABEL_BATCH if b == "claudecode" else LABEL_BATCH
    system = _label_system()
    users = [json.dumps({"songs": [{"title_key": k, "name": n} for k, n in todo[s:s + batch]]},
                        ensure_ascii=False) for s in range(0, len(todo), batch)]
    out = {"backend": b, "songs": len(titles), "to_label": len(todo), "calls": len(users)}
    if dry_run:   # no model call, no CLI needed
        return out | {"dry_run": True, "prompt_chars": sum(len(system) + len(u) for u in users)}
    tag = ""
    if chat is None:
        chat, tag, _ = set_ai.backend_chat(LABEL_SCHEMA, b)
    added = changed = answered = 0
    for i, user in enumerate(users):
        data = set_ai._ask(system, user, chat, "labels", tag=tag)
        if data is None:
            out["ai"] = "skipped (no model answering)"
            break
        asked = {s["title_key"] for s in json.loads(user)["songs"]}
        for x in data.get("labels") if isinstance(data.get("labels"), list) else []:
            if not isinstance(x, dict) or x.get("title_key") not in asked:
                continue           # a key the model made up or changed
            answered += 1
            k = x["title_key"]
            for store, v in ((genres, _clean(str(x.get("genre") or "")).lower()), (eras, _era(x.get("era")))):
                if not v or store.get(k) == v:
                    continue
                if not store.get(k):
                    added += 1
                elif overwrite:
                    changed += 1
                else:
                    continue       # the local label wins
                store[k] = v
        log(f"labels: {min((i + 1) * batch, len(todo))}/{len(todo)}")
    if added or changed:
        out["saved"] = merge_save(genres, eras, p)
    return out | {"answered": answered, "added": added, "overwritten": changed}


def main(argv=None) -> int:
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="python -m app.music_brain.analysis.genre_labels")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("label", help="Label library songs with genre / era (names only are sent).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--missing-only", action="store_true", help="songs without a genre or era (default)")
    g.add_argument("--all", action="store_true", help="every library song")
    p.add_argument("--backend", choices=("local", "claudecode"), default=None,
                   help="default: AI_REVIEW_BACKEND, else local")
    p.add_argument("--overwrite", action="store_true", help="the model's answer replaces a local label")
    p.add_argument("--dry-run", action="store_true", help="counts and calls only, no model call")
    args = parser.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv()
        res = label_library(missing_only=not args.all, overwrite=args.overwrite, backend=args.backend,
                            dry_run=args.dry_run, log=lambda m: print(m, file=sys.stderr, flush=True))
    except Exception as exc:  # one JSON error line, never a traceback on stdout
        print(json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:400]}"}, indent=2))
        return 1
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
