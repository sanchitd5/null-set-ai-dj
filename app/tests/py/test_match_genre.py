"""Genre/era penalty reaches every real match path (user: "going from Fred Again
to Metal Rock (Dorian Electra) is stupid")."""
from pathlib import Path

import pytest

from app.music_brain.analysis.genre import ERA_JUMP_PENALTY, GENRE_JUMP_PENALTY, era_score, vibe_score
from app.music_brain.matching.knowledge_parser import KnowledgeParser
from app.music_brain.matching.recipe_matcher import RecipeMatcher
from app.tests.py.test_recipe_matcher import _track


@pytest.fixture(scope="module")
def matcher():
    return RecipeMatcher(KnowledgeParser())


def _top(matcher, **kw):
    return matcher.match(_track(), _track(), top_n=1, **kw)[0].score


def test_house_to_metal_gets_genre_multiplier(matcher):
    base = _top(matcher)
    assert base > 0
    metal = _top(matcher, genre_a="melodic house", genre_b="industrial metal")
    assert metal == pytest.approx(base * GENRE_JUMP_PENALTY, abs=1.0)


def test_house_to_house_unpenalised(matcher):
    assert _top(matcher, genre_a="melodic house", genre_b="deep house") == _top(matcher)
    assert _top(matcher, genre_a="melodic house", genre_b=None) == _top(matcher)


def test_resolve_candidate_applies_penalty(matcher):
    a, b = _track(), _track()
    vibe = dict(genre_a="melodic house", genre_b="metal")
    for kw in ({}, {"recipe_name": "Bass Swap"}, {"recipe_name": "Bass Swap", "a_time": 200.0, "b_time": 15.0}):
        base = matcher.resolve_candidate(a, b, **kw).score
        hit = matcher.resolve_candidate(a, b, **kw, **vibe).score
        assert hit == pytest.approx(base * GENRE_JUMP_PENALTY, abs=1.0), kw


def test_era_penalty_only_past_max_gap(matcher):
    assert era_score("1990s", "2000s") == 1.0 and era_score("1990s", None) == 1.0
    assert era_score("1990s", "2010s") == ERA_JUMP_PENALTY
    assert vibe_score("house", "metal", "1990s", "2020s") == pytest.approx(GENRE_JUMP_PENALTY * ERA_JUMP_PENALTY)
    base = _top(matcher)
    assert _top(matcher, era_a="1997", era_b="2017") == pytest.approx(base * ERA_JUMP_PENALTY, abs=1.0)


def test_server_resolver_maps_track_to_labels(monkeypatch):
    import app.ui.server as server
    monkeypatch.setattr(server, "_tracks", {"a": Path("/x/a.mp3"), "b": Path("/x/b.mp3"), "c": Path("/x/c.mp3")})
    monkeypatch.setattr(server, "_track_names", {
        "a": "Fred again.. - Delilah (pull me out of this)", "b": "Dorian Electra - Sodom & Gomorrah"})
    monkeypatch.setattr(server, "_suggested_genres", {
        server._genre_key("Delilah (pull me out of this)"): "melodic house",
        server._genre_key("Sodom & Gomorrah"): "industrial metal"})
    monkeypatch.setattr(server, "_suggested_eras", {server._genre_key("Delilah (pull me out of this)"): "2020s"})
    assert server._pair_vibe("a", "b") == {
        "genre_a": "melodic house", "genre_b": "industrial metal", "era_a": "2020s", "era_b": None}
    # unknown track / unknown title -> None -> no penalty
    assert server._pair_vibe("c", "zzz") == {"genre_a": None, "genre_b": None, "era_a": None, "era_b": None}


def test_agent_bridge_cli_flags():
    from app.music_brain.agent_bridge import _build_parser
    args = _build_parser().parse_args(["match", "a.mp3", "b.mp3", "--genre-a", "house", "--era-b", "1990s"])
    assert (args.genre_a, args.genre_b, args.era_a, args.era_b) == ("house", None, None, "1990s")
