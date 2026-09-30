"""Artist spacing: the Python twin of artist-spacing.js, on the shared cases file."""
import json
from pathlib import Path

import pytest

from app.ui import autopilot_service as svc

CASES = json.loads((Path(__file__).parents[1] / "js" / "artist_spacing_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("c", CASES, ids=[c["why"] for c in CASES])
def test_spacing_block_matches_the_js_rule(c):
    assert svc.spacing_block(c["name"], c["recent"], c["relax"]) == c["kind"]


def test_suggest_filter_sees_a_credit_hidden_in_the_last_title():
    """Session 2026-09-29_191658: Sexy Magic (Fred credited in the title) -> Delilah got through."""
    recent = CASES[0]["recent"]
    delilah = {"artist": "Fred again..", "title": "Delilah (pull me out of this)"}
    other = {"artist": "Bicep", "title": "Glue"}
    assert svc.artist_spacing([delilah, other], recent) == [other]
    assert svc.artist_spacing([delilah], recent) == []      # [] -> suggest re-asks
