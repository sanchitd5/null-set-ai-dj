"""Pre-render scheduler: order, one heavy job at a time, cancellation, cache hits. Stub io, no Demucs."""
from app.ui.services.prerender import STEMS_WAIT_S, Prerender


class FakeIO:
    def __init__(self):
        self.stems = set()                 # track ids with cached stems
        self.queued = []                   # (tid, urgent) in the order queued
        self.dropped = []
        self.running_stems = False
        self.tempo = {}                    # (tid, bpm) -> state
        self.started = []

    def stems_ready(self, tid): return tid in self.stems
    def queue_stems(self, tid, urgent): self.queued.append((tid, urgent))
    def drop_stems(self, tid): self.dropped.append(tid); return True
    def stems_running(self): return self.running_stems
    def heavy_now(self): return int(self.running_stems) + sum(1 for s in self.tempo.values() if s == "running")
    def tempo_state(self, tid, bpm): return self.tempo.get((tid, bpm), "none")
    def start_tempo(self, tid, bpm): self.tempo[(tid, bpm)] = "running"; self.started.append((tid, bpm))


def test_server_dequeue_and_endpoint(monkeypatch, tmp_path):
    import threading

    from app.ui.services import prerender
    from app.ui import server as srv

    monkeypatch.setattr(srv, "_stem_queue", ["u1", "u2"])
    monkeypatch.setattr(srv, "_stem_backlog", ["b1"])
    monkeypatch.setattr(srv, "_stem_cv", threading.Condition())
    assert srv._dequeue_stems_impl("u2") and srv._stem_queue == ["u1"]
    assert srv._dequeue_stems_impl("b1") and srv._stem_backlog == []
    assert not srv._dequeue_stems_impl("nope")
    monkeypatch.setitem(srv._tracks, "t1", tmp_path / "t1.wav")
    monkeypatch.setattr(srv, "_start_prerender_worker", lambda: None)
    prerender.reset()
    monkeypatch.setattr(prerender, "ServerIO", FakeIO)
    body = srv.post_prerender(srv.PrerenderRequest(items=[{"track_id": "t1", "bpms": [120]}, {"track_id": "unknown"}]))
    assert [i["track_id"] for i in body["items"]] == ["t1"] and body["items"][0]["stems"] is False
    prerender.reset()


def make():
    t = {"now": 0.0}
    io = FakeIO()
    return Prerender(io, lambda: t["now"]), io, t


def test_stems_queued_in_rank_order_head_urgent():
    p, io, _ = make()
    p.set_items([{"track_id": "a", "bpms": []}, {"track_id": "b", "bpms": []}])
    p.step()
    assert io.queued == [("a", True), ("b", False)]
    p.step()
    assert io.queued == [("a", True), ("b", False)]            # asked once


def test_tempo_renders_one_at_a_time_after_stems_best_first():
    p, io, t = make()
    p.set_items([{"track_id": "a", "bpms": [120.2, 118.0]}, {"track_id": "b", "bpms": [120.0]}])
    io.stems |= {"a", "b"}
    p.step()
    assert io.started == [("a", 120.0)]                        # rounded to 0.5, best-ranked first
    p.step()
    assert io.started == [("a", 120.0)]                        # still running: no second render
    t["now"] = 30.0
    io.tempo[("a", 120.0)] = "done"
    p.step()
    assert io.started == [("a", 120.0), ("a", 118.0)]
    assert io.heavy_now() == 1
    t["now"] = 60.0
    io.tempo[("a", 118.0)] = "done"
    p.step()
    io.tempo[("b", 120.0)] = "done"
    t["now"] = 61.0
    p.step()
    st = {s["track_id"]: s for s in p.status()}
    assert st["a"]["ready"] and st["b"]["ready"]
    assert p.stats()["heavy_max"] == 1 and p.tempo_rendered[0]["seconds"] == 30.0


def test_tempo_yields_to_a_running_separation_but_not_forever():
    p, io, t = make()
    p.set_items([{"track_id": "a", "bpms": [120.0]}])
    io.stems.add("a")
    io.running_stems = True
    p.step()                                                    # stems seen at t=0
    assert io.started == []
    t["now"] = STEMS_WAIT_S + 1
    p.step()
    assert io.started == [("a", 120.0)]


def test_cache_hit_renders_nothing():
    p, io, _ = make()
    p.set_items([{"track_id": "a", "bpms": [120.0]}])
    io.stems.add("a")
    io.tempo[("a", 120.0)] = "done"
    p.step()
    assert io.started == [] and p.status()[0]["ready"] and p.stats()["tempo_renders"] == 0


def test_dropped_candidate_cancels_queued_separation_and_is_counted():
    p, io, _ = make()
    p.set_items([{"track_id": "a", "bpms": []}, {"track_id": "b", "bpms": []}])
    p.step()
    p.set_items([{"track_id": "b", "bpms": []}])                # a left the pool
    assert io.dropped == ["a"] and p.dropped_before_ready == ["a"]
    p.step()
    assert [s["track_id"] for s in p.status()] == ["b"]


def test_dropped_candidate_tempo_render_in_flight_is_not_stacked_on():
    p, io, t = make()
    p.set_items([{"track_id": "a", "bpms": [120.0]}, {"track_id": "b", "bpms": [120.0]}])
    io.stems |= {"a", "b"}
    p.step()                                                    # a renders
    p.set_items([{"track_id": "b", "bpms": [120.0]}])
    p.step()
    assert io.started == [("a", 120.0)]                         # b waits for a's render to end
    io.tempo[("a", 120.0)] = "done"
    p.step()
    assert io.started[-1] == ("b", 120.0)


def test_error_state_is_final_not_retried():
    p, io, _ = make()
    p.set_items([{"track_id": "a", "bpms": [150.0]}])
    io.stems.add("a")
    io.tempo[("a", 150.0)] = "error: tempo gap too large for a key-locked stretch"
    p.step()
    s = p.status()[0]
    assert s["failed"] and not s["ready"] and io.started == []


def test_bad_bpms_and_limits_are_ignored():
    p, io, _ = make()
    st = p.set_items([{"track_id": "a", "bpms": ["x", 5, 1000, 120.0, 120.1]}] + [{"track_id": f"t{i}", "bpms": []} for i in range(10)])
    assert len(st) == 6
    assert list(st[0]["tempo"]) == ["120.0"]
