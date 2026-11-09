"""Grounded picks: the model answers a candidate id; invented songs are rejected before any lookup."""
import app.ui.services.autopilot_service as svc
from app.ui.services import pick_pool as pp


def _pool():
    cat = [{"artist": f"Artist {i}", "title": f"Song {i}", "source": "catalogue"} for i in range(1, 6)]
    atl = [{"artist": "Lib Artist", "title": "Lib Song", "source": "library", "track_id": "t9"}]
    return pp.build_pool(atlas=atl, cat=cat, exclude=["X - Song 2"], seed_title="Seed")


def test_pool_numbers_and_excludes():
    pool = _pool()
    assert [c["cid"] for c in pool] == [f"C{i}" for i in range(1, len(pool) + 1)]
    assert pool[0]["source"] == "library"
    assert not any(c["title"] == "Song 2" for c in pool)


def test_ground_by_cid_and_exact_name_rejects_free_text():
    pool = _pool()
    picks = [{"cid": "C1", "artist": "wrong", "title": "wrong"},
             {"artist": "Artist 3", "title": "Song 3"},
             {"artist": "Invented", "title": "Nonexistent"},
             {"cid": "C99", "artist": "Invented", "title": "Two"}]
    ok, free = pp.ground(picks, pool)
    assert [(p["artist"], p["title"]) for p in ok] == [("Lib Artist", "Lib Song"), ("Artist 3", "Song 3")]
    assert ok[0]["track_id"] == "t9"
    assert len(free) == 2


def test_ground_same_candidate_once():
    pool = _pool()
    ok, free = pp.ground([{"cid": "C1"}, {"cid": "C1"}], pool)
    assert len(ok) == 1 and len(free) == 1


def test_prompt_block_lists_ids():
    txt = pp.prompt_block(_pool())
    assert "C1 | Lib Artist - Lib Song" in txt and "cid" in txt


def test_catalogue_cached_and_search_failure_not_cached(monkeypatch):
    pp._catalogue.clear()
    calls = []

    def search(q, n):
        calls.append(q)
        return [{"title": "Some Band - Some Tune", "channel": "Some Band", "duration": 200}]
    a = pp.catalogue("seed1", ["q"], search)
    b = pp.catalogue("seed1", ["q"], search)
    assert a == b and len(calls) == 1

    def boom(q, n):
        raise RuntimeError("cooling")
    assert pp.catalogue("seed2", ["q"], boom) == []
    assert "seed2" not in pp._catalogue


def test_atlas_neighbours_names(monkeypatch):
    from app.music_brain.atlas import atlas_store
    monkeypatch.setattr(atlas_store, "pairs_for", lambda a, c=None, path=None: {
        "a>b1": {"score": 5}, "a>b2": {"score": 9}, "a>b3": {"score": 1}})
    names = {"b1": "One Act - First", "b2": "Two Act - Second"}
    out = pp.atlas_neighbours("a", names, None)
    assert [o["track_id"] for o in out] == ["b2", "b1"]


def test_suggest_rejects_free_text_before_lookup(monkeypatch):
    looked = []
    monkeypatch.setattr(svc, "_verify_picks", lambda picks: (looked.append(list(picks)) or (picks, [])))
    replies = iter(['{"suggestions":[{"artist":"Invented","title":"Nope","reason":"x"}]}',
                    '{"suggestions":[{"cid":"C2","artist":"x","title":"y","reason":"x"}]}'])
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: next(replies))
    monkeypatch.setattr(svc, "_filter_suggestions", lambda data, *a, **k: [
        x for x in data["suggestions"]])
    pool = _pool()
    out = svc.suggest_next_tracks("Seed", "Art", 124.0, "8A", 200.0, 0.5, "", [], candidates=pool)
    assert [(s["artist"], s["title"]) for s in out] == [(pool[1]["artist"], pool[1]["title"])]
    assert all(p["title"] != "Nope" for batch in looked for p in batch)
