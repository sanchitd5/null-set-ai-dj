"""PERFB: the request pool stays free for work that needs it (services/load_shed.py), the set lease never
fsyncs under the lock the gate takes, key-lock renders are capped and their orphans swept, and
GET /api/sessions stops re-reading every set per request."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import anyio
import httpx
import pytest
from fastapi import FastAPI

from app.music_brain import history
from app.music_brain.audio import keylock, keylock_cache as kc
from app.music_brain.learning import history_api
from app.ui.services import load_shed, set_lease
from app.ui.services.build_info import router as build_router


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


# ---- async routes skip a saturated pool ---------------------------------------------------------

def test_async_status_routes_answer_while_the_pool_is_saturated(tmp_path, monkeypatch):
    from app.ui import server as srv

    set_lease.use(set_lease.LeaseStore(tmp_path / "lease.json"))
    app = FastAPI()
    app.include_router(build_router)
    app.include_router(set_lease.router)
    for path, fn in (("/api/stems/status", srv.get_stems_status), ("/api/llm/status", srv.get_llm_status),
                     ("/api/youtube/status", srv.get_youtube_status)):
        app.add_api_route(path, fn)
    release, started = threading.Event(), []

    @app.get("/block")
    def block():                                      # a plain def: runs in anyio's pool
        started.append(1)
        release.wait(20)
        return {}

    @app.get("/sync-build")
    def sync_build():
        return {}

    async def go():
        n = anyio.to_thread.current_default_thread_limiter().total_tokens
        async with _client(app) as c:
            blockers = [asyncio.create_task(c.get("/block", timeout=30)) for _ in range(n)]
            for _ in range(200):
                if len(started) >= n:
                    break
                await asyncio.sleep(0.05)
            assert len(started) >= n, "pool not saturated"
            # a sync route now queues for a thread ...
            queued = asyncio.create_task(c.get("/sync-build", timeout=30))
            await asyncio.sleep(0.2)
            assert not queued.done()
            # ... the async ones answer at once
            t0 = time.monotonic()
            for p in ("/api/build", "/api/set/lease", "/api/stems/status", "/api/llm/status", "/api/youtube/status"):
                r = await asyncio.wait_for(c.get(p), 2.0)
                assert r.status_code == 200, p
            beat = await asyncio.wait_for(c.post("/api/set/lease/nope/beat"), 2.0)
            assert beat.status_code == 409                # refused, but answered without a pool thread
            assert time.monotonic() - t0 < 2.0
            release.set()
            await asyncio.gather(*blockers, queued)

    try:
        asyncio.run(go())
    finally:
        release.set()
        set_lease.use(None)


# ---- the heavy-route semaphore ------------------------------------------------------------------

def test_bounded_caps_concurrency_and_leaves_pool_threads_free():
    active, peak, lock = 0, 0, threading.Lock()
    load_shed.stats.update(active=0, peak=0)

    app = FastAPI()

    @app.get("/heavy")
    @load_shed.bounded
    def heavy(i: int = 0):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.15)
        with lock:
            active -= 1
        return {"i": i}

    @app.get("/cheap")
    def cheap():
        return {"ok": True}

    async def go():
        async with _client(app) as c:
            jobs = [asyncio.create_task(c.get("/heavy", params={"i": i}, timeout=30)) for i in range(4 * load_shed.HEAVY_SLOTS)]
            await asyncio.sleep(0.05)
            t0 = time.monotonic()
            assert (await c.get("/cheap", timeout=5)).status_code == 200      # a pool thread is free
            assert time.monotonic() - t0 < 0.1
            rs = await asyncio.gather(*jobs)
            assert sorted(r.json()["i"] for r in rs) == list(range(4 * load_shed.HEAVY_SLOTS))

    asyncio.run(go())
    assert peak == load_shed.HEAVY_SLOTS == load_shed.stats["peak"]
    assert load_shed.stats["active"] == 0


def test_snapshot_serves_without_blocking():
    gate, calls = threading.Event(), []

    def slow():
        calls.append(1)
        gate.wait(5)
        return {"v": len(calls)}

    s = load_shed.Snapshot(slow, ttl=0.0, fallback={"v": 0})
    t0 = time.monotonic()
    assert s.get() == {"v": 0} and time.monotonic() - t0 < 0.1     # stale: answers now, refreshes behind
    s.get()
    assert len(calls) <= 1                                         # single flight
    gate.set()
    for _ in range(100):
        if s.value == {"v": 1}:
            break
        time.sleep(0.02)
    assert s.value == {"v": 1}


# ---- set lease: no disk under the lock ----------------------------------------------------------

@pytest.fixture
def slow_store(tmp_path):
    st = set_lease.LeaseStore(tmp_path / "lease.json", log=lambda *a, **k: None)
    entered, release, orig = threading.Event(), threading.Event(), st._save

    def slow_save(text):
        entered.set()
        release.wait(10)
        orig(text)

    st._save = slow_save
    yield st, entered, release
    release.set()


@pytest.mark.parametrize("op", ["acquire", "takeover", "beat", "release"])
def test_allows_does_not_block_on_a_slow_write(slow_store, op):
    st, entered, release = slow_store
    holder = {"kind": "local", "key": "k", "name": "n"}
    lease_id = "x"
    if op in ("beat", "release"):
        release.set()
        lease, _ = st.acquire("console", "d", holder)
        lease_id = lease["id"]
        release.clear()
        entered.clear()
    fn = {"acquire": lambda: st.acquire("console", "d", holder), "takeover": lambda: st.takeover("console", "d", holder),
          "beat": lambda: st.beat(lease_id), "release": lambda: st.release(lease_id)}[op]
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    assert entered.wait(5), "the write never started"
    t0 = time.monotonic()
    st.allows(None)
    st.status()
    assert time.monotonic() - t0 < 0.2, "allows() waited for the disk"
    release.set()
    t.join(5)
    assert not t.is_alive()


def test_lease_file_is_atomic_and_only_moves_forward(tmp_path):
    p = tmp_path / "lease.json"
    st = set_lease.LeaseStore(p, log=lambda *a, **k: None)
    lease, _ = st.acquire("console", "d", {"kind": "local", "key": "k", "name": "n"})
    old = st._snap()
    st.beat(lease["id"])
    st._flush(old)                                   # an older snapshot arriving late is dropped
    assert json.loads(p.read_text())["current"]["deadline"] == st._data["current"]["deadline"]
    assert not [f for f in os.listdir(tmp_path) if f.startswith(".set_lease-")]
    assert set_lease.LeaseStore(p)._data["current"]["id"] == lease["id"]       # a restart keeps the holder


def test_deferred_beat_returns_before_the_write(slow_store):
    st, entered, release = slow_store
    release.set()
    lease, _ = st.acquire("console", "d", {"kind": "local", "key": "k", "name": "n"})
    release.clear()
    t0 = time.monotonic()
    got, _ = st.beat(lease["id"], defer=True)
    assert got and time.monotonic() - t0 < 0.2
    assert entered.wait(5)
    release.set()


# ---- key-lock render cap and tmp sweep ----------------------------------------------------------

def test_tempo_renders_are_capped_and_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(keylock, "KEYLOCK_DIR", tmp_path)
    monkeypatch.setattr(kc, "run_async", lambda *a, **k: None)
    active, peak, lock, gate = 0, 0, threading.Lock(), threading.Event()

    def fake_render(key, stems, ratio):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        gate.wait(10)
        with lock:
            active -= 1

    monkeypatch.setattr(keylock, "render_tempo", fake_render)
    keys = [keylock.ensure_tempo(f"{i:x}".ljust(40, "0"), {}, 120.0, 124.0)["key"] for i in range(6)]
    time.sleep(0.3)
    assert peak == keylock.RENDER_SLOTS == 2
    assert all(keylock.state(k) == "running" for k in keys)         # queued ones still read "running"
    gate.set()
    for _ in range(100):
        if all(keylock.state(k) == "done" for k in keys):
            break
        time.sleep(0.05)
    assert all(keylock.state(k) == "done" for k in keys) and peak == 2


def test_sweep_removes_old_orphans_and_keeps_live_dirs(tmp_path):
    now = 1_000_000_000.0

    def mk(name, age_h, files=True):
        d = tmp_path / name
        d.mkdir()
        if files:
            (d / "bass.wav").write_bytes(b"x" * 10)
        t = now - age_h * 3600
        os.utime(d, (t, t))
        return d

    orphan, empty = mk("tA_120p0.tmp", 2), mk("tB_120p0.tmp", 5, files=False)
    young, running, done = mk("tC_120p0.tmp", 0.2), mk("tD_120p0.tmp", 3), mk("tE_120p0", 9)
    r = kc.sweep_tmp(tmp_path, running={"tD_120p0"}, now=now)
    assert sorted(x["key"] for x in r["removed"]) == ["tA_120p0", "tB_120p0"]
    assert not orphan.exists() and not empty.exists()
    assert young.exists() and running.exists() and done.exists()


# ---- /api/sessions ------------------------------------------------------------------------------

def _make_sessions(cache, n):
    for i in range(n):
        sid = f"2026-01-{i % 28 + 1:02d}_{i:06d}"
        sd = cache / "sessions" / sid
        for j in range(3):
            d = sd / "songs" / f"0{j}-s"
            d.mkdir(parents=True)
            (d / "meta.json").write_text(json.dumps({"nn": j, "track_id": f"{i * 3 + j:016x}", "name": f"s{i}-{j}", "session": sid}))
            (d / "steps.jsonl").write_text(json.dumps({"t": 1.0 + j, "kind": "x"}) + "\n")
        (sd / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in (
            {"t": 100.0 + i, "kind": "track", "event": "transition_start", "from": "a", "to": "b", "recipe": "Echo Out"},
            {"t": 200.0 + i, "kind": "move", "move": "roll"})))


def test_sessions_with_many_sets_is_not_a_full_rescan(tmp_path, monkeypatch):
    n = 120
    _make_sessions(tmp_path, n)
    synced = []
    real = history.sync_session
    monkeypatch.setattr(history, "sync_session", lambda *a, **k: (synced.append(a[0]), real(*a, **k))[1])

    first = history_api.sessions(tmp_path)
    assert len(first) == n and all(s["songs"] == 3 and s["transitions"] == 1 for s in first)
    n_first = len(synced)
    assert n_first >= n

    t0 = time.monotonic()
    second = history_api.sessions(tmp_path)
    dt = time.monotonic() - t0
    assert second == first
    assert len(synced) == n_first, "an unchanged library must not be re-synced"
    assert dt < 1.5, f"{dt:.2f}s for {n} unchanged sessions"

    # one session grows -> only it is re-read, and its summary follows
    sid = first[0]["id"]
    ev = tmp_path / "sessions" / sid / "events.jsonl"
    ev.write_text(ev.read_text() + json.dumps({"t": 999.0, "kind": "track", "event": "transition_start"}) + "\n")
    third = history_api.sessions(tmp_path)
    assert synced[n_first:] == [sid]
    assert next(s for s in third if s["id"] == sid)["transitions"] == 2
    assert next(s for s in third if s["id"] == sid)["end_t"] == 999.0
