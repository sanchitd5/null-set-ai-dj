"""Lyric remixes are performed live, not rendered: the list + settings (lyric_remix_songs.py), the removed render pipeline, the legacy
cache cleanup, and the AI pick (switch, rate limit, only plans whose song(s) passed the gates). Synthetic plans only."""
import gzip
import json

import pytest

from app.music_brain import djcore_bridge, song_store
from app.music_brain.matching import knowledge as kn
from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import lyric_remix_api as lra
from app.ui.services import lyric_remix_songs as lrs

BPM = 120.0


def _doc():
    songs = {k: {"bpm": BPM, "lang": "en", "lines": [{"i": 0, "t": 8.0, "e": 10.0, "beats": 4, "q": 0.9, "text": "x"}]} for k in ("sa", "sb", "sc")}
    plans = [
        {"id": "aaaa00000001", "a": "sa", "b": None, "pieces": [{"s": "a", "i": 0}], "text": "alpha / beta", "score": 0.8},
        {"id": "bbbb00000002", "a": "sa", "b": "sb", "pieces": [{"s": "a", "i": 0}], "text": "gamma / delta", "score": 0.7},
        {"id": "cccc00000003", "a": "sc", "b": None, "pieces": [{"s": "a", "i": 0}], "text": "eps / zeta", "score": 0.9},
    ]
    return {"v": 1, "songs": songs, "plans": plans}


