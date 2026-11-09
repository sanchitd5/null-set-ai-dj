"""PHONE LEARN: what the phone learned during a set, merged into the server's knowledge.

The phone keeps an on-disk outbox and posts it to POST /api/mobile/learn (app/ui/services/learn_ingest_api.py)
after each set, in small batches. Every item has an idempotent id (set id + item id); a repeat is answered
"duplicate" and changes nothing. What each kind becomes, in the stores knowledge_publish exports from:

    set_log     the djset-v1 log, stored exactly like POST /api/set-logs (set history index)
    bad_pair    an owner veto A -> B (atlas/vetoes.py, source "phone"): PLAYED_BAD evidence at the next atlas build
    like        a liked transition (learning/liked.py, source "phone:<device>"): PLAYED_GOOD evidence + replay
    transition  one A -> B the phone played (landed / skipped): a private row (user DB, mark "phone_pair") that
                `overlay` adds to the pair's `played` evidence in the atlas right away, and again after every
                atlas build and before every publish (a build recomputes `played` without it)

Provenance (device id, name, set id, kind, time) of every item is its own mark ("learn_item"): private, in the
user DB, never exported. Nothing here is a pair_atlas RULE_FILE, so the scored pairs stay valid. Items holding
an absolute path, the home dir or an e-mail address are rejected (the same guard as the knowledge export).
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

MARK_ITEM = "learn_item"
MARK_PAIR = "phone_pair"
KINDS = ("set_log", "transition", "bad_pair", "like")
OUTCOMES = ("landed", "skipped")
MAX_ITEMS = 100                         # per request
KEEP_ITEMS = 50000                      # newest provenance marks kept
KEEP_PAIRS = 50000
ITEM_ID = re.compile(r"[A-Za-z0-9_.:-]{1,120}")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
NAME_MAX = 300
_lock = threading.Lock()


class LearnError(ValueError):
    """The whole request is unusable (bad envelope): HTTP 422."""


def _text(v, limit: int) -> str:
    return _CONTROL.sub(" ", str(v if v is not None else "")).strip()[:limit]


def _clean_at(v) -> str:
    s = _text(v, 40)
    try:
        from datetime import datetime
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return s
    except ValueError:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ------------------------------------------------------------------------------------------ items
def _pair_ids(d: dict) -> Tuple[str, str, str, str]:
    a_id, b_id = _text(d.get("a_id"), 64), _text(d.get("b_id"), 64)
    a_name, b_name = _text(d.get("a_name"), NAME_MAX), _text(d.get("b_name"), NAME_MAX)
    for v in (a_id, b_id):
        if v and not SAFE_ID.fullmatch(v):
            raise ValueError("track id: letters, digits, - and _ only")
    if not (a_id or a_name) or not (b_id or b_name):
        raise ValueError("a pair needs both songs (id or name)")
    if (a_id and a_id == b_id) or (a_name and a_name == b_name):
        raise ValueError("a pair needs two different songs")
    return a_id, b_id, a_name, b_name


def _transition(item: dict, d: dict, dev: dict, cache_dir: Path, **_) -> dict:
    from app.music_brain import user_marks as um

    a_id, b_id, a_name, b_name = _pair_ids(d)
    outcome = d.get("outcome")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    secs = d.get("seconds")
    row = {"a_id": a_id, "b_id": b_id, "a_name": a_name, "b_name": b_name, "outcome": outcome,
           "recipe": _text(d.get("recipe"), 80), "clean": d.get("clean") is True, "hand": d.get("hand") is True,
           "seconds": round(float(secs), 1) if isinstance(secs, (int, float)) and not isinstance(secs, bool)
           and 0 <= secs <= 3600 else None,
           "device": dev["device_id"], "set": dev["set_id"], "at": item["at"]}
    um.add_new(MARK_PAIR, f"{dev['device_id']}:{item['id']}"[:400], row, cache_dir, keep=KEEP_PAIRS)
    return {"pair": True}


def _bad_pair(item: dict, d: dict, dev: dict, cache_dir: Path, name_of: Optional[Callable] = None, **_) -> dict:
    from app.music_brain.atlas import vetoes as vt

    a_id, b_id, a_name, b_name = _pair_ids(d)
    a = a_name or (name_of(a_id) if name_of and a_id else "")
    b = b_name or (name_of(b_id) if name_of and b_id else "")
    e = vt.make("pair", b, a, source="phone", note="bad pair (phone)")
    return {"added": vt.add(e, cache_dir)}


def _like(item: dict, d: dict, dev: dict, cache_dir: Path, liked_dir: Optional[Path] = None, **_) -> dict:
    from app.music_brain.learning import liked as lk

    step = d.get("step")
    if not isinstance(step, dict):
        raise ValueError("like: step must be an object")
    e = lk.like(step, "phone", _text(d.get("note"), 300), True, liked_dir or cache_dir)
    return {"pair": f"{e['a']}>{e['b']}"}


def _set_log(item: dict, d: dict, dev: dict, cache_dir: Path, save_set_log: Optional[Callable] = None, **_) -> dict:
    if save_set_log is None:
        raise ValueError("set logs are not stored on this server")
    return {"ref": save_set_log(d)["set_log_id"]}


_APPLY = {"transition": _transition, "bad_pair": _bad_pair, "like": _like, "set_log": _set_log}


def ingest(body: dict, cache_dir: Optional[Path] = None, *, liked_dir: Optional[Path] = None,
           name_of: Optional[Callable[[str], str]] = None, save_set_log: Optional[Callable[[dict], dict]] = None) -> dict:
    """Validate, dedup and apply one request. -> {"accepted": [ids], "duplicate": [ids],
    "rejected": [{id, why}], "publish": bool, "pairs": [keys to overlay]}. LearnError: bad envelope."""
    from app.music_brain import user_marks as um
    from app.music_brain.config import CACHE_DIR
    from app.music_brain.matching import knowledge as kn

    cache_dir = Path(cache_dir or CACHE_DIR)
    if not isinstance(body, dict) or body.get("v") != 1:
        raise LearnError("v must be 1")
    device_id, set_id = body.get("device_id"), body.get("set_id")
    if not isinstance(device_id, str) or not SAFE_ID.fullmatch(device_id):
        raise LearnError("device_id: 1-64 letters, digits, - or _")
    if not isinstance(set_id, str) or not SAFE_ID.fullmatch(set_id):
        raise LearnError("set_id: 1-64 letters, digits, - or _")
    items = body.get("items")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise LearnError(f"items: a list of at most {MAX_ITEMS}")
    dev = {"device_id": device_id, "device_name": _text(body.get("device_name"), 64), "set_id": set_id}
    out: dict = {"accepted": [], "duplicate": [], "rejected": [], "publish": False, "pairs": []}
    seen: set = set()
    for raw in items:
        iid = raw.get("id") if isinstance(raw, dict) else None
        if not isinstance(iid, str) or not ITEM_ID.fullmatch(iid):
            out["rejected"].append({"id": _text(iid, 120), "why": "bad id"})
            continue
        subject = f"{device_id}:{iid}"[:400]
        if iid in seen or um.is_marked(MARK_ITEM, subject, cache_dir):
            out["duplicate"].append(iid)
            continue
        seen.add(iid)
        kind, data = raw.get("kind"), raw.get("data")
        try:
            if kind not in _APPLY:
                raise ValueError(f"kind must be one of {KINDS}")
            if not isinstance(data, dict):
                raise ValueError("data must be an object")
            hits = kn.privacy_hits(data, "data")
            if hits:
                raise ValueError("private string in the item (path or e-mail)")
            item = {"id": iid, "at": _clean_at(raw.get("at"))}
            res = _APPLY[kind](item, data, dev, cache_dir, liked_dir=liked_dir, name_of=name_of,
                               save_set_log=save_set_log)
        except Exception as exc:  # noqa: BLE001 -- one bad item never fails the batch; the cause goes back
            why = getattr(exc, "detail", None) or str(exc)
            out["rejected"].append({"id": iid, "why": _text(why, 200) or type(exc).__name__})
            continue
        ref = res.get("ref") or res.get("pair")
        um.add_new(MARK_ITEM, subject, {**dev, "kind": kind, "at": item["at"], "ref": ref if isinstance(ref, str) else None},
                   cache_dir, keep=KEEP_ITEMS)
        out["accepted"].append(iid)
        out["publish"] = out["publish"] or kind == "set_log"
        if kind == "transition":
            out["pairs"].append(iid)
    return out


# ------------------------------------------------------------------------------------- overlay
_BLANK = {"count": 0, "good": 0, "bad": 0, "recipes": {}, "executed": {}, "glitches": 0, "dead_air": 0,
          "ear": {}, "merge": {}, "sources": {}, "sessions": [], "learned": []}


def _evidence(rows: List[dict]) -> Dict[Tuple[str, str], dict]:
    """{(a ref, b ref): {count, good, bad}} of the phone's transitions; a ref = id, else name."""
    ev: Dict[Tuple[str, str], dict] = {}
    for r in rows:
        k = (r.get("a_id") or "name:" + r.get("a_name", ""), r.get("b_id") or "name:" + r.get("b_name", ""))
        e = ev.setdefault(k, {"count": 0, "good": 0, "bad": 0, "names": (r.get("a_name"), r.get("b_name")),
                              "ids": (r.get("a_id"), r.get("b_id"))})
        if r.get("outcome") == "skipped":
            e["bad"] += 1                      # an AI pick the owner refused: one bad play (no count: it never played)
        else:
            e["count"] += 1
            e["good"] += 1 if r.get("clean") else 0
    return ev


