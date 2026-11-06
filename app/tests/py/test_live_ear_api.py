"""POST /api/live/ear as the phone calls it: device token, multipart metrics + 16 kHz WAV, capped body.
The decision logic itself is covered in test_live_ear.py."""
import io
import json
import struct

from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import auth, live_ear

METRICS = {"deck": "a", "loop_bars": 16, "secs_looping": 90, "can_move": False, "washed": False, "grid_err_ms": 50.0}


def _bearer(token):
    return TestClient(server.app, auth=False, headers={"Authorization": f"Bearer {token}"})


def _wav(n):
    return (b"RIFF" + struct.pack("<I", 36 + 2 * n) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
            + b"data" + struct.pack("<I", 2 * n) + b"\0" * (2 * n))


def test_needs_auth():
    server.AUTH_GUARD.limiter.reset()
    r = TestClient(server.app, auth=False).post("/api/live/ear", data={"metrics": json.dumps(METRICS)})
    assert r.status_code == 401


def test_device_token_gets_the_rule_decision_without_clip(api_token):
    r = _bearer(api_token).post("/api/live/ear", data={"metrics": json.dumps(METRICS)})
    assert r.status_code == 200
    d = r.json()
    assert d["source"] == "RULE" and d["action"] == "filter_wash" and d["fallback"] == "no audio clip"


def test_clip_reaches_decide(api_token, monkeypatch):
    seen = {}

    def decide(wav, m):
        seen["wav"], seen["m"] = wav, m
        return live_ear.rule_decision(m)

    monkeypatch.setattr(live_ear, "decide", decide)
    r = _bearer(api_token).post("/api/live/ear", data={"metrics": json.dumps(METRICS)},
                                files={"clip": ("ear.wav", io.BytesIO(_wav(16000 * 8)), "audio/wav")})
    assert r.status_code == 200
    assert len(seen["wav"]) == 44 + 2 * 16000 * 8 and seen["m"]["loop_bars"] == 16


def test_bad_input_is_400(api_token):
    c = _bearer(api_token)
    assert c.post("/api/live/ear", data={"metrics": "not json"}).status_code == 400
    assert c.post("/api/live/ear", data={"metrics": json.dumps(METRICS)},
                  files={"clip": ("ear.wav", io.BytesIO(b"nope" * 20), "audio/wav")}).status_code == 400


def test_body_is_capped(api_token):
    c = _bearer(api_token)
    c.get("/health")                                    # builds the middleware stack
    mw = server.app.middleware_stack
    while mw is not None and not isinstance(mw, auth.BodyLimitMiddleware):
        mw = getattr(mw, "app", None)
    limit = mw.limits[("POST", "/api/live/ear")]
    assert live_ear.MAX_AUDIO_BYTES < limit <= 4 * 1024 * 1024
    r = c.post("/api/live/ear", data={"metrics": json.dumps(METRICS)},
               files={"clip": ("ear.wav", io.BytesIO(_wav(limit // 2 + 1000)), "audio/wav")})
    assert r.status_code == 413
