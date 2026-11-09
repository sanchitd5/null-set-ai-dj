"""Phone learn sync: POST /api/mobile/learn -> the server's knowledge -> publish to a (temp, bare) dj-atlas.
Synthetic tmp_path caches and real git against temp repos; nothing under the real cache or knowledge/."""
import threading
import time

import pytest

from app.music_brain import user_marks as um
from app.music_brain.atlas import pair_atlas as pa
from app.music_brain.atlas import vetoes as vt
from app.music_brain.learning import liked as lk
from app.music_brain.learning import phone_learn as pl
from app.music_brain.learning.set_log import SCHEMA
from app.music_brain.matching import knowledge_publish as kp
from app.tests.py.test_atlas_repo_publish import env, git, remote_files, shard  # noqa: F401 (env: fixture)
from app.tests.py.test_knowledge import A, B, C, NAMES, src  # noqa: F401 (src: fixture)
from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import replay_api

STEP = {"a": A, "b": B, "a_name": NAMES[A], "b_name": NAMES[B], "recipe": "Bass Swap", "a_time": 120.0, "b_time": 8.0}


def item(n, kind, data, set_id="dev1-1000"):
    return {"id": f"{set_id}:{kind[0]}{n}", "kind": kind, "at": "2026-11-09T20:00:00Z", "data": data}


def trans(n, a=A, b=B, outcome="landed", clean=True, **kw):
    return item(n, "transition", {"a_id": a, "b_id": b, "a_name": NAMES.get(a, ""), "b_name": NAMES.get(b, ""),
                                  "recipe": "Bass Swap", "outcome": outcome, "clean": clean, "hand": False,
                                  "seconds": 32.5, **kw})


def body(items, set_id="dev1-1000", device="dev1"):
    return {"v": 1, "device_id": device, "device_name": "Pixel", "set_id": set_id, "items": items}


def log_payload():
    return {"$schema": SCHEMA,
            "metadata": {"date": "2026-11-09T20:00:00Z", "duration_seconds": 120.0, "total_tracks": 2, "total_transitions": 1},
            "track_quest_journey": [], "transitions": [], "control_event_stream": []}


def played(cache, a=A, b=B):
    return pa.pairs_for(a, cache)[f"{a}>{b}"].get("played")


# ------------------------------------------------------------------------------------- ingest
def test_envelope_is_validated(src):
    c = src["cache"]
    for bad in ({}, {**body([]), "v": 2}, {**body([]), "device_id": "a b"}, {**body([]), "set_id": ""},
                {**body([]), "items": "x"}, body([trans(i) for i in range(pl.MAX_ITEMS + 1)])):
        with pytest.raises(pl.LearnError):
            pl.ingest(bad, c)
    assert pl.ingest(body([]), c)["accepted"] == []


def test_items_are_idempotent_and_bad_ones_rejected_alone(src):
    c = src["cache"]
    first = pl.ingest(body([trans(1), {"id": "bad id!", "kind": "transition", "data": {}},
                            item(2, "nonsense", {}), trans(3, outcome="maybe"),
                            trans(4, a=A, b=A), item(5, "transition", {"a_name": "/home/someone/x.mp3"})]), c)
    assert first["accepted"] == ["dev1-1000:t1"] and first["publish"] is False
    assert {r["why"] for r in first["rejected"]} >= {"bad id", "outcome must be one of ('landed', 'skipped')"}
    assert len(first["rejected"]) == 5
    again = pl.ingest(body([trans(1), trans(6)]), c)
    assert again["duplicate"] == ["dev1-1000:t1"] and again["accepted"] == ["dev1-1000:t6"]
    assert len(um.rows(pl.MARK_PAIR, c)) == 2
    marks = um.rows(pl.MARK_ITEM, c)
    assert marks[0][1]["device_id"] == "dev1" and marks[0][1]["set_id"] == "dev1-1000" and marks[0][1]["kind"] == "transition"
    # the same item id from another device is its own item
    assert pl.ingest(body([trans(1)], device="dev2"), c)["accepted"] == ["dev1-1000:t1"]


def test_private_strings_are_refused(src):
    r = pl.ingest(body([trans(1, a_name="me@example.com"), item(2, "bad_pair", {"a_name": "x", "b_name": "C:\\Users\\me\\y"})]),
                  src["cache"])
    assert r["accepted"] == [] and all("private" in x["why"] for x in r["rejected"])


