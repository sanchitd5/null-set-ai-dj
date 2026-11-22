"""STEMONLY: every stem plan the web executor builds (stem-moves.js) passes dj-core's stem-ownership rule (no stem role audible
from both decks beyond one beat). The rule lives only in dj-core (service fn stem_overlaps); the console keeps no copy of it."""
import json
import pathlib
import subprocess

import pytest

from app.music_brain import djcore_bridge

pytestmark = pytest.mark.needs_djcore

DUMP = pathlib.Path(__file__).resolve().parents[1] / "js" / "dump_stem_plans.js"


def _plans():
    out = subprocess.run(["node", str(DUMP)], check=True, capture_output=True, text=True, timeout=120).stdout
    return json.loads(out)


def test_every_web_stem_plan_is_stem_exclusive():
    plans = _plans()
    assert len(plans) > 1000
    bad = []
    for p in plans:
        ov = djcore_bridge.call("stem_overlaps", {"events": p["events"], "fader": p["fader"], "span": p["span"]})
        if ov:
            bad.append((p["label"], ov[0]))
    assert not bad, f"{len(bad)} of {len(plans)} plans overlap, e.g. {bad[:3]}"


def test_an_all_stems_crossfade_is_flagged():
    ev = [{"bar": 0, "deck": "in", "stems": {"drums": 1, "bass": 1, "other": 1, "vocals": 0}, "ramp": 0}]
    ov = djcore_bridge.call("stem_overlaps", {"events": ev, "fader": [{"bar": 0, "from": -1, "to": 1, "bars": 8}], "span": 8})
    assert ov and {o["stem"] for o in ov} >= {"drums", "bass"}
