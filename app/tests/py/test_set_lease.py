"""One set at a time: the set lease (app/ui/services/set_lease.py, docs/engineering/SET_LEASE.md)."""
from fastapi import FastAPI

from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import set_lease


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _store(tmp_path, clock=None, log=None):
    return set_lease.LeaseStore(tmp_path / "lease.json", clock=clock or Clock(),
                                log=(lambda kind, **f: log.append((kind, f))) if log is not None else (lambda *a, **k: None))


PHONE = {"kind": "device", "key": "device:p1", "name": "phone"}
CONSOLE = {"kind": "browser", "key": "browser:c1", "name": "browser"}


# ---- the store -------------------------------------------------------------------------------

def test_grant_then_conflict(tmp_path):
    log = []
    s = _store(tmp_path, log=log)
    lease, conflict = s.acquire("console", "studio", CONSOLE)
    assert conflict is None and lease["id"] and lease["client"] == "console"
    lease2, conflict = s.acquire("phone", "pixel", PHONE)
    assert lease2 is None
    assert conflict["code"] == "set_lease" and conflict["holder"]["device_name"] == "studio"
    assert conflict["since"] == lease["started"] and conflict["same_holder"] is False
    assert "id" not in conflict["holder"] and "key" not in str(conflict)        # never leaks the capability
    assert s.acquire("console", "studio", CONSOLE)[1]["same_holder"] is True
    assert [f["action"] for _, f in log] == ["grant"]


def test_expiry_then_beat_revives_or_ends(tmp_path):
    clock = Clock()
    s = _store(tmp_path, clock)
    lease, _ = s.acquire("console", "studio", CONSOLE)
    clock.t += set_lease.TTL_S - 1
    assert s.beat(lease["id"])[0]["deadline"] == clock.t + set_lease.TTL_S       # renewed
    clock.t += set_lease.TTL_S + 1                                               # crashed: no beats
    assert s.status()["active"] is False and s.allows(None) is None
    assert s.beat(lease["id"])[0] is not None                                    # nobody took it: revived
    clock.t += set_lease.TTL_S + 1
    other, conflict = s.acquire("phone", "pixel", PHONE)                         # lapsed: the phone gets it
    assert conflict is None
    _, refusal = s.beat(lease["id"])
    assert refusal["detail"] == "lease ended" and refusal["holder"]["device_name"] == "pixel"


def test_takeover_and_old_beat_409(tmp_path):
    log = []
    s = _store(tmp_path, log=log)
    old, _ = s.acquire("phone", "pixel", PHONE)
    new, prev = s.takeover("console", "studio", CONSOLE)
    assert prev["device_name"] == "pixel" and new["id"] != old["id"]
    _, refusal = s.beat(old["id"])
    assert refusal["detail"] == "taken over" and refusal["holder"]["device_name"] == "studio"
    assert s.beat(new["id"])[0] is not None
    assert s.release(old["id"]) is False and s.status()["active"]               # a stale release changes nothing
    assert [f["action"] for _, f in log] == ["grant", "takeover"]


def test_release(tmp_path):
    s = _store(tmp_path)
    lease, _ = s.acquire("console", "studio", CONSOLE)
    assert s.release(lease["id"]) is True and s.status() == {"active": False, "lease": None}
    assert s.acquire("phone", "pixel", PHONE)[1] is None


def test_sweep_closes_a_set_without_beats(tmp_path):
    clock, log = Clock(), []
    s = _store(tmp_path, clock, log)
    lease, _ = s.acquire("phone", "pixel", PHONE)
    clock.t += 14 * 60
    assert s.sweep() is False and s.beat(lease["id"])[0] is not None          # 14 min: no-op; the beat renews
    clock.t += set_lease.SESSION_CLOSE_MIN * 60 - 1
    assert s.sweep() is False                                                  # one second short
    clock.t += 1
    assert s.sweep() is True
    assert s.status() == {"active": False, "lease": None}
    assert [f["action"] for _, f in log][-1] == "closed"
    lease2, refusal = s.beat(lease["id"])
    assert lease2 is None and refusal["detail"] == "lease ended" and "holder" not in refusal
    assert s.sweep() is False                                                  # nothing left to close
    assert s.acquire("console", "studio", CONSOLE)[1] is None


def test_sweep_does_not_touch_a_live_lease(tmp_path):
    clock = Clock()
    s = _store(tmp_path, clock)
    s.acquire("phone", "pixel", PHONE)
    clock.t += 60 * 60 * 5
    s.beat(s._data["current"]["id"])
    assert s.sweep() is False


