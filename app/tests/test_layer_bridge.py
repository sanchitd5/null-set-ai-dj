"""LAYER planner (app/music_brain/layer.py) and BRIDGE PATH ladder (bridge.py)."""

import pytest

from app.music_brain.analyzer import KeyEstimate, StructureSection, TrackAnalysis
from app.music_brain.blend import plan_blend
from app.music_brain.bridge import bridge_ladder
from app.music_brain.layer import groove_steady, hold_options, plan_layer, vocal_clash


def _track(bpm, duration=300.0, key="8A", jitter=0.0):
    bar = 240.0 / bpm
    beat = bar / 4
    phrases = [i * 8 * bar for i in range(int(duration // (8 * bar)) + 1)]
    beats = [i * beat + (jitter * beat if i % 2 else 0.0) for i in range(int(duration / beat))]
    return TrackAnalysis(
        path="x", duration=duration, bpm=bpm, phrase_boundaries_8bar=phrases, beat_times=beats,
        key=KeyEstimate(camelot=key, key_name=key, is_major=False, confidence=1.0) if key else None,
        sections=[StructureSection("intro", 0, 32, 0.3), StructureSection("verse", 32, 260, 0.6),
                  StructureSection("outro", 260, duration, 0.3)])


def test_layer_plan_holds_then_swaps_on_phrase_lines():
    a, b = _track(120), _track(120, key="9A")       # bar = 2 s
    plan = plan_layer(a, b, 60.0, 140.0, a_vocals=[], b_vocals=[], max_hold_bars=32, unwind_bars=8)
    assert plan["ok"], plan
    assert plan["hold_bars"] == 32 and plan["total_bars"] == 40
    assert plan["swap_at"] == pytest.approx(plan["start"] + 64.0)
    assert plan["start"] % 16 == pytest.approx(0.0) and plan["b_swap"] % 16 == pytest.approx(0.0)
    assert plan["end_at"] <= a.duration


def test_layer_hold_follows_set_mode_cap():
    assert hold_options(16) == [16]
    assert hold_options(64)[0] == 64
    a, b = _track(120, duration=400.0), _track(120, duration=400.0)
    long_plan = plan_layer(a, b, 60.0, 140.0, a_vocals=[], b_vocals=[], max_hold_bars=64, unwind_bars=16)
    quick = plan_layer(a, b, 60.0, 140.0, a_vocals=[], b_vocals=[], max_hold_bars=16, unwind_bars=8)
    assert long_plan["ok"] and long_plan["hold_bars"] == 64
    assert quick["ok"] and quick["hold_bars"] == 16


@pytest.mark.parametrize("b_key, bpm, why", [
    ("3B", 120, "keys too far"),          # key score 0
    ("8A", 140, "not tempo-locked"),      # 16% apart
])
def test_layer_needs_key_and_tempo(b_key, bpm, why):
    plan = plan_layer(_track(120), _track(bpm, key=b_key), 60.0, 140.0, a_vocals=[], b_vocals=[])
    assert not plan["ok"] and why in plan["reasons"][0]


def test_layer_needs_vocal_maps_and_never_two_vocals():
    a, b = _track(120), _track(120)
    assert "vocal maps" in plan_layer(a, b, 60.0, 140.0)["reasons"][0]
    # both sing all the way through: no layer window
    plan = plan_layer(a, b, 60.0, 140.0, a_vocals=[(0.0, 300.0)], b_vocals=[(0.0, 300.0)])
    assert not plan["ok"] and "sing" in plan["reasons"][0]
    # A sings 60-100 s: the layer is placed so B's vocal never lands on it
    plan = plan_layer(a, b, 60.0, 140.0, a_vocals=[(60.0, 100.0)], b_vocals=[(20.0, 60.0)])
    assert plan["ok"] and plan["vocal_clash"] <= 0.03


def test_vocal_clash_maps_b_onto_a_clock():
    # B at 2x the track-seconds rate: B 10-20 s = A 105-110 s
    assert vocal_clash([(100.0, 110.0)], [(10.0, 20.0)], 100.0, 0.0, 20.0, 0.5) == pytest.approx(0.25)
    assert vocal_clash([(0.0, 50.0)], [(60.0, 70.0)], 0.0, 0.0, 50.0, 1.0) == 0.0


def test_layer_needs_a_steady_groove():
    assert groove_steady(_track(120), 0, 60) is True
    assert groove_steady(_track(120, jitter=0.4), 0, 60) is False
    assert groove_steady(TrackAnalysis(path="x", duration=10, bpm=120), 0, 10) is None
    plan = plan_layer(_track(120, jitter=0.4), _track(120), 60.0, 140.0, a_vocals=[], b_vocals=[])
    assert not plan["ok"] and "steady beat" in plan["reasons"][0]


def test_blend_unchanged_by_floor_refactor():
    a, b = _track(120), _track(120)
    plan = plan_blend(a, b, 80.0, 170.0, a_vocals=[], b_vocals=[], bars=16)
    assert plan["ok"] and plan["min_exit"] is None


# ── BRIDGE PATH ───────────────────────────────────────────────────────────────
def test_ladder_climbs_in_small_beat_matched_steps():
    lad = bridge_ladder(128, 174)
    assert lad["feasible"] and lad["link"] == "direct" and lad["direction"] == "up"
    assert lad["step_count"] == 6 and lad["steps"][-1] == pytest.approx(174, abs=0.1)
    prev = 128
    for s in lad["steps"]:
        assert s / prev - 1 <= 0.06 + 1e-3
        prev = s


def test_ladder_uses_half_and_double_time_links():
    assert bridge_ladder(87, 174)["step_count"] == 1           # same pulse: lock directly
    lad = bridge_ladder(100, 174)                               # fall to 87, lock 174 double time
    assert lad["link"] == "double" and lad["direction"] == "down"
    assert lad["target_bpm"] == 87 and lad["steps"][-1] == pytest.approx(87, abs=0.1)
    assert bridge_ladder(140, 72)["link"] == "half"             # 72 locks half-time under 144


def test_ladder_falls_and_caps_steps():
    lad = bridge_ladder(174, 124, max_step_pct=6, max_steps=5)
    assert lad["direction"] == "down"
    # 174 -> 124 needs 6 steps at 6%; capped at 5 (7% each), still inside the lock
    assert lad["capped"] and lad["step_count"] == 5 and lad["feasible"]
    assert not bridge_ladder(90, 124, max_step_pct=6, max_steps=2)["feasible"]


def test_ladder_validates_input():
    with pytest.raises(ValueError):
        bridge_ladder(0, 128)
    with pytest.raises(ValueError):
        bridge_ladder(128, 140, max_step_pct=20)
