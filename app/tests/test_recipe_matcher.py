"""Unit tests for music_brain.recipe_matcher (Phase 4 scoring heuristics)."""

import pytest

from app.music_brain.analyzer import KeyEstimate, StructureSection, TrackAnalysis
from app.music_brain.knowledge_parser import KnowledgeParser
from app.music_brain.recipe_matcher import (
    RecipeMatcher,
    bpm_compatibility,
    camelot_distance_score,
    find_entry_candidates,
    find_exit_candidates,
    nearest_phrase_boundary,
    vocal_overlap_penalty,
)


def _track(
    bpm=128.0,
    camelot="8B",
    duration=240.0,
    sections=None,
    phrase_boundaries=None,
    vocal_regions=None,
) -> TrackAnalysis:
    return TrackAnalysis(
        path="fake.mp3",
        duration=duration,
        bpm=bpm,
        beat_times=[],
        downbeat_times=[],
        phrase_boundaries_8bar=[0.0, 15.0, 30.0, 45.0, 200.0, 215.0] if phrase_boundaries is None else phrase_boundaries,
        phrase_boundaries_16bar=[],
        key=KeyEstimate(camelot=camelot, key_name="A Minor", is_major=False, confidence=0.9),
        energy_curve=[],
        energy_times=[],
        sections=sections or [
            StructureSection(label="intro", start=0.0, end=20.0, energy=0.2),
            StructureSection(label="verse", start=20.0, end=180.0, energy=0.6),
            StructureSection(label="breakdown", start=180.0, end=210.0, energy=0.2),
            StructureSection(label="outro", start=210.0, end=duration, energy=0.3),
        ],
        vocal_active_regions=vocal_regions or [],
    )


# --- Camelot key distance -----------------------------------------------

def test_camelot_identical_keys():
    score, reason = camelot_distance_score("8B", "8B")
    assert score == 1.0
    assert "identical" in reason


def test_camelot_adjacent_hour():
    score, _ = camelot_distance_score("8B", "9B")
    assert score == pytest.approx(0.9)
    score, _ = camelot_distance_score("1B", "12B")  # wraps around the wheel
    assert score == pytest.approx(0.9)


def test_camelot_relative_major_minor():
    score, reason = camelot_distance_score("8B", "8A")
    assert score == pytest.approx(0.85)
    assert "relative" in reason


def test_camelot_energy_boost():
    score, _ = camelot_distance_score("8B", "10B")
    assert score == pytest.approx(0.8)


def test_camelot_clash():
    score, reason = camelot_distance_score("8B", "2B")
    assert score == pytest.approx(0.0)  # confident 3+ hour clash is a hard block
    assert "clash" in reason


def test_camelot_invalid_raises():
    with pytest.raises(ValueError):
        camelot_distance_score("bad", "8B")


# --- BPM compatibility ----------------------------------------------------

def test_bpm_seamless():
    score, label = bpm_compatibility(128.0, 130.0)
    assert label == "seamless"
    assert score == 1.0


def test_bpm_ramp():
    score, label = bpm_compatibility(128.0, 134.0)
    assert label == "ramp"


def test_bpm_cut_required():
    score, label = bpm_compatibility(128.0, 174.0)
    assert label == "cut_required"
    assert score < 0.5


# --- Phrase-boundary candidate selection -----------------------------------

def test_find_exit_candidates_picks_breakdown_and_outro():
    track = _track()
    exits = find_exit_candidates(track)
    assert 200.0 in exits and 215.0 in exits
    assert 0.0 not in exits  # intro is not an exit section


def test_find_entry_candidates_picks_intro_and_verse_start_only():
    track = _track()
    entries = find_entry_candidates(track)
    assert 0.0 in entries  # inside the intro
    assert 30.0 not in entries and 45.0 not in entries  # buried mid-verse, not a real entry point
    assert 200.0 in entries  # breakdown


# --- Vocal collision penalty ------------------------------------------------

def test_vocal_overlap_penalty_zero_when_no_vocals():
    a, b = _track(), _track()
    assert vocal_overlap_penalty(a, 200.0, b, 0.0) == 0.0


def test_vocal_overlap_penalty_high_when_both_active():
    a = _track(vocal_regions=[(200.0, 240.0)])
    b = _track(vocal_regions=[(0.0, 40.0)])
    penalty = vocal_overlap_penalty(a, 200.0, b, 0.0)
    assert penalty > 0.5