def test_bad_pair_becomes_an_owner_veto_and_like_a_liked_transition(src):
    c = src["cache"]
    r = pl.ingest(body([item(1, "bad_pair", {"a_name": NAMES[B], "b_name": NAMES[C]}),
                        item(2, "like", {"step": STEP, "note": "great"}),
                        item(3, "like", {"step": {"nope": 1}})]), c, liked_dir=c)
    assert r["accepted"] == ["dev1-1000:b1", "dev1-1000:l2"] and [x["id"] for x in r["rejected"]] == ["dev1-1000:l3"]
    mine = [v for v in vt.load(c, seed=None) if v.get("source") == "phone"]
    assert [(v["a"], v["b"]) for v in mine] == [(NAMES[B], NAMES[C])]
    e = lk.load(c)[f"{A}>{B}"]
    assert e["source"] == "phone" and e["note"] == "great"
    # a repeat is a duplicate; the veto is not stored twice
    assert pl.ingest(body([item(1, "bad_pair", {"a_name": NAMES[B], "b_name": NAMES[C]})]), c)["duplicate"]
    assert len([v for v in vt.load(c, seed=None) if v.get("source") == "phone"]) == 1


def test_bad_pair_by_ids_uses_the_servers_names(src):
    r = pl.ingest(body([item(1, "bad_pair", {"a_id": A, "b_id": C})]), src["cache"], name_of=lambda t: NAMES[t])
    assert r["accepted"] and vt.load(src["cache"], seed=None)[-1]["b"] == NAMES[C]


def test_set_log_goes_through_the_set_log_store_and_asks_for_a_publish(src):
    saved = []

    def save(payload):
        saved.append(payload)
        return {"set_log_id": "abc123"}

    r = pl.ingest(body([item(1, "set_log", log_payload())]), src["cache"], save_set_log=save)
    assert r["accepted"] == ["dev1-1000:s1"] and r["publish"] is True and len(saved) == 1
    assert um.rows(pl.MARK_ITEM, src["cache"])[0][1]["ref"] == "abc123"
    assert pl.ingest(body([item(2, "set_log", log_payload())]), src["cache"])["rejected"][0]["why"] \
        == "set logs are not stored on this server"


# ------------------------------------------------------------------------------------- overlay
def test_overlay_adds_phone_evidence_idempotently(src):
    c = src["cache"]
    before = pa.pairs_for(B, c)[f"{B}>{C}"]
    assert not before.get("played") and before["works"] == 70
    pl.ingest(body([trans(1, B, C), trans(2, B, C, clean=False), trans(3, B, C, outcome="skipped")]), c)
    rep = pl.overlay(c)
    assert rep["changed"] == 1 and rep["unresolved"] == 0
    p = pa.pairs_for(B, c)[f"{B}>{C}"]
    assert p["played"]["count"] == 2 and p["played"]["good"] == 1 and p["played"]["bad"] == 1
    assert p["played"]["phone"] == {"count": 2, "good": 1, "bad": 1} and p["played"]["sources"] == {"phone": 3}
    assert p["works"] == 70 + pa.played_adjust(p["played"]) == 64
    assert pl.overlay(c)["changed"] == 0                       # a repeat changes nothing
    # evidence the pair already had stays; the phone's part is added on top and replaced, not stacked
    pl.ingest(body([trans(4, A, B)]), c)
    pl.overlay(c)
    ab = played(c)
    assert ab["good"] == 3 and ab["count"] == 1 and ab["sessions"] == ["2026-09-28_212853"]
    pl.ingest(body([trans(5, A, B)]), c)
    pl.overlay(c)
    assert played(c)["phone"]["count"] == 2 and played(c)["good"] == 4


def test_a_rebuild_drops_the_overlay_and_overlay_puts_it_back(src):
    c = src["cache"]
    pl.ingest(body([trans(1, B, C)]), c)
    pl.overlay(c)
    atlas = pa.load(c)
    atlas["pairs"][f"{B}>{C}"]["played"] = None               # what a build writes: mine_history knows nothing of the phone
    pa.write_atlas(atlas, pa.atlas_path(c))
    assert not pa.pairs_for(B, c)[f"{B}>{C}"].get("played")
    assert pl.overlay(c)["changed"] == 1 and played(c, B, C)["phone"]["count"] == 1


def test_overlay_resolves_by_name_and_skips_unknown_songs(src):
    c = src["cache"]
    pl.ingest(body([item(1, "transition", {"a_id": "dev0000only", "b_id": "other0000", "a_name": NAMES[B], "b_name": NAMES[C],
                                           "outcome": "landed", "clean": True}),
                    trans(2, "x" * 16, "y" * 16, a_name="Nobody - Nothing", b_name="Nobody - Else")]), c)
    rep = pl.overlay(c)
    assert rep["changed"] == 1 and rep["unresolved"] == 1
    assert played(c, B, C)["good"] == 1


def test_overlay_without_an_atlas_is_quiet(tmp_path):
    assert pl.overlay(tmp_path)["changed"] == 0


