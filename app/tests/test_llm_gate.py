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


# -- live ear policy (one shared model: start.sh --single-omni) ------------------------

def _hold(gate, prio, started, release, **kw):
    try:
        with gate.slot(prio, **kw):
            started.append((prio, time.monotonic()))
            release.wait(3)
    except GateTimeout:
        started.append((prio, None))


def test_live_ear_never_waits_behind_a_long_suggest():
    gate, started, release = PriorityGate(), [], threading.Event()
    t = threading.Thread(target=_hold, args=(gate, SUGGEST, started, release))
    t.start()
    time.sleep(0.05)
    t0 = time.monotonic()
    with gate.slot(llm_gate.LIVE, wait_timeout=0.1) as waited:  # runs BESIDE the suggest
        assert gate.snapshot()["live"] is True
        assert gate.snapshot()["in_flight"] == "suggest"
    assert waited < 0.5 and time.monotonic() - t0 < 0.5
    release.set(); t.join()
    assert "live" not in gate.snapshot()


def test_live_hold_keeps_new_work_from_starting_mid_loop():
    gate = PriorityGate(suggest_max_hold_s=0.3, behind_ear_s=0.0)
    gate.note_live(hold_s=5.0)
    with pytest.raises(GateTimeout):  # look-ahead refused outright
        with gate.slot(LOOKAHEAD, wait_timeout=1):
            pass
    with pytest.raises(GateTimeout):  # the silent ear is advisory: it gives up
        with gate.slot(llm_gate.EAR, wait_timeout=0.2):
            pass
    t0 = time.monotonic()
    with gate.slot(PLAN, wait_timeout=1) as waited:  # a plan is never held
        pass
    assert waited < 0.1
    with gate.slot(SUGGEST, wait_timeout=2) as waited:  # held, but never starved
        pass
    assert 0.25 <= waited < 1.0 and time.monotonic() - t0 < 1.5


def test_held_work_may_start_right_after_a_live_answer():
    gate = PriorityGate(suggest_max_hold_s=10.0, behind_ear_s=1.0)
    gate.note_live(hold_s=5.0)
    with gate.slot(llm_gate.LIVE):
        pass
    with gate.slot(SUGGEST, wait_timeout=0.5) as waited:  # the gap after the answer is ours
        pass
    assert waited < 0.3


def test_a_waiting_live_call_goes_before_queued_work():
    gate, started, release = PriorityGate(), [], threading.Event()
    first = threading.Thread(target=_hold, args=(gate, PLAN, started, release))
    first.start(); time.sleep(0.05)
    sug = threading.Thread(target=_hold, args=(gate, SUGGEST, started, threading.Event()),
                           kwargs={"wait_timeout": 2})
    live_done = []

    def live():
        with gate.slot(llm_gate.LIVE, wait_timeout=1.0):
            live_done.append(time.monotonic())

    lv = threading.Thread(target=live)
    lv.start(); time.sleep(0.02); sug.start(); time.sleep(0.05)
    assert [p for p, _ in started] == [PLAN]  # suggest may not start while live waits
    lv.join(2)
    assert live_done  # live ran beside the plan after LIVE_WAIT_S
    release.set(); first.join()
    sug.join(3)
    assert [p for p, _ in started] == [PLAN, SUGGEST]


def test_live_ear_takes_the_gate_only_on_the_shared_model(monkeypatch):
    from app.ui import live_ear

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:8901/v1")
    assert live_ear.shares_text_model({"base_url": "http://localhost:8901/v1"})
    assert not live_ear.shares_text_model({"base_url": "http://127.0.0.1:8902/v1"})
    assert not live_ear.shares_text_model({"base_url": "http://127.0.0.1:8901/v1", "cloud": True})
    monkeypatch.setattr(live_ear, "config", lambda: {"configured": True, "cloud": False, "model": "m",
                                                     "base_url": "http://127.0.0.1:8901/v1"})
    monkeypatch.setattr(llm_gate, "gate", PriorityGate())  # its 25 s hold stays in this test
    seen = []
    monkeypatch.setattr(live_ear, "_ask_omni", lambda c, w, m: (seen.append(llm_gate.gate.snapshot()),
                                                                '{"verdict":"clean","action":"keep"}')[1])
    res = live_ear.decide(b"RIFF", {"deck": "a"})
    assert seen and seen[0].get("live") is True and res.get("model") == "m"


def test_fake_shared_model_hold_loop_keeps_the_ear_fast():
    """A fake continuous-batching model: an ear call takes 0.05 s alone and 0.25 s
    beside a decode; a suggest decodes 0.6 s. A suggest asked for mid-loop must not
    overlap the next phrase's ear call, and still runs within the hold bound."""
    gate = PriorityGate(suggest_max_hold_s=0.8, behind_ear_s=0.0, poll_s=0.02)
    decoding = threading.Event()
    ear_lat, sug_start = [], []

    def ear_call():
        t0 = time.monotonic()
        gate.note_live(hold_s=1.0)
        with gate.slot(llm_gate.LIVE, wait_timeout=0.05):
            time.sleep(0.25 if decoding.is_set() else 0.05)
        ear_lat.append(time.monotonic() - t0)

    def suggest():
        t0 = time.monotonic()
        with gate.slot(SUGGEST, wait_timeout=5):
            sug_start.append(time.monotonic() - t0)
            decoding.set()
            time.sleep(0.6)
            decoding.clear()

    ear_call()                                  # phrase 1: the hold starts
    s = threading.Thread(target=suggest)
    s.start()                                   # asked mid-loop
    for _ in range(3):                          # phrases 2-4, one every 0.2 s
        time.sleep(0.2)
        ear_call()
    s.join(3)
    assert max(ear_lat[:3]) < 0.2, ear_lat      # no ear call ran beside the decode
    assert sug_start and sug_start[0] <= 1.0    # bounded: the suggestion still ran


# -- slimmer suggest schema -----------------------------------------------------------

def test_slim_suggest_reply_still_parses_and_fills_display_fields(monkeypatch):
    slim = ('{"steering":"stay","occasion_fit":0,"current_genre":"house","current_era":"2020s",'
            '"current_profile":{"energy":6,"tempo_feel":"driving","mood":"bittersweet"},'
            '"suggestions":[{"artist":"Q","title":"Two","reason":"same rolling bass","genre":"house",'
            '"era":"2020s","expected_bpm":124,"expected_key":"8A","energy_delta":"maintain","genre_hop":0,'
            '"occasion_fit":0,"track_profile":{"energy":6,"tempo_feel":"driving","mood":"bittersweet"}}]}')
    monkeypatch.setattr(svc, "chat_raw", lambda *a, **k: slim)
    monkeypatch.setattr(svc, "VERIFY_SONGS", False)
    out = svc.suggest_next_tracks("T", "A", 124.0, "8A", 200.0, 0.7, "", [])
    assert out and out[0]["title"] == "Two"
    s = out[0]
    for field in ("mix_moment", "vibe_link", "energy_delta", "track_profile", "search_query", "reason"):
        assert field in s  # console (autopilot.js) still reads these
