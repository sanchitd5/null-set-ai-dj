"""GET /api/graph/version and /snapshot (graph_api.py): auth, 404 without a graph, ETag = rev, 304, gzip round trip."""
import gzip
import sqlite3

import pytest

from app.music_brain import atlas_bridge
from app.tests.py.testclient_compat import TestClient
from app.ui import server

PATHS = ["/api/graph/version", "/api/graph/snapshot"]


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def anon():
    return TestClient(server.app, auth=False)


@pytest.mark.parametrize("path", PATHS)
def test_routes_need_auth(anon, path):
    assert anon.get(path).status_code in (401, 403)


@pytest.mark.parametrize("path", PATHS)
def test_404_without_builder(client, monkeypatch, path):
    monkeypatch.setattr(atlas_bridge, "AVAILABLE", False)
    r = client.get(path)
    assert r.status_code == 404 and r.json()["detail"] == "no graph"


needs_builder = pytest.mark.skipif(not atlas_bridge.AVAILABLE, reason=atlas_bridge.SKIP_REASON)


@pytest.fixture
def graph(tmp_path, monkeypatch):
    pytest.importorskip("app.music_brain.atlas.graph_store", reason="builder checkout has no graph store")
    from app.music_brain.atlas import graph_schema as gs
    from app.music_brain.atlas import graph_store
    monkeypatch.setenv(gs.ENV_PATH, str(tmp_path / "graph.db"))
    graph_store._cache.clear()
    conn = gs.open_graph()
    gs.meta_set(conn, "rev", 7)
    a, _ = gs.upsert_node(conn, "song", "synthetic one", source="library")
    b, _ = gs.upsert_node(conn, "song", "synthetic two", source="library")
    gs.upsert_edge(conn, a, b, "measured", 0.5)
    gs.log_op(conn, "edge", "x", "{}")
    conn.commit()
    return tmp_path / "graph.db"


@needs_builder
def test_no_graph_file_is_404(client, tmp_path, monkeypatch):
    pytest.importorskip("app.music_brain.atlas.graph_store", reason="builder checkout has no graph store")
    from app.music_brain.atlas import graph_schema as gs
    monkeypatch.setenv(gs.ENV_PATH, str(tmp_path / "missing.db"))
    for path in PATHS:
        assert client.get(path).status_code == 404


@needs_builder
def test_version_shape(client, graph):
    v = client.get("/api/graph/version").json()
    assert v["rev"] == 7 and v["seq"] == 1 and v["nodes"] == 2 and v["edges"] == 1
    assert {"schema", "built_at"} <= set(v)
    assert v["bytes"] == len(client.get("/api/graph/snapshot").content)


@needs_builder
def test_snapshot_etag_304_and_gzip_roundtrip(client, graph, tmp_path):
    r = client.get("/api/graph/snapshot")
    assert r.status_code == 200 and r.headers["etag"] == '"7"'
    assert r.headers["cache-control"] == "private, no-cache"
    out = tmp_path / "out.db"
    out.write_bytes(gzip.decompress(r.content))
    db = sqlite3.connect(out)
    assert db.execute("SELECT value FROM meta WHERE key = 'rev'").fetchone()[0] == "7"
    assert db.execute("SELECT count(*) FROM node").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM op").fetchone()[0] == 0
    db.close()
    for tag in ('"7"', 'W/"7"', '"1", "7"', "*"):
        n = client.get("/api/graph/snapshot", headers={"If-None-Match": tag})
        assert n.status_code == 304 and n.content == b""
    assert client.get("/api/graph/snapshot", headers={"If-None-Match": '"6"'}).status_code == 200
