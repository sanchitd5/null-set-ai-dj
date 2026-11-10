"""GET /api/graph/song (graph_api.py, G21): auth, graph hit, sparse node filled to 12 songs, unknown song gets a stub
centre with YouTube related neighbours, never a 5xx."""
import pytest

from app.music_brain import atlas_bridge
from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import yt_related

needs_builder = pytest.mark.skipif(not atlas_bridge.AVAILABLE, reason=atlas_bridge.SKIP_REASON)


@pytest.fixture
def client():
    return TestClient(server.app)


def _yt_rows(n, start=0):
    return [{"artist": f"Yt Artist {i}", "title": f"Yt Song {i}", "source": "yt", "yt_id": f"VID{i:08d}", "rank": i}
            for i in range(start, start + n)]


@pytest.fixture
def yt(monkeypatch):
    calls = []

    def fake(vid, seed=None, timeout=0.0):
        calls.append(vid)
        return (_yt_rows(15), {"source": "fetch", "n": 15}) if vid else ([], {"source": "no_id", "n": 0})

    monkeypatch.setattr(yt_related, "for_video", fake)
    monkeypatch.setattr(yt_related, "video_id", lambda tid, g=None: "SEEDSEEDSE0")
    return calls


def test_needs_auth():
    assert TestClient(server.app, auth=False).get("/api/graph/song?yt=x").status_code in (401, 403)


def test_no_identity_is_422(client):
    assert client.get("/api/graph/song").status_code == 422


def test_without_atlas_unknown_song_is_a_stub_with_youtube_neighbours(client, yt, monkeypatch):
    monkeypatch.setattr(atlas_bridge, "AVAILABLE", False)
    j = client.get("/api/graph/song?yt=SEEDSEEDSE0&artist=Seed+Artist&title=Seed+Song").json()
    assert j["source"] == "stub" and j["center"] == -1 and j["nodes"][0]["stub"] is True
    assert len(j["nodes"]) - 1 >= 12 and all(e["kind"] == "yt_related" and e["a"] == -1 for e in j["edges"])
    assert len({n["id"] for n in j["nodes"]}) == len(j["nodes"])


def test_never_a_500(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(yt_related, "for_video", boom)
    monkeypatch.setattr(atlas_bridge, "graph_view", boom)
    r = client.get("/api/graph/song?yt=abc&title=T")
    assert r.status_code == 200 and r.json()["source"] == "stub" and r.json()["nodes"][0]["stub"]


@pytest.fixture
def graph(tmp_path, monkeypatch):
    pytest.importorskip("app.music_brain.atlas.graph_store", reason="builder checkout has no graph store")
    from app.music_brain.atlas import graph_schema as gs
    from app.music_brain.atlas import graph_store
    monkeypatch.setenv(gs.ENV_PATH, str(tmp_path / "graph.db"))
    graph_store._cache.clear()
    conn = gs.open_graph()
    gs.meta_set(conn, "rev", 1)
    return gs, conn


@needs_builder
def test_sparse_node_is_filled_to_twelve_songs(client, graph, yt):
    gs, conn = graph
    from app.ui.services import pick_pool
    centre, _ = gs.upsert_node(conn, "song", pick_pool.key_of("Core Artist", "Core Song"), artist="Core Artist",
                               title="Core Song", yt_id="CENTREVID01")
    mate, _ = gs.upsert_node(conn, "song", pick_pool.key_of("Mate Artist", "Mate Song"), artist="Mate Artist",
                             title="Mate Song")
    gs.upsert_edge(conn, centre, mate, "measured", 0.6)
    scene, _ = gs.upsert_node(conn, "scene", "synthetic scene")
    gs.upsert_edge(conn, centre, scene, "in_scene", 1.0)
    for i in range(4):                                           # same-scene songs reached through the scene node
        n, _ = gs.upsert_node(conn, "song", pick_pool.key_of(f"Scene Artist {i}", f"Scene Song {i}"),
                              artist=f"Scene Artist {i}", title=f"Scene Song {i}")
        gs.upsert_edge(conn, n, scene, "in_scene", 1.0)
    conn.commit()
    j = client.get("/api/graph/song?yt=CENTREVID01").json()
    songs = [n for n in j["nodes"] if n["kind"] == "song" and n["id"] != j["center"]]
    assert j["source"] == "graph" and j["center"] == centre and len(songs) >= 12
    assert any(n["label"].startswith("Scene Artist") for n in songs)      # hop-2 through the scene node
    assert any(n["id"] < -1 for n in songs)                               # topped up with YouTube related
    assert len({n["id"] for n in j["nodes"]}) == len(j["nodes"])


@needs_builder
def test_dense_node_makes_no_youtube_call(client, graph, yt):
    gs, conn = graph
    from app.ui.services import pick_pool
    centre, _ = gs.upsert_node(conn, "song", pick_pool.key_of("Core Artist", "Core Song"), artist="Core Artist",
                               title="Core Song")
    for i in range(14):
        n, _ = gs.upsert_node(conn, "song", pick_pool.key_of(f"A{i}", f"S{i}"), artist=f"A{i}", title=f"S{i}")
        gs.upsert_edge(conn, centre, n, "measured", 0.5)
    conn.commit()
    j = client.get("/api/graph/song?artist=Core+Artist&title=Core+Song").json()
    assert j["source"] == "graph" and len(j["nodes"]) == 13 and not yt


@needs_builder
def test_unknown_song_in_a_graph_is_a_stub(client, graph, yt):
    j = client.get("/api/graph/song?yt=NOTINGRAPH1&artist=Nobody&title=Nothing").json()
    assert j["source"] == "stub" and len(j["nodes"]) - 1 >= 12


# ------------------------------------------------------------------------------------------------ veto
@pytest.fixture
def marks(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE_DIR", tmp_path)
    return tmp_path


_VETO = {"set_id": "set1", "from_artist": "Aa", "from_title": "One", "to_artist": "Bb", "to_title": "Two"}


def test_veto_needs_auth():
    assert TestClient(server.app, auth=False).post("/api/graph/veto", json=_VETO).status_code in (401, 403)


def test_veto_records_skipped_edge_and_set_list(client, marks):
    from app.music_brain import user_marks as um
    from app.music_brain.learning import phone_learn as pl
    from app.ui.services import pick_pool

    r = client.post("/api/graph/veto", json=_VETO).json()
    assert r["ok"] and r["added"] and r["vetoed"] == [pick_pool.key_of("Bb", "Two")]
    rows = [d for _, d in um.rows(pl.MARK_PAIR, marks)]
    assert len(rows) == 1 and rows[0]["outcome"] == "skipped" and rows[0]["a_name"] == "Aa - One" \
        and rows[0]["b_name"] == "Bb - Two" and rows[0]["set"] == "set1"
    again = client.post("/api/graph/veto", json=_VETO).json()                  # idempotent
    assert again["added"] is False and again["vetoed"] == r["vetoed"]
    assert len(um.rows(pl.MARK_PAIR, marks)) == 1
    other = client.post("/api/graph/veto", json={**_VETO, "set_id": "set2"}).json()
    assert len(other["vetoed"]) == 1                                            # per set


def test_veto_bad_input_is_422(client, marks):
    assert client.post("/api/graph/veto", json={**_VETO, "set_id": "a b"}).status_code == 422
    assert client.post("/api/graph/veto", json={**_VETO, "to_title": ""}).status_code == 422
    assert client.post("/api/graph/veto", json={**_VETO, "to_artist": "Aa", "to_title": "One"}).status_code == 422