def test_vocal_overlap_penalty_zero_when_only_one_active():
    a = _track(vocal_regions=[(200.0, 240.0)])
    b = _track(vocal_regions=[])
    assert vocal_overlap_penalty(a, 200.0, b, 0.0) == 0.0


# --- End-to-end matching ----------------------------------------------------

@pytest.fixture(scope="module")
def matcher() -> RecipeMatcher:
    return RecipeMatcher(KnowledgeParser())


def test_match_returns_top_3_ranked_by_score(matcher: RecipeMatcher):
    a, b = _track(bpm=128.0, camelot="8B"), _track(bpm=129.0, camelot="8B")
    results = matcher.match(a, b, top_n=3)
    assert len(results) == 3
    scores = [c.score for c in results]
    assert scores == sorted(scores, reverse=True)


def test_compatible_pair_favors_bass_swap_style_recipe(matcher: RecipeMatcher):
    # Same key, near-identical BPM, no vocal clash: a blend-family recipe
    # should out-score a big-gap bridge recipe like Echo Out.
    a, b = _track(bpm=128.0, camelot="8B"), _track(bpm=129.0, camelot="8B")
    results = {c.recipe.name: c for c in matcher.match(a, b, top_n=28)}
    assert results["Bass Swap"].score > results["Echo Out"].score


def test_clashing_key_and_huge_bpm_gap_favors_echo_out(matcher: RecipeMatcher):
    a = _track(bpm=128.0, camelot="8B")
    b = _track(bpm=174.0, camelot="2B")  # clashing key + huge BPM gap
    results = {c.recipe.name: c for c in matcher.match(a, b, top_n=28)}
    assert results["Echo Out"].score > results["Bass Swap"].score


def test_vocal_collision_is_penalized_unless_recipe_uses_stems(matcher: RecipeMatcher):
    a = _track(bpm=128.0, camelot="8B", vocal_regions=[(200.0, 240.0)])
    b = _track(bpm=129.0, camelot="8B", vocal_regions=[(0.0, 40.0)])
    results = {c.recipe.name: c for c in matcher.match(a, b, top_n=28)}
    assert results["Bass Swap"].vocal_penalty > 0.5  # Bass Swap doesn't use stems
    assert results["Stems Transition"].vocal_penalty < results["Bass Swap"].vocal_penalty


def test_candidate_to_dict_is_json_serializable():
    import json

    matcher = RecipeMatcher(KnowledgeParser())
    a, b = _track(), _track(bpm=129.0)
    for candidate in matcher.match(a, b):
        json.dumps(candidate.to_dict())


# --- Phrase-boundary snapping for manually-picked points --------------------

def test_nearest_phrase_boundary_snaps_to_closest():
    track = _track(phrase_boundaries=[0.0, 30.0, 60.0, 90.0])
    assert nearest_phrase_boundary(track, 28.0) == 30.0
    assert nearest_phrase_boundary(track, 61.0) == 60.0


def test_nearest_phrase_boundary_falls_back_when_no_boundaries():
    track = _track(phrase_boundaries=[])
    assert nearest_phrase_boundary(track, 42.0) == 42.0


# --- score_pair / resolve_candidate: the manual-override bug fix -----------

def test_score_pair_raises_on_unknown_recipe(matcher: RecipeMatcher):
    a, b = _track(), _track()
    with pytest.raises(ValueError):
        matcher.score_pair("Not A Recipe", a, 30.0, b, 0.0)


def test_score_pair_snaps_given_times_to_phrase_boundaries(matcher: RecipeMatcher):
    a = _track(phrase_boundaries=[0.0, 15.0, 30.0, 45.0, 200.0, 215.0])
    b = _track(phrase_boundaries=[0.0, 15.0, 30.0])
    candidate = matcher.score_pair("Bass Swap", a, 28.0, b, 2.0, snap_to_phrase=True)
    assert candidate.a_time == 30.0
    assert candidate.b_time == 0.0


def test_score_pair_can_skip_snapping(matcher: RecipeMatcher):
    a = _track(phrase_boundaries=[0.0, 30.0])
    candidate = matcher.score_pair("Bass Swap", a, 28.0, a, 28.0, snap_to_phrase=False)
    assert candidate.a_time == 28.0


def test_resolve_candidate_no_args_uses_ai_top_match(matcher: RecipeMatcher):
    a, b = _track(bpm=128.0, camelot="8B"), _track(bpm=129.0, camelot="8B")
    resolved = matcher.resolve_candidate(a, b)
    top_match = matcher.match(a, b, top_n=1)[0]
    assert resolved.recipe.name == top_match.recipe.name
    assert resolved.a_time == top_match.a_time


