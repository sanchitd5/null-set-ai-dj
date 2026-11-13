"""HF13: under a pinned scene the ladder accepts a candidate up to 3 genre steps from the PINNED scene (dj-core decides),
prefers the closer one, never lets the language gate follow the genre distance. Fakes only; the dj-core service answers."""
import time

import pytest

from app.ui.services import suggest_ladder as sl

pytestmark = pytest.mark.needs_djcore

GENRES = {"A1": "house", "A2": "techno", "A3": "drum and bass", "A4": "ambient", "A5": "metal", "A6": "hip hop"}


def row(tag, **kw):
    return dict({"artist": f"Artist {tag}", "title": f"Song {tag}", "genre": GENRES[tag]}, **kw)


def run(set_state, cur_genre="house", lang_check=None, **kw):
    base = dict(deadline_at=time.monotonic() + 10, hard_played=[], earlier=[], playing_id="p", library=[], neighbours=[],
                cur_genre=cur_genre, genre_of=lambda r: r.get("genre") or "", family_jump=lambda a, b: a != b, lang_check=lang_check,
                graph_picks=lambda: [], yt_rows=lambda rem: [], yt_pick=lambda rows: [], reason="t", llm=None)
    base.update(kw)
    return sl.ladder(set_state=set_state, **base)


def titles(r):
    return [s["title"] for s in r["suggestions"]]


def test_neighbours_at_1_2_3_steps_pass_and_far_genre_is_refused():
    llm = lambda rem: [row("A5"), row("A4"), row("A3"), row("A2")]      # metal, ambient (2), dnb (2), techno (1)
    r = run({"scene_genre": "house"}, llm=llm)
    assert r["pick_source"] == "llm"
    assert titles(r) == ["Song A2", "Song A4", "Song A3"]      # closer first (1, then 2 and 2), metal refused


def test_three_steps_from_the_scene_passes():
    r = run({"scene_genre": "ambient"}, llm=lambda rem: [row("A3")])      # chill - melodic - bass - dnb
    assert titles(r) == ["Song A3"]


def test_distance_is_from_the_pinned_scene_not_the_song_now_playing():
    # now playing a dnb song (cur_genre), pinned scene ambient. hip hop is far from both; dnb is exactly 3 from the scene.
    llm = lambda rem: [row("A6"), row("A3")]
    r = run({"scene_genre": "ambient"}, cur_genre="drum and bass", llm=llm)
    assert titles(r) == ["Song A3"]


def test_library_scene_step_uses_the_pinned_scene_not_the_playing_genre():
    lib = [{"track_id": "t1", "artist": "Artist A5", "title": "Song A5", "genre": "metal"},
           {"track_id": "t2", "artist": "Artist A2", "title": "Song A2", "genre": "techno"}]
    r = run({"scene_genre": "house"}, cur_genre="metal", library=lib)      # the playing song is metal: it must not become the scene
    assert r["pick_source"] == "library_scene" and titles(r) == ["Song A2"]


def test_language_gate_stays_hard_at_distance_zero():
    lang = lambda r: ("block", "language jump")
    r = run({"scene_genre": "house"}, llm=lambda rem: [row("A1"), row("A2")], lang_check=lang)
    assert r["pick_source"] == "none" and titles(r) == []      # house itself, and a neighbour: both blocked by language


def test_hop_gate_judges_a_candidate_by_the_pinned_scene_not_the_playing_song():
    from app.ui.services.autopilot_service import _filter_suggestions

    def cand(g):
        return {"artist": "X", "title": "T", "genre": g, "data_genre": g, "genre_hop": 0, "occasion_fit": 8}

    # playing a dnb song inside an ambient-pinned set: ambient is a family jump from the playing song, never from the scene
    data = {"steering": "stay", "current_genre": "drum and bass", "suggestions": [cand("ambient"), cand("metal")]}
    assert [s["genre"] for s in _filter_suggestions(data, [], pinned_scene="ambient")] == ["ambient"]
    assert _filter_suggestions(data, []) == []        # no pin: a jump from the playing song is still refused
