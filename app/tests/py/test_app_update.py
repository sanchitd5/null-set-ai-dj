"""GET /api/app/android.json (in-app updater metadata) and deploy/push_apk.sh --dry-run."""
import json
import os
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


def test_push_script_arm64_real_run_with_fake_ssh(tmp_path):
    """ssh joins its arguments into one command string, so the empty arm64 suffix argument vanishes on the host.
    The host script must cope (this broke AUTOAPK 1.1.18: "$4: unbound variable")."""
    fake = tmp_path / "bin"
    fake.mkdir()
    log = tmp_path / "calls.log"
    (fake / "scp").write_text("#!/bin/bash\necho \"scp $*\" >> %s\n" % log)
    # like ssh: drop "-p PORT HOST", join the rest into one string, run it with stdin as the script
    (fake / "ssh").write_text("#!/bin/bash\nshift 3\nexec bash -c \"$*\"\n")
    (fake / "pct").write_text(
        "#!/bin/bash\necho \"pct $*\" >> %s\n"
        "case \"$*\" in *sha256sum*) echo \"$FAKE_SHA  x\";; esac\nexit 0\n" % log)
    (fake / "rm").write_text("#!/bin/bash\nexit 0\n")  # the host script rm -f's files under /root: never for real
    for f in fake.iterdir():
        f.chmod(0o755)
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"hello")
    sha = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "FAKE_SHA": sha, "HOME": str(tmp_path)}
    out = subprocess.run(
        ["bash", str(ROOT / "deploy" / "push_apk.sh"), "--build-name", "1.1.0", "--build-number", "66",
         "--out-dir", str(tmp_path), str(apk), "notes"],
        capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    assert f"verified sha256 in CT116: {sha}" in out.stdout
    assert "null-set-dj.apk" in log.read_text()
