"""Measured energy 1-10 and the next-song energy step rule."""
from app.music_brain import energy


def test_raw_score_orders_ambient_house_peak():
    ambient = energy.raw_score(onset_rate=1.5, low_share=0.04, bpm=85, brightness_hz=900)
    house = energy.raw_score(onset_rate=5.5, low_share=0.2, bpm=124, brightness_hz=2000)
    peak = energy.raw_score(onset_rate=8.5, low_share=0.3, bpm=174, brightness_hz=3200)
    assert ambient < house < peak
    assert energy.level_from_raw(ambient) <= 2 and 4 <= energy.level_from_raw(house) <= 7 and energy.level_from_raw(peak) >= 9
    assert energy.raw_score(5.5, 0.2, 62, 2000) == energy.raw_score(5.5, 0.2, 124, 2000)


def test_library_percentile_once_there_are_enough_tracks():
    lib = [i / 100 for i in range(100)]
    assert energy.level_from_raw(0.5, lib) == 6 and energy.level_from_raw(0.99, lib) == 10
    assert energy.level_from_raw(0.5, lib[:5]) == energy.level_from_raw(0.5)   # too few: fixed scale


def test_next_song_step_rule():
    assert energy.next_ok(6, 8)["ok"] and not energy.next_ok(3, 8)["ok"]
    assert "jump 3 -> 8" in energy.next_ok(3, 8)["why"]
    assert not energy.next_ok(6, 8, relaxed=True)["ok"] and energy.next_ok(6, 7, relaxed=True)["ok"]
    assert not energy.next_ok(7, 5, arc="build")["ok"] and energy.next_ok(7, 6, arc="build")["ok"]
    assert not energy.next_ok(5, 7, arc="cool")["ok"]


def test_prompt_states_the_measured_energy_and_the_range():
    from app.ui import autopilot_service as svc
    line = svc.energy_line(0.6, 6)
    assert "MEASURED ENERGY: 6/10" in line and "MUST be 4-8" in line
    assert "MUST be 5-7" in svc.energy_line(0.6, 6, relaxed=True)
    assert "Avg Energy: 0.60" in svc.energy_line(0.6, None)


def test_suggest_prompt_carries_it(monkeypatch):
    from app.ui import autopilot_service as svc
    seen = []
    monkeypatch.setattr(svc, "chat_raw", lambda s, u, **k: seen.append(u) or '{"current_genre": "house", "suggestions": []}')
    svc.suggest_next_tracks("T", "A", 124.0, "8A", 200.0, 0.7, "", [], measured_energy=3)
    assert "MEASURED ENERGY: 3/10" in seen[0] and "MUST be 1-5" in seen[0]
