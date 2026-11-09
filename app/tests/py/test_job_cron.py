"""Cron scheduling in job_registry: a fake clock, an inline spawn. Fires, overlap skip, missed run once, idle defer /
skip, max runtime, overrides and their persistence."""
import pytest

from app.tests.py.test_job_registry import HOLDER, env  # noqa: F401  (the fixture and a lease holder)
from app.ui.services import cron
from app.ui.services import job_registry as jr
from app.ui.services.job_registry import JobError, JobSpec

HOUR = 3600.0


def _spec(fn=None, **kw):
    kw.setdefault("schedule", "0 * * * *")
    kw.setdefault("kind", "manual")
    return JobSpec("j", "J", fn or (lambda ctx: "done"), **kw)


def _tick_at(r, clock, t, store=None, lid=None):
    """Move the clock, keep a live set's lease fresh (its TTL is 60 s), then run one scheduler pass."""
    clock.t = t
    if lid:
        store.beat(lid)
    r.tick()


def _hour_start(clock, hours=1):
    """Put the fake clock a little past a whole hour (local time) and return that hour's epoch."""
    h = cron.next_fire("0 * * * *", clock.t) + (hours - 1) * HOUR
    clock.t = h + 10
    return h


def test_register_rejects_a_bad_schedule(env):  # noqa: F811
    make, *_ = env
    r = make()
    with pytest.raises(ValueError):
        r.register(_spec(schedule="61 * * * *"))
    with pytest.raises(ValueError):
        r.register(JobSpec("e", "E", kind="external", schedule="0 * * * *"))
    with pytest.raises(ValueError):
        r.register(_spec(max_runtime_s=-1))


def test_fires_on_schedule_and_records_fire_times(env):  # noqa: F811
    make, clock, *_ = env
    r = make()
    runs = []
    h = _hour_start(clock)
    r.register(_spec(lambda ctx: runs.append(1)))
    nf = r.view("j")["schedule"]["next_fire"]
    assert nf == h + HOUR and r.view("j")["next_run"] == nf
    r.tick()
    assert runs == []                                     # not due
    clock.t = nf + 5
    r.tick()
    assert runs == [1]
    v = r.view("j")
    assert v["last"]["status"] == "ok" and v["schedule"]["last_fire"] == nf + 5 and v["schedule"]["next_fire"] == nf + HOUR
    r.tick()
    assert runs == [1]                                    # same hour: not again


def test_state_survives_a_restart_and_history_is_kept(env):  # noqa: F811
    make, clock, *_ = env
    r = make()
    _hour_start(clock)
    r.register(_spec())
    nf = r.view("j")["schedule"]["next_fire"]
    clock.t = nf + 1
    r.tick()
    r2 = make()
    r2.register(_spec())
    v = r2.view("j")
    assert v["schedule"]["last_fire"] == nf + 1 and v["schedule"]["next_fire"] == nf + HOUR
    assert v["last"]["status"] == "ok" and [h["status"] for h in v["history"]] == ["ok"]


def test_missed_run_happens_once_at_startup_never_a_backlog(env):  # noqa: F811
    make, clock, *_ = env
    runs = []
    r = make()
    _hour_start(clock)
    r.register(_spec(lambda ctx: runs.append(1)))         # persists next_fire
    down_until = clock.t + 5 * HOUR                       # down for five fires
    clock.t = down_until
    r2 = make()
    r2.register(_spec(lambda ctx: runs.append(1)))
    r2.tick()
    assert runs == [1]                                    # one catch-up run
    r2.tick()
    clock.t += 30
    r2.tick()
    assert runs == [1]
    assert r2.view("j")["schedule"]["next_fire"] > down_until


def test_overlap_is_skipped_and_logged(env):  # noqa: F811
    make, clock, store, spawned = env
    r = make(inline=False)
    _hour_start(clock)
    r.register(_spec())
    nf = r.view("j")["schedule"]["next_fire"]
    clock.t = nf + 1
    r.tick()
    assert r.view("j")["state"] == "running"
    clock.t = nf + HOUR + 1
    r.tick()                                              # the next fire while still running
    h = r.runs("j")
    assert [x["status"] for x in h] == ["skipped"] and "still running" in h[0]["summary"]
    assert len(spawned) == 1                              # no second run queued
    assert r.view("j")["schedule"]["next_fire"] == nf + 2 * HOUR


