"""CLEANLIB music_span: ad / talk / silence head and tail found on synthetic audio; clean songs untouched."""
import json
from pathlib import Path

import numpy as np

from app.tests.py.test_not_music import _music, _speech
from app.ui.services import music_span as ms
from app.ui.services import not_music as nm

SR = nm.SR


def _pcm_of(x):
    return lambda p, s, d: x[int(s * SR):int((s + d) * SR)]


def _song(head=0, mid=100, tail=0, silent_tail=0):
    parts = ([_speech(head, 3)] if head else []) + [_music(mid)] + ([_speech(tail, 4)] if tail else []) \
        + ([np.zeros(int(silent_tail * SR), np.float32)] if silent_tail else [])
    return np.concatenate(parts).astype(np.float32)


def test_clean_song_untouched():
    x = _song(mid=200)
    r = ms.analyse(Path("a"), 200.0, pcm=_pcm_of(x))
    assert (r["start"], r["end"], r["head"], r["tail"]) == (0.0, 200.0, None, None)


def test_speech_tail_and_head_found():
    x = _song(head=18, mid=160, tail=30)
    dur = len(x) / SR
    r = ms.analyse(Path("a"), dur, pcm=_pcm_of(x))
    assert r["head"] == "speech" and 12 <= r["start"] <= 24
    assert r["tail"] == "speech" and dur - 36 <= r["end"] <= dur - 24


def test_silent_tail_and_cap():
    x = _song(mid=120, silent_tail=20)
    r = ms.analyse(Path("a"), len(x) / SR, pcm=_pcm_of(x))
    assert r["tail"] == "silence" and r["end"] < len(x) / SR - 10
    short = _song(head=30, mid=70, tail=30)                          # would trim > 25 %: left whole
    r = ms.analyse(Path("a"), len(short) / SR, pcm=_pcm_of(short))
    assert r["head"] is None and r["tail"] is None


def test_beat_snap():
    x = _song(mid=120, tail=30)
    dur = len(x) / SR
    beats = [i * 0.5 for i in range(int(dur * 2))]
    r = ms.analyse(Path("a"), dur, beats=beats, pcm=_pcm_of(x))
    assert r["tail"] and r["end"] % 0.5 == 0


def test_run_writes_into_analysis(tmp_path, monkeypatch):
    from app.music_brain.analysis import analyzer
    p = tmp_path / "a.wav"
    p.write_bytes(b"x")
    cp = tmp_path / "rec.json"
    cp.write_text(json.dumps({"duration": 100.0, "beat_times": []}))
    monkeypatch.setattr(analyzer, "_cache_path_for", lambda path: cp)
    span = {"v": 1, "start": 0.0, "end": 80.0, "head": None, "tail": "speech"}
    r = ms.run({"a": p}, None, span_fn=lambda path, rec: span)
    assert r["trimmed"] == 1 and r["tail"] == 1
    assert json.loads(cp.read_text())["music_span"] == span
    assert ms.run({"a": p}, None, span_fn=lambda *a: 1 / 0)["judged"] == 0          # already judged: skipped
    analyzer.TrackAnalysis(path="p", duration=1, bpm=1, music_span=span)            # the record still loads
