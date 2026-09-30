"""REPLAY, TIME TRAVEL and LIKED (learning/history_api.py, replay.py, liked.py, services/replay_api.py).

The fixture is a 3-song session shaped like 2026-09-29_235413 steps 9-11: No Control -> Neverland
(Bass Swap, a refused Stem Merge) and Neverland -> Nocturnal (Echo Out + the vocal throw)."""
import json
from pathlib import Path

import pytest

from app.music_brain.atlas import macros as mc
from app.music_brain.learning import history_api as history
from app.music_brain.learning import liked as lk
from app.music_brain.learning import replay as rp

SID = "2026-09-29_235413"
NOCTRL, NEVER, NOCT = "407c498ddd3a6dab", "c4a392ce13e82bc9", "3ef9ad4b3fd01c79"
T0 = 1790691000.0          # No Control starts
X1 = T0 + 182.0            # No Control -> Neverland starts (A at 190.0)
X2 = T0 + 332.0            # Neverland -> Nocturnal starts
THROW = {"delayS": 0.46875, "feedback": 0.6, "wet": 0.35, "gapS": 7.32, "band": [250, 3000]}


def _w(p: Path, rows):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _session(cache: Path, sid: str = SID) -> Path:
    sd = cache / "sessions" / sid
    songs = [
        ("01-no-control", {"nn": 1, "track_id": NOCTRL, "name": "PACS & Ruiz (BR) - No Control", "deck": "b", "bpm": 128.0,
                           "recipe_in": None, "entry_t": T0, "exit_t": X1 + 16, "entry_song_s": 8.0, "exit_song_s": 206.0},
         [{"t": X1, "phase": "transition-out", "kind": "transition_start", "decision": "Bass Swap", "inputs": {"seconds": 15.2}},
          {"t": X1 - 10, "phase": "playing", "kind": "macro", "decision": "MACRO 9 · Bass Swap",
           "why": "macro: performing step 9: PACS & Ruiz (BR) - No Control -> Anyma & Baset - Neverland (From Japan) (HNTR Remix) "
                  "Bass Swap (exit 3:02, entry 0:07) [refused Stem Merge: stems: stems missing on a deck]"},
          {"t": X1 - 5, "phase": "selection", "kind": "macro", "decision": "preferred", "why": "macro: MACRO MODE studied-set-x step 9"},
          {"t": X1 + 9.7, "phase": "transition-out", "kind": "stem-move", "decision": "AUTO SAMPLER · drop", "why": "the drop"}]),
        ("02-neverland", {"nn": 2, "track_id": NEVER, "name": "Anyma & Baset - Neverland (From Japan) (HNTR Remix)", "deck": "a",
                          "bpm": 128.0, "recipe_in": "Bass Swap", "entry_t": X1 + 1, "exit_t": X2 + 10, "entry_song_s": 8.46,
                          "exit_song_s": 164.46},
         [{"t": X1 - 0.01, "at_song": 7.64, "phase": "planning", "kind": "cue_transition", "decision": "transition"},
          {"t": X2 - 0.005, "at_song": 157.47, "phase": "transition-out", "kind": "artist_move", "decision": "ARTIST MOVE · VOCAL THROW",
           "why": "last word into a 1-beat echo, fb 0.6, gap 7.3 s",
           "inputs": {"move": "vocal_throw", "params": THROW, "t0": 1349.62, "t1": 1357.41}},
          {"t": X2, "phase": "transition-out", "kind": "transition_start", "decision": "Echo Out", "inputs": {"seconds": 7.7}}]),
        ("03-nocturnal", {"nn": 3, "track_id": NOCT, "name": "Adam Sellouk & Doriann - Nocturnal", "deck": "b", "bpm": 128.0,
                          "recipe_in": "Echo Out", "entry_t": X2 + 1, "exit_t": None, "entry_song_s": 19.63, "exit_song_s": None},
         [{"t": X2 - 0.004, "at_song": 18.79, "phase": "planning", "kind": "cue_transition", "decision": "transition"}]),
    ]
    for d, meta, steps in songs:
        (sd / "songs" / d).mkdir(parents=True, exist_ok=True)
        (sd / "songs" / d / "meta.json").write_text(json.dumps(dict(meta, session=sid)), encoding="utf-8")
        _w(sd / "songs" / d / "steps.jsonl", steps)
    _w(sd / "events.jsonl", [
        {"t": T0 + 50, "at": "x", "kind": "move", "move": "stems", "song": songs[0][1]["name"], "pos": 58.0},
        {"t": X1, "at": "00:14:10", "kind": "track", "event": "transition_start", "from": songs[0][1]["name"],
         "to": "Anyma & Baset - Neverland (From Japan) (HNTR Remix)", "recipe": "Bass Swap", "out": "b", "in": "a",
         "seconds": 15.2, "a_pos": 190.0},
        {"t": X1 + 15.2, "at": "x", "kind": "track", "event": "transition_end", "now_playing": songs[1][1]["name"], "deck": "a"},
        {"t": X1 + 100, "at": "x", "kind": "move", "move": "roll", "song": songs[1][1]["name"], "pos": 107.0},
        {"t": X2, "at": "00:16:40", "kind": "track", "event": "transition_start", "from": songs[1][1]["name"],
         "to": songs[2][1]["name"], "recipe": "Echo Out", "out": "a", "in": "b", "seconds": 7.7, "a_pos": 157.47},
        {"t": X2 + 7.7, "at": "x", "kind": "track", "event": "transition_end", "now_playing": songs[2][1]["name"], "deck": "b"},
        {"t": X2 + 60, "at": "x", "kind": "llm", "elapsed": 1.0},
    ])
    return sd


