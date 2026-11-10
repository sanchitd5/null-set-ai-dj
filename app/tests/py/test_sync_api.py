"""/api/sync/*: auth, POST ops, catch-up, SSE stream (driven by hand over ASGI: the sync TestClient cannot read an
endless body). Real auth middleware, example.com addresses, opaque ids only."""
import asyncio
import json

import pytest
from fastapi import FastAPI

from app.music_brain.sync import oplog
from app.tests.py.test_admin_shell import ORIGIN, PUBLIC, _client, _google_cookie  # noqa: F401
from app.ui.services import auth, sync_api


def mk(oid, tbl="edge", kind="incr", delta=None, key="k1", scope="global"):
    import time
    return {"op_id": oid, "tbl": tbl, "key": key, "kind": kind, "delta": delta or {"n": 1},
            "ts": int(time.time() * 1000), "scope": scope}


@pytest.fixture
def env(tmp_path):
    s = auth.AuthSettings.from_env(store_path=tmp_path / "auth.json", environ={
        "DJ_SESSION_SECRET": "s" * 40, "DJ_GOOGLE_CLIENT_ID": "web.apps.googleusercontent.com",
        "DJ_GOOGLE_CLIENT_SECRET": "x", "DJ_PUBLIC_URL": PUBLIC, "DJ_ALLOWED_EMAILS": "a@example.com,b@example.com"})
    app = FastAPI()
    auth.install(app, s, body_limits={("POST", "/api/sync/ops"): 1_000_000}, log_names=())
    app.include_router(sync_api.router)
    log = oplog.OpLog(tmp_path / "sync.db")
    sync_api.use(log)
    yield app, s, log
    sync_api.use(None)


def user(env, sub, email):
    app, s, _ = env
    return _client(app, s, _google_cookie(s, sub, email))


def post(c, ops, device="dev1"):
    return c.post("/api/sync/ops", json={"v": 1, "device_id": device, "ops": ops}, headers=ORIGIN)


def test_auth_required(env):
    app, s, _ = env
    anon = _client(app, s)
    assert anon.get("/api/sync/head").status_code in (401, 403)
    assert anon.post("/api/sync/ops", json={}, headers=ORIGIN).status_code in (401, 403, 404)
    s.store.bind_google("1", "a@example.com")
    tok, _ = s.store.new_token("phone", sub="1")
    c = _client(app, s, bearer=tok)
    assert post(c, [mk("o1")]).json()["accepted"] == ["o1"]


def test_post_ops_shapes_and_idempotence(env):
    c = user(env, "1", "a@example.com")
    r = post(c, [mk("o1"), mk("o2", tbl="zzz"), mk("o1")]).json()
    assert r["ok"] and r["accepted"] == ["o1"] and r["duplicate"] == ["o1"] and r["head"] == 1
    assert r["rejected"] == [{"op_id": "o2", "why": "unknown table or kind"}]
    assert post(c, [mk("o1")]).json()["duplicate"] == ["o1"]


def test_post_validation_and_caps(env):
    c = user(env, "1", "a@example.com")
    assert c.post("/api/sync/ops", content=b"not json", headers=ORIGIN).status_code == 422
    assert c.post("/api/sync/ops", json={"v": 2, "device_id": "d", "ops": []}, headers=ORIGIN).status_code == 422
    assert c.post("/api/sync/ops", json={"v": 1, "device_id": "bad id", "ops": []}, headers=ORIGIN).status_code == 422
    assert post(c, [mk(f"o{i}") for i in range(201)]).status_code == 422
    big = post(c, [mk("o1", kind="lww", delta={"a": "x" * 400, "b": "y" * 400, "c": "z" * 400, "d": "w" * 400,
                                               "e": "v" * 400, "f": "u" * 400, "g": "t" * 400, "h": "s" * 400,
                                               "i": "r" * 400, "j": "q" * 400, "k": "p" * 400})]).json()
    assert big["rejected"][0]["why"].startswith("delta over")
    assert c.post("/api/sync/ops", content=b"x" * 1_100_000, headers=ORIGIN).status_code == 413


def test_rate_limit(env, monkeypatch):
    monkeypatch.setenv("DJ_SYNC_RATE", "3")
    c = user(env, "1", "a@example.com")
    assert post(c, [mk("o1"), mk("o2")]).status_code == 200
    assert post(c, [mk("o3"), mk("o4")]).status_code == 429
    assert post(c, [mk("o3")], device="dev2").status_code == 200


def test_feature_flag(env, monkeypatch):
    monkeypatch.setenv("DJ_SYNC", "0")
    c = user(env, "1", "a@example.com")
    assert c.get("/api/sync/head").status_code == 404 and post(c, [mk("o1")]).status_code == 404


