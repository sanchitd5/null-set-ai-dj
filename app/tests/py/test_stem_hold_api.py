"""POST /api/stem_hold relays dj-core's stem_hold (STEMONLY readiness hold)."""
import pytest
from fastapi import FastAPI
from app.tests.py.testclient_compat import TestClient

from app.music_brain import djcore_bridge
from app.ui.services import stem_hold_api

app = FastAPI()
app.include_router(stem_hold_api.router)
client = TestClient(app)

BODY = {"a": {"state": "ready"}, "b": {"state": "in_flight", "eta_s": 20}, "bar_s": 2.0, "room_s": 100}


def test_bad_body_is_422():
    assert client.post("/api/stem_hold", json={"a": {"state": "ready"}}).status_code == 422
    assert client.post("/api/stem_hold", json={**BODY, "bar_s": 0}).status_code == 422
    assert client.post("/api/stem_hold", json={**BODY, "b": {"state": "bogus"}}).status_code == 422


def test_dj_core_missing_means_cut(monkeypatch):
    def boom(*a, **k):
        raise djcore_bridge.DjCoreUnavailable("no build")
    monkeypatch.setattr(djcore_bridge, "call", boom)
    r = client.post("/api/stem_hold", json=BODY)
    assert r.status_code == 200 and r.json()["action"] == "cut"


def test_relays_the_service_answer(monkeypatch):
    seen = {}
    def fake(fn, args=None):
        seen.update(fn=fn, args=args)
        return {"action": "hold", "phrases": 2, "hold_s": 32.0, "why": "x"}
    monkeypatch.setattr(djcore_bridge, "call", fake)
    assert client.post("/api/stem_hold", json=BODY).json()["phrases"] == 2
    assert seen["fn"] == "stem_hold" and seen["args"]["b"] == {"state": "in_flight", "eta_s": 20.0}


@pytest.mark.needs_djcore
def test_real_dj_core():
    r = client.post("/api/stem_hold", json=BODY).json()
    assert (r["action"], r["phrases"]) == ("hold", 2)
    assert client.post("/api/stem_hold", json={**BODY, "b": {"state": "failed"}}).json()["action"] == "cut"
