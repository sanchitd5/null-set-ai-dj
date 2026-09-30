"""Set history (app/music_brain/history.py): the user-DB index over sessions/*/events.jsonl,
songs/*/meta.json and set_logs/, rebuildable from them. Synthetic tmp_path caches only."""
import json

from app.music_brain import db, history

S = "2026-09-30_200000"
T0 = 1_790_000_000.0
A_ID, B_ID, C_ID = "a" * 16, "b" * 16, "c" * 16
A, B, C = "Anyma - Eternity", "Cassian - SOS", "Argy - WIND"


def _ev(t, kind, **f):
    return json.dumps({"t": t, "at": "20:00:00", "kind": kind, **f}) + "\n"


def _song(sdir, nn, tid, name, deck, entry, exit_, entry_pos=0.0, steps=()):
    d = sdir / "songs" / f"{nn:02d}-{name.split(' - ')[1].lower()}"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "nn": nn, "track_id": tid, "name": name, "deck": deck, "bpm": 124.0, "key": "8A", "energy_level": 5,
        "recipe_in": None, "recipe_out": None, "entry_t": entry, "exit_t": exit_, "entry_song_s": entry_pos,
        "exit_song_s": None, "steps": len(steps), "dropped": 0, "session": S}))
    (d / "steps.jsonl").write_text("".join(json.dumps(s) + "\n" for s in steps))


def _cache(tmp_path):
    cache = tmp_path / "cache"
    sdir = cache / "sessions" / S
    sdir.mkdir(parents=True)
    (sdir / "events.jsonl").write_text(
        _ev(T0 + 1, "llm", call="suggest")
        + _ev(T0 + 30, "move", move="stem-move", label="REMIX", why="bars 12-16", deck="a", song=A, pos=40.0)
        + _ev(T0 + 100, "track", event="transition_start", **{"from": A, "to": B}, recipe="Bass Swap",
              out="a", **{"in": "b"}, seconds=20.0)
        + _ev(T0 + 120, "track", event="transition_end", now_playing=B, deck="b", set_songs=2)
        + _ev(T0 + 200, "track", event="transition_start", **{"from": B, "to": C}, recipe="Echo Out",
              out="b", **{"in": "a"}, seconds=10.0))
    # song_log files the choice of the NEXT song under the song playing while it is made
    _song(sdir, 1, A_ID, A, "a", T0, T0 + 120, entry_pos=10.0, steps=[
        {"t": T0, "phase": "transition-in", "kind": "song_start"},
        {"t": T0 + 60, "phase": "selection", "kind": "macro", "decision": "skipped", "why": "no macro"},
        {"t": T0 + 90, "phase": "selection", "kind": "macro", "decision": "PLAY MACRO step 2", "why": "Bass Swap"}])
    _song(sdir, 2, B_ID, B, "b", T0 + 100, T0 + 210, entry_pos=32.0, steps=[
        {"t": T0 + 80, "phase": "selection", "kind": "studied", "decision": "follow", "why": "chose B itself"},
        {"t": T0 + 100, "phase": "transition-in", "kind": "song_start"},
        {"t": T0 + 150, "phase": "selection", "kind": "studied", "decision": "follow", "why": "follow: set X"}])
    _song(sdir, 3, C_ID, C, "a", T0 + 200, None)
    (cache / "set_logs").mkdir()
    (cache / "set_logs" / "abc.djset.json").write_text(json.dumps({
        "metadata": {"created_at": "2026-09-30T06:17:36Z", "duration_seconds": 12.5, "track_a_id": A_ID,
                     "track_b_id": B_ID}, "control_event_stream": [{"t": 1, "param": "a:low", "val": 0}]}))
    return cache


def _dump(cache):
    conn = db.connect(cache / db.USER_DB)
    return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
            for t in ("sessions", "plays", "transitions", "moves", "set_logs")}


def test_first_open_ingests_everything(tmp_path):
    cache = _cache(tmp_path)
    ss = history.sessions(cache)
    assert [s["id"] for s in ss] == [S] and ss[0]["songs"] == 3 and ss[0]["transitions"] == 2 and ss[0]["moves"] == 1
    tl = history.timeline(S, cache)
    assert [r["type"] for r in tl][:4] == ["play_start", "move", "transition", "play_start"]
    t1 = history.pair_plays(A_ID, B_ID, cache)
    assert len(t1) == 1 and t1[0]["recipe"] == "Bass Swap" and t1[0]["source"] == "macro"
    assert "PLAY MACRO step 2" in t1[0]["step"]
    assert history.pair_plays(B, C, cache)[0]["source"] == "follow set", "by name works too"
    assert [r["session"] for r in history.sessions_with(C_ID, cache)] == [S]
    assert _dump(cache)["set_logs"][0][:5] == ("abc", "2026-09-30T06:17:36Z", 12.5, A_ID, B_ID)


