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


def test_all_jumps_triggers_one_retry(monkeypatch):
    import json
    import app.ui.autopilot_service as svc
    jump = {"current_genre": "Urdu pop", "steering": "move", "suggestions": [
        {"artist": "Fred again..", "title": "Jungle", "genre": "UK garage", "genre_hop": 1}]}
    step = {"current_genre": "Urdu pop", "suggestions": [
        {"artist": "AP Dhillon", "title": "With You", "genre": "Punjabi pop", "genre_hop": 1}]}
    replies, prompts = iter([json.dumps(jump), json.dumps(step)]), []
    monkeypatch.setattr(svc, "chat_raw", lambda s, u, **k: (prompts.append(u), next(replies))[1])
    out = svc.suggest_next_tracks("Pal Pal", "Afusic", 100.0, "8A", 200.0, 0.5, "", [])
    assert [s["title"] for s in out] == ["With You"]
    assert "REJECTED" in prompts[1] and "Jungle" in prompts[1]


def test_parroted_few_shot_is_retried(monkeypatch):
    import json
    import app.ui.autopilot_service as svc
    copy = {"current_genre": "melodic house", "suggestions": [
        {"artist": "Fred again..", "title": "Marea (We've Lost Dancing)", "genre": "melodic house", "genre_hop": 0},
        {"artist": "Fred again..", "title": "Delilah (pull me out of this)", "genre": "melodic house", "genre_hop": 0}]}
    real = {"current_genre": "dream pop", "suggestions": [
        {"artist": "Beach House", "title": "Space Song", "genre": "dream pop", "genre_hop": 0}]}
    replies = iter([json.dumps(copy), json.dumps(real)])
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: next(replies))
    out = svc.suggest_next_tracks("Apocalypse", "Cigarettes After Sex", 94.0, "8A", 290.0, 0.3, "", [])
    assert [s["title"] for s in out] == ["Space Song"]


def test_indie_dance_is_a_jump_from_indie_pop():
    from app.ui.autopilot_service import _family_jump
    assert _family_jump("indie pop", "indie dance")
