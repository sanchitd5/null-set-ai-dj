"""POST /api/app/crash (phone start-up crash reports) and the force-update list (--block / blocked_version_codes)."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import app_crash_api

ROOT = Path(__file__).resolve().parents[3]
META = {"version_name": "1.0.0-parity.3", "version_code": 4, "sha256": "ab" * 32, "size": 12,
        "notes": "fixes", "published_at": "2026-11-07T10:00:00Z"}
REPORT = {"kind": "init_error", "message": "Bad state: no engine", "stack": "#0 main (main.dart:55)",
          "version_name": "1.0.0-parity.16", "version_code": 16, "device": "Pixel 8", "at": "2026-11-08T12:00:00Z"}


@pytest.fixture
def log(tmp_path, monkeypatch):
    p = tmp_path / "app" / "crash_reports.jsonl"
    monkeypatch.setattr(app_crash_api, "CRASH_LOG", p)
    return p


def _client(api_token):
    return TestClient(server.app, auth=False, headers={"Authorization": f"Bearer {api_token}"})


def test_report_is_stored_as_one_json_line(api_token, log):
    r = _client(api_token).post("/api/app/crash", json=REPORT)
    assert r.status_code == 200 and r.json() == {"ok": True}
    _client(api_token).post("/api/app/crash", json={**REPORT, "version_code": 17})
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert [x["version_code"] for x in rows] == [16, 17]
    assert rows[0]["message"] == REPORT["message"] and rows[0]["device"] == "Pixel 8" and rows[0]["received_at"].endswith("Z")


def test_needs_auth(log):
    server.AUTH_GUARD.limiter.reset()
    r = TestClient(server.app, auth=False).post("/api/app/crash", json=REPORT)
    assert r.status_code in (401, 403)
    assert not log.exists()


def test_body_over_64kb_is_refused(api_token, log):
    r = _client(api_token).post("/api/app/crash", content=json.dumps({**REPORT, "stack": "x" * 70_000}),
                                headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert not log.exists()


def test_oversized_field_and_bad_types_are_422(api_token, log):
    c = _client(api_token)
    assert c.post("/api/app/crash", json={**REPORT, "stack": "x" * 20_001}).status_code == 422
    assert c.post("/api/app/crash", json={**REPORT, "version_code": "sixteen"}).status_code == 422
    assert not log.exists()


def test_secrets_are_scrubbed_before_storing(api_token, log):
    tok = "dj_" + "A1b2C3d4" * 4
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
    msg = f"HTTP 401 for Authorization: Bearer {tok} and {tok} and {jwt} at https://x/api?token=sekrit&a=1"
    _client(api_token).post("/api/app/crash", json={**REPORT, "message": msg, "stack": f"Bearer {tok}"})
    text = log.read_text()
    assert tok not in text and jwt not in text and "sekrit" not in text
    assert "[redacted]" in text and "a=1" in text


def test_log_rotates(api_token, log, monkeypatch):
    monkeypatch.setattr(app_crash_api, "MAX_LOG_BYTES", 200)
    c = _client(api_token)
    for _ in range(4):
        c.post("/api/app/crash", json=REPORT)
    assert log.with_name(log.name + ".1").is_file() and log.is_file()


# ---- force update list ---------------------------------------------------------------------------

@pytest.fixture
def build(tmp_path, monkeypatch):
    apk, meta = tmp_path / "null-set-dj.apk", tmp_path / "null-set-dj.json"
    apk.write_bytes(b"x")
    monkeypatch.setattr(server, "ANDROID_APK", apk)
    monkeypatch.setattr(server, "ANDROID_META", meta)
    return meta


def test_blocked_codes_are_served_when_valid(build):
    build.write_text(json.dumps({**META, "blocked_version_codes": [15, 16]}))
    assert TestClient(server.app).get("/api/app/android.json").json() == {**META, "blocked_version_codes": [15, 16]}


@pytest.mark.parametrize("bad", [[], "16", [16, "x"], [True], [-1], None])
def test_blocked_codes_absent_or_malformed_are_left_out(build, bad):
    build.write_text(json.dumps({**META, "blocked_version_codes": bad}))
    assert TestClient(server.app).get("/api/app/android.json").json() == META


def test_push_script_block_option(tmp_path):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"hello")

    def push(*extra):
        subprocess.run(["bash", str(ROOT / "deploy" / "push_apk.sh"), "--dry-run", "--build-name", "1.0.0-x", "--build-number", "7",
                        "--out-dir", str(tmp_path), *extra, str(apk), "n"], capture_output=True, text=True, check=True)
        return json.loads((tmp_path / "null-set-dj.json").read_text())

    assert "blocked_version_codes" not in push()
    assert push("--block", "5,6")["blocked_version_codes"] == [5, 6]
    assert push("--block", "none")["blocked_version_codes"] == []
    bad = subprocess.run(["bash", str(ROOT / "deploy" / "push_apk.sh"), "--dry-run", "--build-name", "x", "--build-number", "7",
                          "--block", "5;rm", str(apk), "n"], capture_output=True, text=True)
    assert bad.returncode == 1 and "--block" in bad.stderr


def test_publish_keeps_the_earlier_block_list(tmp_path):
    spec = importlib.util.spec_from_file_location("apk_delta", ROOT / "deploy" / "apk_delta.py")
    delta = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(delta)
    apk, meta = tmp_path / "null-set-dj.apk", tmp_path / "null-set-dj.json"
    apk.write_bytes(b"PK-new")

    def publish(new_meta, old_meta):
        meta.write_text(json.dumps(new_meta))
        (tmp_path / "null-set-dj.json.bak").write_text(json.dumps(old_meta))
        delta.publish(str(tmp_path), 3)
        return json.loads(meta.read_text())

    new = {**META, "version_code": 9}
    assert publish(new, {**META, "version_code": 8, "blocked_version_codes": [6, 7]})["blocked_version_codes"] == [6, 7]
    assert publish({**new, "blocked_version_codes": [7]}, {**META, "blocked_version_codes": [6]})["blocked_version_codes"] == [7]
    assert publish({**new, "blocked_version_codes": []}, {**META, "blocked_version_codes": [6]})["blocked_version_codes"] == []
    assert "blocked_version_codes" not in publish(new, META)
