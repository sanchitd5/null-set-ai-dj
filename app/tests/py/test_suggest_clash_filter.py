from app.ui.services.autopilot_service import _filter_suggestions


def _s(title, energy, artist="A"):
    return {"artist": artist, "title": title, "track_profile": {"energy": energy, "tempo_feel": "mid", "mood": "chill"}}


def test_measured_energy_beats_model_guess():
    # model believes the playing song is a 3, the library measured 9
    data = {"current_profile": {"energy": 3, "tempo_feel": "mid", "mood": "chill"},
            "suggestions": [_s("Quiet One", 3), _s("Loud One", 9)]}
    got = _filter_suggestions(data, [], measured_energy=9)
    assert [x["title"] for x in got] == ["Loud One"]


def test_measured_energy_of_one_is_not_rescaled():
    data = {"suggestions": [_s("Peak", 9)]}
    assert _filter_suggestions(data, [], measured_energy=1) == []


def test_all_clashing_returns_empty_not_a_clash():
    data = {"current_profile": {"energy": 9}, "suggestions": [_s("Quiet", 3), _s("Quieter", 2)]}
    assert _filter_suggestions(data, [], measured_energy=9) == []


def test_key_clash_not_kept_as_last_resort():
    d = {"suggestions": [{"artist": "A", "title": "T", "expected_key": "1A", "track_profile": {}}]}
    assert _filter_suggestions(d, [], current_key="7A") == []
