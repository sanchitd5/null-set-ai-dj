"""LLM_CAPTURE: chat_raw writes prompt + reply as JSONL only when switched on."""
import json
import threading
import types

import pytest

from app.ui.services import autopilot_service as svc
from app.ui.services import llm_capture, llm_gate, session_log


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(session_log, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session_log, "SESSION_ID", "2026-11-06_000000")
    monkeypatch.setenv("LLM_CAPTURE_DIR", str(tmp_path / "cap"))
    # the model behind chat_raw, whatever engine an earlier test installed
    ai = types.SimpleNamespace(chat=lambda *a, **k: '{"suggestions": []}')
    monkeypatch.setattr(svc.engine.current(), "ai", ai)
    monkeypatch.setattr(llm_capture, "_CTX", threading.local())   # no note from another test
    monkeypatch.setattr(llm_gate, "gate", llm_gate.PriorityGate())  # no ear hold left by another test


def test_capture_off_by_default_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_CAPTURE", raising=False)
    svc.chat_raw("sys", "usr", max_tokens=900, kind="suggest")
    assert not (tmp_path / "cap").exists()


def test_capture_on_appends_one_row_per_call(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CAPTURE", "1")
    monkeypatch.setenv("LLM_CAPTURE_TAG", "s7-quick/t10")
    svc.chat_raw("sys", "usr", temperature=0.3, max_tokens=600, priority=llm_gate.PLAN, kind="plan")
    svc.chat_raw("sys2", "usr2", max_tokens=900, priority=llm_gate.LOOKAHEAD)
    files = list((tmp_path / "cap").glob("*.jsonl"))
    assert [f.name for f in files] == ["s7-quick_t10.jsonl"]          # tag sanitised into a file name
    rows = [json.loads(x) for x in files[0].read_text().splitlines()]
    assert rows[0] | {"ts": 0} == {"kind": "plan", "priority": "plan", "system": "sys", "user": "usr",
                                   "temperature": 0.3, "max_tokens": 600, "reply": '{"suggestions": []}',
                                   "quality": "ok", "ts": 0, "tag": "s7-quick/t10"}
    assert rows[1]["kind"] == "lookahead"                              # unnamed call: the gate's name


def test_capture_never_breaks_the_call(monkeypatch):
    monkeypatch.setenv("LLM_CAPTURE", "1")
    monkeypatch.setenv("LLM_CAPTURE_DIR", "/dev/null/not-a-dir")
    assert llm_capture.enabled()
    assert svc.chat_raw("s", "u") == '{"suggestions": []}'
