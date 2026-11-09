"""job_registry: scheduler, idle signal, controls, persistence. A fake clock, a LeaseStore on the same clock and a
`spawn` that runs the job inline, so nothing sleeps and no thread is started (except the one shutdown test)."""
import threading

import pytest

from app.music_brain import db
from app.ui.services import job_registry as jr
from app.ui.services import set_lease
from app.ui.services.job_registry import JobError, JobSpec, JobStop, Registry, SettingSpec

HOLDER = {"kind": "device", "key": "device:p1", "name": "phone"}


class Clock:
    def __init__(self):
        self.t = 10_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    store = set_lease.LeaseStore(tmp_path / "lease.json", clock=clock, log=lambda *a, **k: None)
    set_lease.use(store)
    made = []
    spawned = []

    def make(inline=True, **environ):
        e = {"DJ_IDLE_GRACE_S": "30", "DJ_JOB_CANCEL_GRACE_S": "30"}
        e.update(environ)

        def spawn(name, fn):
            if inline:
                fn()
            else:
                spawned.append(fn)

        r = Registry(tmp_path, clock, spawn=spawn, environ=e)
        set_lease.add_listener(r._on_lease)
        made.append(r)
        return r

    yield make, clock, store, spawned
    for r in made:
        set_lease.remove_listener(r._on_lease)
    set_lease.use(None)
    db.close_all()


def _spec(name="j", fn=None, **kw):
    return JobSpec(name, name.title(), fn or (lambda ctx: None), **kw)


def test_periodic_due_and_not_due(env):
    make, clock, *_ = env
    r = make()
    runs = []
    r.register(_spec(kind="periodic", interval_s=100, fn=lambda ctx: runs.append(clock.t)))
    r.tick()
    assert runs == []                                  # first slot is one interval away
    clock.t += 99
    r.tick()
    assert runs == []
    clock.t += 1
    r.tick()
    assert len(runs) == 1
    r.tick()
    assert len(runs) == 1                              # not due again until another interval
    clock.t += 100
    r.tick()
    assert len(runs) == 2
    v = r.view("j")
    assert v["state"] == "idle" and v["last"]["status"] == "ok" and v["next_run"] == clock.t + 100


def test_run_at_start_runs_on_first_tick(env):
    make, *_ = env
    r = make()
    runs = []
    r.register(_spec("q", kind="periodic", interval_s=86400, run_at_start=True, fn=lambda ctx: runs.append(1) or {"n": 1}))
    r.tick()
    assert runs == [1] and r.view("q")["last"]["summary"] == {"n": 1}


def test_manual_runs_only_on_run_now(env):
    make, *_ = env
    r = make()
    runs = []
    r.register(_spec(kind="manual", fn=lambda ctx: runs.append(1)))
    r.tick()
    assert runs == []
    assert r.run_now("j")["last"]["status"] == "ok" and runs == [1]


def test_idle_only_waits_for_grace(env):
    make, clock, *_ = env
    r = make()
    runs = []
    r.register(_spec(kind="idle_worker", idle_only=True, fn=lambda ctx: runs.append(1)))
    r.tick()                                           # registry just born: grace not over
    assert runs == [] and r.idle().reason == "grace"
    clock.t += 29
    r.tick()
    assert runs == []
    clock.t += 1
    r.tick()
    assert runs == [1] and r.idle().idle


def test_non_idle_only_ignores_the_gate(env):
    make, clock, store, _ = env
    r = make()
    runs = []
    r.register(_spec(kind="periodic", interval_s=10, fn=lambda ctx: runs.append(1)))
    store.acquire("phone", "p", HOLDER)
    clock.t += 10
    r.tick()
    assert runs == [1]


def test_lease_acquire_stops_idle_job_and_release_restarts_with_checkpoint(env):
    make, clock, store, _ = env
    r = make()
    seen = []

    def fn(ctx):
        seen.append(dict(ctx.checkpoint))
        for i in range(ctx.checkpoint.get("i", 0), 100):
            ctx.checkpoint["i"] = i
            if i == 3 and len(seen) == 1:
                store.acquire("phone", "p", HOLDER)    # a set starts mid-run
            if ctx.should_stop():
                ctx.save_checkpoint()
                return
        ctx.checkpoint["done"] = True

    r.register(_spec(kind="idle_worker", idle_only=True, fn=fn))
    clock.t += 30
    r.tick()
    assert r.view("j")["last"]["status"] == "stopped"
    assert r.view("j")["state"] == "idle"
    assert seen == [{}]
    r.tick()
    assert len(seen) == 1                              # lease live: not restarted
    assert r.idle().reason == "set_live"
    lease = store.status()["lease"]
    assert lease is not None
    store.force_close("admin")
    clock.t += 29
    r.tick()
    assert len(seen) == 1                              # grace not over
    clock.t += 1
    r.tick()
    assert seen[1] == {"i": 3}                         # resumed from the saved checkpoint
    assert r.view("j")["last"]["status"] == "ok"


def test_should_stop_flips_at_once_on_acquire(env):
    make, clock, store, spawned = env
    r = make(inline=False)
    ctxs = []
    r.register(_spec(kind="idle_worker", idle_only=True, fn=lambda ctx: ctxs.append(ctx)))
    clock.t += 30
    r.tick()
    spawned.pop()()                                    # runs fn: captures ctx
    ctx = ctxs[0]
    assert ctx.should_stop() is False
    store.acquire("phone", "p", HOLDER)                # listener: no tick needed
    assert ctx.should_stop() is True


def test_tick_asks_a_running_idle_job_to_stop_when_busy(env):
    make, clock, store, spawned = env
    r = make(inline=False)
    r.register(_spec(kind="idle_worker", idle_only=True))
    clock.t += 30
    r.tick()
    assert r.view("j")["state"] == "running"
    store.acquire("phone", "p", HOLDER)
    r.tick()
    assert r.view("j")["state"] == "cancelling"


def test_pause_persists_across_registries_and_resume(env):
    make, clock, *_ = env
    r = make()
    runs = []
    r.register(_spec(kind="periodic", interval_s=10, fn=lambda ctx: runs.append(1)))
    assert r.pause("j")["state"] == "paused"
    clock.t += 50
    r.tick()
    assert runs == []
    r2 = make()
    r2.register(_spec(kind="periodic", interval_s=10, fn=lambda ctx: runs.append(1)))
    assert r2.view("j")["paused"] and r2.view("j")["state"] == "paused"
    r2.tick()
    assert runs == []
    assert r2.run_now.__self__ is r2
    with pytest.raises(JobError) as ei:
        r2.run_now("j")
    assert ei.value.status == 409 and ei.value.code == "paused"
    assert r2.resume("j")["state"] == "idle"
    clock.t += 50
    r2.tick()
    assert runs == [1]


def test_pause_a_running_job_ends_paused(env):
    make, clock, *_ = env
    r = make()

    def fn(ctx):
        r.pause("j")
        assert ctx.should_stop()
        raise JobStop()

    r.register(_spec(kind="manual", fn=fn))
    r.run_now("j")
    v = r.view("j")
    assert v["last"]["status"] == "paused" and v["state"] == "paused"


def test_cancel_ends_cancelled_and_next_slot_runs(env):
    make, clock, *_ = env
    r = make()
    n = []

    def fn(ctx):
        n.append(1)
        if len(n) == 1:
            r.cancel("j")
            return
        return "fine"

    r.register(_spec(kind="periodic", interval_s=10, fn=fn))
    clock.t += 10
    r.tick()
    assert r.view("j")["last"]["status"] == "cancelled" and r.view("j")["state"] == "idle"
    clock.t += 10
    r.tick()
    assert len(n) == 2 and r.view("j")["last"]["status"] == "ok"


def test_cancel_when_not_running_is_409(env):
    make, *_ = env
    r = make()
    r.register(_spec())
    with pytest.raises(JobError) as ei:
        r.cancel("j")
    assert ei.value.status == 409


def test_ignored_stop_is_stuck_after_grace(env):
    make, clock, _, spawned = env
    r = make(inline=False)
    r.register(_spec(kind="manual"))
    r.run_now("j")
    r.cancel("j")
    clock.t += 29
    r.tick()
    assert r.view("j")["state"] == "cancelling"
    clock.t += 1
    r.tick()
    assert r.view("j")["state"] == "stuck"
    assert not r.view("j")["controls"]["cancel"]
    spawned.pop()()                                    # it finally returns
    assert r.view("j")["state"] == "idle" and r.view("j")["last"]["status"] == "cancelled"


