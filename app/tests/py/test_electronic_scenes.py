"""electronic split into sub-families (genre.ELECTRONIC_CLUSTERS): owner, session 2026-09-30_205816,
The Sound of Goodbye (trance) -> Bonobo - Me and You (downtempo) was a genre mismatch."""
import json

from app.music_brain.analysis.genre import (ELECTRONIC_CLUSTERS, family_jump, genre_families, scene_keys,
                                            scene_relation)

# Every distinct label in the library's labels table (app.db, 2026-09-30), with its expected families.
LIBRARY = {
    "electronic": {"electronic"}, "house": {"house"}, "pop": {"pop"}, "melodic house": {"melodic"},
    "melodic techno": {"melodic"}, "punjabi": {"south_asian"}, "hip hop": {"hiphop"}, "techno": {"techno"},
    "reggaeton": {"latin"}, "dubstep": {"bass"}, "r&b": {"rnb"}, "downtempo": {"chill"}, "indie": set(),
    "electronica": {"chill"}, "ambient": {"chill"}, "afro house": {"afro", "house"}, "afropop": {"afro", "pop"},
    "trance": {"melodic"}, "bollywood": {"south_asian"}, "edm": {"edm"}, "electro": {"edm"}, "dance": {"edm"},
    "synth": set(), "latin": {"latin"}, "uk garage": {"house"}, "indie folk": {"country"}, "afrobeat": {"afro"},
    "rock": {"rock"}, "disco": {"jazz"}, "bhangra": {"south_asian"}, "progressive house": {"melodic"},
    "indie pop": {"pop"}, "deep house": {"house"}, "bass house": {"house", "bass"}, "synth pop": {"pop"},
    "dnb": {"dnb"}, "punjabi bhangra": {"south_asian"}, "pakistani pop": {"pop", "south_asian"},
    "hindi": {"south_asian"}, "indie rock": {"rock"}, "spoken word": set(), "trap": {"hiphop"},
    "punjabi pop": {"pop", "south_asian"}, "disco house": {"house", "jazz"}, "haryanvi": {"south_asian"},
    "ubiquitous techno": {"techno"}, "afro electronic": {"afro", "electronic"}, "punk": {"rock"},
    "punjabi hip hop": {"hiphop", "south_asian"}, "breaks": {"breaks"}, "electronic pop": {"electronic", "pop"},
    "bollywood pop": {"pop", "south_asian"}, "folk": {"country"}, "disco pop": {"jazz", "pop"},
    "melodic dubstep": {"bass"}, "drill": {"hiphop"}, "dance pop": {"edm", "pop"}, "tech house": {"house"},
    "bollywood funk": {"jazz", "south_asian"}, "soul": {"rnb"}, "rap": {"hiphop"}, "minimal techno": {"techno"},
    "melodic": set(), "punjabi metal": {"rock", "south_asian"}, "ubiquitous": set(), "synth house": {"house"},
    "indie electronic": {"electronic"}, "future bass": {"bass"}, "hindi rap": {"hiphop", "south_asian"},
    "drum and bass": {"dnb"}, "latin electronic": {"electronic", "latin"}, "bass music": {"bass"},
    "latin house": {"house", "latin"}, "dancehall": {"electronic"}, "jazz": {"jazz"},
    "electro pop": {"edm", "pop"}, "idm": {"chill"}, "pakistani": {"south_asian"}, "hindi pop": {"pop", "south_asian"},
}


def test_every_library_label_maps():
    for label, want in LIBRARY.items():
        assert genre_families(label) == want, label
    # an unrecognised label stays unknown (no family), it is never silently put in a cluster
    assert genre_families("spoken word") == set() and genre_families(None) == set()
    assert "electronic" not in genre_families("melodic techno")   # the broad family never leaks out


def test_owner_cases_are_jumps():
    assert family_jump("trance", "downtempo")                      # The Sound of Goodbye -> Bonobo
    assert family_jump("downtempo", "trance")
    assert family_jump("hip hop", "electronic")                    # Pretty Girls Walk -> Four Tet bootleg
    assert scene_relation("trance", "downtempo") == "cross"


def test_liked_pairs_stay_in_cluster_or_neighbours():
    # Catchaman (electronic) -> No Control (bass house) -> Neverland (melodic techno / progressive house)
    # -> Nocturnal (melodic techno): owner_liked_pairs.json, golden
    for a, b in (("electronic", "bass house"), ("bass house", "melodic techno"),
                 ("bass house", "progressive house"), ("melodic techno", "melodic techno"),
                 ("progressive house", "melodic techno")):
        assert not family_jump(a, b), (a, b)


