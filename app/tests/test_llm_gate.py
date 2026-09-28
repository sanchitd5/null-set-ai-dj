"""LLM priority gate (app/ui/llm_gate.py) and suggest JSON repair."""
import threading
import time

import pytest

from app.ui import autopilot_service as svc
from app.ui import llm_gate
from app.ui.llm_gate import LOOKAHEAD, PLAN, SUGGEST, GateTimeout, PriorityGate


def _run(gate, prio, order, hold=0.0):
    try:
        with gate.slot(prio):
            order.append(prio)
            time.sleep(hold)
    except GateTimeout:                  # a refused look-ahead is an answer, not a thread crash
        pass


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
    # extra look-aheads behind a running one are refused (never pile up);
    # the plan still goes right after the call in flight
    assert order == [LOOKAHEAD, PLAN, SUGGEST]


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


def test_lookahead_never_piles_up():
    import threading
    import pytest
    from app.ui import llm_gate as g
    gate = g.PriorityGate()
    release = threading.Event()
    def hold():
        with gate.slot(g.SUGGEST):
            release.wait(2)
    t = threading.Thread(target=hold); t.start()
    import time; time.sleep(0.05)
    with pytest.raises(g.GateTimeout):            # something urgent running+queued? a look-ahead is refused
        waiter = threading.Thread(target=lambda: gate.slot(g.SUGGEST, wait_timeout=2).__enter__())
        waiter.start(); time.sleep(0.05)
        with gate.slot(g.LOOKAHEAD, wait_timeout=1):
            pass
    release.set(); t.join(); waiter.join()


def test_suggest_cut_off_reply_retries_with_more_tokens(monkeypatch):
    asked = []

    def model(*a, **k):
        asked.append(k["max_tokens"])
        if k["max_tokens"] < 1800:                                   # too verbose to fit: cut mid-object
            return '{"current_genre": "house", "suggestions": [{"artist": "A", "reason": "long reason that goes on'
        return '{"current_genre": "house", "suggestions": []}'
    monkeypatch.setattr(svc, "chat_raw", model)
    assert svc.suggest_next_tracks("T", "A", 124.0, "8A", 200.0, 0.7, "", []) == []
    assert asked == [900, 1800]                                        # not 900, 900, 900 -> 500


def test_cut_off_detection():
    assert svc.cut_off('{"a": [1, 2')
    assert not svc.cut_off('{"a": 1}')
    assert not svc.cut_off("no json at all")
    assert not svc.cut_off('<think>{"half"</think>{"a": 1}')


def test_prompt_shows_a_rolling_window_of_3_played_and_3_queued():
    played = [f"P{i}" for i in range(1, 11)]
    queued = ["Q1", "Q2", "Q3", "Q4"]
    assert svc.prompt_history(played, queued) == "last played: P8, P9, P10 | queued next: Q1, Q2, Q3"
    assert svc.prompt_history(played[:2]) == "last played: P1, P2"
    assert svc.prompt_history([], ["Q1"]) == "queued next: Q1"
    assert svc.prompt_history(played, ["P10", "Q1"]) == "last played: P8, P9, P10 | queued next: Q1"   # no double
    assert svc.prompt_history(played, [], ["X - Y", "P9"]) == "last played: P8, P9, P10 | rejected: X - Y"
    assert svc.prompt_history([]) == "none"


def test_window_prompt_still_never_repeats_a_played_song(monkeypatch):
    seen = []
    monkeypatch.setattr(svc, "chat_raw", lambda s, u, **k: seen.append(u) or
                        '{"current_genre": "house", "suggestions": [{"artist": "A0", "title": "T0", "expected_bpm": 124}]}')
    history = [f"A{i} - T{i}" for i in range(6)] + ["Old - Played"]           # A0 falls outside the 3-song window
    out = svc.suggest_next_tracks("T", "A", 124.0, "8A", 200.0, 0.7, "", history)
    assert "A0 - T0" not in seen[0]                                            # not shown to the model
    assert not any(s.get("title") == "T0" for s in out)                         # hidden, but still filtered as played


def test_queued_songs_are_shown_and_never_suggested(monkeypatch):
    seen = []
    monkeypatch.setattr(svc, "chat_raw", lambda s, u, **k: seen.append(u) or
                        '{"current_genre": "house", "suggestions": [{"artist": "Q", "title": "One", "expected_bpm": 124}]}')
    out = svc.suggest_next_tracks("T", "A", 124.0, "8A", 200.0, 0.7, "", ["P - 1", "Q - One"],
                                  history_display=["P - 1"], queue_display=["Q - One"])
    assert "last played: P - 1 | queued next: Q - One" in seen[0]
    assert not any(s.get("title") == "One" for s in out)