def overlay(cache_dir: Optional[Path] = None, log: Callable[[str], None] = lambda m: None) -> dict:
    """Add the phone's transition evidence to the atlas pairs' `played` (idempotent: the previous overlay,
    kept as played["phone"], is taken off first, so a repeat or a rebuild never double counts). Pairs whose
    songs the atlas does not know (by id, else by name) are skipped. -> {pairs, changed, unresolved}."""
    from app.music_brain import user_marks as um
    from app.music_brain.atlas import atlas_store as st
    from app.music_brain.atlas import pair_atlas as pa
    from app.music_brain.config import CACHE_DIR
    from app.music_brain.learning.set_import import _atlas_lock

    cache_dir = Path(cache_dir or CACHE_DIR)
    rows = [r for _, r in um.rows(MARK_PAIR, cache_dir) if isinstance(r, dict)]
    rep = {"pairs": 0, "changed": 0, "unresolved": 0}
    root = st._root(cache_dir, None)
    if root.is_file():
        return rep
    with _atlas_lock(cache_dir):
        conn = st._db(root)
        if not st._has_atlas(conn):
            return rep
        names = dict(conn.execute("SELECT id, name FROM atlas_tracks"))
        idx = pa._NameIndex({t: n for t, n in names.items() if n})

        def resolve(tid: Optional[str], name: Optional[str]) -> Optional[str]:
            return tid if tid in names else (idx.find(name or "") if name else None)

        want: Dict[Tuple[str, str], dict] = {}
        for e in _evidence(rows).values():
            a, b = resolve(e["ids"][0], e["names"][0]), resolve(e["ids"][1], e["names"][1])
            if not a or not b or a == b:
                rep["unresolved"] += 1
                continue
            w = want.setdefault((a, b), {"count": 0, "good": 0, "bad": 0})
            for f in w:
                w[f] += e[f]
        have = {(a, b): d for a, b, d in conn.execute(
            "SELECT a, b, data FROM atlas_pairs WHERE json_extract(data, '$.played.phone') IS NOT NULL")}
        rep["pairs"] = len(want)
        todo = {k: want.get(k) for k in set(want) | set(have)}
        put = ("INSERT OR REPLACE INTO atlas_pairs (" + ", ".join(st._PAIR_COLS) + ") VALUES ("
               + ", ".join("?" * len(st._PAIR_COLS)) + ")")
        from app.music_brain import db
        with db.tx(conn):
            for (a, b), w in sorted(todo.items()):
                raw = have.get((a, b)) or (conn.execute("SELECT data FROM atlas_pairs WHERE a = ? AND b = ?", (a, b)).fetchone() or [None])[0]
                if raw is None:
                    continue
                p = json.loads(raw)
                played = _apply_overlay(p.get("played"), w)
                if played == p.get("played"):
                    continue
                p["played"] = played
                p["works"] = int(max(0, min(100, p.get("works_base", p.get("works", 0)) + pa.played_adjust(played))))
                enc = json.JSONEncoder(sort_keys=True, separators=(",", ":")).encode(p)
                conn.execute(put, st._pair_row(p, enc))
                rep["changed"] += 1
            if rep["changed"]:
                conn.execute("INSERT INTO atlas_meta (key, value) VALUES (?, '1') ON CONFLICT(key) "
                             "DO UPDATE SET value = CAST(value AS INTEGER) + 1", (st._REV,))
    if rep["changed"]:
        log(f"phone evidence: {rep['changed']} atlas pairs updated")
    return rep


