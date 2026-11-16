"""Lyric remix server side: the data file travels with the knowledge (sync file list, snapshot zip), and
/api/lyric_remix/plan + /has (lyric_remix_api.py) build dj-core's ctx from it. The data is synthetic and written at run time
(no .json.gz is tracked in this public repo)."""
import gzip
import io
import json
import zipfile

import pytest

from app.music_brain import djcore_bridge
from app.music_brain.matching import knowledge as kn
from app.music_brain.matching import knowledge_publish as kp
from app.music_brain.matching import knowledge_sync as ks
from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import lyric_remix_api as lra


def _line(i, t, beats=4):
    return {"i": i, "t": t, "e": t + beats * 0.5, "beats": beats, "q": 0.9, "text": f"line {i}"}


def _doc():
    songs = {k: {"bpm": 120.0, "lang": "en", "lines": [_line(0, 8.0), _line(1, 12.0), _line(2, 16.0)]} for k in ("s1", "s2", "s3")}
    plan = lambda pid, a, b: {"id": pid, "a": a, "b": b, "pieces": [{"s": "a", "i": 1}, {"s": "a" if b is None else "b", "i": 0}],  # noqa: E731
                              "text": "alpha / beta", "score": 0.8, "why": "synthetic"}
    return {"v": 1, "built": 1, "rules": "r", "songs": songs,
            "plans": [plan("p1", "s1", None), plan("p2", "s1", None), plan("p3", "s1", "s2"), plan("p4", "s2", "s3"), plan("p5", "s3", None)]}


def _write(d, doc):
    (d / kn.LYRIC_REMIX).write_bytes(gzip.compress(json.dumps(doc).encode(), mtime=0))


@pytest.fixture
def kdir(tmp_path, monkeypatch):
    d = tmp_path / "know"
    d.mkdir()
    (d / "names.json").write_text("{}")
    monkeypatch.setattr(kn, "KNOWLEDGE_DIR", d)
    kp._snap_cache.clear()
    return d


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def bridge(monkeypatch):
    seen = []

    def fake(fn, args=None, timeout=5.0):
        seen.append((fn, args))
        return {"ok": False, "gate": "no_plan", "reason": "fake"}
    monkeypatch.setattr(djcore_bridge, "call", fake)
    return seen


# ---------------------------------------------------------------------------------- knowledge plumbing
def test_sync_lists_the_data_file_only_when_present(kdir):
    assert kn.LYRIC_REMIX not in ks._files(kdir)
    _write(kdir, _doc())
    assert kn.LYRIC_REMIX in ks._files(kdir)


def test_snapshot_zip_carries_the_data_file_under_its_name(kdir):
    _write(kdir, _doc())
    _, data = kp.snapshot_zip(kdir)
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.read("lyric_remix.json.gz") == (kdir / kn.LYRIC_REMIX).read_bytes()
    assert "version.json" in z.namelist()


def test_snapshot_zip_without_the_file_has_none(kdir):
    _, data = kp.snapshot_zip(kdir)
    assert "lyric_remix.json.gz" not in zipfile.ZipFile(io.BytesIO(data)).namelist()


def test_the_file_is_a_remote_wins_kind_and_the_privacy_scan_reads_it(kdir):
    assert kp._kind(kn.LYRIC_REMIX) == "other"
    raw = (lambda: (_write(kdir, _doc()), (kdir / kn.LYRIC_REMIX).read_bytes())[1])()
    assert kp.privacy_scan({kn.LYRIC_REMIX: raw}) == []
    ctx = kp._Ctx(set(), {})
    assert kp._merge_file(kn.LYRIC_REMIX, b"base", b"mine", b"theirs", ctx) == b"theirs"
    assert kp._merge_file(kn.LYRIC_REMIX, b"base", b"mine", b"base", ctx) == b"mine"


# ---------------------------------------------------------------------------------- the API
def test_load_missing_or_corrupt_is_empty_and_cached_by_mtime(kdir):
    assert lra.load() == {"songs": {}, "plans": []}
    (kdir / kn.LYRIC_REMIX).write_bytes(b"not gzip")
    assert lra.load() == {"songs": {}, "plans": []}
    _write(kdir, _doc())
    d1 = lra.load()
    assert len(d1["plans"]) == 5 and lra.load() is d1
    doc = _doc()
    doc["plans"] = doc["plans"][:2]
    _write(kdir, doc)
    assert len(lra.load()["plans"]) == 2


