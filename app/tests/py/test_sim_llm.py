"""The sim's live/record path talks to the app's own model client (a fake OpenAI-compatible server stands in
for the model here: no real model, no network beyond localhost)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.sim import llm_probe
from app.ui.services import engine


class _Model(BaseHTTPRequestHandler):
    reply = json.dumps({"suggestions": [{"artist": "A", "title": "B"}]})

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "fake-model", "loaded": True}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": "fake-model",
                           "choices": [{"index": 0, "finish_reason": "stop",
                                        "message": {"role": "assistant", "content": type(self).reply}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def model_server():
    srv = HTTPServer(("127.0.0.1", 0), _Model)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def test_probe_finds_the_server_and_publishes_it_like_the_app(model_server, monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("AUTOPILOT_MODEL", raising=False)
    ep = llm_probe.resolve(env={"OLLAMA_BASE_URL": model_server}, publish=True)
    assert (ep.backend, ep.model, ep.base_url) == ("env", "fake-model", model_server)
    import os

    assert os.environ["OLLAMA_BASE_URL"] == model_server and os.environ["AUTOPILOT_MODEL"] == "fake-model"
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("AUTOPILOT_MODEL", raising=False)


def test_probe_reports_a_dead_server_and_never_falls_back():
    env = {"MLX_PORT": "1", "OMNI_PORT": "1", "OLLAMA_URL": "http://127.0.0.1:1"}
    with pytest.raises(llm_probe.LLMDown) as e:
        llm_probe.resolve(env=env, publish=False)
    assert "never starts or stops it" in str(e.value) or "Start it" in str(e.value)


def test_a_recording_keeps_the_real_reply_latency_and_quality(model_server, monkeypatch, tmp_path):
    from app.sim.simhost import SimAI, SimHost
    from app.sim.world import World

    monkeypatch.setenv("OLLAMA_BASE_URL", model_server)
    monkeypatch.setenv("AUTOPILOT_MODEL", "fake-model")
    w = World("live", "t", 1, tmp_path, record=True, fixtures_dir=tmp_path)
    with engine.using(engine.Engine(SimHost(w), SimAI(w))):
        from app.ui.services import autopilot_service as svc

        w.begin_request()
        raw = svc.chat_raw("system", 'NOW PLAYING: "Song X" by Y | 120 BPM\n')      # the app's own client, through the gate
        lat = w.end_request()
    assert json.loads(raw)["suggestions"][0]["title"] == "B"
    (e,) = w.fx["llm"]
    assert e["kind"] in ("suggest", "lookahead") and e["cur"] == "Song X"
    assert e["reply"] == raw and e["quality"] == "ok" and e["latency_s"] is not None and e["latency_s"] >= 0
    assert lat is not None                      # the request carries the model's own latency to the console


def test_empty_and_invalid_replies_are_flagged():
    from app.sim.world import reply_quality

    assert reply_quality("suggest", json.dumps({"suggestions": []})) == "empty"
    assert reply_quality("suggest", "") == "empty"
    assert reply_quality("suggest", "sorry, I cannot") == "invalid"
    assert reply_quality("plan", "{}") == "empty"
    assert reply_quality("plan", '{"pick": 1}') == "ok"
    assert reply_quality("suggest", json.dumps({"suggestions": [{"artist": "a", "title": "b"}]})) == "ok"