@pytest.fixture
def env(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(song_store, "_cache", lambda: cache)
    know = tmp_path / "know"
    know.mkdir()
    (know / kn.LYRIC_REMIX).write_bytes(gzip.compress(json.dumps(_doc()).encode(), mtime=0))
    monkeypatch.setattr(kn, "KNOWLEDGE_DIR", know)
    have = {"sa", "sb", "sc"}
    monkeypatch.setattr(lrs.Host, "track_path", staticmethod(lambda tid: tmp_path / tid if tid in have else None))
    monkeypatch.setattr(lrs.Host, "name_of", staticmethod(lambda tid: {"sa": "Song A", "sb": "Song B", "sc": "Song C"}.get(tid, tid)))
    lra._cache.update(key=None, doc=None)
    lrs._state.clear()
    return TestClient(server.app)


def _picks(*ids):
    return [{"track_id": i, "artist": "x", "title": i} for i in ids]


def _call(**kw):
    return dict(playing_id="sa", set_id="s1", history_len=10, **kw)


# ------------------------------------------------------------------------------------------------ list, settings, no render


def test_list_has_no_render_state_and_filters(env):
    d = env.get("/api/lyric_remix/songs").json()
    assert d["total"] == 3 and [s["plan_id"] for s in d["songs"]] == ["cccc00000003", "aaaa00000001", "bbbb00000002"]
    s = d["songs"][2]
    assert s["kind"] == "pair" and s["a"] == "sa" and s["b"] == "sb" and s["title"] == "Song A x Song B - lyric remix"
    for k in ("state", "progress", "stage", "error", "duration"):
        assert k not in s
    assert [x["plan_id"] for x in env.get("/api/lyric_remix/songs?q=gamma").json()["songs"]] == ["bbbb00000002"]


def test_render_routes_are_gone(env):
    assert env.post("/api/lyric_remix/songs/aaaa00000001/render").status_code in (404, 405)
    assert env.get("/api/audio/remix/aaaa00000001?q=phone").status_code == 404
    assert env.get("/api/lyric_remix/songs/aaaa00000001").status_code in (404, 405)
    assert env.get("/api/lyric_remix/pickable").status_code in (404, 405)


def test_settings_round_trip_and_clamp(env):
    assert env.get("/api/lyric_remix/settings").json() == {"ai_picks": False, "min_songs": 4, "auto_remix": True}
    assert env.put("/api/lyric_remix/settings", json={"ai_picks": True}).json() == {"ai_picks": True, "min_songs": 4, "auto_remix": True}
    assert env.put("/api/lyric_remix/settings", json={"min_songs": 9}).json() == {"ai_picks": True, "min_songs": 9, "auto_remix": True}
    assert env.put("/api/lyric_remix/settings", json={"auto_remix": False}).json()["auto_remix"] is False
    assert env.put("/api/lyric_remix/settings", json={"auto_remix": True}).json()["auto_remix"] is True
    assert env.put("/api/lyric_remix/settings", json={"min_songs": 999}).json()["min_songs"] == 50
    assert env.get("/api/lyric_remix/songs").json()["ai_picks"] is True


def test_legacy_render_cache_is_removed_and_the_setting_carried_over(env, tmp_path):
    old = song_store.songs_dir() / "_remix"
    (old / "aaaa00000001").mkdir(parents=True)
    (old / "aaaa00000001" / "audio.m4a").write_bytes(b"x" * 10)
    (old / "settings.json").write_text(json.dumps({"ai_picks": True}))
    assert lrs.cleanup_legacy() >= 3
    assert not old.exists()
    assert lrs.get_settings()["ai_picks"] is True
    assert lrs.cleanup_legacy() == 0


# ------------------------------------------------------------------------------------------------ AI picks (dj-core replaced by the real rule's shape)


@pytest.fixture
def core(monkeypatch):
    """dj-core's lyric_remix_pick as a call recorder that applies a tiny stand-in rule (the real rule is tested in dj-core and below)."""
    seen = []

    def call(fn, args=None, timeout=0):
        assert fn == "lyric_remix_pick"
        c = args["ctx"]
        seen.append(c)
        if c["songsSinceRemix"] < c["minSongs"]:
            return None
        for p in c["plans"]:
            song = p["a"] if p["b"] is None else p["b"]
            if song in c["pool"] and (p["b"] is None or p["a"] == c["playingId"]):
                return {"plan_id": p["id"], "kind": "pair" if p["b"] else "within", "song": song, "a": p["a"], "b": p["b"], "text": p["text"], "score": p["score"]}
        return None

    monkeypatch.setattr(djcore_bridge, "call", call)
    return seen


def test_off_by_default_nothing_changes(env, core):
    picks = _picks("sb", "sc")
    assert lrs.attach_pick(picks, **_call()) is None
    assert [p["track_id"] for p in picks] == ["sb", "sc"] and all("lyric_remix" not in p for p in picks)
    assert core == []


def test_on_attaches_to_the_chosen_pick_and_moves_it_first(env, core):
    lrs.set_settings(ai_picks=True)
    picks = _picks("sc", "sb")
    lr = lrs.attach_pick(picks, **_call())
    assert lr["plan_id"] == "bbbb00000002" and lr["kind"] == "pair" and lr["a"] == "sa" and lr["b"] == "sb"
    assert picks[0]["track_id"] == "sb" and picks[0]["lyric_remix"] == lr and "lyric_remix" not in picks[1]
    c = core[0]
    assert c["pool"] == ["sc", "sb"] and c["playingId"] == "sa" and c["enabled"] is True
    assert {p["id"] for p in c["plans"]} == {"aaaa00000001", "bbbb00000002", "cccc00000003"}
    assert c["bpms"]["sb"] == BPM


def test_rate_limit_one_remix_per_min_songs_per_set(env, core):
    lrs.set_settings(ai_picks=True, min_songs=4)
    assert lrs.attach_pick(_picks("sb"), **_call())["plan_id"] == "bbbb00000002"
    assert lrs.attach_pick(_picks("sb"), **dict(_call(), history_len=13)) is None            # 3 songs later
    assert lrs.attach_pick(_picks("sb"), **dict(_call(), history_len=14))["plan_id"]         # 4 songs later
    assert lrs.attach_pick(_picks("sb"), **dict(_call(), set_id="other"))["plan_id"]         # another set has its own count


def test_only_picks_that_passed_the_gates_and_failures_never_raise(env, core, monkeypatch):
    lrs.set_settings(ai_picks=True)
    assert lrs.attach_pick(_picks("zz"), **_call()) is None            # no plan for the picked song
    assert lrs.attach_pick([{"artist": "a", "title": "b"}], **_call()) is None   # a pick without a server track id
    assert lrs.attach_pick(_picks("sb"), **dict(_call(), playing_id="")) is None

    def boom(*a, **k):
        raise djcore_bridge.DjCoreUnavailable("no exe")

    monkeypatch.setattr(djcore_bridge, "call", boom)
    picks = _picks("sb")
    assert lrs.attach_pick(picks, **_call()) is None and "lyric_remix" not in picks[0]


def test_suggest_route_hook(env, core):
    lrs.set_settings(ai_picks=True)
    req = server.AutopilotSuggestRequest(track_id="sa", history=["h"] * 10, set_id="s9")
    res = {"suggestions": _picks("sc", "sb")}
    assert server._attach_lyric_remix(req, res) is res
    assert res["suggestions"][0]["lyric_remix"]["plan_id"] == "bbbb00000002"
    look = server.AutopilotSuggestRequest(track_id="sa", lookahead=True)
    res2 = {"suggestions": _picks("sb")}
    server._attach_lyric_remix(look, res2)
    assert "lyric_remix" not in res2["suggestions"][0]


@pytest.mark.needs_djcore
def test_real_dj_core_rule(env):
    lrs.set_settings(ai_picks=True)
    picks = _picks("sc", "sb")
    lr = lrs.attach_pick(picks, **_call())
    assert lr["plan_id"] == "cccc00000003" and lr["kind"] == "within"        # the best placed song of the picks wins
    assert picks[0]["lyric_remix"] == lr
    assert lrs.attach_pick(_picks("sb"), **dict(_call(), history_len=11)) is None             # rate limit (default 4)


def test_full_song_plan_is_listed_as_full(env):
    doc = _doc()
    doc["plans"].append({"id": "ffff00000004", "a": "sb", "b": None, "kind": "full", "text": "one / two / three", "score": 0.95,
                         "pieces": [{"s": "a", "i": 0, "at": 0}]})
    (kn.KNOWLEDGE_DIR / kn.LYRIC_REMIX).write_bytes(gzip.compress(json.dumps(doc).encode(), mtime=0))
    lra._cache.update(key=None, doc=None)
    d = env.get("/api/lyric_remix/songs").json()
    row = next(s for s in d["songs"] if s["plan_id"] == "ffff00000004")
    assert row["kind"] == "full" and row["b"] is None and row["title"] == "Song B - full lyric remix"
    assert env.get("/api/lyric_remix/has?a=sb").json() == {"within": 1, "pair": 0}      # a full plan counts as a plan of the song: the clock asks dj-core
