"""Per-session event log, the ear holding off look-aheads, the suggest token budget."""
import pytest

from app.ui import autopilot_service as svc
from app.ui import llm_gate, session_log


@pytest.fixture(autouse=True)
def _dir(tmp_path, monkeypatch):
    monkeypatch.setattr(session_log, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(session_log, "SESSION_ID", "2026-09-28_212853")


def test_events_are_logged_per_session_with_a_summary():
    session_log.log("track", event="transition_start", **{"from": "A", "to": "B"})
    session_log.log("llm", priority="suggest", elapsed=40.5, ok=True)
    session_log.log("llm", priority="lookahead", elapsed=2.0, ok=False, error="GateTimeout")
    session_log.log("ear", latency=1.4, source="model")
    ev = session_log.read()
    assert [e["kind"] for e in ev] == ["track", "llm", "llm", "ear"] and ev[0]["to"] == "B"
    s = session_log.summary(ev)
    assert s["transitions"] == 1 and s["by_kind"]["llm"] == {"count": 2, "errors": 1, "mean_s": 21.25, "max_s": 40.5}
    assert s["slowest"][0]["elapsed"] == 40.5
    assert session_log.sessions() == ["2026-09-28_212853"]
    with pytest.raises(ValueError):
        session_log.read("../etc")


def test_chat_raw_logs_every_call(monkeypatch):
    monkeypatch.setattr(svc, "_chat_call", lambda *a, **k: '{"ok": true}')
    svc.chat_raw("sys", "user", max_tokens=900, priority=llm_gate.SUGGEST)
    e = session_log.read()[-1]
    assert e["kind"] == "llm" and e["priority"] == "suggest" and e["ok"] and e["cut_off"] is False and e["max_tokens"] == 900


def test_the_ear_holds_off_lookaheads():
    g = llm_gate.PriorityGate()
    g.note_ear(30)
    with pytest.raises(llm_gate.GateTimeout, match="live ear"):
        with g.slot(llm_gate.LOOKAHEAD, wait_timeout=1):
            pass
    with g.slot(llm_gate.SUGGEST, wait_timeout=1):     # a needed suggestion still runs
        pass
    assert any(e["kind"] == "gate_skip" and e["why"] == "ear" for e in session_log.read())


def test_suggest_budget_learns_from_replies(monkeypatch):
    monkeypatch.setattr(svc, "_suggest_need", 900)
    asked = []
    reply = '{"current_genre": "house", "suggestions": [], "pad": "' + "x" * 3300 + '"}'
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: asked.append(k["max_tokens"]) or reply)
    svc.suggest_next_tracks("T", "A", 124.0, "8A", 200.0, 0.7, "", [])
    svc.suggest_next_tracks("T2", "A", 124.0, "8A", 200.0, 0.7, "", [])
    assert asked[0] == 900 and asked[1] >= 1250                     # second call starts with room
