"""PICKFIX_B: forced steering on a low occasion fit / flat low energy, unknown genre is a big hop, artist re-ask names the artists."""
from app.music_brain.generated import dj_config as DJ
from app.ui.services import autopilot_service as ap
from app.ui.services.autopilot_service import (
    _filter_suggestions, artists_to_exclude, energy_rut, energy_rut_line, forced_steer_reason,
)


# ---- 1) steering back toward the occasion
def test_low_occasion_fit_forces_steering():
    assert forced_steer_reason(True, 0, [5, 6, 7])
    assert forced_steer_reason(True, DJ.OCCASION_FIT_LOW, [])
    assert forced_steer_reason(True, DJ.OCCASION_FIT_LOW + 1, [6, 6, 6]) == ""


def test_flat_low_energy_forces_steering():
    assert energy_rut([2, 2, 3])
    assert forced_steer_reason(True, 8, [7, 2, 2, 3])          # only the last 3 count
    assert not energy_rut([2, 2])                                # fewer than 3 songs
    assert not energy_rut([2, 4, 6])                             # a climb is no rut
    assert not energy_rut([7, 7, 7])                             # flat but not low
    assert not energy_rut([2, "x", 3])


def test_no_forcing_without_occasion_or_rating():
    assert forced_steer_reason(False, 0, [2, 2, 2]) == ""
    assert forced_steer_reason(True, None, [5, 5, 5]) == ""


def test_rut_line_names_the_arc_target_and_no_songs():
    line = energy_rut_line(0.5)
    assert f"level {DJ.ARC_TARGET['peak']} or higher" in line


def test_forced_move_lets_a_low_fit_bridge_through_the_filter():
    """The model said stay (fit 0); the forced move waives the continuity clash against the playing song."""
    pick = {"artist": "Artist Two", "title": "Song Two", "occasion_fit": 3, "genre": "bhangra", "genre_hop": 2,
            "track_profile": {"energy": 8, "tempo_feel": "driving", "mood": "euphoric"}}
    cur = {"energy": 2, "tempo_feel": "slow", "mood": "bittersweet"}
    stay = {"steering": "stay", "occasion_fit": 0, "current_profile": cur, "current_genre": "melodic house",
            "suggestions": [dict(pick)]}
    assert _filter_suggestions(stay, [], occasion_set=True, current_artist="Artist One") == []
    forced = dict(stay, suggestions=[dict(pick)], steering="move")
    assert _filter_suggestions(forced, [], occasion_set=True, current_artist="Artist One")


# ---- 2) unknown genre
def _data(**pick):
    p = {"artist": "Artist Two", "title": "Song Two", "genre_hop": 0, "occasion_fit": 8}
    p.update(pick)
    return {"steering": "stay", "current_genre": "melodic house", "suggestions": [p]}


def test_unknown_genre_is_a_big_hop_not_the_models_zero():
    d = _data(genre="melodic house")
    d["suggestions"].append({"artist": "Artist Three", "title": "Song Three", "genre_hop": 0, "occasion_fit": 8})
    kept = _filter_suggestions(d, [], current_artist="Artist One", genre_of=lambda r: "melodic house" if r["artist"] == "Artist Three" else "")
    assert kept[0]["artist"] == "Artist Three"       # the known genre ranks first
    assert kept[1]["genre_unknown"] and kept[1]["genre_hop"] == DJ.UNKNOWN_GENRE_HOP and kept[1]["genre"] == ""


def test_genre_filled_from_artist_labels():
    seen = []

    def gof(row):
        seen.append(row["artist"])
        return "melodic house"
    kept = _filter_suggestions(_data(), [], current_artist="Artist One", genre_of=gof)
    assert seen == ["Artist Two"]
    assert kept and "rejected_reason" not in kept[0] and kept[0]["genre"] == "melodic house"


def test_genre_of_failure_means_unknown():
    def boom(row):
        raise RuntimeError("x")
    kept = _filter_suggestions(_data(), [], current_artist="Artist One", genre_of=boom)
    assert kept[0]["genre"] == "" and kept[0]["genre_hop"] == DJ.UNKNOWN_GENRE_HOP


def test_unknown_hop_never_applies_without_a_playing_genre():
    d = _data()
    d["current_genre"] = ""
    kept = _filter_suggestions(d, [], current_artist="Artist One")
    assert kept and "rejected_reason" not in kept[0]


