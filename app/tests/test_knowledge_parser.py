"""Unit tests for music_brain.knowledge_parser (Phase 1 acceptance criteria:
every transition recipe parses correctly with its tags and principles).

The cookbook started with 28 recipes; "Vocal Punchline Drop Snap" is the 29th, a
full 17-part recipe (the order and section checks below cover it), so the count
is 29. Other tests take the count from the parser rather than repeating it."""

import re

import pytest

from app.music_brain.config import TRANSITION_COOKBOOK_DIR
from app.music_brain.knowledge_parser import (
    SECTION_HEADINGS,
    KnowledgeParser,
    TransitionRecipe,
    parse_recipe_file,
)

EXPECTED_RECIPE_COUNT = 29


@pytest.fixture(scope="module")
def parser() -> KnowledgeParser:
    return KnowledgeParser()


def test_cookbook_directory_has_29_recipe_files():
    md_files = list(TRANSITION_COOKBOOK_DIR.glob("*.md"))
    assert len(md_files) == EXPECTED_RECIPE_COUNT


def test_parser_loads_all_29_recipes(parser: KnowledgeParser):
    assert len(parser) == EXPECTED_RECIPE_COUNT
    recipes = parser.get_all()
    assert len(recipes) == EXPECTED_RECIPE_COUNT
    assert all(isinstance(r, TransitionRecipe) for r in recipes)


def test_vocal_punchline_drop_snap_is_a_full_recipe(parser: KnowledgeParser):
    r = parser.get("Vocal Punchline Drop Snap")
    assert r is not None and r.difficulty == "Intermediate"
    assert "dj/transition" in r.tags and len(r.steps) == 5
    assert "Drop Swap" in r.related


def test_every_recipe_has_required_sections_populated(parser: KnowledgeParser):
    required = [
        "technique_name", "what_it_is", "problem_it_solves",
        "musical_principle", "setup", "step_by_step", "when_to_use",
        "when_not_to_use", "best_genres", "risk_of_gimmicky",
    ]
    for recipe in parser.get_all():
        for field_name in required:
            value = getattr(recipe, field_name)
            assert value, f"{recipe.name}: '{field_name}' section is empty"


def test_every_recipe_has_a_difficulty_and_tags(parser: KnowledgeParser):
    for recipe in parser.get_all():
        assert recipe.difficulty in {"Beginner", "Intermediate", "Advanced"}, recipe.name
        assert isinstance(recipe.tags, list)
        assert len(recipe.tags) > 0, f"{recipe.name} has no tags"
        assert "dj/transition" in recipe.tags


def test_recipe_steps_parses_numbered_list(parser: KnowledgeParser):
    bass_swap = parser.get("Bass Swap")
    assert bass_swap is not None
    assert len(bass_swap.steps) == 4
    assert "Alignment" in bass_swap.steps[0]


def test_recipe_related_extracts_wikilinks(parser: KnowledgeParser):
    bass_swap = parser.get("Bass Swap")
    assert "EQ Blend" in bass_swap.related
    assert "Drop Swap" in bass_swap.related


def test_recipe_genres_parses_comma_list(parser: KnowledgeParser):
    bass_swap = parser.get("Bass Swap")
    assert "House" in bass_swap.genres
    assert "Drum & Bass" in bass_swap.genres


def test_lookup_by_slug_and_exact_name(parser: KnowledgeParser):
    assert parser.get("Bass Swap") is parser.get("bass-swap")
    assert parser.get("Nonexistent Recipe") is None


def test_find_by_tag(parser: KnowledgeParser):
    all_transitions = parser.find_by_tag("dj/transition")
    assert len(all_transitions) == EXPECTED_RECIPE_COUNT
    stems_tagged = parser.find_by_tag("dj/stems")
    assert any(r.name == "Stems Transition" for r in stems_tagged)


def test_find_by_genre(parser: KnowledgeParser):
    house_recipes = parser.find_by_genre("House")
    assert len(house_recipes) > 0
    assert all("house" in " ".join(r.genres).lower() for r in house_recipes)


def test_find_by_difficulty(parser: KnowledgeParser):
    beginner_recipes = parser.find_by_difficulty("Beginner")
    assert len(beginner_recipes) > 0
    assert all(r.difficulty == "Beginner" for r in beginner_recipes)


def test_stems_prerequisite_tagging(parser: KnowledgeParser):
    stems_recipes = parser.stem_only_recipes()
    names = {r.name for r in stems_recipes}
    assert "Stems Transition" in names
    assert "Acapella Overlay" in names
    assert "Basic Blend" not in names


def test_camelot_bypass_tagging(parser: KnowledgeParser):
    echo_out = parser.get("Echo Out")
    bass_swap = parser.get("Bass Swap")
    assert echo_out.camelot_compatible_only is False
    assert bass_swap.camelot_compatible_only is True


def test_unlimited_bpm_delta_for_bridge_recipes(parser: KnowledgeParser):
    echo_out = parser.get("Echo Out")
    assert echo_out.max_bpm_delta is None
    bass_swap = parser.get("Bass Swap")
    assert bass_swap.max_bpm_delta == pytest.approx(0.06)


def test_to_dict_is_json_serializable(parser: KnowledgeParser):
    import json

    for recipe in parser.get_all():
        payload = recipe.to_dict()
        json.dumps(payload)  # raises if anything isn't serializable


def test_section_headings_cover_the_17_part_template():
    # Technique Name + 16 documented sections referenced in CLAUDE.md's
    # "17-Part Transition Template" rule (title heading + 16 '### ' sections).
    assert len(SECTION_HEADINGS) == 16


def test_parse_recipe_file_directly_on_each_file():
    for path in TRANSITION_COOKBOOK_DIR.glob("*.md"):
        recipe = parse_recipe_file(path)
        assert recipe.name == path.stem
        assert recipe.source_path == path


def test_all_recipe_files_use_identical_section_order():
    # A couple of recipes suffix "Step-by-step" with a parenthetical, e.g.
    # "Step-by-step (The Pop -> Drum & Bass Pivot)" — matched by prefix.
    heading_pattern = re.compile(r"^### (.+)$", re.MULTILINE)
    expected_order = list(SECTION_HEADINGS.values())
    for path in TRANSITION_COOKBOOK_DIR.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        headings = heading_pattern.findall(text)
        normalized = [
            next((h for h in expected_order if raw.startswith(h)), raw)
            for raw in headings
        ]
        assert normalized == expected_order, f"{path.name} has non-standard section order"
