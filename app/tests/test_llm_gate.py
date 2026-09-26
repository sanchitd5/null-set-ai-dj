"""LLM priority gate (app/ui/llm_gate.py) and suggest JSON repair."""
import threading
import time

import pytest

from app.ui import autopilot_service as svc
from app.ui import llm_gate
from app.ui.llm_gate import LOOKAHEAD, PLAN, SUGGEST, GateTimeout, PriorityGate


def _run(gate, prio, order, hold=0.0):
    with gate.slot(prio):
        order.append(prio)
        time.sleep(hold)


def test_plan_jumps_queued_lookaheads():
    gate, order = PriorityGate(), []
    first = threading.Thread(target=_run, args=(gate, LOOKAHEAD, order, 0.3))
    first.start()
    time.sleep(0.05)  # look-ahead #1 is in flight
    waiters = [threading.Thread(target=_run, args=(gate, p, order))
               for p in (LOOKAHEAD, LOOKAHEAD, SUGGEST)]
    for t in waiters:
        t.start()
        time.sleep(0.02)
    plan = threading.Thread(target=_run, args=(gate, PLAN, order))
    plan.start()  # arrives last, runs right after the call in flight
    for t in [first, plan, *waiters]:
        t.join(3)
    assert order == [LOOKAHEAD, PLAN, SUGGEST, LOOKAHEAD, LOOKAHEAD]


def test_one_call_at_a_time_and_timeout_leaves_queue_clean():
    gate = PriorityGate()
    with gate.slot(LOOKAHEAD):
        with pytest.raises(GateTimeout):
            with gate.slot(PLAN, wait_timeout=0.1):
                pass
        assert gate.snapshot() == {"in_flight": "lookahead", "queued": []}
    with gate.slot(PLAN, wait_timeout=0.1) as waited:
        assert waited < 0.1


def test_bad_priority_rejected():
    with pytest.raises(ValueError):
        with PriorityGate().slot(7):
            pass


def test_chat_raw_passes_gate_with_priority(monkeypatch):
    seen = []
    monkeypatch.setattr(svc, "_chat_call", lambda *a: seen.append(llm_gate.gate.snapshot()) or "{}")
    svc.chat_raw("s", "u", priority=PLAN, timeout=30)
    assert seen == [{"in_flight": "plan", "queued": []}]


@pytest.mark.parametrize("raw, want", [
    ('{"a": 1 "b": 2}', {"a": 1, "b": 2}),
    ('{"s": [{"t": "a"}\n  {"t": "b"}]}', {"s": [{"t": "a"}, {"t": "b"}]}),
    ('{"a": [1, 2,], "b": {"x": "y",}}', {"a": [1, 2], "b": {"x": "y"}}),
    ('```json\n{"t": "a, b"}\n```', {"t": "a, b"}),
])
def test_extract_json_repairs_small_model_slips(raw, want):
    assert svc._extract_json(raw) == want


def test_extract_json_unfixable_raises():
    with pytest.raises(ValueError):
        svc._extract_json('{"a": }')


def test_suggest_retries_once_on_bad_json(monkeypatch):
    replies = iter(['{"suggestions": [ oops', '{"current_genre": "house", "suggestions": []}'])
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: next(replies))
    out = svc.suggest_next_tracks("T", "A", 124.0, "8A", 200.0, 0.7, "", [])
    assert out == []
