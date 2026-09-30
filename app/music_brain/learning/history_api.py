"""HISTORY API: the thin read interface over past sessions (replay, time travel, liked).

Four functions over the set-history store (app/music_brain/history.py, CACHE_DIR/user.db):
    sessions()               every logged session, newest first
    timeline(session)        its songs and transitions in play order (with in-transition moves)
    state_at(session, t)     each deck's song and song position at moment t
    pair_plays(a, b)         every time the pair A -> B was played, across sessions
They read the raw rows history.py indexed from data/cache/sessions/<id>/ (events.jsonl +
songs/*/meta.json + steps.jsonl; caught up incrementally on every read), so a session whose log
folder is gone is still served. Only when the DB cannot be opened do they read the JSONL directly.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Union

SESSION_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
TRACK_ID_RE = re.compile(r"^[0-9a-f]{16}$")
# steps.jsonl kinds that are moves inside a transition (the replay's "in-transition moves")
MOVE_KINDS = ("artist_move", "learned_move", "stem-move", "cue_drop")
MOVE_PAD_S = 1.0          # a move logged up to 1 s either side of the transition window still belongs to it
MAX_T_S = 36000.0


def _cache(cache_dir: Optional[Path]) -> Path:
    from app.music_brain.config import CACHE_DIR
    return Path(cache_dir or CACHE_DIR)


def _sdir(session: str, cache_dir: Optional[Path]) -> Path:
    if not SESSION_RE.match(session or ""):
        raise ValueError("bad session id")
    return _cache(cache_dir) / "sessions" / session


def _jsonl(p: Path) -> List[dict]:
    rows = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        return []
    return rows


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) < 1e12 else None


def _db_ok(cache_dir: Optional[Path]) -> bool:
    import sqlite3

    from app.music_brain import history

    try:
        history._conn(_cache(cache_dir))
        return True
    except (sqlite3.Error, OSError):
        return False


def _events(session: str, cache_dir: Optional[Path], sync: bool = True) -> List[dict]:
    from app.music_brain import history

    if _db_ok(cache_dir):
        return history.raw_events(session, _cache(cache_dir), sync=sync)
    return _jsonl(_sdir(session, cache_dir) / "events.jsonl")


def _metas(session: str, cache_dir: Optional[Path], sync: bool = True) -> List[dict]:
    """_songs() for one session, from the DB (JSONL fallback)."""
    from app.music_brain import history

    if not _db_ok(cache_dir):
        return _songs(_sdir(session, cache_dir))
    out = []
    for folder, meta, steps in history.raw_songs(session, _cache(cache_dir), sync=sync):
        if isinstance(meta, dict) and TRACK_ID_RE.match(str(meta.get("track_id") or "")):
            meta["_steps"] = [x for x in steps if isinstance(x, dict)]
            meta["_dir"] = folder
            out.append(meta)
    out.sort(key=lambda x: x.get("nn") or 0)
    return out


def _songs(sdir: Path) -> List[dict]:
    """Song metas in folder (first-load) order, each with its AI steps under "_steps"."""
    out = []
    for m in sorted((sdir / "songs").glob("*/meta.json")) if (sdir / "songs").is_dir() else []:
        try:
            meta = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and TRACK_ID_RE.match(str(meta.get("track_id") or "")):
            meta["_steps"] = _jsonl(m.parent / "steps.jsonl")
            meta["_dir"] = m.parent.name
            out.append(meta)
    out.sort(key=lambda x: x.get("nn") or 0)
    return out


def sessions(cache_dir: Optional[Path] = None) -> List[dict]:
    """Every session with a log, newest first: id, songs, transitions, start/end epoch."""
    from app.music_brain import history

    root = _cache(cache_dir) / "sessions"
    if _db_ok(cache_dir):
        ids = history.session_ids(_cache(cache_dir))      # every set ever indexed, newest first
    else:
        ids = [d.name for d in sorted(root.iterdir(), reverse=True) if d.is_dir()] if root.is_dir() else []
    out = []
    for sid in ids:
        if not SESSION_RE.match(sid):
            continue
        events = _events(sid, cache_dir, sync=False)
        songs = _metas(sid, cache_dir, sync=False)
        if not events and not songs:
            continue
        ts = [e["t"] for e in events if _num(e.get("t")) is not None]
        out.append({"id": sid, "songs": len(songs),
                    "transitions": sum(1 for e in events if e.get("kind") == "track" and e.get("event") == "transition_start"),
                    "start_t": min(ts) if ts else None, "end_t": max(ts) if ts else None,
                    "first": songs[0].get("name") if songs else None})
    return out


def _move_of(x: dict, t0: float) -> dict:
    inp = x.get("inputs") if isinstance(x.get("inputs"), dict) else {}
    params = x.get("params") if isinstance(x.get("params"), dict) else inp.get("params")
    mv = {"kind": x.get("kind"), "move": str(inp.get("move") or x.get("decision") or "")[:60],
          "label": str(x.get("decision") or "")[:80], "deck": x.get("deck"),
          "dt": round(float(x["t"]) - t0, 3), "why": str(x.get("why") or "")[:200]}
    if isinstance(params, dict):
        mv["params"] = params
    for k in ("t0", "t1"):         # audio-clock times: kept relative to the move's own start
        if _num(x.get(k)) is not None and _num(x.get("t0")) is not None:
            mv[f"{k}_rel"] = round(float(x[k]) - float(x["t0"]), 3)
    return mv


def _play_order(songs: List[dict], starts: List[dict]) -> List[tuple]:
    """(A, B, transition_start event or None) in the order played. Song folders are numbered
    when a song is first loaded (a pre-render loads ahead, a skipped pick never plays), so the
    track events decide the order; a log without them falls back to the song entry times."""
    def pick(name, t):
        c = [s for s in songs if s.get("name") == name]
        if not c and name:       # the console's name may drop a suffix the song log keeps ("[set cut ...]")
            c = [s for s in songs if str(s.get("name") or "").startswith(name) or name.startswith(str(s.get("name") or "\0"))]
        if not c:
            return None
        return min(c, key=lambda s: abs((_num(s.get("entry_t")) or 0) - (t or 0)))
    out = []
    for e in starts:
        t = _num(e.get("t"))
        pa, pb = pick(e.get("from"), t), pick(e.get("to"), t)
        if pa is not None and pb is not None and pa is not pb:
            out.append((pa, pb, e))
    if out:
        return out
    played = sorted((s for s in songs if _num(s.get("entry_t")) is not None), key=lambda s: s["entry_t"])
    return [(a, b, None) for a, b in zip(played, played[1:])]


def timeline(session: str, cache_dir: Optional[Path] = None) -> dict:
    """The session in play order: songs (entry/exit, song seconds) and transitions, each with
    the recipe that ran, A's exit and B's entry (song s), the merge, and its in-transition moves."""
    _sdir(session, cache_dir)                          # validates the id
    events = _events(session, cache_dir)
    songs = _metas(session, cache_dir, sync=False)
    if not events and not songs:
        raise KeyError(f"no session {session}")
    starts = [e for e in events if e.get("kind") == "track" and e.get("event") == "transition_start"]
    ends = [e for e in events if e.get("kind") == "track" and e.get("event") == "transition_end"]
    ts_all = [e["t"] for e in events if _num(e.get("t")) is not None]
    start_t = min([s.get("entry_t") for s in songs if _num(s.get("entry_t")) is not None] + ts_all, default=None)
    trans = []
    for i, (pa, pb, ev) in enumerate(_play_order(songs, starts)):
        sa, sb = pa["_steps"], pb["_steps"]
        st = next((x for x in sa if x.get("kind") == "transition_start" and x.get("phase") == "transition-out"), None)
        t0 = _num((ev or {}).get("t")) or _num((st or {}).get("t")) or _num(pb.get("entry_t"))
        ex = next((x for x in sa if x.get("kind") == "recipe_executed" and x.get("phase") == "transition-out"), None) \
            or next((x for x in sb if x.get("kind") == "recipe_executed" and x.get("phase") == "transition-in"), None)
        recipe = (ev or {}).get("recipe") or (ex or {}).get("decision") or pb.get("recipe_in") or (st or {}).get("decision")
        planned = (ev or {}).get("planned") or ((st or {}).get("decision") if st and st.get("decision") != recipe else None)
        seconds = _num((ev or {}).get("seconds")) or _num(((st or {}).get("inputs") or {}).get("seconds"))
        a_time = _num((ev or {}).get("a_pos"))
        if a_time is None and t0 is not None and _num(pa.get("entry_t")) is not None and _num(pa.get("entry_song_s")) is not None:
            a_time = round(pa["entry_song_s"] + (t0 - pa["entry_t"]), 3)
        if a_time is None:
            a_time = _num(pa.get("exit_song_s"))
        # B's entry: the console's cue ("B's first downbeat", B's song s) is exact; the song's first
        # logged position (entry_song_s) is up to ~1 s late
        cue = [x for x in sb if x.get("kind") == "cue_transition" and _num(x.get("at_song")) is not None
               and (t0 is None or _num(x.get("t")) is None or abs(x["t"] - t0) <= 5)]
        b_cue = _num(cue[-1]["at_song"]) if cue else None
        end = next((e for e in ends if _num(e.get("t")) and t0 and e["t"] >= t0 and e.get("now_playing") == pb.get("name")), None)
        merge = next((x.get("result") for x in sa if x.get("kind") == "merge_audition" and isinstance(x.get("result"), dict)), None)
        merge_played = any(x.get("kind") in ("merge_start", "hold", "handover") for x in sa + sb)
        moves = []
        if t0 is not None:
            hi = t0 + (seconds or 0) + MOVE_PAD_S
            for x in sorted(sa + sb, key=lambda y: _num(y.get("t")) or 0):
                t = _num(x.get("t"))
                if x.get("kind") in MOVE_KINDS and t is not None and t0 - MOVE_PAD_S <= t <= hi \
                        and str(x.get("phase") or "").startswith("transition"):
                    moves.append(dict(_move_of(x, t0), side="a" if any(x is y for y in sa) else "b"))
        trans.append({"n": i + 1, "a": pa["track_id"], "b": pb["track_id"], "a_name": pa.get("name"), "b_name": pb.get("name"),
                      "t": t0, "at": (ev or {}).get("at") or (time.strftime("%H:%M:%S", time.localtime(t0)) if t0 else None),
                      "set_s": round(t0 - start_t, 2) if t0 is not None and start_t is not None else None,
                      "end_t": _num((end or {}).get("t")) or (t0 + seconds if t0 is not None and seconds else None),
                      "recipe": recipe, "planned": planned, "seconds": seconds,
                      "a_time": a_time, "b_time": b_cue if b_cue is not None else _num(pb.get("entry_song_s")),
                      "b_exact": b_cue is not None,
                      "out": (ev or {}).get("out") or pa.get("deck"), "in": (ev or {}).get("in") or pb.get("deck"),
                      "merge": merge, "merge_played": merge_played, "moves": moves,
                      "macro_lines": [str(x.get("why") or "")[:300] for x in sa if x.get("kind") == "macro"
                                      and t0 is not None and _num(x.get("t")) is not None
                                      and x["t"] <= t0 + (seconds or 0) + MOVE_PAD_S][-6:],
                      "bpm_a": pa.get("bpm"), "bpm_b": pb.get("bpm"), "dropped": bool(pb.get("dropped"))})
    return {"session": session, "start_t": start_t, "end_t": max(ts_all) if ts_all else None,
            "songs": [{k: v for k, v in s.items() if k != "_steps"} for s in songs], "transitions": trans}


def _anchors(song: dict, events: List[dict]) -> List[tuple]:
    """(epoch, song s) points known for one song: entry, exit, every logged move and A's exit."""
    pts = []
    for tk, pk in (("entry_t", "entry_song_s"), ("exit_t", "exit_song_s")):
        if _num(song.get(tk)) is not None and _num(song.get(pk)) is not None:
            pts.append((float(song[tk]), float(song[pk])))
    name = song.get("name")
    for e in events:
        t = _num(e.get("t"))
        if t is None:
            continue
        if e.get("kind") == "move" and e.get("song") == name and _num(e.get("pos")) is not None:
            pts.append((t, float(e["pos"])))
        elif e.get("kind") == "track" and e.get("event") == "transition_start" and e.get("from") == name \
                and _num(e.get("a_pos")) is not None:
            pts.append((t, float(e["a_pos"])))
    return sorted(set(pts))


