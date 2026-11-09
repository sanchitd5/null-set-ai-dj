"""/admin/graph and /api/admin/graph/*: gate (404 off, 403 not admin, 401 anonymous), shapes, filters, empty graph.
Needs the private builder (graph_store views); synthetic names only."""
import pytest

from app.music_brain import atlas_bridge
from app.tests.py.test_admin_shell import ADMIN, OTHER, _admin, _client, _google_cookie, env  # noqa: F401

pytestmark = pytest.mark.skipif(not atlas_bridge.AVAILABLE, reason=atlas_bridge.SKIP_REASON)

API = ("/api/admin/graph/stats", "/api/admin/graph/node?q=a", "/api/admin/graph/node?id=1",
       "/api/admin/graph/neighbours?id=1", "/admin/graph")


def build_graph(path, monkeypatch):
    """A small synthetic graph.db at `path`; returns {index: node id}."""
    from app.music_brain.atlas import graph_schema as gs
    monkeypatch.setenv(gs.ENV_PATH, str(path / "graph.db"))
    conn = gs.open_graph(path)
    ids = {}
    rows = [("Artist A", "Title 1", "en", "t0", None), ("Artist B", "Title 2", "en", "t1", None),
            ("Artist C", "Title 3", "es", None, "vid00000002"), ("Artist D", "Title 4", None, None, "vid00000003")]
    for i, (artist, title, lang, tid, yt) in enumerate(rows):
        ids[i], _ = gs.upsert_node(conn, "song", f"k{i}", 10, artist=artist, title=title, lang=lang, bpm=120.0 + i,
                                   camelot="8A", level=4, track_id=tid, yt_id=yt)
    gs.upsert_edge(conn, ids[0], ids[1], "measured", 0.9, 3, 10)
    gs.upsert_edge(conn, ids[0], ids[2], "yt_related", 0.4, 1, 10)
    gs.upsert_edge(conn, ids[3], ids[0], "played_ok", 0.7, 2, 10)
    gs.meta_set(conn, "rev", 7)
    gs.meta_set(conn, "built_at", 1700000000)
    return ids


@pytest.fixture
def graph(tmp_path, monkeypatch):
    return build_graph(tmp_path, monkeypatch)


def test_feature_off_is_404(env, graph, monkeypatch):  # noqa: F811
    c = _admin(env)
    monkeypatch.delenv("DJ_ADMIN_EMAILS")
    for path in API:
        assert c.get(path).status_code == 404, path


def test_non_admin_403_and_anonymous_401(env, graph):  # noqa: F811
    app, s, *_ = env
    c = _client(app, s, _google_cookie(s, "1002", OTHER))
    for path in API:
        assert c.get(path).status_code == 403, path
    plain, _ = s.store.new_token("laptop")
    for path in API:
        assert _client(app, s, bearer=plain).get(path).status_code == 403, path
    for path in API[:-1]:
        assert _client(app, s).get(path).status_code == 401, path


def test_stats(env, graph):  # noqa: F811
    s = _admin(env).get("/api/admin/graph/stats").json()
    assert s["rev"] == 7 and s["nodes"] == 4 and s["edges"] == 3 and s["edge_kinds"]["measured"] == 1
    assert sorted(s["langs"]) == ["en", "es"]


def test_search_by_name_and_yt_id_then_detail(env, graph):  # noqa: F811
    c = _admin(env)
    hit = c.get("/api/admin/graph/node", params={"q": "artist a"}).json()["results"]
    assert [n["label"] for n in hit] == ["Artist A - Title 1"] and hit[0]["library"] is True
    yt = c.get("/api/admin/graph/node", params={"q": "vid00000003"}).json()["results"]
    assert yt[0]["id"] == graph[3] and yt[0]["library"] is False
    assert c.get("/api/admin/graph/node", params={"q": "nothing like this"}).json() == {"results": []}
    assert c.get("/api/admin/graph/node", params={"q": "  "}).status_code == 422
    d = c.get("/api/admin/graph/node", params={"id": graph[0]}).json()
    assert d["node"]["bpm"] == 120.0 and d["node"]["key"] == "8A" and d["node"]["energy"] == 4
    assert d["edges"]["measured"]["out"] == 1 and d["edges"]["played_ok"]["in"] == 1
    assert c.get("/api/admin/graph/node", params={"id": 9999}).status_code == 404


