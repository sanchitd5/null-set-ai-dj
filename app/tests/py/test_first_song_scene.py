"""SCENEPIN: the set's scene is the first song's, answered by the vet endpoint with dj-core's first_song_scene rule (the song's
label, else the artist's genre from the library / graph, else open). Fakes only; the dj-core service decides."""
import pytest
from app.tests.py.testclient_compat import TestClient

from app.ui import server

pytestmark = pytest.mark.needs_djcore


@pytest.fixture
def lib(monkeypatch):
    names = {"t1": "Anyma - Song One", "t2": "Anyma - Song Two", "t3": "Nobody - Song Three"}
    labels = {"t1": None, "t2": "melodic techno", "t3": None, "t4": "house"}
    monkeypatch.setattr(server, "_tracks", {k: object() for k in names})
    monkeypatch.setattr(server, "_track_names", names)
    monkeypatch.setattr(server, "_track_vibe", lambda tid: {"genre": labels.get(tid), "era": None})
    monkeypatch.setattr(server, "_vibe_by_name", lambda name: {"genre": "pop" if "Pop" in name else None, "era": None})
    monkeypatch.setattr(server, "_graph_artist_genres", lambda: {})
    server._artist_genre_cache.clear()
    return TestClient(server.app)


def vet(c, **kw):
    return c.post("/api/autopilot/vet", json={"cands": [], "pin_set": True, **kw}).json()


def test_labelled_first_song_is_the_scene(lib):
    j = vet(lib, a_id="t2")
    assert j["scene_genre"] == "melodic techno" and j["scene_source"] == "label"
    assert j["scene_families"]


def test_unlabelled_first_song_takes_its_artists_genre(lib):
    j = vet(lib, a_id="t1")          # no label of its own; Anyma's other library song is melodic techno
    assert j["scene_genre"] == "melodic techno" and j["scene_source"] == "library"


def test_unknown_first_song_leaves_the_scene_open(lib):
    j = vet(lib, a_id="t3")
    assert j["scene_genre"] is None and j["scene_families"] == []


def test_first_pick_is_judged_against_the_first_song_scene(lib):
    # the pin comes from the first song at once: a candidate of another family is refused on this very first vet
    r = lib.post("/api/autopilot/vet", json={"a_id": "t2", "pin_set": True, "cands": [{"name": "Some Pop Artist - Song"}]}).json()
    assert r["scene_genre"] == "melodic techno"
    assert r["results"][0]["ok"] is False and r["results"][0]["gate"] == "set_scene"


def test_owner_pin_is_kept(lib):
    j = vet(lib, a_id="t2", set_genre="house", pin_set=False)
    assert "scene_genre" not in j          # an explicit scene is never re-derived
