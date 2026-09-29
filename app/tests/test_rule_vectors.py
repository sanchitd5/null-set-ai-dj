"""The rules that exist twice (Python server planning, browser JS live console) agree:
both copies are checked against the same golden vectors in fixtures/rule_vectors.json.
Exact equality on purpose: the two sides do the same float operations."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.music_brain import energy, merge, preplan

HERE = Path(__file__).parent
V = json.loads((HERE / "fixtures" / "rule_vectors.json").read_text(encoding="utf-8"))
NODE = shutil.which("node")


def test_energy_step_python_matches_vectors():
    cases = V["energy_step"]["cases"]
    assert len(cases) > 100
    for c in cases:
        o = c["o"]
        got = energy.next_ok(c["cur"], c["nxt"], relaxed=bool(o.get("relaxed")), force=bool(o.get("force")),
                             songs=o.get("songs"), set_pos=o.get("setPos"), raw_delta=o.get("rawDelta"),
                             recent=o.get("recent"), reset=bool(o.get("reset")))
        assert got == c["expect"], (c["cur"], c["nxt"], o)


def test_high_spans_python_matches_vectors():
    cases = V["high_spans"]["cases"]
    assert any(c["expect"] for c in cases) and any(not c["expect"] for c in cases)
    for i, c in enumerate(cases):
        got = preplan.high_spans({"energy_times": c["times"], "energy_curve": c["curve"]}, c["bar"])
        assert [list(s) for s in got] == c["expect"], i


def test_merge_rank_python_matches_vectors():
    for i, c in enumerate(V["merge_rank"]["cases"]):
        got = merge.rank(c["eA"], c["eB"], c["keyScore"], b_rap=c["bRap"], a_rap=c["aRap"])
        assert [{"combo": x["combo"], "score": x["score"]} for x in got] == c["expect"], i


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_console_js_matches_vectors():
    res = subprocess.run([NODE, str(HERE / "rule_vectors_check.js")], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout
