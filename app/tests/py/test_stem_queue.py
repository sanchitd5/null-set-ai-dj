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
    from app.music_brain import stem_service
    monkeypatch.setattr(stem_service, "StemWorker", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no worker in tests")))
    monkeypatch.setattr(srv, "_stem_cache", {})
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


@pytest.mark.slow
def test_persistent_worker_separates_two_songs_pipelined(tmp_path):
    from pathlib import Path

    import soundfile as sf

    from app.music_brain import stem_service

    songs = sorted(Path("data/cache/uploads").glob("*.flac"))[:2]
    if len(songs) < 2:
        pytest.skip("no library audio")
    w = stem_service.StemWorker()
    for i, s in enumerate(songs):
        w._proc.stdin.write(__import__("json").dumps({"id": str(i), "path": str(s), "out_dir": str(tmp_path / str(i))}) + "\n")
    w._proc.stdin.flush()
    got = [w.results.get(timeout=300) for _ in songs]
    assert all(r["ok"] for r in got), got
    for r in got:
        assert set(r["stems"]) == {"drums", "bass", "vocals", "other"}
        info = sf.info(r["stems"]["vocals"])
        assert info.samplerate == 44100 and info.duration > 30


def test_wait_note_names_what_the_ai_is_doing(monkeypatch):
    """Owner read "(paused: LLM working)" as "the LLM is paused": the note says the STEMS wait."""
    from app.ui.services import llm_gate

    g = llm_gate.PriorityGate()
    monkeypatch.setattr(llm_gate, "gate", g)
    assert srv._stem_wait_note() == ""
    with g.slot(llm_gate.SUGGEST):
        note = srv._stem_wait_note()
    assert note == "stems wait for the AI (suggest)" and "paused" not in note
    assert srv._stem_wait_note() == ""          # released: nothing left over
