"""Matcher review fixes: half/double time (item 3), live vocal penalty (item 4),
per-recipe candidate points (item 5)."""

import pytest

from app.music_brain.recipe_matcher import bpm_compatibility


@pytest.mark.parametrize("a,b", [(87.0, 174.0), (174.0, 87.0), (86.0, 174.0), (140.0, 72.0)])
def test_half_double_time_is_not_cut_required(a, b):
    score, label = bpm_compatibility(a, b)
    assert label == "half_time"
    assert score >= 0.6


def test_nothing_within_six_percent_is_cut_required():
    assert bpm_compatibility(128.0, 174.0)[1] == "cut_required"
    assert bpm_compatibility(100.0, 174.0)[1] == "cut_required"  # 200 vs 174 = 13%
