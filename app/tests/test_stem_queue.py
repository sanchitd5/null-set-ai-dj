"""Stem pre-separation queue: urgent before backlog, backlog waits for the LLM."""
import threading
import time

import pytest

from app.ui import server as srv


@pytest.fixture
def fake(monkeypatch, tmp_path):
    done, busy = [], {"llm": False}
    for tid in ("u1", "b1", "b2"):
        f = tmp_path / f"{tid}.wav"
        f.write_bytes(b"x")
        monkeypatch.setitem(srv._tracks, tid, f)
    monkeypatch.setattr(srv, "_cached_stems4", lambda tid: None if tid not in done else {"ok": 1})
    monkeypatch.setattr(srv, "_cached_vocal_regions", lambda tid: None)
    monkeypatch.setattr(srv, "separate_stems", lambda path: done.append(path.stem))
    monkeypatch.setattr(srv, "_llm_busy", lambda: busy["llm"])
    monkeypatch.setattr(srv, "_stem_queue", [])
    monkeypatch.setattr(srv, "_stem_backlog", [])
    monkeypatch.setattr(srv, "_stem_cv", threading.Condition())
    return done, busy


def _wait(pred, s=5.0):
    t = time.time()
    while time.time() - t < s:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_urgent_first_and_backlog_waits_for_idle_llm(fake):
    done, busy = fake
    busy["llm"] = True
    srv._stem_worker.started = False
    srv._queue_stems("b1", urgent=False)
    srv._queue_stems("b2", urgent=False)
    srv._queue_stems("u1")                      # a deck / new download
    assert _wait(lambda: done == ["u1"])
    time.sleep(0.3)
    assert done == ["u1"]                       # LLM busy: no backfill
    busy["llm"] = False
    assert _wait(lambda: done == ["u1", "b1", "b2"], 8.0)


def test_deck_request_promotes_a_backlog_track(fake):
    done, busy = fake
    busy["llm"] = True
    with srv._stem_cv:
        srv._stem_backlog.extend(["b1", "b2"])
    srv._queue_stems("b2")                      # a deck asks for b2
    assert srv._stem_queue == ["b2"] or done[:1] == ["b2"]
    assert "b2" not in srv._stem_backlog
