"""Beat-to-beat blend planner: vocal-free exit/entry phrases + tempo lock."""

import pytest

from app.music_brain.analyzer import StructureSection, TrackAnalysis
from app.music_brain.blend import plan_blend, tempo_lock


def _track(bpm, duration=240.0, sections=None):
    bar = 240.0 / bpm
    phrases = [i * 8 * bar for i in range(int(duration // (8 * bar)) + 1)]
    return TrackAnalysis(path="x", duration=duration, bpm=bpm, phrase_boundaries_8bar=phrases,
                         sections=sections or [StructureSection("intro", 0, 16, 0.3),
                                               StructureSection("verse", 16, 200, 0.6),
                                               StructureSection("outro", 200, duration, 0.3)])


def test_tempo_lock():
    assert tempo_lock(123, 121)[0] == pytest.approx(123 / 121)
    assert tempo_lock(174, 87) == (1.0, 2.0)
    assert tempo_lock(128, 100) is None


def test_exit_avoids_vocals_and_entry_is_instrumental():
    a, b = _track(120), _track(120)            # bar = 2 s, 16 bars = 32 s
    a_voc = [(96.0, 150.0)]                    # A sings 96-150 s
    b_voc = [(16.0, 80.0)]                     # B's vocal starts at 16 s
    plan = plan_blend(a, b, 80.0, 170.0, a_vocals=a_voc, b_vocals=b_voc, bars=16)
    assert plan["ok"] and plan["instrumental"]
    assert plan["a_vocal_coverage"] == 0.0 and plan["b_vocal_coverage"] == 0.0
    assert plan["exit"] >= 150.0              # waits for A's vocal to end
    assert plan["exit"] % 16 == pytest.approx(0.0)   # on A's 8-bar grid
    assert plan["entry"] + 32 <= 16.0 or plan["entry"] >= 80.0  # B's blend window has no vocal


def test_no_clean_window_is_flagged_not_hidden():
    a, b = _track(120), _track(120)
    plan = plan_blend(a, b, 80.0, 120.0, a_vocals=[(0.0, 240.0)], b_vocals=[], bars=16)
    assert plan["ok"] and not plan["instrumental"]
    assert "no vocal-free exit" in plan["reasons"][0]


def test_tempo_gap_refuses_beat_blend():
    plan = plan_blend(_track(128), _track(100), 60.0, 150.0, bars=16)
    assert plan["ok"] is False and "tempo gap" in plan["reasons"][0]


def test_rate_locks_b_to_effective_a_tempo():
    plan = plan_blend(_track(121), _track(123), 60.0, 150.0, a_bpm_effective=121.0 * 1.01, bars=8)
    assert plan["rate"] == pytest.approx(121.0 * 1.01 / 123, abs=1e-4)


def test_entry_is_on_bs_beat_grid_not_zero():
    b = _track(120)
    b.phrase_boundaries_8bar = [5.9 + i * 16 for i in range(12)]   # first downbeat at 5.9 s
    plan = plan_blend(_track(120), b, 60.0, 150.0, a_vocals=[], b_vocals=[], bars=16)
    assert plan["entry"] in b.phrase_boundaries_8bar


def test_entry_matches_exit_energy_not_just_intro():
    a, b = _track(120), _track(120)
    a.energy_times = [float(t) for t in range(240)]
    a.energy_curve = [0.9] * 240                       # A leaves at its peak
    b.energy_times = [float(t) for t in range(240)]
    b.energy_curve = [0.2 if t < 64 else 0.9 for t in range(240)]  # quiet intro, peak from 64 s
    plan = plan_blend(a, b, 80.0, 170.0, a_vocals=[], b_vocals=[], bars=16)
    assert plan["entry"] >= 64.0                        # enters at B's matching peak, not its quiet intro
    assert plan["entry_energy"] == pytest.approx(0.9)


# ── entry_mode "drop" (peak moves: DOUBLE DROP / DROP SWAP) ───────────────────
def _droppy(bpm=120, drop_at=96.0):
    # 2 s bars: drop is 1-3 s slivers on the energy grid, merged to 32 s
    secs = [StructureSection("intro", 0, 32, 0.3), StructureSection("build", 32, drop_at, 0.6)]
    secs += [StructureSection("drop", drop_at + i * 2, drop_at + (i + 1) * 2, 0.95) for i in range(16)]
    secs += [StructureSection("drop", 200, 204, 0.9),               # sliver: not a long drop
             StructureSection("outro", 204, 240, 0.3)]
    return _track(bpm, sections=secs)


def test_drop_entry_lands_on_the_first_long_drop():
    a, b = _track(120), _droppy(drop_at=96.0)
    plan = plan_blend(a, b, 80.0, 170.0, bars=8, entry_mode="drop")
    assert plan["ok"] and plan["entry_mode"] == "drop"
    assert plan["entry"] == 96.0 and plan["entry_label"] == "drop"   # past 45%: allowed in drop mode
    assert plan["drop"] == {"start": 96.0, "end": 128.0}
    assert plan["entry_energy"] is None or plan["entry_energy"] > 0


def test_drop_entry_needs_a_long_drop_on_the_grid():
    a = _track(120)
    assert "no long drop" in plan_blend(a, _track(120), 80.0, 170.0, entry_mode="drop")["reasons"][0]
    off = _droppy(drop_at=101.0)                     # 5 s off B's 16 s phrase grid
    assert "no phrase line" in plan_blend(a, off, 80.0, 170.0, bars=8, entry_mode="drop")["reasons"][0]
    with pytest.raises(ValueError):
        plan_blend(a, off, 80.0, 170.0, entry_mode="peak")


def test_match_mode_unchanged_by_drop_fields():
    plan = plan_blend(_track(120), _droppy(), 80.0, 170.0, bars=16)
    assert plan["entry_mode"] == "match" and plan["drop"] is None
