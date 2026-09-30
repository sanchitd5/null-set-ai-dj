"""Matcher review fixes: half/double time (item 3), live vocal penalty (item 4),
per-recipe candidate points (item 5)."""

from pathlib import Path

import pytest

from app.music_brain.analysis.analyzer import KeyEstimate, StructureSection, TrackAnalysis
from app.music_brain.matching.knowledge_parser import KnowledgeParser
from app.music_brain.matching.recipe_matcher import (
    OVERLAP_BARS,
    VOCAL_CLASH_CUTOFF,
    RecipeMatcher,
    bpm_compatibility,
    vocal_overlap_penalty,
)


@pytest.mark.parametrize("a,b", [(87.0, 174.0), (174.0, 87.0), (86.0, 174.0), (140.0, 72.0)])
def test_half_double_time_is_not_cut_required(a, b):
    score, label = bpm_compatibility(a, b)
    assert label == "half_time"
    assert score >= 0.6


def test_nothing_within_six_percent_is_cut_required():
    assert bpm_compatibility(128.0, 174.0)[1] == "cut_required"
    assert bpm_compatibility(100.0, 174.0)[1] == "cut_required"  # 200 vs 174 = 13%


# --- item 4: vocal penalty ---------------------------------------------------

def _track(bpm=128.0, camelot="8A", vocals=None, duration=240.0, sections=None, phrases=None):
    return TrackAnalysis(
        path="fake.mp3", duration=duration, bpm=bpm,
        phrase_boundaries_8bar=phrases if phrases is not None else [0.0, 15.0, 30.0, 45.0, 180.0, 195.0, 210.0],
        key=KeyEstimate(camelot=camelot, key_name="", is_major=False, confidence=0.9),
        sections=sections or [
            StructureSection("intro", 0.0, 20.0, 0.2),
            StructureSection("verse", 20.0, 170.0, 0.6),
            StructureSection("breakdown", 170.0, 205.0, 0.2),
            StructureSection("outro", 205.0, duration, 0.3),
        ],
        vocal_active_regions=vocals or [],
    )


@pytest.fixture(scope="module")
def matcher():
    return RecipeMatcher(KnowledgeParser())


def test_vocal_window_follows_recipe_overlap():
    a = _track(vocals=[(200.0, 210.0)])  # vocals only in the first ~10 s after 200
    b = _track(vocals=[(0.0, 240.0)])
    short = vocal_overlap_penalty(a, 200.0, b, 0.0, bars=OVERLAP_BARS["instant"])
    long = vocal_overlap_penalty(a, 200.0, b, 0.0, bars=OVERLAP_BARS["slow"])
    assert short == pytest.approx(1.0) and long < 0.3


def test_vocal_clash_halves_non_stems_recipes(matcher):
    clean_a, clean_b = _track(), _track(bpm=129.0)
    voc_a = _track(vocals=[(0.0, 240.0)])
    voc_b = _track(bpm=129.0, vocals=[(0.0, 240.0)])
    clean = {c.recipe.name: c for c in matcher.match(clean_a, clean_b, top_n=100)}
    clash = {c.recipe.name: c for c in matcher.match(voc_a, voc_b, top_n=100)}
    for name, c in clash.items():
        if not c.recipe.requires_stems and c.vocal_penalty > VOCAL_CLASH_CUTOFF:
            assert c.score <= clean[name].score * 0.5 + 1e-6
    stems = [c for c in clash.values() if c.recipe.requires_stems]
    assert stems and max(stems, key=lambda c: c.score).score > clash["Bass Swap"].score


