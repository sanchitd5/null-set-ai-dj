"""R3: the YouTube-related fallback (owner: "if nothing passes, use the YouTube suggestions") and a suggest that never 500s.
Fakes only, no network, no LLM."""
from types import SimpleNamespace

import pytest

from app.ui.services.autopilot_service import yt_fallback

ROWS = [
    {"artist": "Aaa", "title": "One", "source": "yt", "yt_id": "v1"},
    {"artist": "Bbb", "title": "Two", "source": "yt", "yt_id": "v2"},
    {"artist": "Ccc", "title": "Three", "source": "yt", "yt_id": "v3", "track_id": "t3"},
]


def _genre(table):
    return lambda r: table.get(r["title"], "")


def test_first_row_that_passes_wins_and_carries_the_source():
    out = yt_fallback(ROWS, cur_genre="indie pop", genre_of=_genre({"One": "indie pop"}))
    assert out[0]["title"] == "One" and out[0]["source"] == "yt_fallback"
    assert out[0]["genre_src"] == "data" and "needs_lang_verify" not in out[0]


def test_a_data_genre_jump_is_refused_never_the_models_label():
    out = yt_fallback(ROWS[:2], cur_genre="indie pop", genre_of=_genre({"One": "heavy metal", "Two": "indie pop"}), limit=3)
    assert [p["title"] for p in out] == ["Two"]


def test_unknown_genre_is_kept_but_must_pass_the_language_verify():
    out = yt_fallback(ROWS[:1], cur_genre="indie pop", genre_of=_genre({}))
    assert out and out[0]["needs_lang_verify"] is True and out[0]["genre_src"] == "unknown"


def test_language_block_and_penalty_refuse():
    verdicts = {"One": ("block", "xx not in set"), "Two": ("penalty", "mixed"), "Three": ("ok", "")}
    out = yt_fallback(ROWS, cur_genre="", genre_of=_genre({}), lang_check=lambda r: verdicts[r["title"]], limit=3)
    assert [p["title"] for p in out] == ["Three"] and out[0]["track_id"] == "t3"


def test_heard_songs_are_skipped_and_nothing_passing_is_an_empty_list():
    assert yt_fallback(ROWS, cur_genre="", genre_of=_genre({}), exclude=["Aaa - One", "Two", "Ccc - Three"]) == []
    assert yt_fallback([], cur_genre="", genre_of=_genre({})) == []
    assert yt_fallback([{"artist": "x"}, "junk", None], cur_genre="", genre_of=_genre({})) == []


def test_a_raising_language_check_does_not_refuse():
    def boom(r):
        raise RuntimeError("down")
    assert yt_fallback(ROWS[:1], cur_genre="", genre_of=_genre({}), lang_check=boom)


# ---- /api/autopilot/suggest never answers 500 -----------------------------------------------------------------------
@pytest.fixture
def suggest_env(monkeypatch, tmp_path):
    import app.ui.server as srv
    from app.ui.services import engine, yt_related

    f = tmp_path / "Aaa Bbb - Seed.mp3"
    f.write_bytes(b"x")
    monkeypatch.setattr(srv, "_track_path", lambda tid: f)
    monkeypatch.setattr(srv, "analyze_track", lambda p: SimpleNamespace(
        energy_curve=[0.5], bpm=120.0, key=None, duration=200.0))
    monkeypatch.setitem(srv._track_names, "seed", "Aaa Bbb - Seed")
    monkeypatch.setattr(yt_related, "candidates", lambda *a, **k: (
        [{"artist": "Rel", "title": "Ated", "source": "yt", "yt_id": "v9"}], {"source": "cache", "n": 1}))
    srv._suggest_inflight.clear()

    def raising(*a, **k):
        raise RuntimeError("LLM exploded")
    monkeypatch.setattr(engine, "current", lambda: SimpleNamespace(suggest=raising, host=SimpleNamespace()))
    return srv


@pytest.mark.needs_djcore
def test_llm_exception_answers_200_with_the_youtube_fallback(suggest_env):
    srv = suggest_env
    out = srv.autopilot_suggest(srv.AutopilotSuggestRequest(track_id="seed", set_id="r3a"))
    # G20 owner order: graph, then YouTube related, then the LLM: a YouTube pick means the LLM is not asked at all
    assert out["suggestions"] and out["suggestions"][0]["source"] == "yt_fallback"
    assert out["pick_source"] == "yt_fallback"


def test_llm_exception_with_nothing_to_fall_back_on_is_an_empty_list_with_a_reason(suggest_env, monkeypatch):
    from app.ui.services import yt_related

    srv = suggest_env
    monkeypatch.setattr(yt_related, "candidates", lambda *a, **k: ([], {"source": "empty", "n": 0}))
    monkeypatch.setattr(srv, "_library_rows", lambda: [])       # G20: an empty library too, else the library ladder answers
    monkeypatch.setattr(srv, "_neighbour_rows", lambda tid: [])
    srv._suggest_inflight.clear()
    out = srv.autopilot_suggest(srv.AutopilotSuggestRequest(track_id="seed", set_id="r3b"))
    assert out["suggestions"] == [] and "LLM exploded" in out["fallback_reason"]


@pytest.mark.needs_djcore
def test_an_unexpected_error_in_the_impl_is_not_a_500(suggest_env, monkeypatch):
    srv = suggest_env
    monkeypatch.setattr(srv, "_autopilot_suggest_impl", lambda req, ctx=None: (_ for _ in ()).throw(ValueError("filter bug")))
    srv._suggest_inflight.clear()
    out = srv.autopilot_suggest(srv.AutopilotSuggestRequest(track_id="seed", set_id="r3c"))
    assert out["suggestions"] and "filter bug" in out["fallback_reason"]   # G20: the ladder still answers a song
