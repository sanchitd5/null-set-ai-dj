import io
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from app.ui.services import live_ear as ear


def _wav(seconds=0.5, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


CLEAN = {"loop_bars": 8, "secs_looping": 10, "can_move": True}


def test_clean_loop_keeps():
    r = ear.rule_decision(CLEAN)
    assert r["action"] == "keep" and r["verdict"] == "clean" and r["source"] == "RULE"


def test_seam_problem_moves_then_washes():
    m = dict(CLEAN, seam_shift_ms=35)
    assert ear.rule_decision(m)["action"] == "move_loop"
    assert ear.rule_decision(dict(m, moved=True))["action"] == "filter_wash"
    assert ear.rule_decision(dict(m, moved=True, washed=True))["action"] == "keep"


def test_fatigue_moves_then_washes():
    m = dict(CLEAN, secs_looping=90)
    assert ear.rule_decision(m)["action"] == "move_loop"
    assert ear.rule_decision(dict(m, moved=True))["action"] == "filter_wash"


def test_allowed_tracks_state():
    assert "shrink_loop" not in ear.allowed({"loop_bars": 32})   # never shrink: sounds stuck
    assert "restore" not in ear.allowed({"washed": True, "grid_err_ms": 50})  # not over a problem
    assert "move_loop" not in ear.allowed({"loop_bars": 8, "can_move": False})
    assert "restore" in ear.allowed({"washed": True}) and "filter_wash" not in ear.allowed({"washed": True})


def test_validate_keeps_allowed_model_move():
    r = ear.validate({"verdict": "off", "action": "filter_wash", "confidence": 2, "reason": "  boring  "}, CLEAN)
    assert r["source"] == "AI" and r["action"] == "filter_wash" and r["confidence"] == 1.0
    assert r["reason"] == "boring"


def test_validate_drops_unknown_or_disallowed_action():
    assert ear.validate({"action": "drop_bomb"}, CLEAN)["source"] == "RULE"
    assert ear.validate({"action": "restore"}, CLEAN)["source"] == "RULE"  # nothing washed


def test_model_keep_never_overrides_seam_flag():
    # measured: the model called an off-grid loop "seamless"
    r = ear.validate({"verdict": "clean", "action": "keep"}, dict(CLEAN, grid_err_ms=40))
    assert r["source"] == "RULE" and r["action"] == "move_loop" and "dropped" in r


def test_check_wav():
    ear.check_wav(_wav())
    with pytest.raises(ValueError):
        ear.check_wav(b"not audio" * 10)
    with pytest.raises(ValueError):
        ear.check_wav(b"RIFF" + b"\x00" * (ear.MAX_AUDIO_BYTES + 1))


def test_decide_without_clip_uses_rules():
    r = ear.decide(None, CLEAN)
    assert r["source"] == "RULE" and r["fallback"] == "no audio clip"


def test_decide_model_error_falls_back(monkeypatch):
    monkeypatch.setattr(ear, "_ask_omni", lambda c, w, m: (_ for _ in ()).throw(ConnectionError("down")))
    r = ear.decide(_wav(), CLEAN)
    assert r["source"] == "RULE" and "ConnectionError" in r["fallback"]


def test_decide_parses_model_json(monkeypatch):
    monkeypatch.setattr(ear, "_ask_omni", lambda c, w, m: '{"verdict":"off","action":"filter_wash","reason":"x"}')
    r = ear.decide(_wav(), dict(CLEAN, secs_looping=70))
    assert r["source"] == "AI" and r["action"] == "filter_wash"


def test_model_shrink_is_refused():
    r = ear.validate({"action": "shrink_loop"}, CLEAN)
    assert r["source"] == "RULE" and "dropped" in r


def test_decide_refuses_while_busy(monkeypatch):
    monkeypatch.setattr(ear, "_ask_omni", lambda c, w, m: pytest.fail("must not call the model"))
    ear._busy.acquire()
    try:
        assert "busy" in ear.decide(_wav(), CLEAN)["fallback"]
    finally:
        ear._busy.release()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_live_ear_js_core():
    check = Path(__file__).parents[1].joinpath("js", "live_ear_check.js")
    res = subprocess.run([shutil.which("node"), str(check)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout


def test_local_is_default(monkeypatch):
    for k in ("OMNI_BASE_URL", "OMNI_MODEL", "OMNI_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    c = ear.config()
    assert c["base_url"] == ear.LOCAL_URL and c["model"] == ear.LOCAL_MODEL and c["configured"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stem_moves_js_core():
    check = Path(__file__).parents[1].joinpath("js", "stem_moves_check.js")
    res = subprocess.run([shutil.which("node"), str(check)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr or res.stdout
