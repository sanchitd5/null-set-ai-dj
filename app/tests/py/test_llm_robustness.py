"""JSON robustness, retry accounting, adaptive ear wait and prompt size (no model, no network)."""
import json

import pytest

import app.ui.autopilot_service as svc
from app.ui import live_ear as ear
from app.ui import mind_plan as mp


@pytest.fixture
def events(monkeypatch):
    from app.ui import session_log
    got = []
    monkeypatch.setattr(session_log, "log", lambda kind, **f: got.append((kind, f)))
    return got


# ---------------------------------------------------------------- parsing
def test_unclosed_think_and_fences_are_tolerated():
    assert svc._extract_json('<think>x</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert svc._extract_json('</think>\n{"a": 2}') == {"a": 2}
    with pytest.raises(ValueError):
        svc._extract_json('<think>never closed {"a": 1}')   # the answer never came


def test_reply_quality_labels():
    assert svc.reply_quality("") == "empty"
    assert svc.reply_quality("sure, here you go") == "no_json"
    assert svc.reply_quality('{"suggestions": [{"a"') == "cut_off"
    assert svc.reply_quality('{"a": 1}') == "ok"
    assert svc.failure_reason('{"a": 1,,}') == "bad_json"


# ---------------------------------------------------------------- suggest
def _pick():
    return {"current_genre": "house", "suggestions": [
        {"artist": "Z Artist", "title": "Zed", "genre": "house", "genre_hop": 0, "expected_bpm": 124}]}


def test_suggest_one_stricter_retry_and_counted(monkeypatch, events):
    replies, prompts = iter(["Sure! Here are picks", json.dumps(_pick())]), []
    monkeypatch.setattr(svc, "chat_raw", lambda s, u, **k: (prompts.append((u, k)), next(replies))[1])
    out = svc.suggest_next_tracks("Song", "Artist", 124.0, "8A", 200.0, 0.5, "", [], lookahead=True)
    assert out and len(prompts) == 2
    assert svc.STRICT_RETRY not in prompts[0][0] and svc.STRICT_RETRY in prompts[1][0]
    assert prompts[0][1]["kind"] == "lookahead"
    assert ("llm_retry", {"call": "lookahead", "reason": "no_json", "attempt": 1}) in events


def test_suggest_never_loops(monkeypatch, events):
    calls = []
    monkeypatch.setattr(svc, "chat_raw", lambda s, u, **k: (calls.append(1), "no json at all")[1])
    with pytest.raises(ValueError):
        svc.suggest_next_tracks("Song", "Artist", 124.0, "8A", 200.0, 0.5, "", [])
    assert len(calls) == svc.MAX_JSON_ATTEMPTS == 2
    assert events[-1][1]["reason"] == "no_json_gave_up"


def test_cut_off_reply_doubles_max_tokens(monkeypatch, events):
    replies, seen = iter(['{"suggestions": [{"artist": "A", "ti', json.dumps(_pick())]), []
    monkeypatch.setattr(svc, "chat_raw", lambda s, u, **k: (seen.append(k["max_tokens"]), next(replies))[1])
    svc.suggest_next_tracks("Song", "Artist", 124.0, "8A", 200.0, 0.5, "", [])
    assert seen[1] == min(svc.MAX_SUGGEST_TOKENS, seen[0] * 2)
    assert events[0][1]["reason"] == "cut_off"


def test_chat_raw_logs_kind_and_quality(monkeypatch, events):
    from app.ui import engine
    monkeypatch.setattr(engine.current().ai, "chat", lambda *a, **k: '{"x": ', raising=False)
    svc.chat_raw("s", "u", kind="plan")
    kind, f = events[-1]
    assert kind == "llm" and f["call"] == "plan" and f["quality"] == "cut_off" and f["cut_off"] is True
    assert "elapsed" in f and "prompt_chars" in f and "chars_per_s" in f


# ---------------------------------------------------------------- prompt size
def test_prompt_drops_empty_lines_and_occasion_block():
    seen = []

    def fake(s, u, **k):
        seen.append((s, u))
        return json.dumps({"suggestions": []})

    orig = svc.chat_raw
    svc.chat_raw = fake
    try:
        svc.suggest_next_tracks("Song", "Artist", 124.0, "8A", 200.0, 0.5, "", [])
        svc.suggest_next_tracks("Song", "Artist", 124.0, "8A", 200.0, 0.5, "punjabi wedding", [],
                                earlier_sets=["X - Y"], favourite_artists=["Fav"])
    finally:
        svc.chat_raw = orig
    (s0, u0), (s1, u1) = seen[0], seen[-1]
    assert "OCCASION FIRST" not in s0 and "OCCASION FIRST" in s1
    assert "FAVOURITE" not in u0 and "EARLIER sets" not in u0
    assert "FAVOURITE artists" in u1 and "Fav" in u1 and "X - Y" in u1
    assert len(s0) < len(svc._SYSTEM) - 2000


# ---------------------------------------------------------------- plan
def test_plan_retries_once_when_unparseable(events):
    calls = []

    def llm(s, u):
        calls.append(u)
        return "no json here" if len(calls) == 1 else "{}"

    p = mp.plan_pair(_facts(), llm=llm)
    assert len(calls) == 2 and svc.STRICT_RETRY in calls[1]
    assert p["parsed"] is True and p["retried"] == "no_json"
    assert ("llm_retry", {"call": "plan", "reason": "no_json", "attempt": 1}) in events


def test_plan_never_loops_and_skips_retry_when_slow(events):
    calls = []
    p = mp.plan_pair(_facts(), llm=lambda s, u: (calls.append(1), "nope")[1])
    assert len(calls) == 2 and p["parsed"] is False


def _facts():
    from app.tests.py.test_mind_plan import _facts as f
    return f()


# ---------------------------------------------------------------- ear
def test_adaptive_timeout_tracks_median_and_never_exceeds_base():
    ear._lat.clear()
    assert ear.adaptive_timeout(10.0) == 10.0             # too few samples: unchanged
    ear._lat.extend([1.5] * 6)
    assert ear.adaptive_timeout(10.0) == 6.0              # 4 x median
    ear._lat.extend([0.2] * 20)
    assert ear.adaptive_timeout(10.0) == 4.0              # floor
    ear._lat.extend([9.0] * 20)
    assert ear.adaptive_timeout(10.0) == 10.0             # never above the configured wait
    assert ear.adaptive_timeout(3.0) == 3.0
    ear._lat.clear()


class _FakeAI:
    def __init__(self, replies):
        self.replies, self.cfgs, self.metrics = list(replies), [], []

    def ear(self, cfg, wav, m):
        self.cfgs.append(cfg)
        self.metrics.append(m)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _decide(monkeypatch, replies, events):
    from app.ui import engine
    ai = _FakeAI(replies)
    monkeypatch.setattr(engine.current(), "ai", ai)
    monkeypatch.setenv("OMNI_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setattr(ear, "shares_text_model", lambda c: False)
    ear._lat.clear()
    res = ear.decide(b"RIFF" + b"\0" * 60, {"loop_bars": 8, "secs_looping": 10, "can_move": True})
    return res, ai


GOOD = json.dumps({"verdict": "clean", "action": "keep", "confidence": 0.9, "reason": "fine"})


def test_ear_no_json_retries_once_then_model_answers(monkeypatch, events):
    res, ai = _decide(monkeypatch, ["I hear a loop.", GOOD], events)
    assert res["source"] == "AI" and len(ai.cfgs) == 2
    assert ai.metrics[1].get("_strict") and "ONLY the JSON" in ear._user_text(ai.metrics[1])
    ear_ev = [f for k, f in events if k == "ear"][0]
    assert ear_ev["retried"] == "no_json" and ear_ev["source"] == "model" and "timeout" in ear_ev
    assert ("llm_retry", {"call": "ear", "reason": "no_json", "attempt": 1}) in events


def test_ear_gives_up_to_rules_after_one_retry(monkeypatch, events):
    res, ai = _decide(monkeypatch, ["nope", "still nope"], events)
    assert res["source"] == "RULE" and "fallback" in res and len(ai.cfgs) == 2
    ev = [f for k, f in events if k == "ear"][0]
    assert ev["source"] == "rules" and ev["quality"] == "no_json" and ev["latency"] is not None


def test_ear_timeout_falls_back_and_logs_latency_and_wait(monkeypatch, events):
    res, ai = _decide(monkeypatch, [TimeoutError("timed out")], events)
    assert res["source"] == "RULE" and len(ai.cfgs) == 1
    ev = [f for k, f in events if k == "ear"][0]
    assert ev["quality"] == "TimeoutError" and ev["timeout"] == ai.cfgs[0]["timeout"]
