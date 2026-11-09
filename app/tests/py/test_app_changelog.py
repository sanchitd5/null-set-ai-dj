"""GET /api/app/changelog and deploy/changelog.py (release-notes history, seed, push_apk.sh hook)."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from app.tests.py.testclient_compat import TestClient
from app.ui import server

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("changelog_tool", ROOT / "deploy" / "changelog.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

E5 = {"version_name": "1.0.0-parity.3", "version_code": 5, "published_at": "2026-11-07T15:54:40Z", "notes": "a"}
E7 = {"version_name": "1.0.0-parity.5", "version_code": 7, "published_at": "2026-11-07T20:58:06Z", "notes": "b"}


@pytest.fixture
def log(tmp_path, monkeypatch):
    p = tmp_path / "changelog.json"
    monkeypatch.setattr(server, "ANDROID_CHANGELOG", p)
    return p


def test_needs_auth():
    assert TestClient(server.app, auth=False).get("/api/app/changelog").status_code == 401


def test_404_without_file_or_on_garbage(log):
    c = TestClient(server.app)
    assert c.get("/api/app/changelog").status_code == 404
    log.write_text("{nope")
    assert c.get("/api/app/changelog").status_code == 404


def test_sorted_newest_first_cleaned_and_cacheable(log):
    log.write_text(json.dumps([E5, {**E7, "secret": "x", "withdrawn": True}, {"version_code": "x"}, "junk",
                               {**E5, "version_code": True}]))
    r = TestClient(server.app).get("/api/app/changelog")
    assert r.status_code == 200 and "max-age" in r.headers["cache-control"]
    assert r.json() == {"entries": [{**E7, "withdrawn": True}, E5]}


def test_not_a_list_gives_empty(log):
    log.write_text(json.dumps({"a": 1}))
    assert TestClient(server.app).get("/api/app/changelog").json() == {"entries": []}


def test_add_is_idempotent_and_keeps_older(tmp_path):
    meta = tmp_path / "null-set-dj.json"
    meta.write_text(json.dumps({**E7, "sha256": "ab", "size": 1}))
    tool.save(str(tmp_path), [E5])
    assert tool.add(str(tmp_path)) == 2
    meta.write_text(json.dumps({**E7, "notes": "b2"}))
    assert tool.add(str(tmp_path)) == 2   # same build again: replaced, not duplicated
    got = tool.load(str(tmp_path))
    assert [e["version_code"] for e in got] == [7, 5] and got[0]["notes"] == "b2" and got[1] == E5


def test_add_keeps_withdrawn_flag(tmp_path):
    tool.save(str(tmp_path), [{**E7, "withdrawn": True}])
    (tmp_path / "null-set-dj.json").write_text(json.dumps(E7))
    tool.add(str(tmp_path))
    assert tool.load(str(tmp_path))[0]["withdrawn"] is True


def test_seed_only_fills_missing(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([E5, {**E7, "notes": "seed"}, {"bad": 1}]))
    tool.save(str(tmp_path), [E7])
    assert tool.seed(str(tmp_path), str(seed)) == 1
    got = {e["version_code"]: e for e in tool.load(str(tmp_path))}
    assert got[7]["notes"] == "b" and got[5] == E5
    assert tool.seed(str(tmp_path), str(seed)) == 0


def test_shipped_seed_is_valid_and_complete():
    seed = json.loads((ROOT / "deploy" / "changelog_seed.json").read_text())
    assert [e["version_code"] for e in seed] == list(range(5, 25))
    assert all(tool.clean(e) == e for e in seed)
    assert [e["version_name"] for e in seed] == [f"1.0.0-parity.{n}" for n in range(3, 23)]
    assert [e["version_code"] for e in seed if e.get("withdrawn")] == [18]
    assert all(len(e["notes"]) <= 600 for e in seed)


def test_push_script_dry_run_adds_changelog(tmp_path):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"hello")
    out = subprocess.run(
        ["bash", str(ROOT / "deploy" / "push_apk.sh"), "--dry-run", "--build-name", "1.0.0-x", "--build-number", "7",
         "--out-dir", str(tmp_path), str(apk), "n"], capture_output=True, text=True, check=True).stdout
    assert 'changelog.py" add' in out and "/root/changelog.py" in out


def test_seed_script_dry_run():
    out = subprocess.run(["bash", str(ROOT / "deploy" / "push_changelog_seed.sh"), "--dry-run"],
                         capture_output=True, text=True, check=True).stdout
    assert "changelog.py seed" in out and "changelog_seed.json" in out
