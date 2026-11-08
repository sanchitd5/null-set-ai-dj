"""MACROS: fully specified, replayable sets and transitions (the macros table of CACHE_DIR/app.db).

A macro is what the owner can reproduce: ordered track ids and, per transition, the
recipe, A's exit and B's entry (song seconds, on 8-bar lines), the merge -> hold plan,
the tempo / key-lock decision and any learned / artist move with its parameters.
The console plays it step by step (macro-mode.js) and still runs every safety gate:
a stored decision that is no longer valid is logged and falls back.

Sources: the console's current set (POST /api/macros), a past session's logs
(from_session), a list of picks ordered by the pair atlas (from_picks), or the CLI:

    python3 -m app.music_brain.atlas.macros list
    python3 -m app.music_brain.atlas.macros show <name>
    python3 -m app.music_brain.atlas.macros from-session <session id> [--name N] [--dry-run]
    python3 -m app.music_brain.atlas.macros picks <id|name> <id|name> ... [--locked] [--name N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

SCHEMA = 1
NAME_RE = re.compile(r"[^a-z0-9_-]+")
MAX_STEPS = 64
TRACK_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def macros_dir(cache_dir: Optional[Path] = None) -> Path:
    """The old macros folder (migration source); its parent holds the app DB."""
    from app.music_brain.config import CACHE_DIR

    return Path(cache_dir or CACHE_DIR) / "macros"


def slug(name: str) -> str:
    s = NAME_RE.sub("-", (name or "").strip().lower()).strip("-")[:48]
    if not s:
        raise ValueError("macro name is empty")
    return s


def _num(x, lo: float = 0.0, hi: float = 36000.0) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, 3) if lo <= v <= hi else None


def clean_step(s: dict, n: int) -> dict:
    """One transition, normalised; raises ValueError on a bad step."""
    a, b = str(s.get("a") or ""), str(s.get("b") or "")
    if not TRACK_ID_RE.match(a) or not TRACK_ID_RE.match(b):
        raise ValueError(f"step {n}: track ids must be 16 hex chars")
    out = {"n": n, "a": a, "b": b, "a_name": str(s.get("a_name") or "")[:200], "b_name": str(s.get("b_name") or "")[:200],
           "recipe": str(s.get("recipe") or "Echo Out")[:60], "a_time": _num(s.get("a_time")), "b_time": _num(s.get("b_time")),
           "why": str(s.get("why") or "")[:300]}
    m = s.get("merge")
    if isinstance(m, dict):
        out["merge"] = {k: m.get(k) for k in ("hold_bars", "hold_phrases", "M", "aT", "bT", "pick", "phases") if k in m}
    t = s.get("tempo")
    if isinstance(t, dict):
        out["tempo"] = {k: t.get(k) for k in ("lock", "rate", "keylock", "pitch_percent") if k in t}
    mv = s.get("moves")
    if isinstance(mv, list):
        out["moves"] = [x for x in mv if isinstance(x, dict)][:8]
    if s.get("combo"):
        out["combo"] = str(s["combo"])[:24]
    if s.get("works") is not None:
        out["works"] = s.get("works")
    # a replay step (source replay:<session>): when it played and what the log could not say
    for k in ("seconds", "set_s"):
        if _num(s.get(k)) is not None:
            out[k] = _num(s.get(k))
    if s.get("played_at"):
        out["played_at"] = str(s["played_at"])[:8]
    if isinstance(s.get("gaps"), list):
        out["gaps"] = [str(g)[:200] for g in s["gaps"]][:12]
    return out


def normalize(macro: dict) -> dict:
    """A valid macro dict (schema, name, tracks, steps chained A->B->C), or ValueError."""
    if not isinstance(macro, dict):
        raise ValueError("macro must be an object")
    steps = macro.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise ValueError("macro needs at least one step")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"at most {MAX_STEPS} steps")
    steps = [clean_step(s, i + 1) for i, s in enumerate(steps)]
    for x, y in zip(steps, steps[1:]):
        if x["b"] != y["a"]:
            raise ValueError(f"step {y['n']} starts from {y['a']}, step {x['n']} ended on {x['b']}")
    tracks = [steps[0]["a"]] + [s["b"] for s in steps]
    out = {"schema": SCHEMA, "name": slug(macro.get("name") or "macro"), "version": int(macro.get("version") or 1),
           "parent": macro.get("parent"), "created": macro.get("created") or time.time(),
           "source": str(macro.get("source") or "console")[:80], "tracks": tracks, "steps": steps,
           "note": str(macro.get("note") or "")[:500]}
    out["title"] = " ".join(str(macro.get("title") or "").split())[:TITLE_MAX] or title_of(out)
    return out


# ------------------------------------------------------------------------------------------ titles
# Owner: "macros should have proper names". The slug stays the stable id / filename; `title` is
# what the MACRO dropdown shows, grouped by kind.

TITLE_MAX = 120
KINDS = ("studied", "chain", "combo", "yours")  # dropdown group order
_COMBO_WORDS = {"merge": "Merge-Hold", "riff": "Riff x Rap", "mashup": "Mashup", "double_drop": "Double Drop",
                "drop_swap": "Drop Swap"}
_SET_NOTE = re.compile(r"^studied (?:set|combo from) (.+?)(?: in set order:| \(position (\d+)\))")


def kind_of(m: dict) -> str:
    """studied | chain | combo | yours, from the source (else the slug)."""
    src, name = str(m.get("source") or ""), str(m.get("name") or "")
    if src.startswith("atlas:studied") or name.startswith("studied-"):
        return "studied"
    if src == "atlas:chain" or name.startswith("chain-"):
        return "chain"
    if src.startswith("atlas") or name.startswith("combo-"):
        return "combo"
    return "yours"


def move_of(m: dict) -> Optional[str]:
    """An atlas combo macro's move (merge | riff | mashup | double_drop | drop_swap), from its steps."""
    if kind_of(m) != "combo":
        return None
    return next((s.get("combo") for s in m.get("steps") or [] if isinstance(s, dict) and s.get("combo")), None)


