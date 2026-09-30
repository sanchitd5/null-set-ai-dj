"""Camelot scoring: hard clash block, bypass-neutral, +2/-2 direction, diagonal,
low-confidence softening (review item 2)."""

import pytest

from app.music_brain.analysis.analyzer import KeyEstimate, StructureSection, TrackAnalysis
from app.music_brain.matching.knowledge_parser import KnowledgeParser
from app.music_brain.matching.recipe_matcher import (
    BYPASS_KEY_SCORE,
    RecipeMatcher,
    camelot_distance_score,
    is_key_clash,
)


def _track(camelot, bpm=128.0, confidence=0.9):
    return TrackAnalysis(
        path="fake.mp3", duration=240.0, bpm=bpm,
        phrase_boundaries_8bar=[0.0, 15.0, 30.0, 45.0, 200.0, 215.0],
        key=KeyEstimate(camelot=camelot, key_name="", is_major=camelot.endswith("B"),
                        confidence=confidence),
        sections=[
            StructureSection("intro", 0.0, 20.0, 0.2),
            StructureSection("verse", 20.0, 180.0, 0.6),
            StructureSection("breakdown", 180.0, 210.0, 0.2),
            StructureSection("outro", 210.0, 240.0, 0.3),
        ],
    )


@pytest.fixture(scope="module")
def matcher():
    return RecipeMatcher(KnowledgeParser())


def test_plus_two_is_boost_minus_two_is_drop():
    up, r_up = camelot_distance_score("8A", "10A")
    down, r_down = camelot_distance_score("8A", "6A")
    assert up == pytest.approx(0.8) and "boost" in r_up
    assert down == pytest.approx(0.6) and "drop" in r_down
    assert camelot_distance_score("11A", "1A")[0] == pytest.approx(0.8)  # wraps 12 -> 1


def test_diagonal_move():
    for b in ("9B", "7B"):
        score, reason = camelot_distance_score("8A", b)
        assert score == pytest.approx(0.75) and "diagonal" in reason


def test_clash_is_zero_and_uncertain_key_softens():
    assert camelot_distance_score("8A", "3A")[0] == 0.0
    score, reason = camelot_distance_score("8A", "3A", 0.9, 0.55)
    assert score == pytest.approx(0.5) and "uncertain key" in reason
    assert is_key_clash("8A", "11B") and not is_key_clash("8A", "9B")


def test_clash_hard_blocks_key_locked_recipes_but_not_bypass(matcher):
    a, b = _track("8A"), _track("2A", bpm=129.0)
    results = {c.recipe.name: c for c in matcher.match(a, b, top_n=100)}
    locked = [c for c in results.values() if c.recipe.camelot_compatible_only]
    bypass = [c for c in results.values() if not c.recipe.camelot_compatible_only]
    assert locked and bypass
    assert all(c.score == 0.0 for c in locked)
    assert all(c.camelot_score == pytest.approx(BYPASS_KEY_SCORE) for c in bypass)
    assert all(c.score > 0 for c in bypass)


def test_low_confidence_clash_not_blocked(matcher):
    a, b = _track("8A", confidence=0.5), _track("2A", bpm=129.0)
    locked = [c for c in matcher.match(a, b, top_n=100) if c.recipe.camelot_compatible_only]
    assert locked and all(c.score > 0 for c in locked)
    assert all(c.camelot_score == pytest.approx(0.5) for c in locked)


def test_clashing_pair_never_outscores_a_compatible_pair(matcher):
    clash = matcher.match(_track("8A"), _track("2A", bpm=129.0), top_n=100)
    good = matcher.match(_track("8A"), _track("9A", bpm=129.0), top_n=100)
    assert BYPASS_KEY_SCORE <= 0.5
    assert max(c.score for c in clash) < 80 <= max(c.score for c in good)
