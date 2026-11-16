"""Console stem download: AAC url, parallel fetch, byte progress, status words (node check)."""
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
CHECK = Path(__file__).parents[1].joinpath("js", "stem_fetch_check.js")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_stem_fetch():
    res = subprocess.run([NODE, str(CHECK)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout
