"""Set energy + recipe choice (energy-recipe-choice): the Python twin in analysis/energy.py and the
console's autopilot.js setEnergy / energyRecipeChoice give the same answer on a grid of cases."""
import itertools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.music_brain.analysis import energy

ROOT = Path(__file__).resolve().parents[3]
NODE = shutil.which("node")

SET_CASES = [
    {"setPos": p, "recent": r}
    for p in (None, 0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.85, 0.9, 1.0)
    for r in ([], [3], [2, 2], [8, 9], [3, 3, 3], [9, 1], [1, 9], [5, 6, 7, 8, 9], [4, None, 6])
]
CHOICE_CASES = [
    {"band": b, "mashupFits": mf, "mashupKind": k, "blendOpen": bo, "cleanBoth": cb,
     "longBlendOk": lb, "setLevel": sl, "levels": lv}
    for b, mf, k, bo, cb, lb in itertools.product(
        ("relaxed", "middle", "high", None), (True, False), ("low", "beat", None),
        (True, False), (True, False), (True, False))
    for sl, lv in ((None, {}), (6, {"mashup": 10, "bass": 6, "blend": 6}),
                   (3, {"mashup": 3, "bass": 9, "blend": 8}), (8, {"mashup": 1, "bass": 1}))
]

JS = """
const ap = require(process.argv[2]);
const inp = JSON.parse(require("fs").readFileSync(0, "utf8"));
const se = inp.set.map((o) => { const r = ap.setEnergy(o); return { level: r.level, band: r.band, arc: r.arc }; });
const ch = inp.choice.map((o) => ap.energyRecipeChoice(o).recipe);
process.stdout.write(JSON.stringify({ se, ch }));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_set_energy_and_choice_match_js(tmp_path):
    script = tmp_path / "parity.js"
    script.write_text(JS, encoding="utf-8")
    out = subprocess.run(
        [NODE, str(script), str(ROOT / "app/ui/static/autopilot.js")],
        input=json.dumps({"set": SET_CASES, "choice": CHOICE_CASES}),
        capture_output=True, text=True, check=True, timeout=30)
    js = json.loads(out.stdout)
    for c, want in zip(SET_CASES, js["se"]):
        got = energy.set_energy(c["setPos"], c["recent"])
        assert {k: got[k] for k in ("level", "band", "arc")} == want, c
    for c, want in zip(CHOICE_CASES, js["ch"]):
        got = energy.recipe_choice(c["band"], c["mashupFits"], c["mashupKind"], c["blendOpen"],
                                   c["cleanBoth"], c["longBlendOk"], c["setLevel"], c["levels"])
        assert got == want, c


def test_set_energy_examples():
    e = energy.set_energy(0.5, [3, 3, 3])   # peak 8, played 3 -> 5.5 -> 6
    assert (e["arc"], e["level"], e["band"]) == ("peak", 6, "middle")
    assert energy.set_energy(0.05, [2, 2])["band"] == "relaxed"
    assert energy.set_energy(None, [])["level"] is None