def test_idle_only_defers_then_runs_in_the_same_hour(env):  # noqa: F811
    make, clock, store, _ = env
    runs = []
    r = make()
    _hour_start(clock)
    r.register(_spec(lambda ctx: runs.append(1), idle_only=True))
    nf = r.view("j")["schedule"]["next_fire"]
    lid = store.acquire("phone", "p", HOLDER)[0]["id"]
    _tick_at(r, clock, nf + 1, store, lid)
    assert runs == [] and any("deferred" in m["msg"] for m in r.log_tail("j"))
    _tick_at(r, clock, nf + 1800, store, lid)
    assert runs == []                                     # still busy
    clock.t = nf + 1900
    store.release(lid)
    _tick_at(r, clock, nf + 1900)
    assert runs == []                                     # idle grace (30 s) not over yet
    _tick_at(r, clock, nf + 1900 + 31)
    assert runs == [1] and r.runs("j")[0]["status"] == "ok"


def test_idle_only_skips_when_busy_all_hour(env):  # noqa: F811
    make, clock, store, _ = env
    runs = []
    r = make()
    _hour_start(clock)
    r.register(_spec(lambda ctx: runs.append(1), idle_only=True))
    nf = r.view("j")["schedule"]["next_fire"]
    lid = store.acquire("phone", "p", HOLDER)[0]["id"]
    for t in range(1, int(HOUR), 50):
        _tick_at(r, clock, nf + t, store, lid)
    assert runs == []
    _tick_at(r, clock, nf + HOUR + 1, store, lid)         # the deadline (next fire) passed
    st = [h["status"] + "|" + h["summary"] for h in r.runs("j")]
    assert "skipped|skipped: busy" in st and runs == []


def test_max_runtime_stops_the_job_cleanly(env):  # noqa: F811
    make, clock, store, spawned = env
    r = make(inline=False)
    _hour_start(clock)
    r.register(_spec(lambda ctx: "partial", max_runtime_s=3000))
    nf = r.view("j")["schedule"]["next_fire"]
    clock.t = nf + 1
    r.tick()
    clock.t = nf + 1 + 2999
    r.tick()
    assert r._jobs["j"].stop_at is None
    clock.t = nf + 1 + 3000
    r.tick()
    assert r._jobs["j"].stop_at is not None
    spawned[0]()                                          # the job returns at its next boundary
    assert r.view("j")["last"]["status"] == "timeout"


def test_disabled_does_not_fire_and_enabling_never_backfills(env):  # noqa: F811
    make, clock, *_ = env
    runs = []
    r = make()
    _hour_start(clock)
    r.register(_spec(lambda ctx: runs.append(1)))
    r.set_schedule("j", {"enabled": False})
    assert r.view("j")["next_run"] is None
    clock.t += 3 * HOUR
    r.tick()
    assert runs == []
    r.set_schedule("j", {"enabled": True})
    r.tick()
    assert runs == []                                     # counted from now
    assert r.view("j")["schedule"]["next_fire"] > clock.t


def test_set_schedule_validates_persists_and_clears(env):  # noqa: F811
    make, clock, *_ = env
    r = make()
    r.register(_spec())
    for bad in ({"schedule": "nope"}, {"schedule": "* * * * 9"}, {"enabled": 1}, {"max_runtime_s": "5"}, {"x": 1}, {}):
        with pytest.raises(ValueError):
            r.set_schedule("j", bad)
    v = r.set_schedule("j", {"schedule": "  */10   * * * * ", "max_runtime_s": 120, "idle_only": True})
    s = v["schedule"]
    assert s["expr"] == "*/10 * * * *" and s["max_runtime_s"] == 120 and v["idle_only"] and s["default"] == "0 * * * *"
    assert s["next_fire"] - clock.t <= 600
    r2 = make()
    r2.register(_spec())
    assert r2.view("j")["schedule"]["expr"] == "*/10 * * * *" and r2.view("j")["idle_only"]
    v = r2.set_schedule("j", {"schedule": None})
    assert v["schedule"]["expr"] is None and v["next_run"] is None
    r2.tick()                                             # a manual job without a schedule never starts by itself
    assert r2.runs("j") == []
    with pytest.raises(JobError):
        r2.register(JobSpec("x", "X", kind="external"))
        r2.set_schedule("x", {"enabled": False})


def test_paused_job_does_not_fire_or_backfill(env):  # noqa: F811
    make, clock, *_ = env
    runs = []
    r = make()
    _hour_start(clock)
    r.register(_spec(lambda ctx: runs.append(1)))
    r.pause("j")
    clock.t += 2 * HOUR
    r.tick()
    r.resume("j")
    r.tick()
    assert runs == []
