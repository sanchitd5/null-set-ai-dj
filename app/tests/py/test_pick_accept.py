"""G23: every golden vector (dj-config goldens/pick_rules) through the Python port, default hooks."""
import json
import os

import pytest

from app.music_brain.pick_accept import accept_candidate, resolve_genre

_FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "pick_rules_goldens.json")
_CASES = [(f, c) for f, body in json.load(open(_FIX))["files"].items() if "kind" not in body for c in body["cases"]]
_GENRE = json.load(open(_FIX))["files"]["genre"]["cases"]


@pytest.mark.parametrize("fname,case", _CASES, ids=[f"{f}:{c['name']}" for f, c in _CASES])
def test_golden(fname, case):
    ok, reason = accept_candidate(case["candidate"], case["state"])
    assert (ok, reason) == (case["expect"]["ok"], case["expect"]["reason"])


@pytest.mark.parametrize("case", _GENRE, ids=[c["name"] for c in _GENRE])
def test_resolve_genre(case):
    assert resolve_genre(case["input"]) == case["expect"]
