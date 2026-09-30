"""Runs the node check for the SHOW mode core (app/ui/static/anyma-show.js)."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_anyma_show_js():
    res = subprocess.run([shutil.which("node"), str(Path(__file__).parents[1].joinpath("js", "anyma_show_check.js"))],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout
