"""Pure autopilot transition maths (stem blend length, fader shape, phrase wait)."""
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
CHECK = Path(__file__).parents[1].joinpath("js", "autopilot_check.js")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_autopilot_core():
    res = subprocess.run([NODE, str(CHECK)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout
