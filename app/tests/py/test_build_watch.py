"""Reload prompt (app/ui/static/build-watch.js): node check, and the page loads it."""
import shutil
import subprocess
from pathlib import Path

import pytest

from app.ui import server

STATIC = Path(server.STATIC_DIR)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_build_watch_js():
    res = subprocess.run([shutil.which("node"), str(Path(__file__).parents[1].joinpath("js", "build_watch_check.js"))],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout


def test_index_loads_build_watch_and_styles_it():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<script src="/build-watch.js"></script>' in html
    assert ".build-banner" in (STATIC / "style.css").read_text(encoding="utf-8")
