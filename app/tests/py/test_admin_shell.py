"""Admin shell (app/ui/services/admin.py): DJ_ADMIN_EMAILS gate, sessions page, force close, audit log.
Its own small app with real auth middleware; no Google call. Addresses are example.com only."""
import pytest
from fastapi import FastAPI

from app.tests.py.testclient_compat import TestClient
from app.ui.services import admin, auth, set_lease

PUBLIC = "https://testserver"
ORIGIN = {"Origin": PUBLIC}
ADMIN = "boss@example.com"
OTHER = "guest@example.com"
PHONE = {"kind": "device", "key": "device:p1", "name": "phone"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DJ_ADMIN_EMAILS", ADMIN.upper())
    s = auth.AuthSettings.from_env(store_path=tmp_path / "auth.json", environ={
        "DJ_SESSION_SECRET": "s" * 40, "DJ_GOOGLE_CLIENT_ID": "web.apps.googleusercontent.com",
        "DJ_GOOGLE_CLIENT_SECRET": "x", "DJ_PUBLIC_URL": PUBLIC, "DJ_ALLOWED_EMAILS": f"{ADMIN},{OTHER}"})
    app = FastAPI()
    guard = auth.install(app, s, log_names=())
    admin.configure(guard)
    app.include_router(admin.router)
    app.include_router(set_lease.router)
    clock = type("C", (), {"t": 5000.0, "__call__": lambda self: self.t})()
    store = set_lease.LeaseStore(tmp_path / "lease.json", clock=clock, log=lambda *a, **k: None)
    set_lease.use(store)
    monkeypatch.setattr("app.music_brain.matching.knowledge_publish.on_set_end", lambda **k: None)
    admin.use_audit(admin.AuditLog(tmp_path))
    yield app, s, store, clock
    set_lease.use(None)
    admin.use_audit(None)


def _google_cookie(s, sub, email):
    row = s.store.bind_google(sub, email)
    return auth.make_session(s, {"name": "g-" + sub, "pw_version": row["version"]})


def _client(app, s, cookie=None, bearer=None):
    c = TestClient(app, auth=False, base_url=PUBLIC)
    if cookie:
        c.cookies.set(s.cookie_name, cookie)
    if bearer:
        c.headers["Authorization"] = f"Bearer {bearer}"
    return c


def _admin(env):
    app, s, *_ = env
    return _client(app, s, _google_cookie(s, "1001", ADMIN))


def test_feature_off_is_404(env, monkeypatch):
    monkeypatch.delenv("DJ_ADMIN_EMAILS")
    c = _admin(env)
    for path in ("/admin", "/admin/sessions", "/api/admin/me", "/api/admin/sessions", "/api/admin/audit"):
        assert c.get(path).status_code == 404, path
    monkeypatch.setenv("DJ_ADMIN_EMAILS", " , ")
    assert c.get("/admin").status_code == 404


def test_admin_gets_pages(env):
    c = _admin(env)
    r = c.get("/admin")
    assert r.status_code == 200 and "no live set" in r.text
    assert "default-src 'none'" in r.headers["content-security-policy"] and r.headers["cache-control"] == "no-store"
    assert "<script" not in r.text
    assert c.get("/admin/sessions").status_code == 200
    assert c.get("/api/admin/me").json() == {"email": ADMIN}


def test_not_admin_is_403(env):
    app, s, *_ = env
    assert _client(app, s, _google_cookie(s, "1002", OTHER)).get("/admin").status_code == 403
    s.store.set_password("owner", "a-long-enough-password")
    pw = auth.make_session(s, s.store.user)
    c = _client(app, s, pw)
    assert c.get("/admin").status_code == 403 and c.get("/api/admin/audit").status_code == 403


def test_device_token_is_403_even_google_issued(env):
    app, s, *_ = env
    s.store.bind_google("1001", ADMIN)
    plain, _ = s.store.new_token("laptop")
    issued, _ = s.store.new_token("phone", sub="1001")
    for tok in (plain, issued):
        c = _client(app, s, bearer=tok)
        for path in ("/admin", "/api/admin/me", "/api/admin/sessions", "/api/admin/audit"):
            assert c.get(path).status_code == 403, path
        assert c.post("/api/admin/sessions/close").status_code == 403


def test_unauthenticated(env):
    app, s, *_ = env
    c = _client(app, s)
    assert c.get("/api/admin/me").status_code == 401
    r = c.get("/admin", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and "login" in r.headers["location"]


def test_forged_cookie_is_401(env):
    app, s, *_ = env
    good = _google_cookie(s, "1001", ADMIN)
    assert _client(app, s, good[:-3] + "AAA").get("/api/admin/me").status_code == 401


def test_admin_list_change_applies_without_restart(env, monkeypatch):
    c = _admin(env)
    assert c.get("/api/admin/me").status_code == 200
    monkeypatch.setenv("DJ_ADMIN_EMAILS", OTHER)
    assert c.get("/api/admin/me").status_code == 403
    monkeypatch.setenv("DJ_ADMIN_EMAILS", f"{OTHER}, {ADMIN}")
    assert c.get("/api/admin/me").status_code == 200


def test_sessions_page_live_and_closed(env):
    app, s, store, clock = env
    lease, _ = store.acquire("phone", "pixel <b>", PHONE)
    clock.t += 20
    c = _admin(env)
    page = c.get("/admin/sessions").text
    assert "pixel &lt;b&gt;" in page and "<b>" not in page and "Force close" in page and lease["id"] not in page
    live = c.get("/api/admin/sessions").json()["live"]
    assert live["active"] and live["lease"]["lapses_in"] == 40.0 and "id" not in live["lease"]
    store.release(lease["id"])
    body = c.get("/api/admin/sessions").json()
    assert not body["live"]["active"] and body["closed"][0]["reason"] == "released"
    assert lease["id"] not in c.get("/api/admin/sessions").text


def test_force_close(env):
    app, s, store, clock = env
    c = _admin(env)
    r = c.post("/api/admin/sessions/close", headers=ORIGIN)
    assert r.status_code == 409 and r.json()["code"] == "no_live_set"
    assert c.get("/api/admin/audit").json()["audit"] == []
    lease, _ = store.acquire("phone", "pixel", PHONE)
    assert c.post("/api/admin/sessions/close", headers={"Origin": "https://evil.example.com"}).status_code == 403
    assert store.status()["active"]
    r = c.post("/api/admin/sessions/close", headers=ORIGIN)
    assert r.status_code == 200 and r.json()["closed"]["device_name"] == "pixel"
    lease2, conflict = store.beat(lease["id"])
    assert lease2 is None and conflict["detail"] == "closed by admin"
    audit = c.get("/api/admin/audit").json()["audit"]
    assert len(audit) == 1 and audit[0]["actor"] == ADMIN and audit[0]["action"] == "force_close"
    assert c.get("/api/admin/sessions").json()["closed"][0]["reason"] == "admin"
    assert store.acquire("console", "studio", PHONE)[1] is None


def test_close_form_redirects_back(env):
    app, s, store, clock = env
    store.acquire("phone", "pixel", PHONE)
    r = _admin(env).post("/api/admin/sessions/close", headers={**ORIGIN, "Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/sessions"


def test_audit_keeps_newest(env, tmp_path, monkeypatch):
    log = admin.AuditLog(tmp_path / "other")
    monkeypatch.setattr(admin, "MAX_AUDIT", 5)
    for i in range(8):
        log.add(ADMIN, "act", str(i))
    rows = log.recent(100)
    assert [r["target"] for r in rows] == ["7", "6", "5", "4", "3"]
