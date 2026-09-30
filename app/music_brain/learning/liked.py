"""LIKED transitions: the owner's favourite moves, kept exactly as they played.

The positive twin of the "bad pair" veto. One entry per pair A -> B (CACHE_DIR/user.db, marks of kind
"liked": user data, never exported to knowledge/; the pre-DB liked.json migrates once, .migrated):

    {"a", "b", "a_name", "b_name", "step": <macro step as played>, "replay": bool,
     "source": "session:<id>#<n>" | "seed", "note", "at"}

A liked pair counts as PLAYED_GOOD evidence in the pair atlas (pair_atlas.mine_history) and,
when "replay" is true, the console performs the stored step AS STORED whenever the pair comes up
(macro-mode.js defaultPlan: a forced plan, the live gates may only refuse it). "replay": false
keeps the record without steering the autopilot (an open question for the owner).
The step is also saved as a one-step "yours" macro (liked-<slug>) through macros.py's public API.

    python3 -m app.music_brain.learning.liked list
    python3 -m app.music_brain.learning.liked seed          # the owner's picks (liked_seed.json)
    python3 -m app.music_brain.learning.liked like <session> <n> [--note N] [--no-replay]
    python3 -m app.music_brain.learning.liked unlike <a id> <b id>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

TRACK_ID_RE = re.compile(r"^[0-9a-f]{16}$")
SEED_FILE = Path(__file__).with_name("liked_seed.json")
MAX_LIKED = 500


MARK = "liked"


def _file(cache_dir: Optional[Path]) -> Path:
    """The pre-DB CACHE_DIR/liked.json (migration source); its folder holds user.db."""
    from app.music_brain.config import CACHE_DIR
    return Path(cache_dir or CACHE_DIR) / "liked.json"


def _parse(p: Path):
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None                                   # broken: left alone, tried again next open
    items = doc.get("liked") if isinstance(doc, dict) else None
    return [(k, v) for k, v in (items or {}).items() if isinstance(v, dict)]


def _dir(cache_dir: Optional[Path]) -> Path:
    """The folder whose user.db holds the liked marks (migrates liked.json there once)."""
    from app.music_brain import user_marks as um

    p = _file(cache_dir)
    um.migrate_json(MARK, p, _parse)
    return p.parent


def load(cache_dir: Optional[Path] = None) -> Dict[str, dict]:
    """{"A>B": entry}, oldest first; empty when the store cannot be read."""
    import sqlite3

    from app.music_brain import user_marks as um

    try:
        return {k: v for k, v in um.rows(MARK, _dir(cache_dir)) if isinstance(v, dict)}
    except (sqlite3.Error, OSError):
        return {}


def _clean(step: dict) -> dict:
    """The stored step through the macro normaliser (same limits as any macro step)."""
    from app.music_brain.atlas import macros as mc
    s = mc.clean_step(step, 1)
    s.pop("n", None)
    return s


def like(step: dict, source: str, note: str = "", replay: bool = True, cache_dir: Optional[Path] = None,
         save_macro: bool = True) -> dict:
    """Keep A -> B exactly as `step` (a macro step: recipe, exit, entry, merge, tempo, moves)."""
    from app.music_brain import db
    from app.music_brain import user_marks as um

    s = _clean(step)
    key = f"{s['a']}>{s['b']}"
    entry = {"a": s["a"], "b": s["b"], "a_name": s.get("a_name"), "b_name": s.get("b_name"), "step": s,
             "replay": bool(replay), "source": str(source or "console")[:80], "note": str(note or "")[:300],
             "at": time.time()}
    d = _dir(cache_dir)
    conn = um._conn(d)
    with db.tx(conn):                                 # set + prune in one transaction
        um.mark(MARK, key, entry, d)
        conn.execute("DELETE FROM marks WHERE kind = ? AND rowid NOT IN (SELECT rowid FROM marks "
                     "WHERE kind = ? ORDER BY t DESC, rowid DESC LIMIT ?)", (MARK, MARK, MAX_LIKED))
    if save_macro:
        entry["macro"] = _save_macro(entry, cache_dir)
    return entry


def unlike(a: str, b: str, cache_dir: Optional[Path] = None) -> bool:
    from app.music_brain import user_marks as um

    return um.unmark(MARK, f"{a}>{b}", _dir(cache_dir))


def get(a: str, b: str, cache_dir: Optional[Path] = None) -> Optional[dict]:
    return load(cache_dir).get(f"{a}>{b}")


def pairs(cache_dir: Optional[Path] = None) -> List[tuple]:
    """(a, b) of every liked transition: PLAYED_GOOD evidence for the pair atlas."""
    return [(v["a"], v["b"]) for v in load(cache_dir).values() if TRACK_ID_RE.match(str(v.get("a") or ""))
            and TRACK_ID_RE.match(str(v.get("b") or ""))]


def _save_macro(entry: dict, cache_dir: Optional[Path]) -> Optional[str]:
    """The liked step as a one-step "yours" macro; an identical one already saved is reused."""
    from app.music_brain.atlas import macros as mc
    step = dict(entry["step"], why=f"liked ({entry['source']})")
    title = f"Liked: {mc.song_label(entry.get('a_name') or entry['a'])} > {mc.song_label(entry.get('b_name') or entry['b'])}"
    name = mc.slug(f"liked-{entry['a'][:6]}-{entry['b'][:6]}")
    m = {"name": name, "title": title, "source": "liked", "steps": [step], "note": entry.get("note") or ""}
    try:
        old = mc.load(name, cache_dir)
        if [{k: v for k, v in s.items() if k != "n"} for s in old.get("steps") or []] == \
                [{k: v for k, v in s.items() if k != "n"} for s in mc.normalize(m)["steps"]]:
            return old["name"]
    except (KeyError, ValueError, OSError):
        pass
    try:
        return mc.save(m, cache_dir, new_version=True)["name"]
    except (ValueError, OSError):
        return None


def like_played(session: str, n: int, note: str = "", replay: bool = True, cache_dir: Optional[Path] = None) -> dict:
    """Like transition n of a past session, exactly as it played (the replay step)."""
    from app.music_brain.learning import replay as rp
    r = rp.build(session, int(n), int(n), cache_dir)
    step = r["macro"]["steps"][0]
    return like(step, f"session:{session}#{int(n)}", note, replay, cache_dir)


def seed(cache_dir: Optional[Path] = None, path: Path = SEED_FILE) -> List[dict]:
    """The owner's picks (liked_seed.json: each step as built from its session log, kept in the file
    because session logs are pruned). An entry already liked is left as it is."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    have = load(cache_dir)
    out = []
    for e in doc.get("liked") or []:
        step = e["step"]
        if f"{step['a']}>{step['b']}" in have:
            continue
        out.append(like(step, e.get("source") or "seed", e.get("note") or "", e.get("replay", True), cache_dir))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="liked")
    ap.add_argument("cmd", choices=("list", "seed", "like", "unlike"))
    ap.add_argument("arg", nargs="*")
    ap.add_argument("--note", default="")
    ap.add_argument("--no-replay", action="store_true")
    ap.add_argument("--cache-dir", default=None)
    a = ap.parse_args(argv)
    cd = Path(a.cache_dir) if a.cache_dir else None
    try:
        if a.cmd == "list":
            out = {"liked": list(load(cd).values())}
        elif a.cmd == "seed":
            out = {"seeded": seed(cd)}
        elif a.cmd == "like":
            out = like_played(a.arg[0], int(a.arg[1]), a.note, not a.no_replay, cd)
        else:
            out = {"removed": unlike(a.arg[0], a.arg[1], cd)}
    except (ValueError, KeyError, IndexError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