# ------------------------------------------------------------------------------------- the route
@pytest.fixture
def client(src, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CACHE_DIR", src["cache"])
    monkeypatch.setattr(replay_api, "REPLAY_CACHE_DIR", src["cache"])
    monkeypatch.setattr(server, "SET_LOGS_CACHE_DIR", tmp_path / "set_logs")
    (tmp_path / "set_logs").mkdir()
    server._set_logs.clear()
    published = []
    monkeypatch.setattr(kp, "on_set_end", lambda delay_s=5.0, log=None: published.append(delay_s) or True)
    monkeypatch.setattr(pl.threading, "Timer", lambda delay, fn, args=(), **kw: _Now(fn, args))
    return TestClient(server.app), published


class _Now:
    """A Timer that runs at start(), on a thread joined by wait()."""
    last = None

    def __init__(self, fn, args):
        self.t = threading.Thread(target=fn, args=args)
        self.daemon = False
        _Now.last = self

    def start(self):
        self.t.start()


def wait_idle():
    for _ in range(200):
        if not pl._state["running"]:
            return
        time.sleep(0.05)
    raise AssertionError("the overlay thread did not finish")


def test_route_ingests_dedups_and_publishes_once_per_set(client, src):
    cl, published = client
    mid = cl.post("/api/mobile/learn", json=body([trans(1, B, C), trans(2, A, B)]))
    assert mid.status_code == 200 and mid.json()["accepted"] == ["dev1-1000:t1", "dev1-1000:t2"] and mid.json()["publish"] is False
    wait_idle()
    assert played(src["cache"], B, C)["phone"]["count"] == 1 and published == []     # evidence now, no commit mid-set
    end = cl.post("/api/mobile/learn", json=body([trans(1, B, C), item(3, "set_log", log_payload())]))
    j = end.json()
    assert j["duplicate"] == ["dev1-1000:t1"] and j["accepted"] == ["dev1-1000:s3"] and j["publish"] is True
    wait_idle()
    assert published == [2.0]
    assert len(list((server.SET_LOGS_CACHE_DIR).glob("*.djset.json"))) == 1
    # resend of the whole batch: all duplicates, nothing scheduled
    again = cl.post("/api/mobile/learn", json=body([trans(1, B, C), item(3, "set_log", log_payload())])).json()
    assert again["accepted"] == [] and len(again["duplicate"]) == 2 and published == [2.0]


def test_route_refuses_bad_requests(client):
    cl, _ = client
    assert cl.post("/api/mobile/learn", json={"v": 9}).status_code == 422
    assert cl.post("/api/mobile/learn", content=b"not json", headers={"content-type": "application/json"}).status_code == 422
    assert cl.post("/api/mobile/learn", json=body([item(1, "set_log", {"pad": "x" * 1_100_000})])).status_code == 413
    bad_log = cl.post("/api/mobile/learn", json=body([item(1, "set_log", {"$schema": "wrong"})])).json()
    assert bad_log["accepted"] == [] and bad_log["rejected"][0]["id"] == "dev1-1000:s1"


def test_route_needs_the_device_token(client):
    cl, _ = client
    bare = TestClient(server.app, headers={"Authorization": "Bearer nope"})
    assert bare.post("/api/mobile/learn", json=body([])).status_code in (401, 403)


# ------------------------------------------------------------------------------- end to end
def test_ingest_to_private_repo_commit(env, monkeypatch):  # noqa: F811
    """ingest -> knowledge (veto, like, overlaid pair) -> publish -> the bare remote has the phone's evidence."""
    cache = env["cache"]
    before = shard(remote_files(env), B)
    assert not before[f"{B}>{C}"].get("played")
    pl.ingest(body([trans(1, B, C), trans(2, B, C), item(3, "like", {"step": STEP})]), cache, liked_dir=cache)
    rep = kp.publish(cache, env["server"])
    assert rep["status"] == "pushed", rep
    remote = shard(remote_files(env), B)[f"{B}>{C}"]
    assert remote["played"]["phone"] == {"count": 2, "good": 2, "bad": 0} and remote["works"] == 70 + pa.played_adjust(remote["played"])
    assert "sessions" not in remote["played"]
    import gzip
    blob = b"".join(gzip.decompress(v) if k.endswith(".gz") else v for k, v in remote_files(env).items())
    assert b"dev1" not in blob and b"Pixel" not in blob and b"phone_pair" not in blob     # no provenance in the repo
    # a second publish with nothing new is a no-op (the overlay is stable)
    assert kp.publish(cache, env["server"])["status"] == "noop"
    # a build that drops the overlay does not lose it from the next publish
    atlas = pa.load(cache)
    atlas["pairs"][f"{B}>{C}"]["played"] = None
    pa.write_atlas(atlas, pa.atlas_path(cache))
    assert kp.publish(cache, env["server"])["status"] == "noop"