def test_double_run_now_is_409(env):
    make, _, _, spawned = env
    r = make(inline=False)
    r.register(_spec(kind="manual"))
    r.run_now("j")
    with pytest.raises(JobError) as ei:
        r.run_now("j")
    assert ei.value.status == 409 and ei.value.code == "running"
    spawned.pop()()


def test_run_now_when_busy_is_not_idle_409(env):
    make, clock, store, _ = env
    r = make()
    r.register(_spec(kind="manual", idle_only=True))
    clock.t += 30
    store.acquire("phone", "p", HOLDER)
    with pytest.raises(JobError) as ei:
        r.run_now("j")
    assert ei.value.status == 409 and ei.value.code == "not_idle"
    store.force_close("admin")
    with pytest.raises(JobError):                      # grace again
        r.run_now("j")
    clock.t += 30
    assert r.run_now("j")["last"]["status"] == "ok"


def test_unknown_job_404(env):
    make, *_ = env
    with pytest.raises(JobError) as ei:
        make().run_now("nope")
    assert ei.value.status == 404


def test_error_is_stored_and_next_run_still_happens(env):
    make, clock, *_ = env
    r = make()
    calls = []

    def fn(ctx):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    r.register(_spec(kind="periodic", interval_s=10, fn=fn))
    clock.t += 10
    r.tick()
    v = r.view("j")
    assert v["state"] == "error" and "boom" in v["last"]["error"] and v["last"]["status"] == "error"
    assert any("boom" in row["msg"] for row in r.log_tail("j"))
    clock.t += 10
    r.tick()
    assert r.view("j")["state"] == "idle" and r.view("j")["last"]["summary"] == {"ok": True}
    assert [x["status"] for x in r.runs("j")] == ["ok", "error"]


def test_settings_validation_and_live_read(env):
    make, *_ = env
    r = make()
    got = []

    def fn(ctx):
        got.append((ctx.settings["n"], ctx.setting("n")))

    r.register(_spec(kind="manual", fn=fn, settings={
        "n": SettingSpec("int", 5, 1, 10), "f": SettingSpec("float", 0.5, 0, 1),
        "b": SettingSpec("bool", False), "s": SettingSpec("str", "x")}))
    assert r.view("j")["settings"]["n"] == {"value": 5, "default": 5, "min": 1, "max": 10, "type": "int"}
    for bad in ({"zz": 1}, {"n": 11}, {"n": 0}, {"n": "3"}, {"n": 2.5}, {"n": True}, {"b": 1}, {"s": 3}, {"f": 2}):
        with pytest.raises(ValueError):
            r.set_settings("j", bad)
    assert r.view("j")["settings"]["n"]["value"] == 5
    r.set_settings("j", {"n": 7, "f": 1, "b": True, "s": "y"})
    r.run_now("j")
    assert got == [(7, 7)]
    r2 = make()                                        # persisted
    r2.register(_spec(kind="manual", settings={"n": SettingSpec("int", 5, 1, 10)}))
    assert r2.view("j")["settings"]["n"]["value"] == 7


def test_log_ring_and_run_history_caps(env):
    make, clock, *_ = env
    r = make()

    def fn(ctx):
        for i in range(jr.LOG_KEEP + 50):
            ctx.log(f"line {i}")
        ctx.progress(3, 4, "note")

    r.register(_spec(kind="manual", fn=fn))
    r.run_now("j")
    rows = r.log_tail("j", 1000)
    assert len(rows) == jr.LOG_KEEP and rows[-1]["msg"] == f"line {jr.LOG_KEEP + 49}"
    assert [x["msg"] for x in r.log_tail("j", 2)] == [f"line {jr.LOG_KEEP + 48}", f"line {jr.LOG_KEEP + 49}"]
    assert r.view("j")["progress"] == {"done": 3, "total": 4, "note": "note"}
    for _ in range(jr.RUN_KEEP + 5):
        r.run_now("j")
    assert len(r.runs("j", 1000)) == jr.RUN_KEEP


