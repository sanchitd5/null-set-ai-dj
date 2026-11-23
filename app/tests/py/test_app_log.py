"""POST /api/app/log (device incident logs) and GET /api/app/logs."""
import gzip
import json

import pytest

from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import app_log_api

BODY = {"incident": "inc-0001", "trigger": "unfinished_boot", "message": "boot stalled", "version_name": "1.1.20",
        "version_code": 90, "abi": "arm64-v8a", "device": "Pixel 8", "android": "15", "form_factor": "phone",
        "renderer": "impeller", "at": "2026-11-23T10:00:00Z", "log": "line 1\nAuthorization: Bearer abcdef123456\nline 3"}


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(app_log_api, "LOG_DIR", tmp_path / "device_logs")
    return tmp_path / "device_logs"


def _client(api_token):
    return TestClient(server.app, auth=False, headers={"Authorization": f"Bearer {api_token}"})


def test_stored_scrubbed_and_listed(api_token, root):
    c = _client(api_token)
    assert c.post("/api/app/log", json=BODY).json() == {"ok": True}
    files = list(root.glob("*/*_inc-0001.json"))
    assert len(files) == 1
    stored = json.loads(files[0].read_text())
    assert "abcdef123456" not in stored["log"] and "[redacted]" in stored["log"] and stored["device"] == "Pixel 8"
    rows = c.get("/api/app/logs").json()["incidents"]
    assert rows[0]["incident"] == "inc-0001" and "log" not in rows[0] and rows[0]["log_chars"] > 0
    assert c.get("/api/app/logs", params={"id": "inc-0001"}).json()["log"] == stored["log"]
    assert c.get("/api/app/logs", params={"id": "nope-nope"}).status_code == 404


def test_same_incident_stored_once(api_token, root):
    c = _client(api_token)
    c.post("/api/app/log", json=BODY)
    assert c.post("/api/app/log", json=BODY).json() == {"ok": True, "duplicate": True}
    assert len(list(root.glob("*/*.json"))) == 1


def test_gzip_body_and_needs_auth(api_token, root):
    r = _client(api_token).post("/api/app/log", content=gzip.compress(json.dumps(BODY).encode()),
                                headers={"Content-Type": "application/json", "Content-Encoding": "gzip"})
    assert r.status_code == 200
    server.AUTH_GUARD.limiter.reset()
    assert TestClient(server.app, auth=False).post("/api/app/log", json=BODY).status_code in (401, 403)


def test_size_caps(api_token, root):
    c = _client(api_token)
    assert c.post("/api/app/log", json={**BODY, "log": "x" * (app_log_api.MAX_LOG_CHARS + 1)}).status_code in (413, 422)
    assert c.post("/api/app/log", json={**BODY, "incident": "bad id!"}).status_code == 422
    assert not list(root.glob("*/*.json"))


def test_retention_drops_oldest(api_token, root, monkeypatch):
    monkeypatch.setattr(app_log_api, "MAX_FILES", 3)
    c = _client(api_token)
    for i in range(5):
        c.post("/api/app/log", json={**BODY, "incident": f"inc-{i:04d}"})
    left = sorted(p.name.split("_", 1)[1] for p in root.glob("*/*.json"))
    assert len(left) == 3 and "inc-0004.json" in left and "inc-0000.json" not in left
