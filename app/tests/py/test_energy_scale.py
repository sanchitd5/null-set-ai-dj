"""LLM energy scores: a whole-number 1 on the 1-10 scale is calm, not 10 (regression: slow in-scene picks were
rejected as "energy gap 9" next to a calm song, and the fallback then crossed genre)."""
import pytest

from app.ui.services.autopilot_service import _energy_1_10, _profile_clash


@pytest.mark.parametrize("raw, want", [
    (1, 1.0), ("1", 1.0), (2, 2.0), (10, 10.0), ("7", 7.0),      # whole numbers: 1-10 scale as given
    (0.3, 3.0), (0.85, 8.5), (1.0, 10.0), ("0.4", 4.0),          # decimals at or below 1: 0-1 scale
    (6.5, 6.5), ("8.0", 8.0),                                     # decimals above 1: already 1-10
])
def test_energy_scale(raw, want):
    assert _energy_1_10(raw) == pytest.approx(want)


@pytest.mark.parametrize("bad", [True, None, "high", ""])
def test_energy_scale_rejects_non_numbers(bad):
    with pytest.raises((TypeError, ValueError)):
        _energy_1_10(bad)


def test_slow_candidate_fits_a_calm_measured_song():
    calm = {"energy": 2, "_measured": True}
    assert _profile_clash(calm, {"energy": 1}) is None
    assert _profile_clash(calm, {"energy": "1"}) is None
    assert _profile_clash(calm, {"energy": 0.2}) is None


def test_real_energy_gap_still_rejected():
    calm = {"energy": 2, "_measured": True}
    assert _profile_clash(calm, {"energy": 9}).startswith("energy gap")
    assert _profile_clash(calm, {"energy": 0.95}).startswith("energy gap")