def test_resolve_candidate_recipe_only_uses_ai_points_for_that_recipe(matcher: RecipeMatcher):
    a, b = _track(bpm=128.0, camelot="8B"), _track(bpm=129.0, camelot="8B")
    resolved = matcher.resolve_candidate(a, b, recipe_name="Echo Out")
    all_matches = {c.recipe.name: c for c in matcher.match(a, b, top_n=28)}
    assert resolved.a_time == all_matches["Echo Out"].a_time
    assert resolved.b_time == all_matches["Echo Out"].b_time


def test_resolve_candidate_manual_time_overrides_even_when_recipe_already_matched(matcher: RecipeMatcher):
    """The exact bug this fixes: picking a recipe that the AI already
    scored (true for virtually every recipe, since match() covers all 28)
    must NOT silently discard a user-supplied manual time."""
    a = _track(bpm=128.0, camelot="8B", phrase_boundaries=[0.0, 15.0, 30.0, 45.0, 100.0, 215.0])
    b = _track(bpm=129.0, camelot="8B", phrase_boundaries=[0.0, 15.0, 30.0])

    ai_default = matcher.resolve_candidate(a, b, recipe_name="Bass Swap")
    manual = matcher.resolve_candidate(a, b, recipe_name="Bass Swap", a_time=100.0)

    assert manual.a_time == 100.0  # the manual point, snapped, not discarded
    assert manual.a_time != ai_default.a_time
    assert manual.b_time == ai_default.b_time  # b_time wasn't overridden, so it keeps the AI default


def test_resolve_candidate_partial_override_keeps_other_ai_default(matcher: RecipeMatcher):
    a = _track(bpm=128.0, camelot="8B", phrase_boundaries=[0.0, 15.0, 30.0, 45.0, 200.0, 215.0])
    b = _track(bpm=129.0, camelot="8B", phrase_boundaries=[0.0, 15.0, 30.0])

    resolved = matcher.resolve_candidate(a, b, recipe_name="Bass Swap", b_time=15.0)
    ai_default = matcher.resolve_candidate(a, b, recipe_name="Bass Swap")

    assert resolved.b_time == 15.0
    assert resolved.a_time == ai_default.a_time


def test_resolve_candidate_manual_times_without_recipe_uses_top_recipe(matcher: RecipeMatcher):
    # a_time=100.0 has no exact boundary on track A ([0, 15, 30, 45, 200, 215]);
    # it snaps to the *nearest* one (45.0, distance 55, beats 200.0's distance 100) --
    # this is the expected snap-to-phrase behavior, not a discarded override.
    a = _track(bpm=128.0, camelot="8B", phrase_boundaries=[0.0, 15.0, 30.0, 45.0, 200.0, 215.0])
    b = _track(bpm=129.0, camelot="8B", phrase_boundaries=[0.0, 15.0, 30.0])

    resolved = matcher.resolve_candidate(a, b, a_time=100.0, b_time=15.0)
    top_match = matcher.match(a, b, top_n=1)[0]

    assert resolved.recipe.name == top_match.recipe.name
    assert resolved.a_time == 45.0
    assert resolved.b_time == 15.0


# --- Overlap style (set study gfF8jzBVWvM, item 3) -----------------------

def test_overlap_style_per_recipe():
    from app.music_brain.recipe_matcher import PRE_CLEAR_BARS, overlap_style

    assert overlap_style("Drop Swap") == "instant"
    assert overlap_style("Breakdown Transition") == "slow"
    assert overlap_style("EQ Blend") == "standard"
    assert PRE_CLEAR_BARS["instant"] == 0 < PRE_CLEAR_BARS["standard"] < PRE_CLEAR_BARS["slow"]


def test_overlap_style_names_exist_in_cookbook():
    from app.music_brain.recipe_matcher import _INSTANT_RECIPES, _SLOW_RECIPES

    names = {r.name for r in KnowledgeParser().get_all()}
    assert (_INSTANT_RECIPES | _SLOW_RECIPES) <= names


def test_candidate_dict_carries_overlap():
    a, b = _track(bpm=128.0, camelot="8B"), _track(bpm=129.0, camelot="8B")
    d = RecipeMatcher().match(a, b, top_n=1)[0].to_dict()
    assert d["overlap_style"] in {"instant", "standard", "slow"}
    assert d["pre_clear_bars"] in {0, 8, 16}
