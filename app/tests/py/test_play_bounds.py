"""CL-6: the analysis endpoints carry dj-core's song_bounds as play_bounds; no bounds rule lives in the server."""
import pytest

from app.music_brain import djcore_bridge, play_bounds


def test_attach_adds_service_answer(monkeypatch):
    seen = {}

    def fake(fn, args=None, timeout=0):
        seen["fn"], seen["n"] = fn, len(args["items"])
        return [{"play_start": 8.0, "play_end": 190.0}] * len(args["items"])

    monkeypatch.setattr(djcore_bridge, "call", fake)
    a, b = {"duration": 200}, {"duration": 100}
    play_bounds.attach([a, None, b])
    assert seen == {"fn": "song_bounds", "n": 2}                       # one batched call, nulls skipped
    assert a["play_bounds"]["play_start"] == 8.0 and b["play_bounds"]["play_end"] == 190.0


def test_unavailable_service_leaves_payload_unbounded(monkeypatch):
    def boom(*a, **k):
        raise djcore_bridge.DjCoreUnavailable("down")

    monkeypatch.setattr(djcore_bridge, "call", boom)
    a = {"duration": 200}
    play_bounds.attach([a])
    assert "play_bounds" not in a


@pytest.mark.needs_djcore
def test_real_service_trims_span():
    out = play_bounds.bounds_for([{"duration": 200, "music_span": {"v": 1, "start": 8, "end": 190}}, {"duration": 100}])
    assert out[0]["play_start"] == 8.0 and out[0]["play_end"] == 190.0 and out[0]["play_duration"] == 182.0
    assert out[1]["play_start"] == 0.0 and out[1]["play_end"] == 100.0