def song_label(name: str) -> str:
    """'Artist - Title' without upload noise ('(Official Audio)', '[Odd One Out]', lyric tags)."""
    from app.ui.services.track_identity import clean_identity
    raw = str(name or "").strip()
    if not raw:
        return "?"
    artist, title = clean_identity(raw)
    return title if artist == "Unknown" else f"{artist} - {title}"


def _artist(name: str) -> str:
    from app.ui.services.track_identity import clean_identity
    artist, title = clean_identity(str(name or "").strip() or "?")
    return title if artist == "Unknown" else artist


def set_label(set_title: str, dj: str = "") -> str:
    """'Anyma | Live from Atomium' -> 'Anyma @ Live from Atomium' (the title if it has no DJ prefix)."""
    t = " ".join(str(set_title or "").split())
    parts = re.split(r"\s+[|\-–—]\s+", t, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1] and (not dj or parts[0].lower() == str(dj).lower()):
        return f"{parts[0]} @ {parts[1]}"
    return t


def title_of(m: dict) -> str:
    """A human title for a normalised macro that has none (see the dropdown groups)."""
    steps = m.get("steps") or []
    names = [steps[0].get("a_name") or steps[0]["a"]] + [s.get("b_name") or s["b"] for s in steps] if steps else []
    n = len(names)
    kind = kind_of(m)
    pair = f"{song_label(names[0])} → {song_label(names[-1])}" if n else str(m.get("name") or "macro")
    if kind == "studied":
        hit = _SET_NOTE.match(str(m.get("note") or ""))
        where = set_label(hit.group(1)) if hit else "studied set"
        if str(m.get("name") or "").startswith("studied-set-") or n > 2:
            return f"{where} (studied set, {n} songs)"[:TITLE_MAX]
        return f"{where} #{hit.group(2) if hit and hit.group(2) else '?'}: {pair}"[:TITLE_MAX]
    if kind == "chain":
        return f"{_artist(names[0])} → {_artist(names[-1])} · {n} songs"[:TITLE_MAX]
    if kind == "combo":
        if str(m.get("source")) == "atlas:seed" or str(m.get("name") or "").startswith("combo-seed-"):
            return f"Seed combo: {pair}"[:TITLE_MAX]
        combo = next((s.get("combo") for s in steps if s.get("combo")), None)
        tag = _COMBO_WORDS.get(str(combo), str(combo or "").replace("_", " ").title())
        return (f"{pair} ({tag})" if tag else pair)[:TITLE_MAX]
    return (pair if n == 2 else f"{pair} · {n} songs")[:TITLE_MAX]


def save(macro: dict, cache_dir: Optional[Path] = None, new_version: bool = True) -> dict:
    """Write a macro. An existing name is never overwritten: new_version saves it as
    <name>-v<k> (version k, parent = the name it came from); else ValueError. One transaction,
    so two saves of the same name get two versions."""
    from app.music_brain import db

    m = normalize(macro)
    conn = _mdb(cache_dir)
    with db.tx(conn):
        base = m["name"]
        if _get(conn, base) is not None:
            if not new_version:
                raise ValueError(f"macro {base} exists")
            root = re.sub(r"-v\d+$", "", base)
            k = 2
            while _get(conn, f"{root}-v{k}") is not None:
                k += 1
            m["parent"], m["name"], m["version"] = base, f"{root}-v{k}", k
        m["created"] = time.time()
        _put(conn, m)
    return m


