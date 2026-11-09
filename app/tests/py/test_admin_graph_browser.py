"""Browser check of /admin/graph at 360x740 (touch) and 1280x800: page loads, no overflow, tap targets >= 44 px,
search draws the node and its neighbours, a tap expands, the kind filter hides edges. Uses the global node
playwright (or the one bundled with @playwright/mcp) and the preinstalled chromium; skipped when either is missing."""
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.music_brain import atlas_bridge
from app.tests.py.test_admin_graph_api import build_graph
from app.tests.py.test_admin_shell import ADMIN, _google_cookie, env  # noqa: F401

pytestmark = pytest.mark.skipif(not atlas_bridge.AVAILABLE, reason=atlas_bridge.SKIP_REASON)
SCRIPT = Path(__file__).resolve().parents[1] / "js" / "admin_graph_check.js"
STATIC = Path(__file__).resolve().parents[2] / "ui" / "static"


def _node_playwright():
    node = shutil.which("node")
    if not node:
        return None, None
    root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
    for base in (Path(root), Path(root) / "@playwright" / "mcp" / "node_modules"):
        if (base / "playwright").is_dir():
            return node, str(base)
    return None, None


def test_graph_page_in_browser(env, tmp_path, monkeypatch):  # noqa: F811
    import uvicorn
    from fastapi.staticfiles import StaticFiles

    node, root = _node_playwright()
    if not node:
        pytest.skip("needs node and the global playwright package")
    build_graph(tmp_path, monkeypatch)
    app, s, *_ = env
    app.mount("/", StaticFiles(directory=str(STATIC)), name="static")
    cookie = _google_cookie(s, "1001", ADMIN)
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        r = subprocess.run([node, str(SCRIPT), f"http://127.0.0.1:{port}", s.cookie_name, cookie, "artist a", "4"],
                           capture_output=True, text=True, timeout=120, env={**os.environ, "NODE_PATH": root})
        assert r.returncode == 0, r.stdout + r.stderr
        assert r.stdout.strip() == "ok"
    finally:
        server.should_exit = True
        t.join(10)
