"""The virtual set is deterministic: replaying one fixture twice gives the same report (zero network)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _replay(name, out):
    subprocess.run([sys.executable, "-m", "app.sim.virtual_set", "--replay", name, "--out", str(out)],
                   cwd=ROOT, check=True, capture_output=True, timeout=600)
    return json.loads((out / "report.json").read_text())


@pytest.mark.slow
def test_replay_is_deterministic(tmp_path):
    if not (ROOT / "app/sim/fixtures/lib-s1-quick/run.json").exists():
        pytest.skip("fixture not recorded")
    a, b = _replay("lib-s1-quick", tmp_path / "a"), _replay("lib-s1-quick", tmp_path / "b")
    assert a["score"] == b["score"]
    assert a == b