def test_disabled_runner_runs_nothing_but_shows_external(env):
    make, clock, *_ = env
    r = make(DJ_JOBS="0")
    runs = []
    r.register(_spec(kind="periodic", interval_s=1, fn=lambda ctx: runs.append(1)))
    r.register(JobSpec("ext", "Ext", kind="external", status_fn=lambda: {"busy": None}))
    clock.t += 10
    r.tick()
    assert runs == [] and r.view("j")["state"] == "disabled" and r.view("ext")["state"] == "idle"
    with pytest.raises(JobError) as ei:
        r.run_now("j")
    assert ei.value.code == "disabled"


def test_external_job_status_pause_resume(env):
    make, *_ = env
    r = make()
    calls = []
    r.register(JobSpec("ext", "Ext", kind="external", status_fn=lambda: {"done": 2, "queued": 3, "busy": "t1"},
                       pause_fn=lambda: calls.append("p"), resume_fn=lambda: calls.append("r")))
    v = r.view("ext")
    assert v["state"] == "running" and v["progress"]["done"] == 2 and v["progress"]["total"] == 5
    assert v["controls"] == {"pause": True, "resume": False, "cancel": False, "run": False}
    assert r.pause("ext")["state"] == "paused"
    with pytest.raises(JobError):
        r.run_now("ext")
    with pytest.raises(JobError):
        r.cancel("ext")
    r.resume("ext")
    assert calls == ["p", "r"]
    r.pause("ext")
    r2 = make()                                        # a pause outlives a restart: pause_fn is called again
    r2.register(JobSpec("ext", "Ext", kind="external", pause_fn=lambda: calls.append("p2")))
    assert calls[-1] == "p2" and r2.view("ext")["paused"]


def test_observe_only_external_has_no_controls(env):
    make, *_ = env
    r = make()
    r.register(JobSpec("pub", "Pub", kind="external", status_fn=lambda: {"enabled": False}))
    assert r.view("pub")["controls"] == {"pause": False, "resume": False, "cancel": False, "run": False}
    with pytest.raises(JobError):
        r.pause("pub")


def test_register_rejects_bad_specs(env):
    make, *_ = env
    r = make()
    with pytest.raises(ValueError):
        r.register(JobSpec("a", "A", kind="periodic", fn=lambda c: 1))
    with pytest.raises(ValueError):
        r.register(JobSpec("a", "A", kind="manual"))
    with pytest.raises(ValueError):
        r.register(JobSpec("a", "A", kind="weird", fn=lambda c: 1))


def test_a_listener_error_never_breaks_a_lease_change(env, capsys):
    _, _, store, _ = env

    def bad():
        raise RuntimeError("nope")

    set_lease.add_listener(bad)
    try:
        lease, conflict = store.acquire("phone", "p", HOLDER)
        assert lease is not None and conflict is None
    finally:
        set_lease.remove_listener(bad)
    assert "listener failed" in capsys.readouterr().out


def test_lease_changes_notify_listeners(env):
    _, _, store, _ = env
    hits = []
    set_lease.add_listener(lambda: hits.append(1))
    try:
        lease, _ = store.acquire("phone", "p", HOLDER)
        store.takeover("tablet", "t", HOLDER)
        store.release(store.status() and store._data["current"]["id"])
        lease, _ = store.acquire("phone", "p", HOLDER)
        store.force_close("admin")
    finally:
        set_lease._listeners.clear()
    assert len(hits) == 5


def test_shutdown_joins_every_thread(env, tmp_path):
    make, clock, store, _ = env
    before = {t.name for t in threading.enumerate()}
    r = Registry(tmp_path, clock, environ={"DJ_JOBS_TICK_S": "0.05", "DJ_IDLE_GRACE_S": "0"})
    started, stopped = threading.Event(), threading.Event()

    def fn(ctx):
        started.set()
        while not ctx.should_stop():
            threading.Event().wait(0.01)
        stopped.set()

    r.register(_spec("w", kind="idle_worker", fn=fn))
    r.start()
    assert started.wait(30)
    assert any(t.name == "jobs-scheduler" for t in threading.enumerate())
    r.shutdown(timeout=30)                              # generous: the bench can be loaded
    assert stopped.is_set()
    after = {t.name for t in threading.enumerate()}
    assert not ({"jobs-scheduler", "job-w"} & after) and after <= before | {"MainThread"}
    assert r._on_lease not in set_lease._listeners
