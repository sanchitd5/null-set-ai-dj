"""Phone Lite view (app/ui/static/phone-ui.js + phone.css) and the PWA shell it ships with."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.tests.py.testclient_compat import TestClient
from app.ui import server

STATIC = Path(server.STATIC_DIR)
ICONS = ["/icons/icon-192.png", "/icons/icon-512.png", "/icons/icon-maskable-512.png", "/icons/apple-touch-icon.png"]


@pytest.fixture
def anon():
    server.AUTH_GUARD.limiter.reset()
    yield TestClient(server.app, auth=False)
    server.AUTH_GUARD.limiter.reset()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_phone_ui_js():
    res = subprocess.run([shutil.which("node"), str(Path(__file__).parents[1].joinpath("js", "phone_ui_check.js"))],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout


def test_manifest_and_icons_are_public_and_nothing_else_is(anon):
    # an install / home-screen fetch may carry no cookie: the manifest and its icons are public
    r = anon.get("/manifest.json")
    assert r.status_code == 200
    man = json.loads(r.content)
    assert man["display"] == "standalone" and man["start_url"] == "/"
    for icon in man["icons"]:
        assert icon["src"] in ICONS
    for path in ICONS:
        r = anon.get(path)
        assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n", path
    # the console itself, its scripts and the API stay behind sign-in
    for path in ["/", "/index.html", "/phone-ui.js", "/phone.css", "/icons/other.png", "/api/tracks"]:
        assert anon.get(path).status_code == 401, path
    assert anon.post("/manifest.json").status_code in (401, 405)


def test_index_loads_the_phone_files_last():
    html = (STATIC / "index.html").read_text()
    assert 'href="/manifest.json"' in html and "viewport-fit=cover" in html
    assert 'href="/phone.css"' in html
    scripts = [line.strip() for line in html.splitlines() if line.strip().startswith("<script src=\"/")]
    assert scripts[-1] == '<script src="/phone-ui.js"></script>'   # after every module it drives


def test_phone_css_never_touches_the_desktop():
    # every selector is scoped to html.phone-lite (or hides the Lite elements outside it)
    css = (STATIC / "phone.css").read_text()
    import re
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    body = re.sub(r"@media[^{]*\{", "", body)
    for block in re.findall(r"([^{}]+)\{", body):
        for sel in block.split(","):
            sel = sel.strip()
            if not sel:
                continue
            ok = sel.startswith("html.phone-lite") or sel.startswith("html:not(.phone-lite)") or sel.startswith(".l")
            assert ok, sel
