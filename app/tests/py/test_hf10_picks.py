"""HF10: a pinned scene binds every ladder step; the server starts downloads for picks it lacks. Fakes only."""
import time

from app.ui import server
from app.ui.services import suggest_ladder as sl

LIB = [{"track_id": f"t{i}", "artist": f"Art{i}", "title": f"Song{i}"} for i in range(4)]


def run(**kw):
    base = dict(deadline_at=time.monotonic() + 10, hard_played=[], earlier=[], playing_id="t0", library=LIB,
                neighbours=[], cur_genre="", genre_of=lambda r: r.get("genre", ""), family_jump=lambda a, b: a != b,
                lang_check=None, graph_picks=lambda: [], yt_rows=lambda rem: [], yt_pick=lambda rows: [], reason="t", llm=None)
    base.update(kw)
    return sl.ladder(**base)


def test_off_scene_graph_picks_refused_in_pinned_scene():
    graph = [{"artist": "A", "title": "Off", "genre": "pop"}, {"artist": "B", "title": "On", "genre": "rap"}]
    r = run(graph_picks=lambda: graph, set_state={"scene_genre": "rap"})
    assert [p["title"] for p in r["suggestions"]] == ["On"] and r["pick_source"] == "graph"


def test_off_scene_yt_and_llm_refused_too():
    off = [{"artist": "A", "title": "Off", "genre": "pop", "yt_id": "x" * 11}]
    r = run(yt_pick=lambda rows: off, llm=lambda rem: off, set_state={"scene_genre": "rap"},
            genre_of=lambda r: "rap" if r.get("track_id") else "")
    assert r["pick_source"].startswith("library")


def test_pinned_scene_gives_way_only_at_the_last_library_step():
    r = run(set_state={"scene_genre": "rap"}, genre_of=lambda r: "pop")
    assert r["pick_source"] == "library_any" and r["suggestions"]


def test_no_pin_graph_unchanged():
    graph = [{"artist": "A", "title": "Off", "genre": "pop"}]
    assert run(graph_picks=lambda: graph)["pick_source"] == "graph"


def test_prefetch_starts_jobs_for_picks_without_audio(monkeypatch):
    started = []
    monkeypatch.setattr(server, "_start_download_job", lambda url, label: started.append(url) or f"job{len(started)}")
    picks = [{"artist": "A", "title": "T", "yt_id": "abcdefghijk"}, {"artist": "B", "title": "U", "track_id": "t1"},
             {"artist": "C", "title": "V"}]
    server._prefetch_picks(picks)
    assert started == ["https://www.youtube.com/watch?v=abcdefghijk"]
    assert picks[0]["download_job_id"] == "job1" and "download_job_id" not in picks[1] and "download_job_id" not in picks[2]


def test_prefetch_failure_leaves_pick(monkeypatch):
    def boom(url, label):
        raise RuntimeError("x")
    monkeypatch.setattr(server, "_start_download_job", boom)
    picks = [{"artist": "A", "title": "T", "yt_id": "abcdefghijk"}]
    server._prefetch_picks(picks)
    assert "download_job_id" not in picks[0]