def test_neighbours_and_filters(env, graph):  # noqa: F811
    c = _admin(env)
    g = c.get("/api/admin/graph/neighbours", params={"id": graph[0]}).json()
    assert g["center"] == graph[0] and len(g["nodes"]) == 4
    assert {e["kind"] for e in g["edges"]} == {"measured", "yt_related", "played_ok"}
    only = c.get("/api/admin/graph/neighbours", params={"id": graph[0], "kinds": "measured,nonsense"}).json()
    assert [e["kind"] for e in only["edges"]] == ["measured"]
    heavy = c.get("/api/admin/graph/neighbours", params={"id": graph[0], "min_weight": 0.5}).json()
    assert {e["kind"] for e in heavy["edges"]} == {"measured", "played_ok"}
    es = c.get("/api/admin/graph/neighbours", params={"id": graph[0], "lang": "es"}).json()
    assert [e["kind"] for e in es["edges"]] == ["yt_related"]
    assert c.get("/api/admin/graph/neighbours", params={"id": 9999}).status_code == 404
    assert c.get("/api/admin/graph/neighbours").status_code == 422


def test_empty_graph(env, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("DJ_GRAPH_DB", str(tmp_path / "none.db"))
    c = _admin(env)
    assert c.get("/api/admin/graph/stats").json() == {"available": False}
    assert c.get("/api/admin/graph/node", params={"q": "a"}).json() == {"results": []}
    assert c.get("/api/admin/graph/neighbours", params={"id": 1}).status_code == 404
    assert not (tmp_path / "none.db").exists()


def test_page_has_scripts_and_nav(env, graph):  # noqa: F811
    c = _admin(env)
    r = c.get("/admin/graph")
    assert r.status_code == 200 and "script-src 'self'" in r.headers["content-security-policy"]
    assert "/vendor/cytoscape.min.js" in r.text and "/admin-graph.js" in r.text and 'href="/admin/graph"' in r.text
    assert 'http-equiv="refresh"' not in r.text and "width=device-width" in r.text


def test_trending_filter_badge_and_history(env, graph):  # noqa: F811
    import time

    from app.music_brain.atlas import graph_schema as gs
    conn = gs.open_graph(None)
    now = int(time.time())
    conn.execute("INSERT INTO trend (node_id, region, rank, seen_at) VALUES (?, 'XX', 3, ?)", (graph[2], now))
    conn.execute("INSERT INTO trend_hist (node_id, region, day, rank) VALUES (?, 'XX', '2026-01-02', 3)", (graph[2],))
    c = _admin(env)
    hit = c.get("/api/admin/graph/node", params={"trending": 1}).json()["results"]
    assert [n["id"] for n in hit] == [graph[2]] and hit[0]["trend"]["rank"] == 3 and hit[0]["trend"]["regions"] == {"XX": 3}
    assert c.get("/api/admin/graph/node", params={"trending": 1, "q": "artist a"}).json() == {"results": []}
    plain = c.get("/api/admin/graph/node", params={"q": "artist c"}).json()["results"]
    assert plain[0]["trend"]["rank"] == 3
    d = c.get("/api/admin/graph/node", params={"id": graph[2]}).json()["node"]
    assert d["trend"]["rank"] == 3 and d["trend_history"] == [{"day": "2026-01-02", "region": "XX", "rank": 3}]
    assert c.get("/api/admin/graph/node", params={"id": graph[0]}).json()["node"]["trend"] is None
    assert c.get("/api/admin/graph/stats").json()["trending"] == 1
    assert "gtrend" in c.get("/admin/graph").text
