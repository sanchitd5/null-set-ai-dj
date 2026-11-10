"""G16: YouTube's related songs as the third source of the suggest pool. Fakes only: no network. Synthetic names."""
import threading
import time

import pytest

from app.ui.services import pick_pool as pp
from app.ui.services import yt_related as yr


def entries(n, tag="A"):
    return [{"id": f"{tag}{i:010d}"[:11], "title": f"{tag}Artist{i} - {tag}Song{i}", "channel": "", "duration": 200}
            for i in range(n)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(yr, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(yr, "PACE_S", 0.0)
    monkeypatch.setattr(yr, "_cache", {})
    monkeypatch.setattr(yr, "_loaded", False)
    monkeypatch.setattr(yr, "_inflight", {})
    monkeypatch.setattr(yr, "_failed", {})
    monkeypatch.setattr(yr, "_last_call", 0.0)
    e = type("E", (), {})()
    e.graph, e.fetches, e.marks, e.vid = None, [], [], "SEEDSEEDSE0"
    e.mix = entries(12)
    e.fail = False
    e.gate = None

    monkeypatch.setattr(yr, "_graph", lambda track_id="", yt_id="": e.graph)
    monkeypatch.setattr("app.ui.services.album_art._video_id", lambda tid: e.vid)

    def fetch(vid, limit=yr.LIMIT):
        e.fetches.append(vid)
        if e.gate:
            e.gate.wait(5)
        if e.fail:
            raise RuntimeError("bot check")
        return list(e.mix)

    monkeypatch.setattr(yr, "fetch_mix", fetch)
    monkeypatch.setattr("app.music_brain.learning.phone_learn._yt_edges",
                        lambda item, d, dev, cd: e.marks.append(d) or {"edges": len(d["related"])})
    return e


def graph_rows(n):
    return {"yt_id": "SEEDSEEDSE0", "rows": [{"artist": f"G Artist{i}", "title": f"G Song{i}", "yt_id": f"g{i:010d}"[:11],
                                               "track_id": None, "weight": 0.5, "n": 1} for i in range(n)]}


def test_graph_edges_are_enough_and_nothing_is_fetched(env):
    env.graph = graph_rows(8)
    rows, info = yr.candidates("t1")
    assert info == {"source": "graph", "n": 8} and len(rows) == 8 and not env.fetches
    assert rows[0]["source"] == "yt" and rows[0]["artist"] == "G Artist0"


def test_a_library_song_in_the_graph_list_keeps_its_track_id(env):
    g = graph_rows(6)
    g["rows"][0]["track_id"] = "t7"
    env.graph = g
    rows, _ = yr.candidates("t1")
    assert rows[0]["track_id"] == "t7" and "track_id" not in rows[1]


def test_short_graph_list_is_topped_up_by_a_fetch_and_deduped(env):
    env.graph = graph_rows(2)
    env.mix = entries(6) + [{"id": "dup00000000", "title": "G Artist0 - G Song0", "duration": 200}]
    rows, info = yr.candidates("t1")
    assert info["source"] == "fetch" and len(rows) == 2 + 6                      # the duplicate of a graph row is dropped
    assert env.fetches == ["SEEDSEEDSE0"]


def test_fetch_caches_per_video_and_stores_yt_edge_marks(env):
    rows, info = yr.candidates("t1", {"artist": "Seed Artist", "title": "Seed Song"})
    assert info["source"] == "fetch" and len(rows) == 12
    rows2, info2 = yr.candidates("t1")
    assert info2["source"] == "cache" and rows2 == rows and env.fetches == ["SEEDSEEDSE0"]
    assert len(env.marks) == 1 and env.marks[0]["seed"] == {"yt": "SEEDSEEDSE0", "id": "t1", "artist": "Seed Artist",
                                                            "title": "Seed Song"}
    assert len(env.marks[0]["related"]) == 12 and env.marks[0]["related"][0]["rank"] == 0


def test_cache_survives_a_restart_and_expires_after_a_week(env, monkeypatch):
    yr.candidates("t1")
    monkeypatch.setattr(yr, "_cache", {})
    monkeypatch.setattr(yr, "_loaded", False)                                    # a new process reads the file
    assert yr.candidates("t1")[1]["source"] == "cache" and len(env.fetches) == 1
    real = time.time
    monkeypatch.setattr(yr.time, "time", lambda: real() + yr.CACHE_TTL_S + 60)
    assert yr.candidates("t1")[1]["source"] == "fetch" and len(env.fetches) == 2


def test_no_video_id_is_no_candidates(env):
    env.vid = None
    assert yr.candidates("t1") == ([], {"source": "no_id", "n": 0}) and not env.fetches


def test_slow_fetch_times_out_fast_then_fills_the_cache_for_the_next_request(env):
    env.gate = threading.Event()
    t0 = time.monotonic()
    rows, info = yr.candidates("t1", timeout=0.2)
    assert info["source"] == "timeout" and rows == [] and time.monotonic() - t0 < 1.5
    env.gate.set()
    for _ in range(100):                                                         # the fetch finished in the background
        if yr.cached("SEEDSEEDSE0") is not None:
            break
        time.sleep(0.05)
    rows, info = yr.candidates("t1")
    assert info["source"] == "cache" and len(rows) == 12 and env.fetches == ["SEEDSEEDSE0"]


def test_failed_fetch_gives_no_rows_and_is_not_hammered(env):
    env.fail = True
    assert yr.candidates("t1")[0] == []
    rows, info = yr.candidates("t1")
    assert rows == [] and info["source"] == "cooling" and len(env.fetches) == 1
    env.fail = False
    yr._failed.clear()
    assert yr.candidates("t1")[1]["source"] == "fetch"


def test_two_requests_for_one_video_share_one_fetch(env):
    env.gate = threading.Event()
    out = []
    ts = [threading.Thread(target=lambda: out.append(yr.candidates("t1", timeout=3)[1]["source"])) for _ in range(2)]
    for t in ts:
        t.start()
    time.sleep(0.2)
    env.gate.set()
    for t in ts:
        t.join(5)
    assert out == ["fetch", "fetch"] and len(env.fetches) == 1


def test_fetches_are_paced(env, monkeypatch):
    monkeypatch.setattr(yr, "PACE_S", 0.4)
    env.graph = None
    starts = []
    orig = yr.fetch_mix

    def stamp(vid, limit=yr.LIMIT):
        starts.append(time.monotonic())
        return orig(vid, limit)

    monkeypatch.setattr(yr, "fetch_mix", stamp)
    env.vid = "VIDONEVIDON"
    yr.candidates("t1")
    env.vid = "VIDTWOVIDTW"
    yr.candidates("t2")
    assert len(starts) == 2 and starts[1] - starts[0] >= 0.35


def test_kick_fetches_in_the_background_so_the_next_request_has_it(env):
    yr.kick("t1", {"artist": "Seed Artist", "title": "Seed Song"})
    for _ in range(100):
        if yr.cached("SEEDSEEDSE0") is not None:
            break
        time.sleep(0.05)
    assert env.fetches == ["SEEDSEEDSE0"]
    assert yr.candidates("t1")[1]["source"] == "cache" and env.fetches == ["SEEDSEEDSE0"]


def test_kick_does_nothing_when_the_graph_already_has_enough(env):
    env.graph = graph_rows(9)
    yr.kick("t1")
    time.sleep(0.3)
    assert not env.fetches


def test_rows_drop_mixes_non_music_and_wrong_lengths_and_read_topic_channels():
    ent = [{"id": "a" * 11, "title": "Artist One - Song One", "duration": 200},
           {"id": "b" * 11, "title": "Artist Two - Song Two (1 Hour Mix)", "duration": 200},
           {"id": "c" * 11, "title": "Artist Three - Song Three", "duration": 30},
           {"id": "d" * 11, "title": "Song Four", "channel": "Artist Four - Topic", "duration": 200},
           {"id": "e" * 11, "title": "Song Five", "channel": "Some Channel", "duration": 200}]
    rows = yr._rows_of(ent)
    assert [(r["artist"], r["title"], r["rank"]) for r in rows] == [("Artist One", "Song One", 0),
                                                                    ("Artist Four", "Song Four", 3)]


# ---------------------------------------------------------------------------------------------- the pool
def yt_pool_rows(n):
    return [{"artist": f"Y Artist{i}", "title": f"Y Song{i}", "source": "yt"} for i in range(n)]


def test_pool_includes_yt_as_a_third_source_with_a_quarter_of_the_slots():
    cat = [{"artist": f"C Artist{i}", "title": f"C Song{i}", "source": "catalogue"} for i in range(20)]
    atl = [{"artist": f"L Artist{i}", "title": f"L Song{i}", "source": "library", "track_id": f"t{i}"} for i in range(20)]
    pool = pp.build_pool(atlas=atl, cat=cat, yt=yt_pool_rows(20), exclude=[], seed_title="Seed")
    by = [c["source"] for c in pool]
    assert len(pool) == pp.MAX_POOL and by[:8] == ["library"] * 8 and by[8:12] == ["yt"] * 4 and by[12:] == ["catalogue"] * 4


def test_pool_gives_unused_slots_to_yt_when_the_others_are_short():
    pool = pp.build_pool(atlas=[], cat=[], yt=yt_pool_rows(30), exclude=[], seed_title="Seed")
    assert len(pool) == pp.MAX_POOL and {c["source"] for c in pool} == {"yt"}


def test_pool_without_yt_is_unchanged():
    cat = [{"artist": f"C Artist{i}", "title": f"C Song{i}", "source": "catalogue"} for i in range(5)]
    assert [c["title"] for c in pp.build_pool(atlas=[], cat=cat, exclude=[], seed_title="Seed")] == \
        [c["title"] for c in pp.build_pool(atlas=[], cat=cat, yt=None, exclude=[], seed_title="Seed")]


def test_yt_rows_get_the_same_gates_language_repeats_and_dedupe():
    yt = yt_pool_rows(4) + [{"artist": "Y Artist0", "title": "Y Song0", "source": "yt"}]
    cat = [{"artist": "Y Artist1", "title": "Y Song1", "source": "catalogue"}]

    def lang(row):
        return ("block", "x") if row["title"] == "Y Song2" else ("ok", "")

    pool = pp.build_pool(atlas=[], cat=cat, yt=yt, exclude=["Played - Y Song3"], seed_title="Seed", lang_check=lang)
    titles = [c["title"] for c in pool]
    assert titles == ["Y Song0", "Y Song1"] and [c["source"] for c in pool] == ["yt", "yt"]   # blocked, played, repeats gone


def test_a_grounded_yt_pick_is_marked_pool_source_yt():
    pool = pp.build_pool(atlas=[], cat=[], yt=yt_pool_rows(5), exclude=[], seed_title="Seed")
    ok, free = pp.ground([{"cid": "C2", "artist": "x", "title": "y"}], pool)
    assert ok[0]["pool_source"] == "yt" and ok[0]["title"] == "Y Song1" and not free


# ---------------------------------------------------------------------------------------------- the suggest route
def test_suggest_puts_yt_in_the_pool_and_logs_the_shadow_event(monkeypatch):
    import types
    from pathlib import Path

    import app.ui.server as srv
    from app.ui.services import engine, session_log

    events, seen = [], {}
    monkeypatch.setattr(session_log, "log", lambda kind, **f: events.append((kind, f)))
    monkeypatch.setattr(srv, "_track_path", lambda tid: Path("x.wav"))
    monkeypatch.setattr(srv, "analyze_track", lambda p: types.SimpleNamespace(energy_curve=None, key=None, bpm=120.0,
                                                                              duration=200.0))
    monkeypatch.setattr(srv, "_track_names", {"t1": "Seed Artist - Seed Song"})
    monkeypatch.setattr(srv, "_set_memory", types.SimpleNamespace(record=lambda *a, **k: None,
                                                                 earlier_sets=lambda *a, **k: []))
    monkeypatch.setattr(srv, "_save_labels", lambda: None)
    monkeypatch.setattr(srv, "_song_step", lambda *a, **k: None)
    monkeypatch.setattr(yr, "candidates", lambda tid, seed=None, timeout=3.0: (
        [{"artist": f"Y Artist{i}", "title": f"Y Song{i}", "source": "yt"} for i in range(6)],
        {"source": "fetch", "n": 6}))

    def suggest(**kw):
        seen.update(kw)
        pick = {"artist": "Y Artist1", "title": "Y Song1", "reason": "x"}
        pick["pool_source"] = "yt"
        return [pick]

    monkeypatch.setattr(engine, "current", lambda: types.SimpleNamespace(suggest=suggest, host=types.SimpleNamespace()))
    out = srv._autopilot_suggest_impl(srv.AutopilotSuggestRequest(track_id="t1", occasion="club", history=[]))
    assert [c["source"] for c in seen["candidates"]] == ["yt"] * 6 and out["suggestions"][0]["title"] == "Y Song1"
    ev = [f for k, f in events if k == "yt_pool"]
    assert ev == [{"seed": "Seed Artist - Seed Song", "source": "fetch", "n_yt": 6, "in_pool": 6, "n_pool": 6,
                   "picked": True, "n_picked": 1, "lookahead": False}]