def test_api_match_uses_cached_vocal_stem_without_separating(monkeypatch, tmp_path):
    from app.music_brain.audio import stem_service
    from app.music_brain.analysis import analyzer
    from app.ui import server

    stem = tmp_path / "vocals.wav"
    stem.write_bytes(b"")
    tracks = {"a": _track(), "b": _track(bpm=129.0)}
    monkeypatch.setattr(server, "_track_path", lambda tid: Path(tid))
    monkeypatch.setattr(server, "analyze_track", lambda p: tracks[str(p)])
    monkeypatch.setattr(stem_service, "file_hash", lambda p: str(p))
    monkeypatch.setattr(
        stem_service, "_load_from_cache",
        lambda d: {"vocals": str(stem)} if d.name.startswith(("a_", "b_")) and "_ft_vocals" in d.name else None,
    )
    monkeypatch.setattr(analyzer, "vocal_presence_map", lambda p: [(0.0, 240.0)])
    monkeypatch.setattr(server, "separate_stems", lambda *a, **k: pytest.fail("must not separate"))
    monkeypatch.setattr(server, "_vocal_regions", {})
    out = server.post_match(server.MatchRequest(track_a_id="a", track_b_id="b", top_n=len(KnowledgeParser())))
    assert any(c["vocal_penalty"] > 0.5 for c in out["candidates"])
    assert tracks["a"].vocal_active_regions == []  # cached analysis not mutated


def test_api_match_without_cached_stem_keeps_zero_penalty(monkeypatch):
    from app.music_brain.audio import stem_service
    from app.ui import server

    tracks = {"a": _track(), "b": _track(bpm=129.0)}
    monkeypatch.setattr(server, "_track_path", lambda tid: Path(tid))
    monkeypatch.setattr(server, "analyze_track", lambda p: tracks[str(p)])
    monkeypatch.setattr(stem_service, "file_hash", lambda p: str(p))
    monkeypatch.setattr(stem_service, "_load_from_cache", lambda d: None)
    monkeypatch.setattr(server, "separate_stems", lambda *a, **k: pytest.fail("must not separate"))
    monkeypatch.setattr(server, "_vocal_regions", {})
    out = server.post_match(server.MatchRequest(track_a_id="a", track_b_id="b", top_n=5))
    assert all(c["vocal_penalty"] == 0.0 for c in out["candidates"])


# --- item 5: per-recipe candidate points -------------------------------------

def _sectioned():
    """A: intro 0-20, drop 20-80, breakdown 80-120, drop 120-180, outro 180-240."""
    return _track(
        sections=[
            StructureSection("intro", 0.0, 20.0, 0.2),
            StructureSection("drop", 20.0, 80.0, 0.9),
            StructureSection("breakdown", 80.0, 120.0, 0.3),
            StructureSection("drop", 120.0, 180.0, 0.9),
            StructureSection("outro", 180.0, 240.0, 0.3),
        ],
        phrases=[0.0, 30.0, 60.0, 90.0, 150.0, 195.0, 225.0],
    )


def test_exit_points_follow_recipe_class(matcher):
    a, b = _sectioned(), _track(bpm=129.0)
    res = {c.recipe.name: c for c in matcher.match(a, b, top_n=100)}
    assert res["Drop Swap"].exit_section == "drop"
    assert res["Echo Out"].exit_section in ("breakdown", "outro")
    assert res["Basic Blend"].exit_section in ("outro", "breakdown")
    d = res["Drop Swap"].to_dict()
    assert {"exit_section", "entry_section", "recipe_class"} <= set(d)
    assert d["recipe_class"] == "drop"


def test_phrase_score_is_real(matcher):
    a, b = _sectioned(), _track(bpm=129.0)
    good = matcher.score_pair("Drop Swap", a, 150.0, b, 0.0, snap_to_phrase=False)
    assert good.phrase_score == 1.0  # drop exit, intro entry, both on-grid
    role_miss = matcher.score_pair("Drop Swap", a, 195.0, b, 0.0, snap_to_phrase=False)
    assert role_miss.phrase_score == 0.5  # outro isn't a drop exit
    off = matcher.score_pair("Drop Swap", a, 151.0, b, 0.0, snap_to_phrase=False)
    assert off.phrase_score == 0.0


def test_runway_keeps_the_overlap_inside_track_a(matcher):
    # 128 BPM: slow overlap = 32 bars = 60 s; an exit at 225 s would run past 240 s.
    a, b = _sectioned(), _track(bpm=129.0)
    for c in matcher.match(a, b, top_n=100):
        from app.music_brain.matching.recipe_matcher import overlap_style
        bars = OVERLAP_BARS[overlap_style(c.recipe.name)]
        assert c.a_time + bars * 4 * 60.0 / a.bpm <= a.duration + 1e-6