def _studied(cache: Path):
    mc.save({"name": "studied-set-x", "source": "atlas:studied", "steps": [
        {"a": NOCTRL, "b": NEVER, "recipe": "Stem Merge", "a_time": 182.184, "b_time": 7.639, "tempo": {"lock": "pitched"}}]}, cache)


def test_timeline_and_sessions(tmp_path):
    _session(tmp_path)
    assert [s["id"] for s in history.sessions(tmp_path)] == [SID]
    tl = history.timeline(SID, tmp_path)
    t1, t2 = tl["transitions"]
    assert (t1["recipe"], t1["a_time"], t1["b_time"], t1["b_exact"]) == ("Bass Swap", 190.0, 7.64, True)
    assert (t2["recipe"], t2["a_time"], t2["b_time"]) == ("Echo Out", 157.47, 18.79)
    assert [m["move"] for m in t2["moves"]] == ["vocal_throw"] and t2["moves"][0]["params"] == THROW
    assert history.pair_plays(NEVER, NOCT, tmp_path)[0]["session"] == SID


def test_replay_reproduces_exits_entries_recipes_moves_and_gaps(tmp_path):
    _session(tmp_path)
    _studied(tmp_path)
    r = rp.build(SID, cache_dir=tmp_path)
    m = r["macro"]
    assert m["source"] == f"replay:{SID}" and m["tracks"] == [NOCTRL, NEVER, NOCT]
    s1, s2 = m["steps"]
    assert (s1["recipe"], s1["a_time"], s1["b_time"]) == ("Bass Swap", 190.0, 7.64)
    assert s1["tempo"] == {"lock": "pitched"}, "tempo from the stored macro step the set performed"
    assert any("Stem Merge was refused" in g for g in s1["gaps"])
    assert s1["moves"][0]["move"] == "AUTO SAMPLER · drop" and s1["moves"][0]["dt"] == pytest.approx(9.7)
    # the owner's favourite: Echo Out + the vocal throw with its logged params
    assert (s2["recipe"], s2["a_time"], s2["b_time"]) == ("Echo Out", 157.47, 18.79)
    vt = s2["moves"][0]
    assert (vt["kind"], vt["move"], vt["side"], vt["params"]) == ("artist_move", "vocal_throw", "a", THROW)
    assert any("tempo / keylock decision not logged" in g for g in s2["gaps"])
    assert [g["n"] for g in r["gaps"]] == [1, 2]
    # a range: that transition only
    one = rp.build(SID, 2, 2, tmp_path)["macro"]
    assert len(one["steps"]) == 1 and one["steps"][0]["b"] == NOCT


def test_time_travel_mid_song_mid_transition_start_end(tmp_path):
    _session(tmp_path)
    # mid-song: No Control 50 s in (a move logged pos 58 at +50 s): interpolated, the replay starts at step 1
    t = rp.travel(SID, T0 + 100, cache_dir=tmp_path)
    assert t["state"]["decks"]["b"]["track_id"] == NOCTRL
    assert t["state"]["decks"]["b"]["pos"] == pytest.approx(58.0 + (190.0 - 58.0) * (50 / 132), abs=0.01)
    assert t["start_step"] == 1 and not t["restarted_transition"]
    assert t["load"]["track_id"] == NOCTRL and t["load"]["pos"] == t["state"]["decks"]["b"]["pos"]
    # mid-transition (3 s into Neverland -> Nocturnal): restarts that transition, A before its exit
    t = rp.travel(SID, X2 + 3, cache_dir=tmp_path)
    assert t["restarted_transition"] and t["start_step"] == 2
    assert t["state"]["decks"]["a"]["pos"] == pytest.approx(157.47, abs=0.01)
    assert t["load"] == {"deck": "a", "track_id": NEVER, "name": "Anyma & Baset - Neverland (From Japan) (HNTR Remix)",
                         "pos": pytest.approx(157.47 - rp.PRE_ROLL_S)}
    assert len(t["replay"]["macro"]["steps"]) == 1 and t["replay"]["macro"]["steps"][0]["moves"][0]["move"] == "vocal_throw"
    # both decks during the first transition, by seconds into the set
    st = history.state_at(SID, 182.0 + 5, tmp_path)
    assert set(st["decks"]) == {"a", "b"} and st["in_transition"] == 1
    # set start / end
    s0 = rp.travel(SID, 0, cache_dir=tmp_path)
    assert s0["load"]["track_id"] == NOCTRL and s0["load"]["pos"] == pytest.approx(8.0)
    end = rp.travel(SID, T0 + 99999, cache_dir=tmp_path)
    assert end["replay"] is None and end["load"]["track_id"] == NOCT
    # a chosen transition
    assert rp.travel(SID, step=1, cache_dir=tmp_path)["load"]["pos"] == pytest.approx(190.0 - rp.PRE_ROLL_S)
    with pytest.raises(ValueError):
        rp.travel("../x", 0, cache_dir=tmp_path)