def write_seed(macro: dict, cache_dir: Optional[Path] = None, owner: str = "atlas") -> dict:
    """A ready-made macro from the atlas (source atlas:*, or another `owner` prefix such as
    "tracklist"): overwrites only a macro that owner wrote before, never one the user saved
    (ValueError then)."""
    from app.music_brain import db

    m = normalize(macro)
    if not m["source"].startswith(owner):
        raise ValueError(f"seed macros come from {owner}")
    conn = _mdb(cache_dir)
    with db.tx(conn):
        old = _get(conn, m["name"])
        if old is not None:
            if not str(old.get("source", "")).startswith(owner):
                raise ValueError(f"macro {m['name']} is the user's")
            if "created" in old and {k: v for k, v in old.items() if k != "created"} == \
                    {k: v for k, v in m.items() if k != "created"}:
                return dict(m, created=old["created"])      # unchanged: keep its created, write nothing
        _put(conn, m)
    return m


def load(name: str, cache_dir: Optional[Path] = None) -> dict:
    raw = _get(_mdb(cache_dir), slug(name))
    if raw is None:
        raise KeyError(name)
    return normalize(raw)


def list_macros(cache_dir: Optional[Path] = None) -> List[dict]:
    out = []
    for m in stored(cache_dir).values():
        if isinstance(m, dict) and m.get("schema") == SCHEMA:
            title = m.get("title")
            if not title:
                try:
                    title = normalize(m)["title"]
                except ValueError:
                    title = m.get("name")
            out.append({"name": m.get("name"), "title": title, "kind": kind_of(m), "move": move_of(m), "version": m.get("version"),
                        "parent": m.get("parent"),
                        "source": m.get("source"), "created": m.get("created"), "songs": len(m.get("tracks") or []),
                        "steps": len(m.get("steps") or []), "tracks": m.get("tracks") or []})
    out.sort(key=lambda x: -(x["created"] or 0))
    return out


def backfill_titles(cache_dir: Optional[Path] = None) -> List[str]:
    """Give every stored macro without a title its derived one (writes only the macros table).
    The slug, steps and source are left as they are. -> names written."""
    from app.music_brain import db

    done = []
    conn = _mdb(cache_dir)
    with db.tx(conn):
        for name, raw in _all(conn).items():
            if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or raw.get("title"):
                continue
            try:
                raw["title"] = normalize(raw)["title"]
            except ValueError:
                continue
            _put(conn, raw, name)
            done.append(name)
    return done


# ------------------------------------------------------------------------------------------ storage
# SQLite, the APP DB (app.music_brain.db): the macros of CACHE_DIR live in CACHE_DIR/app.db, table
# macros (name, source, created, data = the macro JSON exactly as saved). The old folder
# CACHE_DIR/macros/<name>.json migrates once on first open and is renamed macros.migrated/.
MACRO_STEPS = (
    """CREATE TABLE macros (name TEXT PRIMARY KEY, source TEXT, created REAL, data TEXT NOT NULL);
    CREATE INDEX macros_source ON macros (source);
    CREATE TABLE macros_migrated (dir TEXT PRIMARY KEY, at REAL)""",
)


