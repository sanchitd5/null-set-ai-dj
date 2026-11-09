"""Pick identity scorer (app/ui/services/pick_identity.py) on the shared fixtures
(app/tests/fixtures/pick_identity_cases.json; dj_core runs the same file) plus the rules the fixtures can't express."""
import json
from pathlib import Path

import pytest

from app.ui.services import pick_identity as pi

CASES = json.loads((Path(__file__).resolve().parent.parent / "fixtures" / "pick_identity_cases.json").read_text())["cases"]


def _run(case):
    c = case["cand"]
    return pi.judge(case["pick"][0], case["pick"][1], c["title"], channel=c.get("channel", ""),
                    uploader=c.get("uploader", ""), artists=c.get("artists"), duration=c.get("duration"),
                    **(case.get("opts") or {}))


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_fixture(case):
    v = _run(case)
    assert v.ok == case["ok"], (v.reasons, v.score)
    if case["ok"]:
        assert v.tier == case["tier"], (v.tier, v.score)
    else:
        assert case["reason"] in v.reason, v.reason


def test_scores_are_bounded_and_ordered():
    best = _run(CASES[0])
    fan = _run(CASES[2])
    assert 0.8 <= fan.score < pi.HIGH_SCORE <= best.score <= 1.0


def test_scripts():
    assert pi.scripts_of("Glass Harbor") == {"latin"}
    assert pi.scripts_of("ਗਲਾਸ Harbor") == {"gurmukhi", "latin"}
    assert pi.scripts_of("123 !!") == set()


def test_side_artists_of_the_playing_song():
    assert pi.side_artists("Main Act & Side Kay", "Orbit Line") == {"sidekay"}
    assert pi.side_artists("Main Act", "Orbit Line (feat. Side Kay)") == {"sidekay"}
    assert pi.side_artists("Main Act", "Orbit Line") == set()
    assert pi.side_artists("Main Act x Side Kay, Dax Rho", "") == {"sidekay", "daxrho"}


def test_pick_of():
    assert pi.pick_of("Vela Moon - Glass Harbor") == ("Vela Moon", "Glass Harbor")
    assert pi.pick_of("Glass Harbor") == ("", "Glass Harbor")


def test_empty_inputs_reject_without_raising():
    assert not pi.judge("", "x", "x").ok
    assert not pi.judge("a", "", "x").ok
    assert not pi.judge("a", "b", "").ok


SCORES = json.loads((Path(__file__).resolve().parent.parent / "fixtures" / "pick_identity_scores.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_score_matches_the_golden_the_dart_twin_also_checks(case):
    assert _run(case).score == pytest.approx(SCORES[case["name"]], abs=1e-4)