# ---- 3) artist re-ask
def test_artists_to_exclude_is_distinct_names():
    picks = [{"artist": "Artist One", "title": "A"}, {"artist": "artist one", "title": "B"},
             {"artist": "Artist Two", "title": "C"}, {"artist": "", "title": "D"}]
    assert artists_to_exclude(picks) == "Artist One; Artist Two"


def test_artist_spacing_still_empties_on_recent_artists():
    picks = [{"artist": "Artist One", "title": "A"}, {"artist": "Artist Two", "title": "B"}]
    assert ap.artist_spacing(picks, ["Artist One - X", "Artist Two - Y"]) == []


# ---- end to end through suggest_next_tracks (chat_raw stubbed)
def _pk(artist, title, fit=8):
    return {"artist": artist, "title": title, "genre": "dream pop", "genre_hop": 0, "expected_bpm": 95, "occasion_fit": fit}


def test_all_picks_repeat_recent_artists_one_reask_names_the_artists(monkeypatch):
    import json
    first = {"current_genre": "dream pop", "suggestions": [_pk("Artist One", "Song A"), _pk("Artist Two", "Song B")]}
    second = {"current_genre": "dream pop", "suggestions": [_pk("Artist Three", "Song C")]}
    replies, prompts = iter([json.dumps(first), json.dumps(second)]), []
    monkeypatch.setattr(ap, "chat_raw", lambda s, u, **k: (prompts.append(u), next(replies))[1])
    out = ap.suggest_next_tracks("Cur", "Cur Artist", 96.0, "8A", 290.0, 0.3, "", ["Artist One - X", "Artist Two - Y"])
    assert [s["title"] for s in out] == ["Song C"]
    assert len(prompts) == 2 and "Do not suggest any song by these artists: Artist One; Artist Two" in prompts[1]


def test_second_ask_still_repeating_falls_through_to_no_picks(monkeypatch):
    import json
    bad = {"current_genre": "dream pop", "suggestions": [_pk("Artist One", "Song A")]}
    replies, prompts = iter([json.dumps(bad), json.dumps(bad), json.dumps(bad)]), []
    monkeypatch.setattr(ap, "chat_raw", lambda s, u, **k: (prompts.append(u), next(replies))[1])
    out = ap.suggest_next_tracks("Cur", "Cur Artist", 96.0, "8A", 290.0, 0.3, "", ["Artist One - X", "Artist Two - Y"])
    assert out == [] and sum("REJECTED - too many songs" in p for p in prompts) == 1   # one re-ask, then the caller's fallback


def test_stay_reply_at_fit_zero_is_forced_to_move_and_flagged(monkeypatch):
    import json
    reply = {"steering": "stay", "occasion_fit": 0, "current_genre": "dream pop",
             "suggestions": [_pk("Artist Two", "Song B", fit=7)]}
    monkeypatch.setattr(ap, "chat_raw", lambda s, u, **k: json.dumps(reply))
    meta = {}
    out = ap.suggest_next_tracks("Cur", "Cur Artist", 96.0, "8A", 290.0, 0.3, "wedding", [], meta=meta)
    assert out and meta["steering"] == "move"


def test_flat_low_energy_adds_the_rut_line_and_forces_move(monkeypatch):
    import json
    reply = {"steering": "stay", "occasion_fit": 8, "current_genre": "dream pop", "suggestions": [_pk("Artist Two", "Song B")]}
    prompts = []
    monkeypatch.setattr(ap, "chat_raw", lambda s, u, **k: (prompts.append(u), json.dumps(reply))[1])
    meta = {}
    ap.suggest_next_tracks("Cur", "Cur Artist", 96.0, "8A", 290.0, 0.3, "wedding", [], energy_history=[2, 2, 3], meta=meta)
    assert "ENERGY RUT" in prompts[0] and meta["steering"] == "move"
    meta2 = {}
    prompts.clear()
    ap.suggest_next_tracks("Cur", "Cur Artist", 96.0, "8A", 290.0, 0.3, "wedding", [], energy_history=[6, 6, 7], meta=meta2)
    assert "ENERGY RUT" not in prompts[0] and meta2["steering"] == "stay"