def test_liked_round_trip_seed_and_played_good(tmp_path):
    _session(tmp_path)
    e = lk.like_played(SID, 2, "vocal throw", cache_dir=tmp_path)
    assert e["replay"] and e["step"]["moves"][0]["params"] == THROW
    got = lk.get(NEVER, NOCT, tmp_path)
    assert got["step"]["recipe"] == "Echo Out" and got["source"] == f"session:{SID}#2"
    assert mc.load(e["macro"], tmp_path)["source"] == "liked" and mc.kind_of(mc.load(e["macro"], tmp_path)) == "yours"
    assert lk.like_played(SID, 2, cache_dir=tmp_path)["macro"] == e["macro"], "same step: the liked macro is reused"
    assert lk.pairs(tmp_path) == [(NEVER, NOCT)]
    assert lk.unlike(NEVER, NOCT, tmp_path) and lk.get(NEVER, NOCT, tmp_path) is None
    # the owner's seed: the three picks; Catchaman -> No Control is a record only (replay false)
    seeded = lk.seed(tmp_path)
    by = {(x["a_name"].split(" - ")[-1][:10], x["b_name"].split(" - ")[-1][:10]): x for x in seeded}
    assert len(seeded) == 3 and lk.seed(tmp_path) == []
    cn = by[("Catchaman", "No Control")]
    assert (cn["step"]["recipe"], cn["step"]["a_time"], cn["step"]["b_time"], cn["replay"]) == ("Long Blend", 194.93, 3.32, False)
    nn = by[("Neverland ", "Nocturnal")]
    assert nn["replay"] and nn["step"]["recipe"] == "Echo Out"
    assert nn["step"]["moves"][0]["move"] == "vocal_throw" and nn["step"]["moves"][0]["params"]["feedback"] == 0.6
    # PLAYED_GOOD evidence on the pair
    from app.music_brain.atlas import pair_atlas as pa
    ev = pa.mine_history(tmp_path, {})
    assert ev[f"{NEVER}>{NOCT}"]["good"] >= 1 and ev[f"{NEVER}>{NOCT}"]["sources"].get("liked") == 1


def test_liked_file_is_atomic_and_tolerant(tmp_path):
    (tmp_path / "liked.json").write_text("{broken", encoding="utf-8")
    assert lk.load(tmp_path) == {}
    lk.like({"a": NEVER, "b": NOCT, "recipe": "Echo Out", "a_time": 1, "b_time": 2}, "t", cache_dir=tmp_path, save_macro=False)
    assert json.loads((tmp_path / "liked.json").read_text())["liked"][f"{NEVER}>{NOCT}"]["step"]["recipe"] == "Echo Out"
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(ValueError):
        lk.like({"a": "x", "b": NOCT}, "t", cache_dir=tmp_path)


def test_api(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from app.tests.py.testclient_compat import TestClient
    from app.ui.services import replay_api

    _session(tmp_path)
    monkeypatch.setattr(replay_api, "REPLAY_CACHE_DIR", tmp_path)
    app = FastAPI()
    app.include_router(replay_api.router)
    c = TestClient(app)
    assert c.get("/api/sessions").json()["sessions"][0]["id"] == SID
    assert len(c.get(f"/api/sessions/{SID}/timeline").json()["transitions"]) == 2
    assert c.get("/api/sessions/nope/timeline").status_code == 404
    assert c.get(f"/api/sessions/{SID}/state", params={"at": "187"}).json()["in_transition"] == 1
    r = c.post("/api/replay", json={"session": SID, "step": 2, "to": 2}).json()
    assert r["replay"]["macro"]["source"] == f"replay:{SID}" and (tmp_path / "macros" / f"{r['replay']['macro']['name']}.json").exists()
    assert r["load"]["track_id"] == NEVER
    r = c.post("/api/replay", json={"session": SID, "at": X2 + 2, "save": False}).json()
    assert r["restarted_transition"] and r["start_step"] == 2
    assert c.post("/api/replay", json={"session": SID}).status_code == 400
    assert c.post("/api/liked", json={"session": SID, "n": 2}).json()["liked"]["b"] == NOCT
    assert c.get(f"/api/sessions/{SID}/timeline").json()["transitions"][1]["liked"] is True
    assert c.get("/api/liked").json()["liked"][0]["a"] == NEVER
    assert c.delete("/api/liked", params={"a": NEVER, "b": NOCT}).json()["removed"] is True
    assert c.post("/api/liked", json={}).status_code == 400
    assert len(c.post("/api/liked/seed").json()["seeded"]) == 3
