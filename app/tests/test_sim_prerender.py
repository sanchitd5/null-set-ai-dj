"""The sim's readiness model (world.py: separation and key-lock render latency on the virtual clock) and the
pre-render metrics (runlog._prepare_facts, scorer). No network, no Demucs."""
import pytest

from app.sim import runlog, scorer
from app.sim.world import World


@pytest.fixture
def world(tmp_path):
    from app.ui import server

    w = World("library", "t", 1, tmp_path)
    h1, h2, h3 = "1" * 32, "2" * 32, "3" * 32                 # audio hashes are 64 hex chars in the app; only the 24-char prefix matters
    for tid, h in (("t1", h1), ("t2", h2), ("t3", h3), ("t1b", h1)):
        w._tracks[tid] = {"hash": h, "name": tid}
        w._synth.setdefault(h, {"mix": "m", "stems": {n: f"/x/{h}/{n}.wav" for n in ("drums", "bass", "vocals", "other")}})
    server._stem_cache.clear()
    yield w
    server._stem_cache.clear()


def _have(tid):
    from app.ui import server

    return tid in server._stem_cache


def test_separation_is_serial_and_lands_after_the_modelled_time(world):
    w = world
    w.set_time(0.0)
    w._enqueue_stems("t1", w._tracks["t1"])
    w._enqueue_stems("t2", w._tracks["t2"])
    w.set_time(World.SEP_S - 1)
    assert not _have("t1") and w.stems_running()
    w.set_time(World.SEP_S + 0.1)
    assert _have("t1") and not _have("t2")                    # one worker: t2 starts only when t1 is done
    w.set_time(2 * World.SEP_S + 0.1)
    assert _have("t2") and not w.stems_running()


def test_repeat_of_a_separated_song_is_a_cache_hit(world):
    w = world
    w.set_time(0.0)
    w._enqueue_stems("t1", w._tracks["t1"])
    w.set_time(World.SEP_S + 1)
    w._enqueue_stems("t1b", w._tracks["t1b"])                 # the same audio under another track id
    assert _have("t1b") and not w.stems_running()


def test_drop_cancels_a_queued_job_but_not_a_running_one(world):
    w = world
    w.set_time(0.0)
    w._enqueue_stems("t1", w._tracks["t1"])
    w._enqueue_stems("t2", w._tracks["t2"])
    w._enqueue_stems("t3", w._tracks["t3"])
    w.set_time(5.0)
    assert not w.drop_stems("t1")                             # running
    assert w.drop_stems("t2")                                 # queued
    w.set_time(2 * World.SEP_S + 0.1)
    assert _have("t1") and _have("t3") and not _have("t2")    # t3 moved up into t2's slot


def test_tempo_gate_serial_from_first_ask(world):
    w = world
    w.set_time(10.0)
    assert not w.tempo_gate("tAAAA_120p0")
    w.set_time(12.0)
    assert not w.tempo_gate("tBBBB_120p0")                    # asked later: waits behind the first
    assert w.tempo_running() == 1
    w.set_time(10.0 + World.TEMPO_S + 0.1)
    assert w.tempo_gate("tAAAA_120p0") and not w.tempo_gate("tBBBB_120p0")
    w.set_time(10.0 + 2 * World.TEMPO_S + 0.1)
    assert w.tempo_gate("tBBBB_120p0")


def test_report_counts_waste_and_peak_concurrency(world):
    w = world
    w.set_time(0.0)
    w._enqueue_stems("t1", w._tracks["t1"])
    w._enqueue_stems("t2", w._tracks["t2"])                   # never played
    w.tempo_gate("t" + "1" * 24 + "_120p0")                    # a tempo set of the played song
    w.tempo_gate("t" + "2" * 24 + "_120p0")                    # a tempo set of the wasted one
    w.set_time(200.0)
    rep = w.prerender_report({"1" * 32})
    assert rep["wasted_render_seconds"] == World.SEP_S + World.TEMPO_S
    assert rep["max_concurrent_heavy_jobs"] == 2              # a separation and a key-locked render overlap
    assert rep["stems_jobs"] == 2 and rep["tempo_jobs"] == 2


def test_prepare_facts_read_the_console_line():
    lines = ["merge deferred: waiting for stems (budget 80 s)",
             "prepare ready: A - B at_booking=0 defer_s=37 stems=1 tempo=1 gave_up=0 skip=0"]
    f = runlog._prepare_facts(lines)
    assert f == {"prep_seen": True, "prep_at_booking": False, "prep_defer_s": 37.0, "prep_gave_up": False, "prep_skip": False}
    assert runlog._prepare_facts(["nothing"])["prep_seen"] is False


def test_prerender_metrics_are_informational():
    from app.tests.test_sim_scorer import _run, _score, _t

    def t(i, at, defer, gave=False, skip=False):
        return _t(i, prep_seen=True, prep_at_booking=at, prep_defer_s=defer, prep_gave_up=gave, prep_skip=skip)

    run = _run([t(1, True, 0), t(2, False, 40), t(3, False, 100, gave=True), t(4, False, 0, skip=True)])
    run["prerender"] = {"wasted_render_seconds": 56.0, "max_concurrent_heavy_jobs": 1}
    m = _score(run)["metrics"]
    assert m["ready_at_booking_share"] == 0.333           # 1 of the 3 transitions where a merge was possible
    assert m["defer_seconds"] == 140.0 and m["deferred_transitions"] == 2 and m["defer_gave_up"] == 1
    assert m["wasted_render_seconds"] == 56.0 and m["max_concurrent_heavy_jobs"] == 1
    plain = _score(_run([t(1, True, 0), t(2, False, 40), t(3, False, 100, gave=True), t(4, False, 0, skip=True)]))
    assert plain["score"] == _score(run)["score"]         # the score never moves
