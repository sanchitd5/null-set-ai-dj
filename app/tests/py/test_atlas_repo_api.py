"""GET /api/knowledge/version and /snapshot (knowledge_api.py): auth, ETag = the checkout's sha, 304."""
import io
import json
import subprocess
import zipfile

import pytest

from app.music_brain.matching import knowledge as kn
from app.music_brain.matching import knowledge_publish as kp
from app.tests.py.test_knowledge import A, B, src  # noqa: F401 (src: fixture)
from app.tests.py.testclient_compat import TestClient
from app.ui import server


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True, timeout=60).stdout.strip()


@pytest.fixture
def kdir(src, tmp_path, monkeypatch):  # noqa: F811
    """The exported fixture as a git checkout, served as KNOWLEDGE_DIR."""
    d = tmp_path / "atlas_checkout"
    d.mkdir()
    for p in src["out"].rglob("*"):
        if p.is_file():
            t = d / p.relative_to(src["out"])
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_bytes(p.read_bytes())
    git(d, "init", "-q", "-b", "main")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "one")
    monkeypatch.setattr(kn, "KNOWLEDGE_DIR", d)
    kp._snap_cache.clear()
    return d


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def anon():
    return TestClient(server.app, auth=False)


@pytest.mark.parametrize("path", ["/api/knowledge/version", "/api/knowledge/snapshot"])
def test_both_routes_need_auth(anon, kdir, path):
    r = anon.get(path)
    assert r.status_code == 401 and r.json() == {"detail": "unauthorized"}


def test_version_shape(client, kdir):
    sha = git(kdir, "rev-parse", "HEAD")
    v = client.get("/api/knowledge/version").json()
    assert v["sha"] == sha and v["rules_hash"] and v["files"] == 7 and v["dirty"] is False
    assert v["bytes"] == sum(p.stat().st_size for p in kdir.rglob("*") if p.is_file() and ".git" not in p.parts)


def test_snapshot_is_the_phone_asset_layout_with_the_sha_as_etag(client, kdir):
    sha = git(kdir, "rev-parse", "HEAD")
    r = client.get("/api/knowledge/snapshot")
    assert r.status_code == 200 and r.headers["etag"] == f'"{sha}"'
    assert r.headers["content-type"] == "application/zip" and "no-cache" in r.headers["cache-control"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == sorted(["meta.json", "names.json", "learned_techniques.json", "macros.json",
                                           "version.json", f"pairs/{A}.json.gz", f"pairs/{B}.json.gz"])
    macros = json.loads(z.read("macros.json"))["macros"]
    assert sorted(m["name"] for m in macros) == ["chain-1-anyma", "studied-s1-1"]
    assert json.loads(z.read("version.json"))["sha"] == sha
    meta = json.loads(z.read("meta.json"))
    assert meta["shards"] == sorted([A, B]) and meta["rules"] == json.loads(z.read("version.json"))["rules_hash"]
    assert z.read(f"pairs/{A}.json.gz") == (kdir / "atlas" / "pairs" / f"{A}.json.gz").read_bytes()


def test_snapshot_bytes_are_deterministic(client, kdir):
    first = client.get("/api/knowledge/snapshot").content
    kp._snap_cache.clear()
    assert client.get("/api/knowledge/snapshot").content == first


def test_if_none_match_answers_304_without_a_body(client, kdir):
    tag = client.get("/api/knowledge/snapshot").headers["etag"]
    for header in (tag, f"W/{tag}", f'"other", {tag}', "*"):
        r = client.get("/api/knowledge/snapshot", headers={"If-None-Match": header})
        assert r.status_code == 304 and r.content == b"" and r.headers["etag"] == tag
    assert client.get("/api/knowledge/snapshot", headers={"If-None-Match": '"stale"'}).status_code == 200


def test_a_new_commit_changes_the_etag_and_a_dirty_tree_is_told_apart(client, kdir):
    old = client.get("/api/knowledge/snapshot").headers["etag"]
    (kdir / "names.json").write_bytes(kn._dump({A: "Anyma - Eternity", B: "Cassian - SOS", "d" * 16: "New - Song"}))
    dirty = client.get("/api/knowledge/snapshot")
    assert dirty.headers["etag"].startswith(old[:-1] + "+") and client.get("/api/knowledge/version").json()["dirty"]
    git(kdir, "add", "-A")
    git(kdir, "commit", "-qm", "two")
    new = client.get("/api/knowledge/snapshot")
    assert new.headers["etag"] == f'"{git(kdir, "rev-parse", "HEAD")}"' != old
    assert client.get("/api/knowledge/snapshot", headers={"If-None-Match": old}).status_code == 200


def test_no_checkout_still_answers(client, tmp_path, monkeypatch):
    monkeypatch.setattr(kn, "KNOWLEDGE_DIR", tmp_path / "missing")
    assert client.get("/api/knowledge/version").json() == {"sha": "", "rules_hash": None, "files": 0, "bytes": 0,
                                                           "dirty": False}
    assert client.get("/api/knowledge/snapshot").status_code == 404
