"""POST /api/session/events: the phone's batched watchdog reports, item by item through /api/session/event."""
from types import SimpleNamespace

from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import session_log


def _client(api_token, monkeypatch):
    logged, songs = [], []
    monkeypatch.setattr(session_log, "log", lambda kind, **f: logged.append((kind, f)))
    monkeypatch.setattr(server, "_host", lambda: SimpleNamespace(session_event=lambda k, f: songs.append((k, f))))
    c = TestClient(server.app, auth=False, headers={"Authorization": f"Bearer {api_token}"})
    return c, logged, songs


def test_batch_logs_each_event_and_skips_bad_ones(api_token, monkeypatch):
    c, logged, songs = _client(api_token, monkeypatch)
    r = c.post("/api/session/events", json={"events": [
        {"kind": "glitch", "data": {"kind": "silence", "t": 12.5, "detail": {"seconds": 1.1}, "source": "phone"}},
        {"kind": "nope", "data": {}},
        {"kind": "note", "data": {"what": "ear", "action": "filter_wash"}},
        "not an object",
    ]})
    assert r.status_code == 200
    assert r.json()["ok"] == 2 and r.json()["skipped"] == 2
    assert [k for k, _ in logged] == ["glitch", "note"]
    assert logged[0][1]["kind_"] == "silence" and logged[0][1]["t_"] == 12.5     # same renaming as the single route
    assert len(songs) == 2


def test_batch_is_capped_and_needs_auth(api_token, monkeypatch):
    c, logged, _ = _client(api_token, monkeypatch)
    assert c.post("/api/session/events", json={"events": [{"kind": "note"}] * 51}).status_code == 413
    assert not logged
    server.AUTH_GUARD.limiter.reset()
    assert TestClient(server.app, auth=False).post("/api/session/events", json={"events": []}).status_code in (401, 403)