def song_pos(pts: List[tuple], t: float) -> Optional[float]:
    """Song seconds at epoch t: linear between the two nearest anchors, 1 s/s past the ends."""
    if not pts:
        return None
    if t <= pts[0][0]:
        return round(max(0.0, pts[0][1] + (t - pts[0][0])), 3)
    for (t1, p1), (t2, p2) in zip(pts, pts[1:]):
        if t1 <= t <= t2:
            return round(p1 + (p2 - p1) * ((t - t1) / (t2 - t1)) if t2 > t1 else p2, 3)
    return round(pts[-1][1] + (t - pts[-1][0]), 3)


def resolve_t(session: str, at: Union[str, float, int], start_t: Optional[float]) -> float:
    """A moment of the session as epoch: an epoch, seconds into the set, or the log's "HH:MM:SS"
    (the session date comes from its id, YYYY-MM-DD_HHMMSS; a clock before the start is the next day)."""
    if isinstance(at, (int, float)) or re.match(r"^-?\d+(\.\d+)?$", str(at).strip()):
        v = float(at)
        if v > 1e9:
            return v
        if start_t is None or not 0 <= v <= MAX_T_S:
            raise ValueError("seconds into the set out of range")
        return start_t + v
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", str(at).strip())
    if not m or start_t is None:
        raise ValueError("at must be epoch, seconds into the set or HH:MM:SS")
    day = time.localtime(start_t)
    t = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), 0, 0, -1))
    return t + 86400 if t < start_t - 3600 else t


