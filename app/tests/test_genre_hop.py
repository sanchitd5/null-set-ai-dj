"""Genre continuity: suggestions that jump genre are dropped unless the set is moving."""
from app.ui.autopilot_service import _filter_suggestions


def _s(title, hop):
    return {"artist": "X", "title": title, "genre_hop": hop, "occasion_fit": 8}


def test_genre_jump_dropped_when_staying():
    data = {"steering": "stay", "suggestions": [_s("Near", 1), _s("Far", 3)]}
    assert [s["title"] for s in _filter_suggestions(data, [])] == ["Near"]


def test_genre_jump_kept_when_moving_or_allowed():
    data = {"steering": "move", "suggestions": [_s("Far", 2)]}
    assert [s["title"] for s in _filter_suggestions(data, [], occasion_set=True)] == ["Far"]
    data = {"steering": "stay", "suggestions": [_s("Far", 2)]}
    assert [s["title"] for s in _filter_suggestions(data, [], allow_genre_change=True)] == ["Far"]


def test_only_jumps_left_returns_smallest():
    data = {"steering": "stay", "suggestions": [_s("Far", 3), _s("Mid", 2)]}
    assert [s["title"] for s in _filter_suggestions(data, [])] == ["Mid"]


def test_missing_hop_passes():
    data = {"steering": "stay", "suggestions": [{"artist": "X", "title": "T", "occasion_fit": 8}]}
    assert len(_filter_suggestions(data, [])) == 1


def test_move_without_occasion_does_not_license_jump():
    data = {"steering": "move", "suggestions": [_s("Near", 0), _s("Far", 3)]}
    assert [s["title"] for s in _filter_suggestions(data, [])] == ["Near"]


def test_family_jump_overrides_low_self_rating():
    # the real case: Afusic "Pal Pal" -> Fred again.. "Delilah", model said hop 1
    delilah = dict(_s("Delilah", 1), genre="UK garage / electronic")
    near = dict(_s("Near", 1), genre="Urdu pop")
    data = {"steering": "stay", "current_genre": "Pakistani pop / Urdu pop", "suggestions": [delilah, near]}
    assert [s["title"] for s in _filter_suggestions(data, [])] == ["Near"]