def test_sweep_once_publishes_once(tmp_path, monkeypatch):
    from app.music_brain.matching import knowledge_publish
    clock, calls = Clock(), []
    s = _store(tmp_path, clock)
    set_lease.use(s)
    monkeypatch.setattr(knowledge_publish, "on_set_end", lambda **k: calls.append(1))
    try:
        s.acquire("phone", "pixel", PHONE)
        assert set_lease.sweep_once() is False and calls == []
        clock.t += set_lease.SESSION_CLOSE_MIN * 60 + set_lease.TTL_S
        assert set_lease.sweep_once() is True and set_lease.sweep_once() is False
        assert calls == [1]
    finally:
        set_lease.use(None)


def test_restart_keeps_the_holder(tmp_path):
    clock = Clock()
    lease, _ = _store(tmp_path, clock).acquire("phone", "pixel", PHONE)
    again = _store(tmp_path, clock)                                              # server restart
    assert again.status()["lease"]["device_name"] == "pixel"
    assert again.acquire("console", "studio", CONSOLE)[1] is not None
    assert again.beat(lease["id"])[0] is not None
    assert not list(tmp_path.glob(".set_lease-*"))                              # atomic write left no temp file


def test_corrupt_file_means_no_lease(tmp_path):
    (tmp_path / "lease.json").write_text("{nope")
    assert _store(tmp_path).status()["active"] is False


def test_scoped_routes():
    for m, p in [("POST", "/api/autopilot/suggest"), ("POST", "/api/autopilot/plan"), ("POST", "/api/live/ear"),
                 ("POST", "/api/transition/preplan"), ("GET", "/api/learned/pick"), ("POST", "/api/riff/plan"),
                 ("POST", "/api/riff/abc/balance"), ("POST", "/api/session/events"), ("POST", "/api/prerender")]:
        assert set_lease.is_set_scoped(m, p), (m, p)
    for m, p in [("GET", "/api/tracks"), ("GET", "/api/mobile/library"), ("GET", "/api/tracks/x/stems"),
                 ("GET", "/api/tracks/x/analysis"), ("POST", "/api/download/jobs"), ("GET", "/api/transition/preplan/j"),
                 ("GET", "/api/riff/abc"), ("GET", "/api/set/lease")]:
        assert not set_lease.is_set_scoped(m, p), (m, p)


# ---- the routes and the gate -----------------------------------------------------------------

def _c(**kw):
    return TestClient(server.app, **kw)


def test_routes_grant_conflict_takeover_release():
    c = _c()
    r = c.post("/api/set/lease", json={"client": "console", "device_name": "studio"})
    assert r.status_code == 200
    lease = r.json()["lease"]
    assert lease["holder"]["kind"] == "device" and lease["beat_s"] == set_lease.BEAT_S
    r = c.post("/api/set/lease", json={"client": "phone", "device_name": "pixel"})
    assert r.status_code == 409 and r.json()["holder"]["device_name"] == "studio" and r.json()["same_holder"] is True
    assert c.get("/api/set/lease").json()["lease"]["device_name"] == "studio"
    assert "id" not in c.get("/api/set/lease").json()["lease"]
    t = c.post("/api/set/lease/takeover", json={"client": "phone", "device_name": "pixel"})
    assert t.status_code == 200 and t.json()["previous"]["device_name"] == "studio"
    b = c.post(f"/api/set/lease/{lease['id']}/beat")
    assert b.status_code == 409 and b.json()["detail"] == "taken over"
    new = t.json()["lease"]["id"]
    assert c.post(f"/api/set/lease/{new}/beat").status_code == 200
    assert c.post(f"/api/set/lease/{new}/release").json()["released"] is True
    assert c.delete(f"/api/set/lease/{new}").json()["released"] is False
    assert c.post("/api/set/lease", json={"client": "fridge"}).status_code == 422


def test_gate_needs_the_lease_on_set_routes_only():
    c = _c()
    events = {"events": []}
    assert c.post("/api/session/events", json=events).status_code == 200        # no lease: open
    lease = c.post("/api/set/lease", json={"client": "console", "device_name": "studio"}).json()["lease"]
    r = c.post("/api/session/events", json=events)
    assert r.status_code == 409 and r.json()["code"] == "set_lease" and r.json()["holder"]["device_name"] == "studio"
    assert c.post("/api/session/events", json=events, headers={"X-Set-Lease": "wrong"}).status_code == 409
    assert c.post("/api/session/events", json=events, headers={"X-Set-Lease": lease["id"]}).status_code == 200
    assert c.get("/api/learned/pick", params={"a": "x", "b": "y"}).status_code == 409
    # read-only library / analysis / set-lease status stay open with no lease
    assert c.get("/api/tracks").status_code == 200
    assert c.get("/api/recipes").status_code == 200
    assert c.get("/api/set/lease").status_code == 200


def test_gate_answers_401_before_409():
    c = _c()
    c.post("/api/set/lease", json={"client": "console", "device_name": "studio"})
    server.AUTH_GUARD.limiter.reset()
    r = TestClient(server.app, auth=False).post("/api/session/events", json={"events": []})
    assert r.status_code == 401 and "studio" not in r.text                      # the holder is never told to a stranger
    server.AUTH_GUARD.limiter.reset()