def state_at(session: str, at: Union[str, float, int], cache_dir: Optional[Path] = None, tl: Optional[dict] = None) -> dict:
    """Each deck's song and song position at a moment, and the transition in progress (if any).
    Positions are interpolated between logged events by elapsed time (playback rate 1)."""
    tl = tl or timeline(session, cache_dir)
    t = resolve_t(session, at, tl["start_t"])
    _sdir(session, cache_dir)
    events = _events(session, cache_dir, sync=False)
    songs = tl["songs"]
    if not songs:
        raise KeyError("no songs logged in that session")
    end_t = tl["end_t"]
    played = sorted((x for x in songs if _num(x.get("entry_t")) is not None
                     and (end_t is None or x["entry_t"] <= end_t)), key=lambda x: x["entry_t"])
    if not played:
        raise KeyError("no song with a logged start in that session")
    t = min(max(t, played[0]["entry_t"]), max([end_t or 0] + [_num(x.get("exit_t")) or 0 for x in played]))
    decks = {}
    for i, s in enumerate(played):
        hi = _num(s.get("exit_t")) or end_t
        if s["entry_t"] <= t and (hi is None or t <= hi):
            decks[s.get("deck") or ("a" if i % 2 == 0 else "b")] = {
                "track_id": s["track_id"], "name": s.get("name"), "nn": s.get("nn"),
                "pos": song_pos(_anchors(s, events), t)}
    trans = [x for x in tl["transitions"] if x["t"] is not None]
    cur = next((x for x in trans if x["t"] <= t <= (x["end_t"] or x["t"])), None)
    playing = [x for x in played if x["entry_t"] <= t][-1]
    step = cur["n"] if cur else next((x["n"] for x in sorted(trans, key=lambda y: y["t"]) if x["t"] >= t), None)
    return {"session": session, "t": round(t, 3), "set_s": round(t - tl["start_t"], 2) if tl["start_t"] else None,
            "at": time.strftime("%H:%M:%S", time.localtime(t)), "decks": decks,
            "in_transition": cur["n"] if cur else None, "playing": playing["track_id"], "next_step": step}


def pair_plays(a: str, b: str, cache_dir: Optional[Path] = None) -> List[dict]:
    """Every logged play of A -> B across all sessions (newest first)."""
    out = []
    for s in sessions(cache_dir):
        try:
            tl = timeline(s["id"], cache_dir)
        except (KeyError, ValueError):
            continue
        out += [dict(x, session=s["id"]) for x in tl["transitions"] if x["a"] == a and x["b"] == b]
    return out
