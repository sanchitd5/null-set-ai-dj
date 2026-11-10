"""G20: guaranteed suggest. Fakes only: no network, no LLM, no library files."""
import time

from app.ui.services import suggest_ladder as sl

LIB = [{"track_id": f"t{i}", "artist": f"Art{i}", "title": f"Song{i}"} for i in range(6)]


def run(**kw):
    base = dict(deadline_at=time.monotonic() + 10, hard_played=[], earlier=[], playing_id="t0", library=LIB,
                neighbours=[], cur_genre="", genre_of=lambda r: "", family_jump=lambda a, b: a != b,
                lang_check=None, graph_picks=lambda: [], yt_rows=lambda rem: [], yt_pick=lambda rows: [],
                reason="test", llm=None)
    base.update(kw)
    return sl.ladder(**base)


def gp(n=3):
    return [{"artist": f"G{i}", "title": f"Graph{i}", "track_id": f"g{i}"} for i in range(n)]


def test_graph_first_and_llm_not_called_when_enough():
    calls = []
    r = run(graph_picks=lambda: gp(3), llm=lambda rem: calls.append(1) or [])
    assert r["pick_source"] == "graph" and len(r["suggestions"]) == 3 and not calls


def test_yt_before_llm_and_llm_not_called_when_yt_fills():
    calls = []
    yt = [{"artist": f"Y{i}", "title": f"Yt{i}", "yt_id": f"v{i}"} for i in range(3)]
    r = run(yt_pick=lambda rows: yt, llm=lambda rem: calls.append(1) or [])
    assert r["pick_source"] == "yt_fallback" and not calls and r["suggestions"][0]["yt_id"] == "v0"


def test_llm_tops_up_after_graph_and_yt():
    r = run(graph_picks=lambda: gp(1), llm=lambda rem: [{"artist": "L", "title": "Llm1"}, {"artist": "M", "title": "Llm2"}])
    assert [p["pick_source"] for p in r["suggestions"]] == ["graph", "llm", "llm"]


def test_llm_runs_when_graph_and_yt_empty():
    r = run(llm=lambda rem: [{"artist": "L", "title": "Llm1"}])
    assert r["pick_source"] == "llm"


def test_graph_pick_that_was_played_is_dropped_hard():
    r = run(graph_picks=lambda: gp(1), hard_played=["G0 - Graph0"], llm=None)
    assert r["pick_source"].startswith("library")


def test_llm_exception_falls_to_library():
    def boom(rem):
        raise RuntimeError("x")
    assert run(llm=boom)["pick_source"].startswith("library")


def test_library_scene_uses_neighbours_and_skips_genre_jump():
    nb = [LIB[1], LIB[2]]
    r = run(neighbours=nb, cur_genre="pop", genre_of=lambda r: "metal" if r["track_id"] == "t1" else "pop")
    assert r["pick_source"] == "library_scene" and r["suggestions"][0]["track_id"] == "t2"


def test_library_language_when_scene_all_jumps():
    r = run(cur_genre="pop", genre_of=lambda r: "metal")
    assert r["pick_source"] == "library_language"
    assert "t0" not in [p["track_id"] for p in r["suggestions"]]      # the playing song never


def test_lru_when_everything_heard_in_earlier_sets():
    earlier = [f"Art{i} - Song{i}" for i in range(1, 6)]            # newest first: Song1 newest, Song5 oldest
    r = run(earlier=earlier)
    assert r["pick_source"] == "library_lru" and r["suggestions"][0]["track_id"] == "t5"


def test_any_step_last_and_penalty_only_there():
    lc = lambda row: ("penalty", "mixed")  # noqa: E731
    r = run(lang_check=lc)
    assert r["pick_source"] == "library_any"


def test_language_block_never_passes():
    lc = lambda row: ("block", "other language")  # noqa: E731
    r = run(lang_check=lc, graph_picks=lambda: gp(2))
    assert r["suggestions"] == [] and r["pick_source"] == "none"


def test_played_songs_never_returned_even_last_resort():
    played = [f"Art{i} - Song{i}" for i in range(6)]
    r = run(hard_played=played)
    assert r["suggestions"] == []


def test_empty_library_edge():
    r = run(library=[], neighbours=[])
    assert r["suggestions"] == [] and r["pick_source"] == "none" and "no song" in r["fallback_reason"]


def test_every_pick_carries_source_and_reason():
    r = run()
    assert all(p["pick_source"] and p["fallback_reason"] == "test" for p in r["suggestions"])
    assert len(r["suggestions"]) == 3


def test_env_deadline(monkeypatch):
    monkeypatch.setenv("SUGGEST_DEADLINE_S", "6")
    assert sl.deadline_s() == 6.0 and sl.llm_budget_s() == 4.0
    monkeypatch.delenv("SUGGEST_DEADLINE_S")
    assert sl.deadline_s() == 15.0 and sl.llm_budget_s() == 10.0


def test_slow_impl_answers_before_deadline(monkeypatch):
    from app.ui import server

    monkeypatch.setenv("SUGGEST_DEADLINE_S", "4")
    monkeypatch.setenv("SUGGEST_LLM_BUDGET_S", "1")
    monkeypatch.setattr(server, "_autopilot_suggest_impl", lambda req, ctx=None: time.sleep(30))
    monkeypatch.setattr(server, "_library_rows", lambda: LIB)
    monkeypatch.setattr(server, "_neighbour_rows", lambda tid: [])
    monkeypatch.setattr(server, "_tracks", {})
    req = server.AutopilotSuggestRequest(track_id="t0")
    t = time.monotonic()
    out = server._suggest_guaranteed(req)
    assert time.monotonic() - t < 4 and out["suggestions"] and out["pick_source"].startswith("library")
    assert "deadline" in out["fallback_reason"] and out["suggestions"][0]["track_id"] != "t0"


def test_impl_exception_never_raises(monkeypatch):
    from app.ui import server

    def boom(req, ctx=None):
        raise RuntimeError("kaput")
    monkeypatch.setattr(server, "_autopilot_suggest_impl", boom)
    monkeypatch.setattr(server, "_library_rows", lambda: LIB)
    monkeypatch.setattr(server, "_neighbour_rows", lambda tid: [])
    out = server._suggest_guaranteed(server.AutopilotSuggestRequest(track_id="t0"))
    assert out["suggestions"] and "kaput" in out["fallback_reason"]


def test_vetoed_never_returned_at_any_step():
    vet = ["t1", "Art2 - Song2", "G0 - Graph0"]
    r = run(vetoed=vet, graph_picks=lambda: gp(1), llm=lambda rem: [{"artist": "Art3", "title": "Song3", "track_id": "t3"}])
    ids = [p.get("track_id") for p in r["suggestions"]]
    assert "g0" not in ids and r["pick_source"] == "llm"
    r = run(vetoed=["t1", "Art2 - Song2", "t3", "t4", "t5"])
    assert [p["track_id"] for p in r["suggestions"]] == []        # t0 playing, everything else vetoed
    r = run(vetoed=["t1", "Art2 - Song2"])
    assert not {"t1", "t2"} & {p["track_id"] for p in r["suggestions"]}


def test_vetoed_by_song_key():
    from app.ui.services import pick_pool
    k = pick_pool.key_of("Art1", "Song1")
    r = run(vetoed=[k])
    assert "t1" not in [p["track_id"] for p in r["suggestions"]]


def test_vetoed_survives_last_resort_step():
    r = run(lang_check=lambda row: ("penalty", "x"), vetoed=["t1", "t2", "t3", "t4"])
    assert [p["track_id"] for p in r["suggestions"]] == ["t5"] and r["pick_source"] == "library_any"
