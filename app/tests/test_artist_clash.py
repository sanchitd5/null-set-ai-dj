"""Artist scene clash: same broad genre tag, different scene, never suggested back to back."""
from app.ui.autopilot_service import _artist_clash, _filter_suggestions


def test_sidhu_moose_wala_and_karan_aujla_clash_either_direction():
    assert _artist_clash("Sidhu Moose Wala", "Karan Aujla")
    assert _artist_clash("Karan Aujla", "Sidhu Moose Wala")
    assert _artist_clash("sidhu moose wala", "karan aujla")  # case-insensitive


def test_unrelated_artists_do_not_clash():
    assert _artist_clash("Sidhu Moose Wala", "Diljit Dosanjh") is None
    assert _artist_clash("", "Karan Aujla") is None
    assert _artist_clash("Sidhu Moose Wala", "") is None


def test_filter_suggestions_drops_the_clashing_artist():
    data = {"steering": "stay", "suggestions": [
        {"artist": "Karan Aujla", "title": "Softly", "occasion_fit": 8},
    ]}
    kept = _filter_suggestions(data, [], current_artist="Sidhu Moose Wala")
    assert kept and kept[0].get("rejected_reason", "").startswith("scene clash")


def test_filter_suggestions_keeps_it_for_other_artists():
    data = {"steering": "stay", "suggestions": [
        {"artist": "Karan Aujla", "title": "Softly", "occasion_fit": 8},
    ]}
    kept = _filter_suggestions(data, [], current_artist="Diljit Dosanjh")
    assert kept and "rejected_reason" not in kept[0]


def test_artist_clash_applies_even_while_steering():
    data = {"steering": "move", "suggestions": [
        {"artist": "Karan Aujla", "title": "Softly", "occasion_fit": 8},
    ]}
    kept = _filter_suggestions(data, [], occasion_set=True, current_artist="Sidhu Moose Wala")
    assert kept and kept[0].get("rejected_reason", "").startswith("scene clash")
