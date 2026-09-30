"""$Up3R-M@SS!V3-M0v3: the owner's saved stem-mashup variants (mashup_mix.build_plan plans) and the pure
plan maths the live console needs to play one from any of its songs.

A variant is a whole build_plan plan (JSON: songs with their cores, parts and output clock, 8-bar sections).
Stored in the USER DB (CACHE_DIR/user.db, owner data like the liked / veto marks), own table
`supermove_variants` under its own schema_version store "supermoves": no existing table or row is touched.

    from app.music_brain import supermove as smv
    smv.save_variant(plan, "v1");  smv.list_variants();  smv.load_variant("v1", start=2)

CLI (writes the live DB only after a backup copy of it):
    python3 -m app.music_brain.supermove save --plan PLAN.json --drop-first 1 --name v1 [--fixture OUT.json]
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import List, Optional

NAME = "$Up3R-M@SS!V3-M0v3"
MIN_SONGS = 4
VARIANT_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
TRACK_ID_RE = re.compile(r"^[0-9a-f]{16}$")

STEPS = (
    """CREATE TABLE supermove_variants (move TEXT NOT NULL, name TEXT NOT NULL, title TEXT, plan TEXT NOT NULL,
        t REAL NOT NULL, PRIMARY KEY (move, name))""",
)


# ---- pure plan maths ------------------------------------------------------------------------------------------------

def _vocal_mutes(song: dict) -> List[list]:
    return next((list(map(list, p["mute"])) for p in song["parts"] if p["stem"] == "vocals" and p["role"] == "core"), [])


def from_song(plan: dict, k: int) -> dict:
    """The plan from its song k on: songs before k and k's own lead-in (their step into k) are gone, the output clock
    starts on k's core (0.0) and every later song keeps its parts exactly, shifted. Sections are rebuilt on the new
    clock, so nothing hangs where the dropped songs were. k = 0 returns a copy."""
    from app.music_brain.render import mashup_mix as mm

    songs = plan["songs"]
    if not 0 <= k <= len(songs) - 2:
        raise ValueError(f"from_song: {k} leaves fewer than 2 of {len(songs)} songs")
    if k == 0:
        return copy.deepcopy(plan)
    bar = plan["bar_s"]
    off = songs[k]["core_out"][0]
    kept = songs[k:]
    n = len(kept)
    sl, out = [], []
    for j, s in enumerate(kept):
        lead, lead_end = s["core_out"][0] - off, s["core_out"][1] - off
        mutes = [[a - off, b - off] for a, b in _vocal_mutes(s)]
        e = s["enter_bars"]
        if j == 0:
            e = 0
            # vocal_mutes' "the song before plays its drums under my first EXIT_BARS" window: that song is gone
            x = songs[k - 1]["exit_bars"] * bar
            mutes = [m for m in mutes if not (abs(m[0] - lead) < 1e-6 and abs(m[1] - lead - x) < 1e-6)]
        c = {"i": j, "enter_bars": e, "core_bars": s["core"]["bars"], "exit_bars": s["exit_bars"],
             "pre": lead - e * bar, "lead": lead, "lead_end": lead_end, "post": s["out"][1] - off, "vocal_mute": mutes}
        sl.append(c)
        ns = copy.deepcopy(s)
        ns.update(i=j, enter_bars=e, out=[c["pre"], c["post"]], core_out=[lead, lead_end],
                  parts=mm._parts(c, j == 0, j == n - 1, bar))
        if j == 0:
            ns["src"] = [s["core"]["start"], s["src"][1]]
        out.append(ns)
    total = sl[-1]["post"]
    res = {k2: copy.deepcopy(v) for k2, v in plan.items() if k2 not in ("songs", "sections")}
    shape = dict(res.get("shape") or {})
    if isinstance(shape.get("climax_index"), int):
        shape["climax_index"] = shape["climax_index"] - k if shape["climax_index"] >= k else None
    prev = res.get("variant") or {}
    res.update(shape=shape, duration=total, songs=out, sections=mm.sections(out, sl, bar, total),
               variant={"move": NAME, "from": prev.get("from", 0) + k,
                        "dropped": list(prev.get("dropped", [])) + [s["name"] for s in songs[:k]]})
    return res


def validate(plan: dict) -> List[str]:
    """Problems that keep a plan from being a playable variant (empty list: fine)."""
    bad = []
    if not isinstance(plan, dict) or plan.get("kind") != "stem_mashup":
        return ["not a stem_mashup plan"]
    songs = plan.get("songs") or []
    if len(songs) < MIN_SONGS:
        bad.append(f"{len(songs)} songs, a variant needs at least {MIN_SONGS}")
    for s in songs:
        if not TRACK_ID_RE.match(str(s.get("id") or "")):
            bad.append(f"song {s.get('i')}: no track id")
        if not s.get("parts") or not s.get("core_out"):
            bad.append(f"song {s.get('i')}: no parts / core")
    if not (plan.get("bpm") or 0) > 0:
        bad.append("no bpm")
    return bad


def summary(move: str, name: str, title: Optional[str], plan: dict, t: float) -> dict:
    songs = plan.get("songs") or []
    return {"move": move, "name": name, "title": title or move, "n": len(songs), "bpm": plan.get("bpm"),
            "duration": plan.get("duration"), "first_half": len(songs) // 2, "t": t,
            "songs": [{"i": s["i"], "id": s["id"], "name": s["name"], "level": s.get("level"), "bpm": s.get("bpm"),
                       "core": {"start": s["core"]["start"], "end": s["core"]["end"]}, "enter_bars": s["enter_bars"]}
                      for s in songs]}


# ---- store (USER DB) ------------------------------------------------------------------------------------------------

def _conn(cache_dir: Optional[Path] = None):
    from app.music_brain import db

    conn = db.connect(db.db_path(cache_dir, db.USER_DB))
    db.ensure(conn, "supermoves", STEPS)
    return conn


def save_variant(plan: dict, name: str, title: Optional[str] = None, move: str = NAME,
                 cache_dir: Optional[Path] = None) -> dict:
    """Insert or replace one variant. -> its summary."""
    from app.music_brain import db

    if not VARIANT_RE.match(name or ""):
        raise ValueError("variant name: 1-40 of a-z 0-9 _ -")
    bad = validate(plan)
    if bad:
        raise ValueError("; ".join(bad))
    t = time.time()
    conn = _conn(cache_dir)
    with db.tx(conn):
        conn.execute("INSERT OR REPLACE INTO supermove_variants (move, name, title, plan, t) VALUES (?, ?, ?, ?, ?)",
                     (move, name, title, json.dumps(plan, ensure_ascii=False), t))
    return summary(move, name, title, plan, t)


def list_variants(cache_dir: Optional[Path] = None, move: str = NAME) -> List[dict]:
    """Saved variants of `move`, oldest first (the first saved is the default)."""
    from app.music_brain import db

    conn = _conn(cache_dir)
    with db.read(conn):
        rows = conn.execute("SELECT name, title, plan, t FROM supermove_variants WHERE move = ? ORDER BY t, name",
                            (move,)).fetchall()
    out = []
    for name, title, raw, t in rows:
        try:
            out.append(summary(move, name, title, json.loads(raw), t))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def load_variant(name: str, start: int = 0, cache_dir: Optional[Path] = None, move: str = NAME) -> dict:
    """The stored plan, from its song `start` on (from_song). KeyError when there is no such variant."""
    from app.music_brain import db

    conn = _conn(cache_dir)
    with db.read(conn):
        row = conn.execute("SELECT plan FROM supermove_variants WHERE move = ? AND name = ?", (move, name)).fetchone()
    if not row:
        raise KeyError(name)
    plan = json.loads(row[0])
    return from_song(plan, start) if start else plan


def delete_variant(name: str, cache_dir: Optional[Path] = None, move: str = NAME) -> bool:
    from app.music_brain import db

    conn = _conn(cache_dir)
    with db.tx(conn):
        return conn.execute("DELETE FROM supermove_variants WHERE move = ? AND name = ?", (move, name)).rowcount > 0


# ---- hand back ------------------------------------------------------------------------------------------------------

def handback_pick(a: str, level: Optional[int], played: Optional[set] = None, cache_dir: Optional[Path] = None) -> Optional[dict]:
    """The song the move hands back to after its last song `a`: an atlas partner whose Bass Swap passes (the atlas
    ran the live key / tempo / stem gates for it) and whose energy level is exactly one under `level` (else the
    nearest lower level). Best Bass Swap score, then pair works. None when the atlas has no such partner."""
    from app.music_brain import db

    played = set(played or ())
    conn = db.connect(db.db_path(cache_dir, db.APP_DB))
    try:
        with db.read(conn):
            rows = conn.execute("SELECT p.b, p.works, p.data, t.name, t.level FROM atlas_pairs p JOIN atlas_tracks t "
                                "ON t.id = p.b WHERE p.a = ?", (a,)).fetchall()
    except sqlite3.OperationalError:
        return None
    cands = []
    for b, works, data, bname, blevel in rows:
        if b in played or b == a or blevel is None:
            continue
        try:
            bs = (json.loads(data).get("moves") or {}).get("bass_swap") or [0, 0]
        except ValueError:
            continue
        if not bs[0]:
            continue
        if level is not None and blevel >= level:
            continue
        gap = (level - 1 - blevel) if level is not None else 0
        cands.append((abs(gap), -(bs[1] or 0), -(works or 0), b, bname, blevel, bs[1], works))
    if not cands:
        return None
    cands.sort()
    _, _, _, b, bname, blevel, score, works = cands[0]
    return {"b": b, "b_name": bname, "level": blevel, "recipe": "Bass Swap", "bass_score": score, "works": works}


# ---- CLI -----------------------------------------------------------------------------------------------------------

def backup_user_db(cache_dir: Path) -> Optional[Path]:
    """A consistent copy of CACHE_DIR/user.db (sqlite backup API, WAL included) beside it. None: no DB yet."""
    src = Path(cache_dir) / "user.db"
    if not src.exists():
        return None
    dest = src.with_name(f"user.db.bak-supermove-{time.strftime('%Y%m%d-%H%M%S')}")
    a = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    b = sqlite3.connect(str(dest))
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=f"{NAME} variants")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("save", help="store a build_plan plan (optionally without its first songs) as a variant")
    s.add_argument("--plan", required=True)
    s.add_argument("--drop-first", type=int, default=0)
    s.add_argument("--name", required=True)
    s.add_argument("--title", default=None)
    s.add_argument("--cache", default=None, help="cache dir (default: the app's CACHE_DIR)")
    s.add_argument("--fixture", default=None, help="also write the stored plan to this JSON file")
    sub.add_parser("list").add_argument("--cache", default=None)
    args = ap.parse_args(argv)
    from app.music_brain.config import CACHE_DIR

    cache = Path(args.cache) if args.cache else CACHE_DIR
    try:
        if args.cmd == "list":
            print(json.dumps(list_variants(cache), indent=1))
            return 0
        plan = json.loads(Path(args.plan).read_text())
        if args.drop_first:
            plan = from_song(plan, args.drop_first)
        if args.fixture:
            Path(args.fixture).write_text(json.dumps(plan, indent=1, ensure_ascii=False) + "\n")
        bak = backup_user_db(cache)
        out = save_variant(plan, args.name, args.title, cache_dir=cache)
        out["backup"] = str(bak) if bak else None
        out["db"] = str(cache / "user.db")
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, OSError) as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