def test_state_at_mid_song_and_mid_transition(tmp_path):
    cache = _cache(tmp_path)
    mid = history.state_at(S, T0 + 50, cache)
    assert set(mid["decks"]) == {"a"} and mid["transition"] is None
    assert mid["decks"]["a"]["pos"] == 60.0, "the move logged pos 40 at +30 s: +20 s later"
    tr = history.state_at(S, T0 + 110, cache)
    assert set(tr["decks"]) == {"a", "b"}
    assert tr["decks"]["b"]["pos"] == 42.0 and tr["decks"]["a"]["name"] == A
    assert tr["transition"]["recipe"] == "Bass Swap" and tr["transition"]["progress"] == 0.5


def test_incremental_append_and_rebuild_give_the_same_rows(tmp_path):
    cache = _cache(tmp_path)
    history.sessions(cache)
    p = cache / "sessions" / S / "events.jsonl"
    with open(p, "a") as f:
        f.write(_ev(T0 + 300, "move", move="fx", label="ECHO", why="out", deck="a", song=C, pos=12.0))
        f.write('{"t": 1790000400, "kind": "mo')                    # a half-written line: waits
    history.on_event(S, p)
    assert [r["move"] for r in history.timeline(S, cache) if r["type"] == "move"] == ["stem-move", "fx"]
    with open(p, "a") as f:
        f.write('ve", "move": "x", "deck": "b", "song": "B", "pos": 1.0}\n')
    history.on_event(S, p)
    history.on_event(S, p)                                          # nothing new: no duplicate
    rows = _dump(cache)
    assert [r[6] for r in rows["moves"]] == ["stem-move", "fx", "x"]
    assert history.rebuild(cache)["sessions"] == 1
    assert _dump(cache) == rows, "the DB is an index: rebuilt from the JSONL it is the same"


def test_live_play_hook_and_never_pruned(tmp_path):
    cache = _cache(tmp_path)
    history.sessions(cache)
    sdir = cache / "sessions" / S
    folder = next((sdir / "songs").glob("03-*"))
    meta = json.loads((folder / "meta.json").read_text())
    meta["exit_t"] = T0 + 400
    history.on_play(S, meta, folder)
    assert history.sessions_with(C_ID, cache)[0]["ended"] == T0 + 400
    import shutil
    shutil.rmtree(sdir)                                             # the JSONL is gone (old pruning)
    history.rebuild(cache)
    assert [s["id"] for s in history.sessions(cache)] == [S], "every set stays"


def test_user_marks_live_in_the_user_db(tmp_path):
    import pytest
    from app.music_brain import user_marks as um
    um.mark("veto", f"{A_ID}>{B_ID}", {"why": "vocal clash"}, tmp_path)
    um.mark("liked", A_ID, None, tmp_path)
    assert um.is_marked("veto", f"{A_ID}>{B_ID}", tmp_path) and not um.is_marked("veto", A_ID, tmp_path)
    assert um.marks("veto", tmp_path)[f"{A_ID}>{B_ID}"]["data"] == {"why": "vocal clash"}
    assert um.unmark("liked", A_ID, tmp_path) and um.marks("liked", tmp_path) == {}
    assert (tmp_path / db.USER_DB).is_file() and not (tmp_path / db.APP_DB).exists()
    with pytest.raises(ValueError):
        um.mark("", "x", None, tmp_path)


def test_session_log_appends_into_history(tmp_path, monkeypatch):
    from app.ui.services import session_log
    monkeypatch.setattr(session_log, "SESSIONS_DIR", tmp_path / "c2" / "sessions")
    session_log._write("move", move="stem-move", label="HOLD", why="w", deck="a", song=A, pos=5.0)
    session_log._write("track", event="transition_start", **{"from": A, "to": B}, recipe="Echo Out", seconds=8.0)
    got = history.timeline(session_log.SESSION_ID, tmp_path / "c2")
    assert [r["type"] for r in got] == ["move", "transition"]
