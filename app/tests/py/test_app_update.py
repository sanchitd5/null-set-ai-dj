"""GET /api/app/android.json (in-app updater metadata) and deploy/push_apk.sh --dry-run."""
import json
import subprocess
from pathlib import Path

import pytest

from app.tests.py.testclient_compat import TestClient
from app.ui import server

ROOT = Path(__file__).resolve().parents[3]
META = {"version_name": "1.0.0-parity.3", "version_code": 4, "sha256": "ab" * 32, "size": 12,
        "notes": "fixes", "published_at": "2026-11-07T10:00:00Z"}


@pytest.fixture
def build(tmp_path, monkeypatch):
    apk, meta = tmp_path / "null-set-dj.apk", tmp_path / "null-set-dj.json"
    monkeypatch.setattr(server, "ANDROID_APK", apk)
    monkeypatch.setattr(server, "ANDROID_META", meta)
    return apk, meta


def test_needs_auth():
    r = TestClient(server.app, auth=False).get("/api/app/android.json")
    assert r.status_code == 401


def test_404_without_json(build):
    apk, _ = build
    apk.write_bytes(b"x")
    assert TestClient(server.app).get("/api/app/android.json").status_code == 404


def test_404_without_apk(build):
    _, meta = build
    meta.write_text(json.dumps(META))
    assert TestClient(server.app).get("/api/app/android.json").status_code == 404


def test_404_on_garbage_json(build):
    apk, meta = build
    apk.write_bytes(b"x")
    meta.write_text("{nope")
    assert TestClient(server.app).get("/api/app/android.json").status_code == 404


def test_shape_and_extra_keys_dropped(build):
    apk, meta = build
    apk.write_bytes(b"x")
    meta.write_text(json.dumps({**META, "secret": "no"}))
    r = TestClient(server.app).get("/api/app/android.json")
    assert r.status_code == 200 and r.json() == META


def test_push_script_dry_run(tmp_path):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"hello")
    out = subprocess.run(
        ["bash", str(ROOT / "deploy" / "push_apk.sh"), "--dry-run", "--build-name", "1.0.0-x",
         "--build-number", "7", "--out-dir", str(tmp_path), str(apk), "some notes"],
        capture_output=True, text=True, check=True).stdout
    j = json.loads((tmp_path / "null-set-dj.json").read_text())
    assert j["version_code"] == 7 and j["version_name"] == "1.0.0-x" and j["size"] == 5 and j["notes"] == "some notes"
    assert j["sha256"] == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert "scp -P 2298" in out and "pct push 116" in out and "cp -p" not in out
