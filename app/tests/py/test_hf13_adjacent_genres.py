"""HF13 / VIBELOCK: under a pinned scene the ladder accepts a candidate up to 1 genre step from the PINNED scene and inside its mood band (dj-core decides),
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


def test_only_the_exact_family_and_one_step_pass_the_rest_is_refused():
    llm = lambda rem: [row("A5"), row("A4"), row("A3"), row("A2"), row("A1")]      # metal, ambient (2), dnb (2), techno (1), house (0)
    r = run({"scene_genre": "house"}, llm=llm)
    assert r["pick_source"] == "llm"
    assert titles(r) == ["Song A1", "Song A2"]      # closer first (0, then 1); metal, ambient and dnb refused


def test_three_steps_from_the_scene_is_refused_now():
    r = run({"scene_genre": "ambient"}, llm=lambda rem: [row("A3")])      # chill - melodic - bass - dnb
    assert titles(r) == []


def test_distance_is_from_the_pinned_scene_not_the_song_now_playing():
    # now playing a dnb song (cur_genre), pinned scene house. hip hop is far from both; dnb is 2 from the scene (refused), techno 1 (kept).
    llm = lambda rem: [row("A6"), row("A3"), row("A2")]
    r = run({"scene_genre": "house"}, cur_genre="drum and bass", llm=llm)
    assert titles(r) == ["Song A2"]


# VIBELOCK: the picks booked under a hip hop pin in set 2026-11-18_040803 (labels from pick_reason) are refused, the vibe stays
VIBE = {"disco house": "Disco", "indie pop": "Indie", "reggaeton": "Regg", "art pop": "Art", "synth pop": "Synth", "hip hop": "Hip", "trap": "Trap",
        "r&b": "Rnb"}


def vrow(genre):
    return {"artist": "Artist " + VIBE[genre], "title": "Song " + VIBE[genre], "genre": genre}


def test_hip_hop_pin_refuses_the_picks_that_drifted_that_set():
    llm = lambda rem: [vrow(g) for g in VIBE]
    r = run({"scene_genre": "hip hop"}, cur_genre="indie pop", llm=llm)      # the playing song drifted to indie pop: the pin still rules
    assert sorted(titles(r)) == ["Song Hip", "Song Trap"]      # disco house, indie pop, reggaeton, art pop, synth pop refused; r&b is another mood band


def test_romantic_pin_refuses_a_party_genre_one_step_away():
    r = run({"scene_genre": "r&b"}, llm=lambda rem: [vrow("hip hop"), vrow("r&b")])
    assert titles(r) == ["Song Rnb"]


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
