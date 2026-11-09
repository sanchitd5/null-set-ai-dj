"""The picker stays inside the set: a hop to the playing song's side artist needs the same scene, a pick in a
writing system far from the set is dropped, and every kept pick says why (synthetic names)."""
import json

import app.ui.services.autopilot_service as svc
from app.ui.services.autopilot_service import _filter_suggestions


def _s(artist, title, hop=0, genre="indie pop"):
    return {"artist": artist, "title": title, "genre_hop": hop, "genre": genre, "occasion_fit": 8}


def _titles(out):
    return [s["title"] for s in out]


def test_side_artist_hop_inside_the_scene_is_kept_and_flagged():
    data = {"steering": "stay", "current_genre": "indie pop", "suggestions": [_s("Side Kay", "Orbit Line", hop=0)]}
    out = _filter_suggestions(data, [], current_artist="Main Act", current_title="Glass Harbor (feat. Side Kay)")
    assert _titles(out) == ["Orbit Line"] and out[0]["side_artist_hop"] is True


def test_side_artist_hop_that_leaves_the_scene_is_dropped_for_a_same_scene_pick():
    hop = _s("Side Kay", "Orbit Line", hop=1, genre="indie pop")
    home = _s("Main Act", "Paper Boats", hop=0)
    data = {"steering": "stay", "current_genre": "indie pop", "suggestions": [hop, home]}
    out = _filter_suggestions(data, [], current_artist="Main Act", current_title="Glass Harbor (feat. Side Kay)")
    assert _titles(out) == ["Paper Boats"]
    assert hop["rejected_reason"] == "side-artist hop leaves the scene"


def test_the_same_neighbour_hop_by_a_stranger_is_not_a_side_hop():
    data = {"steering": "stay", "current_genre": "indie pop", "suggestions": [_s("Dax Rho", "Orbit Line", hop=1)]}
    out = _filter_suggestions(data, [], current_artist="Main Act", current_title="Glass Harbor (feat. Side Kay)")
    assert _titles(out) == ["Orbit Line"] and out[0]["side_artist_hop"] is False


def test_credit_in_the_artist_field_counts_as_a_side_artist():
    data = {"steering": "stay", "current_genre": "indie pop", "suggestions": [_s("Side Kay", "Orbit Line", hop=1), _s("Main Act", "Home", hop=0)]}
    out = _filter_suggestions(data, [], current_artist="Main Act & Side Kay", current_title="Glass Harbor")
    assert _titles(out) == ["Home"]


def test_a_deliberate_move_may_hop_to_a_side_artist():
    data = {"steering": "stay", "suggestions": [_s("Side Kay", "Orbit Line", hop=1)]}
    out = _filter_suggestions(data, [], allow_genre_change=True, current_artist="Main Act",
                              current_title="Glass Harbor (feat. Side Kay)")
    assert _titles(out) == ["Orbit Line"]


def test_pick_in_a_far_writing_system_is_dropped_but_a_native_one_stays():
    far = _s("ਓਰਿਨ ਪਾਈਕ", "ਗਲਾਸ ਹਾਰਬਰ", hop=0)
    near = _s("Dax Rho", "Paper Boats", hop=0)
    data = {"steering": "stay", "current_genre": "indie pop", "suggestions": [far, near]}
    out = _filter_suggestions(data, [], current_artist="Main Act", current_title="Glass Harbor")
    assert _titles(out) == ["Paper Boats"]
    assert far["rejected_reason"] == "writing system far from the set"


def test_a_set_already_in_that_writing_system_keeps_its_scene():
    far = _s("ਓਰਿਨ ਪਾਈਕ", "ਗਲਾਸ ਹਾਰਬਰ", hop=0)
    data = {"steering": "stay", "current_genre": "punjabi", "suggestions": [far]}
    out = _filter_suggestions(data, [], current_artist="ਮੇਨ ਐਕਟ", current_title="Glass Harbor")
    assert len(out) == 1


def test_every_kept_pick_gets_a_reason(monkeypatch):
    reply = {"current_genre": "indie pop", "current_era": "2010s", "steering": "stay", "suggestions": [
        {"artist": "Side Kay", "title": "Orbit Line", "genre": "indie pop", "era": "2010s", "genre_hop": 0}]}
    monkeypatch.setattr(svc, "chat_raw", lambda s, u, **k: json.dumps(reply))
    monkeypatch.setattr(svc, "_verify_song", lambda a, t: True)
    events = []
    import app.ui.services.session_log as sl
    monkeypatch.setattr(sl, "log", lambda kind, **f: events.append((kind, f)))
    out = svc.suggest_next_tracks("Glass Harbor (feat. Side Kay)", "Main Act", 100.0, "8A", 200.0, 0.5, "", [])
    assert [s["title"] for s in out] == ["Orbit Line"]
    assert "side artist of the playing song" in out[0]["pick_reason"]
    assert ("pick_reason", {"song": "Side Kay - Orbit Line", "reason": out[0]["pick_reason"], "side_hop": True}) in events
