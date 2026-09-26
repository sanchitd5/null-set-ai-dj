import threading
import time

import app.ui.server as srv


def test_identical_suggest_requests_share_one_llm_call(monkeypatch):
    calls = []
    def slow_impl(req):
        calls.append(1)
        time.sleep(0.3)
        return {"suggestions": [{"title": "x"}]}
    monkeypatch.setattr(srv, "_autopilot_suggest_impl", slow_impl)
    srv._suggest_inflight.clear()
    req = srv.AutopilotSuggestRequest(track_id="t1", occasion="club", history=[])
    out = []
    ts = [threading.Thread(target=lambda: out.append(srv.autopilot_suggest(req))) for _ in range(3)]
    for t in ts:
        t.start(); time.sleep(0.02)
    for t in ts:
        t.join(2)
    assert len(calls) == 1 and len(out) == 3 and all(o["suggestions"][0]["title"] == "x" for o in out)
    other = srv.AutopilotSuggestRequest(track_id="t2", occasion="club", history=[])
    srv.autopilot_suggest(other)
    assert len(calls) == 2          # a different request still gets its own call