def _mdb(cache_dir: Optional[Path] = None):
    from app.music_brain import db

    d = macros_dir(cache_dir)
    conn = db.connect(d.parent / db.APP_DB)
    db.ensure(conn, "macros", MACRO_STEPS)
    if d.is_dir() and conn.execute("SELECT 1 FROM macros_migrated WHERE dir = ?", (d.name,)).fetchone() is None:
        with db.tx(conn):
            if conn.execute("SELECT 1 FROM macros_migrated WHERE dir = ?", (d.name,)).fetchone() is None:
                for p in sorted(d.glob("*.json")):
                    try:
                        raw = json.loads(p.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue                             # unreadable: stays in macros.migrated/
                    if isinstance(raw, dict) and conn.execute("SELECT 1 FROM macros WHERE name = ?",
                                                              (p.stem,)).fetchone() is None:
                        _put(conn, raw, p.stem)
                conn.execute("INSERT INTO macros_migrated (dir, at) VALUES (?, ?)", (d.name, time.time()))
                db.retire(d)
    return conn


def _get(conn, name: str) -> Optional[dict]:
    row = conn.execute("SELECT data FROM macros WHERE name = ?", (name,)).fetchone()
    return json.loads(row[0]) if row else None


def _put(conn, m: dict, name: Optional[str] = None) -> None:
    created = m.get("created")
    conn.execute("INSERT OR REPLACE INTO macros (name, source, created, data) VALUES (?, ?, ?, ?)",
                 (name or m["name"], str(m.get("source") or ""),
                  created if isinstance(created, (int, float)) else None, json.dumps(m, indent=1)))


def _all(conn) -> Dict[str, dict]:
    out = {}
    for name, data in conn.execute("SELECT name, data FROM macros ORDER BY name"):
        try:
            out[name] = json.loads(data)
        except ValueError:
            continue
    return out


def stored(cache_dir: Optional[Path] = None) -> Dict[str, dict]:
    """Every stored macro as saved ({name: raw dict}, by name), normalised or not."""
    return _all(_mdb(cache_dir))


def exists(name: str, cache_dir: Optional[Path] = None) -> bool:
    return _get(_mdb(cache_dir), name) is not None


def put_new(macro: dict, cache_dir: Optional[Path] = None) -> bool:
    """Store `macro` as is under its name unless that name exists (knowledge seed: the local
    macro wins). True when written."""
    from app.music_brain import db

    conn = _mdb(cache_dir)
    with db.tx(conn):
        if _get(conn, macro["name"]) is not None:
            return False
        _put(conn, macro)
    return True


def delete(name: str, cache_dir: Optional[Path] = None) -> bool:
    from app.music_brain import db

    conn = _mdb(cache_dir)
    with db.tx(conn):
        return conn.execute("DELETE FROM macros WHERE name = ?", (name,)).rowcount > 0


def validate(macro: dict, known: Callable[[str], bool], has_stems: Callable[[str], bool] = lambda t: True) -> List[dict]:
    """Per step: what would make the stored decision invalid now (the console re-checks the
    live gates; this is the offline part). [{n, ok, issues[]}]"""
    out = []
    for s in macro["steps"]:
        issues = []
        for side in ("a", "b"):
            if not known(s[side]):
                issues.append(f"track {s[side]} not in the library")
        if (s.get("merge") or re.search(r"merge|mashup|stem", s["recipe"], re.I)) and \
                not (has_stems(s["a"]) and has_stems(s["b"])):
            issues.append("stems missing: the stem move falls back")
        if s["a_time"] is None:
            issues.append("no stored exit point: the console picks one")
        out.append({"n": s["n"], "ok": not issues, "issues": issues})
    return out


def resolve_ids(macro: dict, resolve: Callable[[str], str]) -> dict:
    """The macro with every track id mapped to its LIBRARY id (resolve: the dedup alias
    map, e.g. dedup_songs.resolve_alias), so the console loads and plays the library copy
    of each song, never a set clip or a removed duplicate. A copy; the input is unchanged."""
    m = json.loads(json.dumps(macro))
    for s in m["steps"]:
        for side in ("a", "b"):
            tid = resolve(s[side])
            if tid != s[side]:
                s.setdefault("resolved", {})[side] = s[side]
                s[side] = tid
    m["tracks"] = [resolve(t) for t in m.get("tracks") or []]
    return m


# ---------------------------------------------------------------- sources

def _read_jsonl(p: Path) -> List[dict]:
    try:
        rows = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        return rows
    except OSError:
        return []


def from_session(session_id: str, cache_dir: Optional[Path] = None, name: Optional[str] = None) -> dict:
    """A past session (CACHE_DIR/sessions/<id>/songs/*) as a macro: the songs in play order,
    each transition's executed recipe and A's song time when it started, B's entry."""
    from app.music_brain.config import CACHE_DIR

    if not re.match(r"^[0-9A-Za-z_-]{1,64}$", session_id or ""):
        raise ValueError("bad session id")
    sdir = Path(cache_dir or CACHE_DIR) / "sessions" / session_id / "songs"
    metas = []
    for m in sorted(sdir.glob("*/meta.json")):
        try:
            metas.append((json.loads(m.read_text(encoding="utf-8")), _read_jsonl(m.parent / "steps.jsonl")))
        except (OSError, ValueError):
            continue
    metas = [x for x in metas if TRACK_ID_RE.match(str(x[0].get("track_id") or ""))]
    metas.sort(key=lambda x: x[0].get("nn") or 0)
    if len(metas) < 2:
        raise ValueError(f"session {session_id}: fewer than 2 songs logged")
    steps = []
    for (pa, sa), (pb, sb) in zip(metas, metas[1:]):
        ts = next((x for x in sa if x.get("kind") == "transition_start"), None)
        ex = next((x for x in sa if x.get("kind") == "recipe_executed" and x.get("decision") and x.get("phase") == "transition-out"), None) \
            or next((x for x in sb if x.get("kind") == "recipe_executed" and x.get("decision") and x.get("phase") == "transition-in"), None)
        recipe = (ex or {}).get("decision") or pb.get("recipe_in") or (ts or {}).get("decision") or "Echo Out"
        a_time = None
        if ts and pa.get("entry_t") is not None and pa.get("entry_song_s") is not None:
            a_time = pa["entry_song_s"] + (ts["t"] - pa["entry_t"])
        elif pa.get("exit_song_s") is not None:
            a_time = pa["exit_song_s"]
        merge = next((x.get("result") for x in sa if x.get("kind") == "merge_audition" and isinstance(x.get("result"), dict)), None)
        steps.append({"a": pa["track_id"], "b": pb["track_id"], "a_name": pa.get("name"), "b_name": pb.get("name"),
                      "recipe": recipe, "a_time": a_time, "b_time": pb.get("entry_song_s"),
                      "why": f"played in session {session_id}", "merge": merge,
                      "tempo": {"lock": None, "bpm_a": pa.get("bpm"), "bpm_b": pb.get("bpm")}})
    return normalize({"name": name or f"session-{session_id}", "source": f"session:{session_id}", "steps": steps})


def from_picks(ids: Sequence[str], atlas: dict, locked: bool = False, name: Optional[str] = None) -> dict:
    """PLAN FROM PICKS: order the picks for the best flow over the atlas (or keep the order
    when locked) and fill every transition from the atlas."""
    from app.music_brain.atlas import pair_atlas as pa

    ids = [i for i in ids if i in atlas["tracks"]]
    if len(ids) < 2:
        raise ValueError("pick at least 2 songs the atlas knows")
    plan = pa.order_picks(atlas, ids, locked=locked)
    steps = []
    for st in plan["steps"]:
        p = atlas["pairs"].get(f"{st['a']}>{st['b']}")
        pl = pa.plan_of(p) if p else {"recipe": "Echo Out", "a_time": None, "b_time": None, "merge": None, "lock": None}
        tn = lambda t: (atlas["tracks"].get(t) or {}).get("name")  # noqa: E731 -- unknown pairs carry no names
        steps.append({"a": st["a"], "b": st["b"], "a_name": st.get("a_name") or tn(st["a"]),
                      "b_name": st.get("b_name") or tn(st["b"]),
                      "recipe": pl["recipe"], "a_time": pl["a_time"], "b_time": pl["b_time"], "merge": pl["merge"],
                      "tempo": {"lock": pl["lock"]}, "combo": pl.get("combo"), "works": st.get("works"),
                      "why": f"atlas: {st.get('best')} (works {st.get('works')})"})
    return normalize({"name": name or "picks", "source": "picks" + (":locked" if locked else ""), "steps": steps})


# ---------------------------------------------------------------- CLI

def main(argv: Optional[Sequence[str]] = None) -> int:
    from app.music_brain.config import CACHE_DIR
    from app.music_brain.atlas import pair_atlas

    ap = argparse.ArgumentParser(prog="macros")
    ap.add_argument("cmd", choices=("list", "show", "from-session", "picks", "titles"))
    ap.add_argument("arg", nargs="*")
    ap.add_argument("--cache-dir", default=str(CACHE_DIR))
    ap.add_argument("--name", default=None)
    ap.add_argument("--locked", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print, do not save")
    a = ap.parse_args(argv)
    cd = Path(a.cache_dir)
    try:
        if a.cmd == "list":
            out = {"macros": list_macros(cd)}
        elif a.cmd == "show":
            out = load(a.arg[0], cd)
        elif a.cmd == "titles":
            out = {"titled": backfill_titles(cd)}
        elif a.cmd == "from-session":
            out = from_session(a.arg[0], cd, a.name)
            if not a.dry_run:
                out = save(out, cd)
        else:
            atlas = pair_atlas.load(cd)
            if atlas is None:
                raise ValueError("no atlas: run `python3 -m app.music_brain.atlas.pair_atlas build`")
            ids = [pair_atlas.resolve(atlas, x) for x in a.arg]
            out = from_picks([i for i in ids if i], atlas, a.locked, a.name)
            if not a.dry_run:
                out = save(out, cd)
    except (ValueError, KeyError, IndexError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
