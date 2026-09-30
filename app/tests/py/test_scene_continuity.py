"""Scene continuity: owner, Anyma "Atoma" went to a pop MashMIX."""
from app.ui.services.autopilot_service import _filter_suggestions, mashup_title, scene_of


def _sugg(artist, title, genre="melodic techno", hop=0):
    return {"artist": artist, "title": title, "genre": genre, "genre_hop": hop, "occasion_fit": 8}


def test_mashup_title():
    assert mashup_title("EVIL x YOU (bjork X Melanie Martinez MashMIX)")
    assert mashup_title("Some Bootleg")
    assert not mashup_title("Atoma")
    assert not mashup_title("Xylo")


def test_scene_of():
    assert scene_of("melodic techno")
    assert scene_of("pop") == ""
    assert scene_of(None) == ""


def test_mashup_loses_to_a_scene_pick():
    data = {"current_genre": "melodic techno", "suggestions": [
        _sugg("Bjork", "EVIL x YOU (bjork X Melanie Martinez MashMIX)"),
        _sugg("Argy", "Aria")]}
    kept = _filter_suggestions(data, ["Anyma - Atoma"])
    assert [s["title"] for s in kept] == ["Aria"]


def test_mashup_is_a_bounded_last_resort():
    data = {"current_genre": "melodic techno", "suggestions": [
        _sugg("Bjork", "EVIL x YOU (bjork X Melanie Martinez MashMIX)")]}
    kept = _filter_suggestions(data, ["Anyma - Atoma"])
    assert len(kept) == 1


def test_open_format_scene_keeps_mashups():
    data = {"current_genre": "pop", "suggestions": [_sugg("A", "Song A x Song B", genre="pop")]}
    assert len(_filter_suggestions(data, [])) == 1
