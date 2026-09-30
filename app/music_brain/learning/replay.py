"""REPLAY and TIME TRAVEL: a past session (or part of it) as a macro that plays it back as it was.

A replay is an ordinary macro (atlas/macros.py) with source "replay:<session>": per step the
songs, the recipe that RAN, A's exit and B's entry as played, the merge -> hold plan when one
played, the tempo decision when the log names it, and the in-transition moves (artist / learned
/ stem moves) with their logged params and their time from the transition start. What the log
cannot say is listed per step under "gaps" and the stored macro step or the recipe default is
used. The console plays it like any macro (PLAY MACRO), so every live safety gate still runs: a
stored decision that is no longer valid is logged and falls back; nothing is re-decided.

Time travel: a moment of a past session (epoch, seconds into the set, "HH:MM:SS" or a step)
-> which song each deck held and where, and the replay from there. A transition in progress
is started from its own start so it plays whole.

Reads sessions only through history_api (swappable for the set-history DB).

    python3 -m app.music_brain.learning.replay timeline <session>
    python3 -m app.music_brain.learning.replay build <session> [--from N] [--to N] [--save]
    python3 -m app.music_brain.learning.replay at <session> <epoch|seconds|HH:MM:SS> [--save]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Callable, List, Optional, Union

from app.music_brain.learning import history_api as history

PERFORM_RE = re.compile(r"performing step (\d+): .* \(exit (\d+):(\d\d), entry (\d+):(\d\d)\)")
MACRO_RE = re.compile(r"MACRO MODE (\S+) step (\d+)")
REFUSED_RE = re.compile(r"\[refused ([^:\]]+): ([^\]]*)\]")
MAX_MOVES = 8


def _mmss(m: str, s: str) -> float:
    return int(m) * 60 + int(s)


def _booked(tr: dict, load_macro: Optional[Callable[[str], dict]]) -> dict:
    """What the console booked for this transition when a macro drove it: macro name / step,
    the stored entry (precise when the stored step still matches the logged m:ss) and a refusal."""
    out: dict = {}
    for line in tr.get("macro_lines") or []:
        m = MACRO_RE.search(line)
        if m:
            out["macro"], out["n"] = m.group(1), int(m.group(2))
        p = PERFORM_RE.search(line)
        if p and tr.get("b_name") and str(tr["b_name"]) in line:
            out["perform_n"] = int(p.group(1))
            out["exit_s"], out["entry_s"] = _mmss(p.group(2), p.group(3)), _mmss(p.group(4), p.group(5))
            r = REFUSED_RE.search(line)
            out["refused"] = (r.group(1), r.group(2)) if r else None
    if out.get("macro") and load_macro and out.get("entry_s") is not None:
        try:
            mac = load_macro(out["macro"])
        except (KeyError, ValueError, OSError):
            mac = None
        st = next((s for s in (mac or {}).get("steps") or [] if s.get("a") == tr["a"] and s.get("b") == tr["b"]), None)
        if st is not None:
            out["stored"] = st
            if isinstance(st.get("b_time"), (int, float)) and int(st["b_time"]) == int(out["entry_s"]):
                out["entry_s"] = st["b_time"]
    return out


def step_of(tr: dict, session: str, load_macro: Optional[Callable[[str], dict]] = None) -> dict:
    """One played transition -> one macro step (as played) with its gaps."""
    gaps: List[str] = []
    bk = _booked(tr, load_macro)
    stored = bk.get("stored") or {}
    recipe = tr.get("recipe")
    if not recipe:
        recipe = stored.get("recipe") or "Echo Out"
        gaps.append(f"recipe not logged: {'stored macro step' if stored else 'default'} {recipe}")
    a_time = tr.get("a_time")
    if a_time is None:
        a_time = bk.get("exit_s")
        gaps.append("A exit not logged" + (" (m:ss from the macro line)" if a_time is not None else ""))
    b_time = tr.get("b_time") if tr.get("b_exact") else None
    if b_time is None and bk.get("stored") is not None and bk.get("entry_s") is not None:
        b_time = bk["entry_s"]            # the stored step's entry (matches the logged m:ss)
    if b_time is None:
        b_time = tr.get("b_time")
        if b_time is not None:
            gaps.append("B entry from the song log (first logged position, up to ~1 s late)")
        elif bk.get("entry_s") is not None:
            b_time = bk["entry_s"]
            gaps.append("B entry to the second only (the macro line's m:ss)")
        else:
            gaps.append("B entry not logged: the console picks one")
    tempo = stored.get("tempo") if isinstance(stored.get("tempo"), dict) else None
    if tempo is None:
        gaps.append("tempo / keylock decision not logged: the live tempo rule decides")
    merge = None
    if tr.get("merge_played"):
        merge = stored.get("merge") if isinstance(stored.get("merge"), dict) else None
        if merge is None and isinstance(tr.get("merge"), dict):
            gaps.append("merge hold played but only its audition is logged: the live merge planner re-plans the hold")
    elif re.search(r"merge", recipe or "", re.I):
        gaps.append("Stem Merge logged but no hold played")
    if tr.get("planned") and tr["planned"] != recipe:
        gaps.append(f"booked {tr['planned']}, ran {recipe}: replayed as it ran")
    if bk.get("refused"):
        gaps.append(f"at the time {bk['refused'][0]} was refused ({bk['refused'][1]})")
    moves = []
    for mv in tr.get("moves") or []:
        m = {k: mv[k] for k in ("kind", "move", "label", "dt", "side", "params", "t1_rel") if k in mv}
        moves.append(m)
        if m["kind"] in ("artist_move", "learned_move") and not isinstance(mv.get("params"), dict):
            gaps.append(f"{m['move']}: params not logged, the move plans its own")
    if len(moves) > MAX_MOVES:
        gaps.append(f"{len(moves) - MAX_MOVES} more moves not kept (a step keeps {MAX_MOVES})")
    return {"a": tr["a"], "b": tr["b"], "a_name": tr.get("a_name"), "b_name": tr.get("b_name"),
            "recipe": recipe, "a_time": a_time, "b_time": b_time, "merge": merge, "tempo": tempo,
            "moves": moves[:MAX_MOVES], "seconds": tr.get("seconds"), "set_s": tr.get("set_s"),
            "played_at": tr.get("at"), "gaps": gaps,
            "why": f"replay {session} step {tr['n']}" + (f" (was {bk['macro']} step {bk['n']})" if bk.get("macro") else "")}


def _macro_loader(cache_dir: Optional[Path]):
    from app.music_brain.atlas import macros as mc
    return lambda name: mc.load(name, cache_dir)


def build(session: str, start: int = 1, end: Optional[int] = None, cache_dir: Optional[Path] = None,
          tl: Optional[dict] = None, name: Optional[str] = None) -> dict:
    """Replay macro for transitions start..end (1-based, inclusive) of a session. A break in the
    chain (a song the log lost) ends the replay there, listed in "cut"."""
    from app.music_brain.atlas import macros as mc
    tl = tl or history.timeline(session, cache_dir)
    trans = [t for t in tl["transitions"] if t["n"] >= start and (end is None or t["n"] <= end)]
    if not trans:
        raise ValueError(f"no transition {start}..{end or 'end'} in session {session}")
    load = _macro_loader(cache_dir)
    steps, cut = [], None
    for tr in trans:
        if steps and steps[-1]["b"] != tr["a"]:
            cut = f"step {tr['n']} does not start where step {tr['n'] - 1} ended: replay stops there"
            break
        steps.append(step_of(tr, session, load))
    first, last = trans[0]["n"], trans[0]["n"] + len(steps) - 1
    m = mc.normalize({"name": name or f"replay-{session}-{first}-{last}", "source": f"replay:{session}",
                      "title": f"Replay {session} {tl['transitions'][first - 1].get('at') or ''} steps {first}-{last}",
                      "note": f"replay of session {session}, transitions {first}-{last}", "steps": steps})
    return {"macro": m, "from_n": first, "to_n": last, "cut": cut,
            "gaps": [{"n": first + i, "gaps": s.get("gaps") or []} for i, s in enumerate(steps)]}


PRE_ROLL_S = 8.0     # a replay that starts ON a transition loads A this much earlier, so the stored exit is ahead


def load_for(replay: Optional[dict], state: dict, pre_roll: float = 0.0) -> Optional[dict]:
    """The song to load and play now: A of the first replayed step at its position in `state`
    (minus pre_roll, never below 0); with no replay, the song playing then."""
    if replay and replay.get("macro", {}).get("steps"):
        s0 = replay["macro"]["steps"][0]
        deck = next((d for d, v in state["decks"].items() if v["track_id"] == s0["a"]), "a")
        pos = (state["decks"].get(deck) or {}).get("pos")
        if pos is None:
            pos = s0.get("a_time") or 0.0
        return {"deck": deck, "track_id": s0["a"], "name": s0.get("a_name"), "pos": round(max(0.0, pos - pre_roll), 3)}
    playing = next((dict(v, deck=d) for d, v in state["decks"].items() if v["track_id"] == state.get("playing")), None)
    return playing and {"deck": playing["deck"], "track_id": playing["track_id"], "name": playing.get("name"),
                        "pos": playing.get("pos") or 0.0}


def travel(session: str, at: Union[str, float, int, None] = None, step: Optional[int] = None,
           cache_dir: Optional[Path] = None) -> dict:
    """Jump to a moment (at) or to the start of a transition (step): each deck's song and song
    position there, and the replay from there. A transition in progress restarts from its own
    start (A loaded PRE_ROLL_S before the stored exit) so it plays whole."""
    tl = history.timeline(session, cache_dir)
    on_transition = step is not None
    if step is not None:
        tr = next((t for t in tl["transitions"] if t["n"] == int(step)), None)
        if tr is None or tr.get("t") is None:
            raise ValueError(f"no step {step} in session {session}")
        at, start = tr["t"], tr["n"]
    else:
        if at is None:
            raise ValueError("give a moment (at) or a step")
        st = history.state_at(session, at, cache_dir, tl)
        start = st["in_transition"] or st["next_step"]
        if st["in_transition"]:
            at, on_transition = tl["transitions"][st["in_transition"] - 1]["t"], True
    state = history.state_at(session, at, cache_dir, tl)
    out = {"session": session, "state": state, "start_step": start, "replay": None,
           "restarted_transition": on_transition}
    if start is not None and start <= len(tl["transitions"]):
        out["replay"] = build(session, start, None, cache_dir, tl)
    out["load"] = load_for(out["replay"], state, PRE_ROLL_S if on_transition else 0.0)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    from app.music_brain.atlas import macros as mc
    from app.music_brain.config import CACHE_DIR
    ap = argparse.ArgumentParser(prog="replay")
    ap.add_argument("cmd", choices=("sessions", "timeline", "build", "at"))
    ap.add_argument("arg", nargs="*")
    ap.add_argument("--from", dest="start", type=int, default=1)
    ap.add_argument("--to", dest="end", type=int, default=None)
    ap.add_argument("--step", type=int, default=None)
    ap.add_argument("--save", action="store_true", help="save the replay macro (data/cache/macros)")
    ap.add_argument("--cache-dir", default=str(CACHE_DIR))
    a = ap.parse_args(argv)
    cd = Path(a.cache_dir)
    try:
        if a.cmd == "sessions":
            out = {"sessions": history.sessions(cd)}
        elif a.cmd == "timeline":
            out = history.timeline(a.arg[0], cd)
        elif a.cmd == "build":
            out = build(a.arg[0], a.start, a.end, cd)
        else:
            out = travel(a.arg[0], a.arg[1] if len(a.arg) > 1 else None, a.step, cd)
        rp = out.get("replay") if a.cmd == "at" else out if a.cmd == "build" else None
        if a.save and rp:
            rp["macro"] = mc.save(rp["macro"], cd)
    except (ValueError, KeyError, IndexError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
