"""G23 contract: every golden vector (dj-config goldens/pick_rules) through the dj-core service via the bridge.

The rules are dj-core's; this test only proves the wire (shape in, shape out) and that the server's client sees the same
answers as the dj-core VM tests. Needs the service executable (DJ_CORE_SVC); skipped without it. dj-core CI runs the same
goldens through the exe too (tool/svc_goldens.dart).
"""
import json
import os

import pytest

from app.music_brain import djcore_bridge
from app.music_brain.pick_accept import accept_candidate, accept_many, resolve_genre

pytestmark = pytest.mark.needs_djcore

_FIX = os.path.join(os.path.dirname(__file__), "..", "fixtures", "pick_rules_goldens.json")
_DOC = json.load(open(_FIX))["files"]
_CASES = [(f, c) for f, body in _DOC.items() if "kind" not in body for c in body["cases"]]
_GENRE = _DOC["genre"]["cases"]


@pytest.mark.parametrize("fname,case", _CASES, ids=[f"{f}:{c['name']}" for f, c in _CASES])
def test_golden(fname, case):
    ok, reason = accept_candidate(case["candidate"], case["state"])
    assert (ok, reason) == (case["expect"]["ok"], case["expect"]["reason"])


def test_batch_equals_single():
    got = accept_many([(c["candidate"], c["state"]) for _, c in _CASES])
    assert got == [(c["expect"]["ok"], c["expect"]["reason"]) for _, c in _CASES]


@pytest.mark.parametrize("case", _GENRE, ids=[c["name"] for c in _GENRE])
def test_resolve_genre(case):
    assert resolve_genre(case["input"]) == case["expect"]


def test_empty_batch_and_protocol():
    assert accept_many([]) == []
    assert djcore_bridge.get_bridge().info()["protocol"] == djcore_bridge.PROTOCOL
