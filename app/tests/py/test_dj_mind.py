"""Runs the node check for the pure DJ-mind decision core (app/ui/static/dj-mind.js)."""

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
CHECK = Path(__file__).parents[1].joinpath("js", "dj_mind_check.js")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_dj_mind_core():
    res = subprocess.run([NODE, str(CHECK)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout
