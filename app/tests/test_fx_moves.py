"""Artist FX moves (app/ui/static/fx-moves.js): runs the node check for the planners, gates and runtime."""

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
CHECK = Path(__file__).with_name("fx_moves_check.js")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_fx_moves_node_check():
    res = subprocess.run([NODE, str(CHECK)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr or res.stdout
