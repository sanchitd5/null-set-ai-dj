"""Genre continuity: suggestions that jump genre are dropped unless the set is moving."""
from app.ui.services.autopilot_service import _filter_suggestions


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
    import app.ui.services.autopilot_service as svc
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
    import app.ui.services.autopilot_service as svc
    copy = {"current_genre": "melodic house", "suggestions": [
        {"artist": "Artist One", "title": "Song One", "genre": "melodic house", "genre_hop": 0}]}
    real = {"current_genre": "dream pop", "suggestions": [
        {"artist": "Beach House", "title": "Space Song", "genre": "dream pop", "genre_hop": 0}]}
    replies = iter([json.dumps(copy), json.dumps(real)])
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: next(replies))
    out = svc.suggest_next_tracks("Apocalypse", "Cigarettes After Sex", 94.0, "8A", 290.0, 0.3, "", [])
    assert [s["title"] for s in out] == ["Space Song"]


def test_indie_dance_is_a_jump_from_indie_pop():
    from app.ui.services.autopilot_service import _family_jump
    assert _family_jump("indie pop", "indie dance")


def _pick(title, bpm):
    return {"artist": "A", "title": title, "genre": "dream pop", "genre_hop": 0, "expected_bpm": bpm}


def test_off_tempo_picks_are_dropped(monkeypatch):
    import json
    import app.ui.services.autopilot_service as svc
    reply = {"current_genre": "dream pop", "suggestions": [_pick("Fast", 120), _pick("Slow", 94)]}
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: json.dumps(reply))
    out = svc.suggest_next_tracks("Apocalypse", "Cigarettes After Sex", 96.0, "8A", 290.0, 0.3, "", [])
    assert [s["title"] for s in out] == ["Slow"]


def test_all_off_tempo_retries_once(monkeypatch):
    import json
    import app.ui.services.autopilot_service as svc
    far = {"current_genre": "dream pop", "suggestions": [_pick("Cirrus", 117)]}
    near = {"current_genre": "dream pop", "suggestions": [_pick("Space Song", 95)]}
    replies, prompts = iter([json.dumps(far), json.dumps(near)]), []
    monkeypatch.setattr(svc, "chat_raw", lambda s, u, **k: (prompts.append(u), next(replies))[1])
    out = svc.suggest_next_tracks("Apocalypse", "Cigarettes After Sex", 96.0, "8A", 290.0, 0.3, "", [])
    assert [s["title"] for s in out] == ["Space Song"]
    assert "wrong tempo" in prompts[1] and "Cirrus" in prompts[1]


def test_double_time_counts_as_locked():
    from app.ui.services.autopilot_service import _tempo_locks
    assert _tempo_locks(96, 190) and _tempo_locks(174, 87) and _tempo_locks(96, None) is None
    assert _tempo_locks(96, 117) is False


def test_invented_songs_dropped_and_retried(monkeypatch):
    import json
    import app.ui.services.autopilot_service as svc
    fake = {"current_genre": "dream pop", "suggestions": [_pick("Morrison", 95)]}
    real = {"current_genre": "dream pop", "suggestions": [_pick("Space Song", 95), _pick("Invented", 95)]}
    replies, prompts = iter([json.dumps(fake), json.dumps(real)]), []
    monkeypatch.setattr(svc, "chat_raw", lambda s, u, **k: (prompts.append(u), next(replies))[1])
    monkeypatch.setattr(svc, "_verify_song", lambda a, t: t == "Space Song")
    out = svc.suggest_next_tracks("Apocalypse", "Cigarettes After Sex", 96.0, "8A", 290.0, 0.3, "", [])
    assert [s["title"] for s in out] == ["Space Song"]
    assert "do not exist" in prompts[1] and "Morrison" in prompts[1]


def test_unknown_lookup_keeps_pick(monkeypatch):
    import json
    import app.ui.services.autopilot_service as svc
    reply = {"current_genre": "dream pop", "suggestions": [_pick("Space Song", 95)]}
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: json.dumps(reply))
    monkeypatch.setattr(svc, "_verify_song", lambda a, t: (_ for _ in ()).throw(OSError("offline")))
    out = svc.suggest_next_tracks("Apocalypse", "Cigarettes After Sex", 96.0, "8A", 290.0, 0.3, "", [])
    assert [s["title"] for s in out] == ["Space Song"]


def test_current_song_never_its_own_next(monkeypatch):
    import json
    import app.ui.services.autopilot_service as svc
    reply = {"current_genre": "dream pop", "suggestions": [_pick("Apocalypse", 96), _pick("Space Song", 95)]}
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: json.dumps(reply))
    out = svc.suggest_next_tracks("Apocalypse", "Cigarettes After Sex", 96.0, "8A", 290.0, 0.3, "", [])
    assert [s["title"] for s in out] == ["Space Song"]


def test_no_corrective_retry_past_budget(monkeypatch):
    import json
    import app.ui.services.autopilot_service as svc
    far = {"current_genre": "dream pop", "suggestions": [_pick("Cirrus", 117)]}
    calls = []
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: (calls.append(1), json.dumps(far))[1])
    monkeypatch.setattr(svc, "SUGGEST_BUDGET_S", 0.0)
    out = svc.suggest_next_tracks("Apocalypse", "Cigarettes After Sex", 96.0, "8A", 290.0, 0.3, "", [])
    assert len(calls) == 1 and [s["title"] for s in out] == ["Cirrus"]  # client ladders toward it


def test_dated_concert_upload_is_not_the_song():
    from app.ui.services.download_service import _DATED_RE
    assert _DATED_RE.search('Skrillex & Four Tet - "Butterflies" - April 29, 2023 - Morrison, Colorado')
    assert _DATED_RE.search("Fred again.. live 23 July 2021")
    assert not _DATED_RE.search("Fred again.. - Delilah (pull me out of this)")


def test_qwen3_think_block_is_stripped():
    from app.ui.services.autopilot_service import _extract_json
    raw = '<think>\nmaybe {"a": 1}?\n</think>\n\n{"suggestions": []}'
    assert _extract_json(raw) == {"suggestions": []}


def test_sequel_is_not_the_song():
    from app.ui.services.download_service import _sequel
    assert _sequel("Victory Lap Five", "Victory Lap")
    assert _sequel("Fred again.. - Victory Lap Two (with Skepta)", "Victory Lap")
    assert _sequel("Song Pt. 2", "Song")
    assert not _sequel("Fred again.. - Victory Lap (feat. Skepta & PlaqueBoyMax)", "Victory Lap")
    assert not _sequel("Victory Lap Five", "Victory Lap Five")
    assert not _sequel("22 (Over U)", "22")


def test_genre_near_scene_level():
    from app.music_brain.genre import genre_near
    # user: Aqua "Barbie Girl" (eurodance) -> Bicep "Glue" (breakbeat/electronica)
    assert genre_near("eurodance", "breakbeat") is False
    assert genre_near("eurodance", "electronica") is False
    assert genre_near("melodic house", "deep house") is True
    assert genre_near("melodic house", "melodic techno") is True
    assert genre_near("eurodance", "dance-pop") is True
    assert genre_near("synth-pop", "pop") is True
    assert genre_near("drum and bass", "future bass") is False   # longer term wins over "bass"
    assert genre_near("eurodance", "") is None and genre_near("", "house") is None


def test_era_parsing_and_gap():
    from app.music_brain.genre import decade_of, era_gap
    assert decade_of("1990s") == 1990 and decade_of("90s") == 1990 and decade_of("late 90's") == 1990
    assert decade_of("1997") == 1990 and decade_of("2010s") == 2010 and decade_of("00s") == 2000
    assert decade_of("") is None and decade_of("modern") is None
    assert era_gap("1990s", "2010s") == 2        # Barbie Girl -> Glue: a jump
    assert era_gap("1990s", "2000s") == 1        # neighbour: fine
    assert era_gap("1990s", "") is None


def test_era_jump_dropped_unless_moving():
    data = {"current_genre": "eurodance", "current_era": "1990s", "suggestions": [
        {"artist": "Bicep", "title": "Glue", "genre": "dance", "era": "2010s", "genre_hop": 0, "occasion_fit": 8},
        {"artist": "Vengaboys", "title": "Boom, Boom, Boom, Boom!!", "genre": "eurodance", "era": "1990s",
         "genre_hop": 0, "occasion_fit": 8}]}
    assert [s["title"] for s in _filter_suggestions(data, [])] == ["Boom, Boom, Boom, Boom!!"]
    only_jump = {**data, "suggestions": data["suggestions"][:1]}
    kept = _filter_suggestions(only_jump, [])
    assert [s["title"] for s in kept] == ["Glue"] and kept[0]["rejected_reason"].startswith("era jump")
    assert [s["title"] for s in _filter_suggestions(data, [], allow_genre_change=True)] == ["Glue", "Boom, Boom, Boom, Boom!!"]


def test_relaxed_session_drops_upbeat_picks(monkeypatch):
    import app.ui.services.autopilot_service as ap
    fake = ('{"current_profile":{"energy":4,"tempo_feel":"laid-back","mood":"chill"},"suggestions":['
            '{"artist":"A","title":"Banger","energy_delta":"up","track_profile":{"energy":8,"tempo_feel":"driving","mood":"euphoric"}},'
            '{"artist":"B","title":"Calm One","energy_delta":"maintain","track_profile":{"energy":4,"tempo_feel":"laid-back","mood":"chill"}}]}')
    seen = {}
    def chat(system, user, **kw):
        seen["user"] = user
        return fake
    monkeypatch.setattr(ap, "chat_raw", chat)
    out = ap.suggest_next_tracks("Song", "Artist", 110, "8A", 240, 0.5, "chill sunday", [], relaxed=True)
    assert [s["title"] for s in out] == ["Calm One"]
    assert "RELAXED SESSION" in seen["user"] and ap.RELAXED_ARC in seen["user"]
    out = ap.suggest_next_tracks("Song", "Artist", 110, "8A", 240, 0.5, "", [])
    assert "RELAXED SESSION" not in seen["user"]


def test_relaxed_only_never_empty():
    from app.ui.services.autopilot_service import _relaxed_only
    picks = [{"title": "Loud", "energy_delta": "up", "track_profile": {"energy": 9}},
             {"title": "Less Loud", "energy_delta": "up", "track_profile": {"energy": 6}}]
    kept = _relaxed_only(picks, {"energy": 3})
    assert [s["title"] for s in kept] == ["Less Loud"] and kept[0]["rejected_reason"].startswith("relaxed")