def test_plan_builds_the_within_ctx(client, kdir, bridge):
    _write(kdir, _doc())
    r = client.post("/api/lyric_remix/plan", json={"a": "s1", "b": None, "ctx": {"pos": 10.0, "mode": "song"}})
    assert r.status_code == 200 and r.json() == {"ok": False, "gate": "no_plan", "reason": "fake"}
    fn, args = bridge[0]
    c = args["ctx"]
    assert fn == "lyric_remix_plan" and c["aId"] == "s1" and c["bId"] is None and c["pos"] == 10.0
    assert sorted(c["songs"]) == ["s1"] and sorted(p["id"] for p in c["plans"]) == ["p1", "p2"]


def test_plan_builds_the_cross_ctx(client, kdir, bridge):
    _write(kdir, _doc())
    client.post("/api/lyric_remix/plan", json={"a": "s1", "b": "s2", "ctx": {"mode": "transition"}})
    c = bridge[0][1]["ctx"]
    assert c["bId"] == "s2" and sorted(c["songs"]) == ["s1", "s2"] and [p["id"] for p in c["plans"]] == ["p3"]


def test_the_server_owns_the_ids_songs_and_plans(client, kdir, bridge):
    _write(kdir, _doc())
    client.post("/api/lyric_remix/plan", json={"a": "s1", "ctx": {"aId": "evil", "songs": {"x": 1}, "plans": [{"id": "x"}]}})
    c = bridge[0][1]["ctx"]
    assert c["aId"] == "s1" and "x" not in c["songs"] and all(p["id"] != "x" for p in c["plans"])


def test_a_plan_is_passed_through_unchanged(client, kdir, monkeypatch):
    _write(kdir, _doc())
    plan = {"ok": True, "kind": "lyric_remix", "plan_id": "p1", "slices": [{"from": 1.0, "dur": 2.0, "off": 0.0, "gain": 1, "src": "self"}]}
    monkeypatch.setattr(djcore_bridge, "call", lambda fn, args=None, timeout=5.0: plan)
    assert client.post("/api/lyric_remix/plan", json={"a": "s1", "ctx": {}}).json() == plan


def test_unavailable_bridge_is_a_200_refusal(client, kdir, monkeypatch):
    def boom(*a, **k):
        raise djcore_bridge.DjCoreUnavailable("no exe")
    monkeypatch.setattr(djcore_bridge, "call", boom)
    r = client.post("/api/lyric_remix/plan", json={"a": "s1", "ctx": {}})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["gate"] == "unavailable"


@pytest.mark.parametrize("body", [
    {"a": "../x", "ctx": {}}, {"a": "s1", "b": "a b", "ctx": {}}, {"a": "", "ctx": {}},
    {"a": "s1", "ctx": "nope"}, {"a": "s1"}, {"ctx": {}}, {"a": "s1", "ctx": {"big": "x" * (lra.MAX_CTX_BYTES + 1)}},
])
def test_bad_bodies_are_422(client, kdir, bridge, body):
    assert client.post("/api/lyric_remix/plan", json=body).status_code == 422
    assert bridge == []


def test_has_counts(client, kdir):
    _write(kdir, _doc())
    assert client.get("/api/lyric_remix/has", params={"a": "s1", "b": ""}).json() == {"within": 2, "pair": 0}
    assert client.get("/api/lyric_remix/has", params={"a": "s1", "b": "s2"}).json() == {"within": 2, "pair": 1}
    assert client.get("/api/lyric_remix/has", params={"a": "s2", "b": "s1"}).json() == {"within": 0, "pair": 0}
    assert client.get("/api/lyric_remix/has", params={"a": "zz"}).json() == {"within": 0, "pair": 0}
    assert client.get("/api/lyric_remix/has", params={"a": "bad id"}).status_code == 422


@pytest.mark.needs_djcore
def test_round_trip_through_the_real_dj_core(client, kdir):
    """Needs a dj-core-svc with lyric_remix_plan (DJ_CORE_SVC); a song without data is the no_plan refusal."""
    r = client.post("/api/lyric_remix/plan", json={"a": "s1", "ctx": {"mode": "song"}})
    assert r.status_code == 200 and r.json()["ok"] is False