def test_neighbour_moves_allowed_and_non_neighbours_refused():
    for a, b in (("melodic house", "house"), ("melodic techno", "techno"), ("trance", "melodic techno"),
                 ("tech house", "techno"), ("house", "edm"), ("dubstep", "drum and bass"), ("edm", "dubstep"),
                 ("breaks", "uk garage"), ("electronic", "downtempo"), ("electronic", "trance")):
        assert not family_jump(a, b) and not family_jump(b, a), (a, b)
    for a, b in (("melodic techno", "ambient"), ("house", "downtempo"), ("techno", "dubstep"),
                 ("trance", "drum and bass"), ("electronica", "melodic house")):
        assert family_jump(a, b) and family_jump(b, a), (a, b)
    assert set(ELECTRONIC_CLUSTERS) == {"house", "melodic", "techno", "bass", "dnb", "chill", "edm", "breaks"}


def test_scene_keys_share_a_token_exactly_for_neighbours():
    share = lambda a, b: bool(set(scene_keys(a)) & set(scene_keys(b)))
    assert share("melodic techno", "house") and share("electronic", "ambient") and share("trance", "melodic house")
    assert not share("trance", "downtempo") and not share("techno", "dubstep")
    assert scene_keys("hip hop") == ["hiphop"] and scene_keys("r&b") == ["rnb"]   # hip-hop anchor unchanged
    assert not share("hip hop", "r&b") and scene_keys(None) == []
    # pinned for app/tests/js/electronic_scenes_check.js
    assert scene_keys("trance") == ["electronic|melodic", "house|melodic", "melodic", "melodic|techno"]
    assert scene_keys("downtempo") == ["chill", "chill|electronic"]
    assert scene_keys("house") == ["breaks|house", "edm|house", "electronic|house", "house", "house|melodic",
                                   "house|techno"]


def test_punjabi_and_hiphop_unchanged():
    assert not family_jump("punjabi", "bhangra") and family_jump("punjabi", "house")
    assert not family_jump("hip hop", "r&b") and family_jump("hip hop", "deep house")


def test_vet_refuses_trance_to_downtempo():
    from app.ui.services import booking_vet as bv
    r = bv.vet_one("Above & Beyond - The Sound of Goodbye", "Bonobo - Me And You", a_genre="trance",
                   b_genre="downtempo", stored=True)
    assert r and r["gate"] == "scene"
    assert bv.vet_one("A - x", "B - y", a_genre="trance", b_genre="melodic techno", stored=True) is None
    # scene anchor: a fallback left melodic techno for downtempo; the next song goes back
    r = bv.vet_one("Bonobo - Me And You", "Four Tet - Parallel 4", a_genre="downtempo", b_genre="electronica",
                   anchor_genre="melodic techno")
    assert r and r["gate"] == "scene_anchor"
    res = bv.vet("A - x", [{"name": "Bonobo - Me And You", "genre": "downtempo"}], a_genre="trance")
    assert res[0]["scene_clash"] and res[0]["scene_rel"] == "cross"


def test_mashup_gate_refuses_trance_to_downtempo():
    from app.music_brain.render import mashup
    assert mashup.scene_gate("trance", "downtempo")
    assert mashup.scene_gate("melodic techno", "house") is None


def test_suggestion_filter_drops_trance_to_downtempo(monkeypatch):
    import app.ui.services.autopilot_service as svc
    reply = {"current_genre": "trance", "suggestions": [
        {"artist": "Bonobo", "title": "Me And You", "genre": "downtempo", "genre_hop": 0},
        {"artist": "Argy", "title": "Dreamstates", "genre": "trance", "genre_hop": 0}]}
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: json.dumps(reply))
    out = svc.suggest_next_tracks("The Sound of Goodbye", "Above & Beyond", 136.0, "8A", 400.0, 0.5, "", [])
    assert [s["title"] for s in out] == ["Dreamstates"]


def test_fallback_ranking_puts_downtempo_after_the_scene(monkeypatch):
    from app.ui import server as srv
    from app.ui.services import atlas_api
    lab = {"a": "trance", "x": "downtempo", "y": "melodic house"}
    monkeypatch.setattr(srv, "_track_vibe", lambda tid: {"genre": lab.get(tid), "era": None})
    monkeypatch.setattr(srv, "_vibe_by_name", lambda n: {"genre": None, "era": None})
    rows = [{"b": "x"}, {"b": "y"}]
    atlas_api._mark_scene("a", "trance", rows)
    assert [r["scene_rel"] for r in rows] == ["cross", "family"]