def test_catch_up_scope_paging_echo_and_resnapshot(env):
    a, b = user(env, "1", "a@example.com"), user(env, "2", "b@example.com")
    post(a, [mk("g1"), mk("u1", tbl="pref", kind="lww", delta={"x": 1}, scope="user")], "dev-a")
    post(b, [mk("g2"), mk("u2", tbl="pref", kind="lww", delta={"x": 1}, scope="user")], "dev-b")
    ids = lambda r: [o["op_id"] for o in r.json()["ops"]]  # noqa: E731
    ra = a.get("/api/sync/ops?since=0")
    assert ids(ra) == ["g1", "u1", "g2"] and ra.json()["head"] == 4 and ra.json()["more"] is False
    assert ids(b.get("/api/sync/ops?since=0")) == ["g1", "g2", "u2"]               # no leak of the other user's op
    assert ids(a.get("/api/sync/ops?since=0&device_id=dev-a")) == ["g2"]
    p = a.get("/api/sync/ops?since=0&limit=2").json()
    assert [o["seq"] for o in p["ops"]] == [1, 2] and p["more"] is True
    assert set(p["ops"][0]) == {"seq", "op_id", "tbl", "key", "kind", "delta", "ts", "scope", "device"}
    assert a.get("/api/sync/head").json() == {"head": 4, "floor": 0}
    env[2].register_applier("edge", lambda o: None)
    env[2].compact(0, now=4_000_000_000)
    r = a.get("/api/sync/ops?since=0")
    assert r.status_code == 410 and r.json()["code"] == "resnapshot"
    assert a.get(f"/api/sync/ops?since={env[2].floor()}").status_code == 200


def test_learn_outbox_op_kind_converges_with_the_sync_path(env, tmp_path):
    from app.music_brain.learning import phone_learn as pl
    body = {"v": 1, "device_id": "dev1", "device_name": "Pixel", "set_id": "dev1-1",
            "items": [{"id": "dev1:7", "kind": "op", "at": "2026-11-09T20:00:00Z",
                       "data": {k: v for k, v in mk("ignored").items() if k != "op_id"}}]}
    assert pl.ingest(body, tmp_path, user_scope="user:1")["accepted"] == ["dev1:7"]
    assert pl.ingest(body, tmp_path, user_scope="user:1")["duplicate"] == ["dev1:7"]
    c = user(env, "1", "a@example.com")
    assert post(c, [mk("dev1:7")]).json()["duplicate"] == ["dev1:7"]                # same op_id: stored once
    assert env[2].head() == 1
    body["items"][0] = {**body["items"][0], "id": "dev1:8", "data": {"tbl": "zzz"}}
    assert pl.ingest(body, tmp_path)["rejected"][0]["id"] == "dev1:8"


# --------------------------------------------------------------------------- SSE over raw ASGI
class Stream:
    """One open GET /api/sync/stream: collects the SSE frames the app sends."""

    def __init__(self, app, s, sub, email, query, headers=()):
        cookie = _google_cookie(s, sub, email) if sub else None
        hdrs = [(b"host", b"testserver")] + list(headers)
        if cookie:
            hdrs.append((b"cookie", f"{s.cookie_name}={cookie}".encode()))
        self.scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "https",
                      "path": "/api/sync/stream", "raw_path": b"/api/sync/stream", "query_string": query.encode(),
                      "headers": hdrs, "client": ("127.0.0.1", 5000), "server": ("testserver", 443)}
        self.app, self.status, self.headers, self.buf = app, None, {}, ""
        self.gone = asyncio.Event()
        self.q: asyncio.Queue = asyncio.Queue()
        self.task = None

    async def start(self):
        async def receive():
            await self.gone.wait()
            return {"type": "http.disconnect"}

        async def send(m):
            if m["type"] == "http.response.start":
                self.status = m["status"]
                self.headers = {k.decode(): v.decode() for k, v in m["headers"]}
            elif m["type"] == "http.response.body":
                await self.q.put(m.get("body", b"").decode())
        self.task = asyncio.ensure_future(self.app(self.scope, receive, send))
        await asyncio.sleep(0)
        return self

    async def frames(self, n, timeout=3.0):
        """The next n complete frames (hello / op / ping) as (event, id, data)."""
        out = []
        while len(out) < n:
            while "\n\n" not in self.buf:
                self.buf += await asyncio.wait_for(self.q.get(), timeout)
            raw, self.buf = self.buf.split("\n\n", 1)
            f = {"event": "", "id": "", "data": ""}
            for line in raw.split("\n"):
                if line.startswith(":"):
                    f["event"] = "ping"
                else:
                    k, _, v = line.partition(": ")
                    f[k] = v
            out.append((f["event"], f["id"], json.loads(f["data"]) if f["data"] else None))
        return out

    async def close(self):
        self.gone.set()
        if self.task:
            try:
                await asyncio.wait_for(self.task, 3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.task.cancel()


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_stream_hello_backlog_live_and_fan_out(env):
    app, s, log = env
    log.ingest([mk("g0")], "dev-x", "user:9")

    async def go():
        a = await Stream(app, s, "1", "a@example.com", "since=0&device_id=dev-a").start()
        b = await Stream(app, s, "2", "b@example.com", "since=0&device_id=dev-b").start()
        fa = await a.frames(2)
        assert a.status == 200 and a.headers["content-type"].startswith("text/event-stream")
        assert a.headers["cache-control"] == "no-store" and a.headers["x-accel-buffering"] == "no"
        assert fa[0][0] == "hello" and fa[0][2] == {"head": 1, "floor": 0}
        assert fa[1][0] == "op" and fa[1][1] == "1" and fa[1][2]["op_id"] == "g0"
        await b.frames(2)
        # an op accepted from a third device reaches both within a second; the user op only its owner
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: log.ingest([mk("g1"), mk("u1", tbl="pref", kind="lww", delta={"x": 1}, scope="user")],
                                     "dev-c", "user:1"))
        ga = await a.frames(2, 1.0)
        gb = await b.frames(1, 1.0)
        assert [f[2]["op_id"] for f in ga] == ["g1", "u1"] and [f[2]["op_id"] for f in gb] == ["g1"]
        # echo: dev-a's own op is not sent back to dev-a
        await asyncio.get_running_loop().run_in_executor(None, lambda: log.ingest([mk("g2")], "dev-a", "user:1"))
        assert (await b.frames(1, 1.0))[0][2]["op_id"] == "g2"
        with pytest.raises(asyncio.TimeoutError):
            await a.frames(1, 0.3)
        await a.close()
        await b.close()
    run(go())


