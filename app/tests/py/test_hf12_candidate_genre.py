"""HF12: a candidate's genre is resolved by the owner rule (dj-core resolve_genre) from the artist's library / graph labels,
and the phone's relax id rides through the ladder. Fakes only, the dj-core service answers the decisions."""
import time

import pytest

from app.ui import server
from app.ui.services import suggest_ladder as sl

pytestmark = pytest.mark.needs_djcore


@pytest.fixture
def lib(monkeypatch):
    names = {"t1": "Kodak Black - Song One", "t2": "Kodak Black - Song Two", "t3": "Anyma - Song Three"}
    labels = {"t1": "hip hop", "t2": "hip hop", "t3": "melodic techno"}
    monkeypatch.setattr(server, "_tracks", {k: object() for k in names})
    monkeypatch.setattr(server, "_track_names", names)
    monkeypatch.setattr(server, "_track_vibe", lambda tid: {"genre": labels.get(tid), "era": None})
    monkeypatch.setattr(server, "_graph_artist_genres", lambda: {})
    server._artist_genre_cache.clear()


def test_artist_in_library_gets_its_majority_genre(lib):
    assert server._artist_genre_of({"artist": "Kodak Black", "title": "Unseen"}) == "hip hop"
    assert server._artist_genre_of({"artist": "Anyma", "title": "Unseen"}) == "melodic techno"


def test_credit_order_primary_artist_decides(lib):
    assert server._artist_genre_of({"artist": "Kodak Black & Anyma", "title": "x"}) == "hip hop"


def test_unknown_artist_is_unknown_and_the_llm_label_never_counts(lib):
    assert server._artist_genre_of({"artist": "Nobody", "title": "x", "genre": "hip hop"}) == ""


def test_graph_genre_beats_library_majority(lib, monkeypatch):
    monkeypatch.setattr(server, "_graph_artist_genres", lambda: {"nobody": "house"})
    assert server._artist_genre_of({"artist": "Nobody", "title": "x"}) == "house"


def run(**kw):
    base = dict(deadline_at=time.monotonic() + 10, hard_played=[], earlier=[], playing_id="p", library=[], neighbours=[],
                cur_genre="hip hop", genre_of=server._genre_of_row, family_jump=lambda a, b: a != b, lang_check=None,
                graph_picks=lambda: [], yt_rows=lambda rem: [], yt_pick=lambda rows: [], reason="t", llm=None)
    base.update(kw)
    return sl.ladder(**base)


def test_llm_pick_by_a_library_artist_passes_a_pinned_scene_with_its_label(lib):
    llm = [{"artist": "Kodak Black", "title": "Unseen", "genre": ""}]
    r = run(llm=lambda rem: llm, set_state={"scene_genre": "hip hop"})
    assert r["pick_source"] == "llm" and r["suggestions"][0]["genre"] == "hip hop"


def test_known_off_scene_artist_is_still_refused_even_relaxed(lib):
    llm = [{"artist": "Anyma", "title": "Unseen"}]
    r = run(llm=lambda rem: llm, set_state={"scene_genre": "hip hop", "relax": ["scene_unlabelled"]})
    assert r["pick_source"] != "llm"


def test_unlabelled_pick_waits_until_the_phone_relaxes_then_lands(lib):
    llm = [{"artist": "Nobody", "title": "Unseen"}]
    strict = run(llm=lambda rem: llm, set_state={"scene_genre": "hip hop"})
    assert strict["pick_source"] == "none"
    relaxed = run(llm=lambda rem: llm, set_state={"scene_genre": "hip hop", "relax": ["scene_unlabelled"]})
    assert relaxed["pick_source"] == "llm" and relaxed["suggestions"][0]["title"] == "Unseen"