def test_browser_session_is_its_own_holder(owner_login):
    browser = TestClient(server.app, auth=False, base_url="http://testserver")
    user, pw = owner_login
    assert browser.post("/login", data={"username": user, "password": pw},
                        headers={"Origin": "http://testserver"}, follow_redirects=False).status_code == 303
    r = browser.post("/api/set/lease", json={"client": "console", "device_name": ""},
                     headers={"Origin": "http://testserver"})
    assert r.status_code == 200 and r.json()["lease"]["holder"]["kind"] == "browser"
    assert r.json()["lease"]["device_name"] == "browser"                          # empty name: the holder's
    conflict = _c().post("/api/set/lease", json={"client": "phone", "device_name": "pixel"}).json()
    assert conflict["same_holder"] is False and conflict["holder"]["kind"] == "browser"


def test_non_holder_stem_split_goes_to_backfill():
    """may_lead() inside a request, through the threadpool a sync route runs in."""
    app = FastAPI()
    app.add_middleware(set_lease.Gate)

    @app.get("/api/probe")
    def probe():
        return {"lead": set_lease.may_lead()}

    c = TestClient(app, auth=False)
    assert c.get("/api/probe").json()["lead"] is True                           # no lease: deck priority
    lease, _ = set_lease.store().acquire("console", "studio", CONSOLE)
    assert c.get("/api/probe").json()["lead"] is False                          # someone else's set
    assert c.get("/api/probe", headers={"X-Set-Lease": lease["id"]}).json()["lead"] is True
    assert set_lease.may_lead() is True                                         # outside a request


def test_queue_stems_downgrades_for_a_non_holder(monkeypatch):
    asked = []
    monkeypatch.setattr(server, "_host", lambda: type("H", (), {"queue_stems": lambda self, t, u: asked.append(u) or True})())
    token = set_lease._may_lead.set(False)
    try:
        server._queue_stems("abc")
    finally:
        set_lease._may_lead.reset(token)
    server._queue_stems("abc")
    assert asked == [False, True]


# ---- history + force close (admin shell) -----------------------------------------------------

def test_history_per_end_reason(tmp_path):
    clock = Clock()
    s = _store(tmp_path, clock)
    a, _ = s.acquire("phone", "p1", PHONE)
    s.release(a["id"])
    b, _ = s.acquire("phone", "p2", PHONE)
    s.takeover("console", "c1", CONSOLE)
    clock.t += 500
    s.acquire("phone", "p3", PHONE)                       # the console lease lapsed
    clock.t += 5000
    s.sweep()                                            # long idle: closed by the server
    c, _ = s.acquire("phone", "p4", PHONE)
    assert s.force_close() is not None
    rows = s.history_view(20)
    reasons = {r["device_name"]: r["reason"] for r in rows}
    assert reasons == {"p1": "released", "p2": "taken_over", "c1": "lapsed", "p3": "paused_timeout", "p4": "admin"}
    assert all(r["duration_s"] >= 0 for r in rows)
    assert s.history_view(20)[0]["device_name"] == "p4"        # newest first


def test_history_cap_and_no_ids(tmp_path):
    s = _store(tmp_path)
    ids = []
    for i in range(set_lease.MAX_HISTORY + 5):
        lease, _ = s.acquire("phone", f"d{i}", PHONE)
        ids.append(lease["id"])
        s.release(lease["id"])
    view = s.history_view(500)
    assert len(view) == set_lease.MAX_HISTORY and view[0]["device_name"] == f"d{set_lease.MAX_HISTORY + 4}"
    text = str(view)
    assert not any(i in text for i in ids) and all(len(r["id_hash"]) == 8 for r in view)


def test_force_close_then_old_beat_and_new_acquire(tmp_path):
    s = _store(tmp_path)
    assert s.force_close() is None                            # nothing live
    lease, _ = s.acquire("phone", "pixel", PHONE)
    view = s.force_close()
    assert view["device_name"] == "pixel" and "id" not in view and s.status()["active"] is False
    got, body = s.beat(lease["id"])
    assert got is None and body["detail"] == "closed by admin" and body["code"] == "set_lease"
    assert s.acquire("console", "studio", CONSOLE)[1] is None
    s2 = _store(tmp_path)                                    # file round trip
    assert [r["reason"] for r in s2.history_view(5)] == ["admin"]


def test_status_admin(tmp_path):
    clock = Clock()
    s = _store(tmp_path, clock)
    assert s.status_admin() == {"active": False, "lease": None}
    s.acquire("phone", "pixel", PHONE)
    clock.t += 25
    v = s.status_admin()["lease"]
    assert v["last_beat"] == 1000.0 and v["lapses_in"] == 35.0 and "id" not in v