def test_stream_resumes_from_last_event_id_without_loss_or_repeat(env):
    app, s, log = env
    log.ingest([mk("o1"), mk("o2"), mk("o3")], "dev-x", "user:9")

    async def go():
        a = await Stream(app, s, "1", "a@example.com", "since=0&device_id=dev-a",
                         [(b"last-event-id", b"2")]).start()
        fr = await a.frames(2)
        assert [f[1] for f in fr[1:]] == ["3"]
        log.ingest([mk("o4")], "dev-x", "user:9")
        assert (await a.frames(1))[0][1] == "4"
        await a.close()
    run(go())


def test_stream_gone_when_older_than_the_floor_and_one_stream_per_device(env, monkeypatch):
    app, s, log = env
    log.ingest([mk("o1")], "dev-x", "user:9")
    log.register_applier("edge", lambda o: None)
    log.compact(0, now=4_000_000_000)

    async def go():
        old = await Stream(app, s, "1", "a@example.com", "since=0&device_id=dev-a").start()
        await old.close()
        assert old.status == 410
        monkeypatch.setenv("DJ_SYNC_MAX_STREAMS", "2")
        a1 = await Stream(app, s, "1", "a@example.com", "since=1&device_id=dev-a").start()
        await a1.frames(1)
        a2 = await Stream(app, s, "1", "a@example.com", "since=1&device_id=dev-a").start()   # replaces a1
        await a2.frames(1)
        await asyncio.wait_for(a1.task, 3)
        b = await Stream(app, s, "1", "a@example.com", "since=1&device_id=dev-b").start()
        await b.frames(1)
        c = await Stream(app, s, "1", "a@example.com", "since=1&device_id=dev-c").start()    # third device: over the cap
        await c.close()
        assert c.status == 429
        for st in (a2, b):
            await st.close()
        assert sync_api._streams == {}
    run(go())


def test_stream_requires_auth_and_pings(env, monkeypatch):
    app, s, log = env
    monkeypatch.setattr(sync_api, "PING_S", 0.2)

    async def go():
        anon = await Stream(app, s, None, "", "since=0&device_id=dev-a").start()
        await anon.close()
        assert anon.status in (401, 403)
        a = await Stream(app, s, "1", "a@example.com", "since=0&device_id=dev-a").start()
        fr = await a.frames(2)
        assert fr[0][0] == "hello" and fr[1][0] == "ping"
        await a.close()
    run(go())


def test_reserved_device_id_is_refused_on_every_path(env, tmp_path):
    from app.music_brain.learning import phone_learn as pl
    c = user(env, "1", "a@example.com")
    assert post(c, [mk("x1")], device="server").status_code == 422
    assert c.get("/api/sync/ops?device_id=server").status_code == 422
    body = {"v": 1, "device_id": "server", "device_name": "x", "set_id": "s-1",
            "items": [{"id": "s:1", "kind": "op", "at": "2026-11-09T20:00:00Z",
                       "data": {k: v for k, v in mk("i").items() if k != "op_id"}}]}
    assert pl.ingest(body, tmp_path)["rejected"][0]["why"] == "reserved device id"
    assert env[2].head() == 0
    r = post(c, [mk("x2") | {"device": "server"}]).json()                      # a body field never sets the device
    assert r["accepted"] == ["x2"] and env[2].since({"global"}, 0)[0]["device"] == "dev1"
