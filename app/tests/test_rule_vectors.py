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


def test_breakdown_spans_python_matches_vectors():
    cases = V["breakdown_spans"]["cases"]
    assert any(c["expect"] for c in cases) and any(not c["expect"] for c in cases)
    for i, c in enumerate(cases):
        got = preplan.breakdown_spans({"energy_times": c["times"], "energy_curve": c["curve"]}, c["bar"])
        assert [list(s) for s in got] == c["expect"], i


def test_preplan_never_starts_inside_a_breakdown():
    """S22: no candidate's B entry (a_in) or handover line lands inside A's breakdown."""
    bpm, bar = 128.0, 240.0 / 128.0
    et = [i * 0.5 for i in range(int(300 / 0.5))]
    # intro, verse, breakdown 100-140 s, drop 140-200 s, verse, breakdown 220-260 s, verse, outro
    ec = [0.1 if t < 10 or t >= 280 else 0.2 if (100 <= t < 140 or 220 <= t < 260)
          else 1.0 if 140 <= t < 200 else 0.6 for t in et]
    a = {"duration": 300.0, "energy_times": et, "energy_curve": ec,
         "phrase_boundaries_8bar": [i * 8 * bar for i in range(int(300 / (8 * bar)) + 1)]}
    bd = preplan.breakdown_spans(a, bar)
    assert [(round(x), round(y)) for x, y in bd] == [(100, 140), (220, 260)]
    b = {"duration": 240.0, "phrase_boundaries_8bar": [i * 8 * bar for i in range(16)]}
    stems = {r: __import__("numpy").full(200, 0.3) for r in merge.ROLES}
    got = preplan.candidates(a, b, bpm, bpm, 30.0, 280.0, 0.0, stems, stems, 1.0)
    assert got
    for c in got:
        handover = c["a_in"] + c["bars"] * bar
        assert not preplan.in_breakdown(bd, c["a_in"]) and not preplan.in_breakdown(bd, handover), c


def test_merge_rank_python_matches_vectors():
    for i, c in enumerate(V["merge_rank"]["cases"]):
        got = merge.rank(c["eA"], c["eB"], c["keyScore"], b_rap=c["bRap"], a_rap=c["aRap"])
        assert [{"combo": x["combo"], "score": x["score"]} for x in got] == c["expect"], i


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_console_js_matches_vectors():
    res = subprocess.run([NODE, str(HERE / "rule_vectors_check.js")], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout
