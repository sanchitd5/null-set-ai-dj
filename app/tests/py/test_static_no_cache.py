from app.tests.py.testclient_compat import TestClient
from app.ui import server


def test_console_static_files_are_revalidated():
    c = TestClient(server.app)
    for path in ("/mascot.js", "/index.html"):
        r = c.get(path)
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-cache"
        etag = r.headers.get("etag")
        assert etag
        assert c.get(path, headers={"If-None-Match": etag}).status_code == 304   # cheap when unchanged
