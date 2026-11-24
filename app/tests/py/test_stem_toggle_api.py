"""POST /api/stem_toggle relays dj-core's stem_toggle_rule (the stem chip lock)."""
import pytest
from fastapi import FastAPI
from app.tests.py.testclient_compat import TestClient

from app.music_brain import djcore_bridge
from app.ui.services import stem_toggle_api

app = FastAPI()
app.include_router(stem_toggle_api.router)
client = TestClient(app)

ALL_ON = {"drums": True, "bass": True, "vocals": True, "other": True}
BODY = {"stem_state": ALL_ON, "readiness": "ready", "transitioning": False}


def test_bad_body_is_422():
    assert client.post("/api/stem_toggle", json={"readiness": "ready"}).status_code == 422
    assert client.post("/api/stem_toggle", json={**BODY, "readiness": "bogus"}).status_code == 422
    assert client.post("/api/stem_toggle", json={**BODY, "stem": "kazoo"}).status_code == 422


def test_dj_core_missing_does_not_lock(monkeypatch):
    def boom(*a, **k):
        raise djcore_bridge.DjCoreUnavailable("no build")
    monkeypatch.setattr(djcore_bridge, "call", boom)
    assert client.post("/api/stem_toggle", json=BODY).json() == {"ok": True, "reason": None}


def test_relays_the_service_answer(monkeypatch):
    seen = {}
    def fake(fn, args=None):
        seen.update(fn=fn, args=args)
        return {"ok": False, "reason": "A transition is mixing"}
    monkeypatch.setattr(djcore_bridge, "call", fake)
    r = client.post("/api/stem_toggle", json={**BODY, "transitioning": True}).json()
    assert r == {"ok": False, "reason": "A transition is mixing"}
    assert seen["fn"] == "stem_toggle_rule" and seen["args"]["transitioning"] is True and "stem" not in seen["args"]


@pytest.mark.needs_djcore
def test_real_dj_core():
    post = lambda **k: client.post("/api/stem_toggle", json={**BODY, **k}).json()
    assert post() == {"ok": True, "reason": None}
    assert post(transitioning=True)["reason"] == "A transition is mixing"
    assert post(readiness="in_flight")["reason"] == "Stems still loading"
    one = {"drums": False, "bass": False, "vocals": True, "other": False}
    assert post(stem_state=one, stem="vocals", on=False)["reason"] == "Keep one stem up"
    assert post(stem_state=one, stem="drums", on=False)["ok"] is True
