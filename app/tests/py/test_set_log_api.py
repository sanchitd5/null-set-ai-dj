from app.music_brain.learning.set_log import SCHEMA
from app.tests.py.testclient_compat import TestClient
from app.ui import server


def _payload():
    return {
        "$schema": SCHEMA,
        "metadata": {
            "created_at": "2026-09-10T02:00:00Z",
            "duration_seconds": 12.5,
            "track_a_id": "a",
            "track_b_id": "b",
        },
        "control_event_stream": [{"t": 0.0, "param": "recording-start", "val": "a:b"}],
    }


def test_set_log_api_validates_persists_and_exports_markdown(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SET_LOGS_CACHE_DIR", tmp_path)
    server._set_logs.clear()
    client = TestClient(server.app)

    created = client.post("/api/set-logs", json=_payload())
    assert created.status_code == 200
    response = created.json()
    assert response["set_log_id"]

    stored = client.get(response["json_url"])
    assert stored.status_code == 200
    assert stored.json()["metadata"]["track_a_id"] == "a"

    markdown = client.get(response["markdown_url"])
    assert markdown.status_code == 200
    assert "# Set Log: 2026-09-10" in markdown.text


def test_set_log_api_rejects_invalid_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SET_LOGS_CACHE_DIR", tmp_path)
    client = TestClient(server.app)
    response = client.post("/api/set-logs", json={"$schema": "wrong"})
    assert response.status_code == 422
