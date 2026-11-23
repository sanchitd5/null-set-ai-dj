"""The console's per-track caches stay bounded, and the memory watch reports / warns."""
import pytest

from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import mem_watch
from app.ui.services.bounded_cache import BoundedCache


def test_bounded_cache_is_lru_and_dict_like():
    c = BoundedCache(3)
    for i in range(3):
        c[i] = i
    assert c.get(0) == 0                      # touch 0: 1 is now the oldest
    c[3] = 3
    assert 1 not in c and 0 in c and len(c) == 3
    assert c.get(1, "x") == "x" and c.pop(2) == 2 and c.pop(2, None) is None
    with pytest.raises(KeyError):
        c[99]
    c.clear()
    assert len(c) == 0
    with pytest.raises(ValueError):
        BoundedCache(0)


def test_bounded_cache_setdefault_like_dict():
    # the stem prep for a pick calls _stem_pick_at.setdefault (it raised AttributeError, so no pick ever started a split)
    c = BoundedCache(2)
    assert c.setdefault("a", 1) == 1 and c.setdefault("a", 9) == 1
    c.setdefault("b", 2)
    c.setdefault("c", 3)                      # past maxsize: the oldest ("a") goes
    assert "a" not in c and c.get("c") == 3 and len(c) == 2


def test_stem_pick_cache_supports_setdefault():
    assert server._stem_pick_at.setdefault("t-x", 1.0) == 1.0
    server._stem_pick_at.pop("t-x", None)


@pytest.mark.parametrize("name", ["_stem_cache", "_vocal_entry_cache", "_voiced_cache", "_hook_drop_cache", "_pair_cache"])
def test_server_caches_stay_bounded(name):
    cache = getattr(server, name)
    cap = cache.maxsize if isinstance(cache, BoundedCache) else 64
    assert isinstance(cache, BoundedCache) or name == "_pair_cache"
    if isinstance(cache, BoundedCache):
        saved = dict(cache._d)
        try:
            for i in range(cap * 3):
                cache[f"t{i}"] = {"x": i}
            assert len(cache) == cap
        finally:
            cache.clear()
            for k, v in saved.items():
                cache[k] = v


def test_lyrics_cache_is_bounded():
    from app.ui.services import mobile_api

    assert isinstance(mobile_api._lyrics_cache, BoundedCache) and mobile_api._lyrics_cache.maxsize <= 1024


def test_memory_watch_warns_once_per_step(monkeypatch):
    monkeypatch.setenv("DJ_RSS_WARN_MB", "100")
    monkeypatch.setitem(mem_watch._state, "last_warn_at", 0.0)
    monkeypatch.setitem(mem_watch._state, "last_warn_mb", 0.0)
    mb = [150.0]
    monkeypatch.setattr(mem_watch, "rss_mb", lambda: mb[0])
    seen = []
    assert mem_watch.check(seen.append, now=1000.0) and len(seen) == 1 and "150 MB" in seen[0]
    mem_watch.check(seen.append, now=1010.0)
    assert len(seen) == 1                      # same size, 10 s later: quiet
    mb[0] = 700.0
    mem_watch.check(seen.append, now=1020.0)
    assert len(seen) == 2                      # +512 MB: warns again
    mb[0] = 50.0
    assert mem_watch.check(seen.append, now=3000.0) is False and len(seen) == 2


def test_health_stays_bare_and_api_health_has_the_memory_line():
    client = TestClient(server.app)
    anon = TestClient(server.app, auth=False)
    assert anon.get("/health").json() == {"ok": True}
    r = client.get("/api/health").json()
    assert r["ok"] and r["memory"]["rss_mb"] > 0 and r["memory"]["warn_mb"] > 0
    assert set(r["caches"]) == {"stems", "pairs", "hook_drop", "voiced", "vocal_entry"}