def _apply_overlay(played: Optional[dict], w: Optional[dict]) -> Optional[dict]:
    """`played` with the previous overlay removed and `w` ({count, good, bad} or None) added."""
    base = json.loads(json.dumps(played)) if played else None
    if base:
        old = base.pop("phone", None) or {}
        for f in ("count", "good", "bad"):
            base[f] = max(0, int(base.get(f) or 0) - int(old.get(f) or 0))
        src = dict(base.get("sources") or {})
        src.pop("phone", None)
        base["sources"] = src
    if not w or not any(w.values()):
        if played and not played.get("phone"):
            return played                                  # no overlay there, none wanted
        left = base and (any(base.get(f) for f in ("count", "good", "bad")) or base.get("sessions") or base.get("learned"))
        return base if left else None                      # only the overlay was there: the pair is unplayed again
    out = base or json.loads(json.dumps(_BLANK))
    for f in ("count", "good", "bad"):
        out[f] = int(out.get(f) or 0) + w[f]
    out["sources"] = dict(out.get("sources") or {}, phone=w["count"] + w["bad"])
    out["phone"] = dict(w)
    return out


# ---------------------------------------------------------------------------------- background
_state = {"running": False, "pending": False, "publish": False}


def schedule(publish: bool = False, cache_dir: Optional[Path] = None, log: Callable[[str], None] = lambda m: None,
             delay_s: float = 1.0) -> None:
    """Overlay the phone's evidence into the atlas in the background (coalesced: calls during a run
    fold into one more run), then, when `publish`, hand over to the knowledge publisher (opt-in by
    DJ_ATLAS_AUTOPUBLISH, its own coalescing)."""
    with _lock:
        _state["publish"] = _state["publish"] or publish
        if _state["running"]:
            _state["pending"] = True
            return
        _state["running"] = True
    t = threading.Timer(delay_s, _run, args=(cache_dir, log))
    t.daemon = True
    t.start()


def _run(cache_dir, log) -> None:
    while True:
        with _lock:
            publish, _state["publish"], _state["pending"] = _state["publish"], False, False
        try:
            overlay(cache_dir, log)
        except Exception as exc:  # noqa: BLE001 -- evidence is best effort; the items are stored
            log(f"phone evidence overlay failed: {type(exc).__name__}: {exc}")
        if publish:
            try:
                from app.music_brain.matching import knowledge_publish
                knowledge_publish.on_set_end(delay_s=2.0, log=log)
            except Exception as exc:  # noqa: BLE001
                log(f"phone learn publish not scheduled: {type(exc).__name__}: {exc}")
        with _lock:
            if not _state["pending"]:
                _state["running"] = False
                return
