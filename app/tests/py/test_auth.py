"""Owner-account auth (app/ui/services/auth.py) as wired into the console API."""
import logging
import time

import pytest

from app.tests.py.testclient_compat import TestClient
from app.ui import server
from app.ui.services import auth

ORIGIN = {"Origin": "http://testserver"}
SAMPLE_ROUTES = ["/api/recipes", "/api/tracks", "/api/llm/status", "/api/audio/tracks/0000000000000000",
                 "/api/samples", "/api/atlas/status", "/api/auth/devices", "/index.html", "/"]


@pytest.fixture
def anon():
    server.AUTH_GUARD.limiter.reset()
    yield TestClient(server.app, auth=False)
    server.AUTH_GUARD.limiter.reset()


@pytest.fixture
def store():
    return server.AUTH_SETTINGS.store


def _login(c, user, pw, headers=ORIGIN):
    return c.post("/login", data={"username": user, "password": pw}, headers=headers, follow_redirects=False)


def _bearer(token):
    return TestClient(server.app, auth=False, headers={"Authorization": f"Bearer {token}"})


# ---- what is public, what is not ----------------------------------------------------------------

def test_health_is_public_and_bare(anon):
    r = anon.get("/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


@pytest.mark.parametrize("path", SAMPLE_ROUTES)
def test_401_without_auth(anon, path):
    r = anon.get(path)
    assert r.status_code == 401 and r.json() == {"detail": "unauthorized"}


def test_browser_navigation_redirects_to_login(anon):
    r = anon.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    page = anon.get("/login")
    assert page.status_code == 200 and 'name="username"' in page.text and 'name="password"' in page.text


def test_websocket_refused_without_auth(anon):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with anon.websocket_connect("/ws"):
            pass


def test_docs_off(api_token):
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert _bearer(api_token).get(p).status_code == 404


# ---- bearer device tokens ------------------------------------------------------------------------

def test_bearer_ok(api_token):
    c = _bearer(api_token)
    assert c.get("/api/recipes").status_code == 200
    assert c.get("/index.html").status_code == 200


def test_wrong_bearer(anon, api_token):
    r = anon.get("/api/recipes", headers={"Authorization": f"Bearer {api_token}x"})
    assert r.status_code == 401 and r.json() == {"detail": "unauthorized"}
    assert anon.get("/api/recipes", headers={"Authorization": f"Basic {api_token}"}).status_code == 401


def test_query_string_secret_rejected(anon, api_token, owner_login):
    for q in ("token", "access_token", "api_key"):
        assert anon.get(f"/api/recipes?{q}={api_token}").status_code == 401
    assert anon.get(f"/api/recipes?username={owner_login[0]}&password={owner_login[1]}").status_code == 401


def test_device_token_issue_list_revoke(anon, owner_login, store):
    r = anon.post("/api/auth/token", json={"username": owner_login[0], "password": owner_login[1],
                                            "device_name": "phone"})
    assert r.status_code == 200
    body = r.json()
    tok, tid = body["token"], body["id"]
    assert tok.startswith(auth.TOKEN_PREFIX) and "sha256" not in body
    assert tok not in store.path.read_text()                   # only the hash is stored
    c = _bearer(tok)
    assert c.get("/api/recipes").status_code == 200
    devices = c.get("/api/auth/devices").json()["devices"]
    row = next(d for d in devices if d["id"] == tid)
    assert row["name"] == "phone" and row["last_used"] and "sha256" not in row
    assert c.delete(f"/api/auth/devices/{tid}").status_code == 200
    assert c.get("/api/recipes").status_code == 401             # revoked -> 401
    assert anon.delete("/api/auth/devices/nope").status_code == 401


def test_device_token_wrong_password(anon, owner_login):
    for creds in ((owner_login[0], "wrong-password-x"), ("nobody", owner_login[1])):
        r = anon.post("/api/auth/token", json={"username": creds[0], "password": creds[1], "device_name": "x"})
        assert r.status_code == 401 and r.json() == {"detail": auth.INVALID_LOGIN}


def test_cli_revoke_applies_to_running_server(owner_login, store):
    tok, row = auth.AuthStore(store.path).new_token("cli")          # another process's view of the file
    assert _bearer(tok).get("/api/recipes").status_code == 200
    assert auth.main(["--file", str(store.path), "revoke-token", row["id"]]) == 0
    assert _bearer(tok).get("/api/recipes").status_code == 401


# ---- browser login ---------------------------------------------------------------------------------

def test_login_cookie_flow(anon, owner_login):
    r = _login(anon, *owner_login)
    assert r.status_code == 303
    sc = r.headers["set-cookie"]
    assert "HttpOnly" in sc and "SameSite=Strict" in sc and "Max-Age=" in sc
    assert "Secure" not in sc                                    # plain-HTTP test request
    assert owner_login[1] not in sc
    assert anon.get("/api/recipes").status_code == 200
    assert anon.get("/api/audio/tracks/0000000000000000").status_code == 404   # past auth, unknown id
    r = anon.get("/logout", follow_redirects=False)
    assert r.status_code == 303 and "Max-Age=0" in r.headers["set-cookie"]
    anon.cookies.clear()
    assert anon.get("/api/recipes").status_code == 401


def test_login_failures_say_the_same_thing(anon, owner_login):
    a = _login(anon, owner_login[0], "wrong-password-x")
    b = _login(anon, "nobody", owner_login[1])
    c = _login(anon, "", "")
    for r in (a, b, c):
        assert r.status_code == 401 and "set-cookie" not in r.headers
        assert auth.INVALID_LOGIN in r.text
    assert a.text == b.text == c.text
    assert anon.get("/api/recipes").status_code == 401


def test_unknown_user_still_pays_the_hash(monkeypatch, store, owner_login):
    calls = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda pw, h: calls.append(1) or real(pw, h))
    assert store.check_login("nobody", "whatever-password") is None
    assert store.check_login(owner_login[0], "whatever-password") is None
    assert len(calls) == 2


def test_secure_cookie_only_from_trusted_proxy(owner_login):
    s = server.AUTH_GUARD.settings
    old = s.trusted_proxies
    try:
        c = TestClient(server.app, auth=False)
        r = _login(c, *owner_login, headers={**ORIGIN, "X-Forwarded-Proto": "https"})
        assert "Secure" not in r.headers["set-cookie"]        # peer "testclient" is not trusted
        s.trusted_proxies = ("0.0.0.0/0", "::/0")
        c = TestClient(server.app, auth=False, client=("127.0.0.1", 5000))
        r = _login(c, *owner_login, headers={**ORIGIN, "X-Forwarded-Proto": "https"})
        assert "Secure" in r.headers["set-cookie"]
    finally:
        s.trusted_proxies = old


def test_login_needs_same_origin(anon, owner_login):
    assert _login(anon, *owner_login, headers={"Origin": "https://evil.example"}).status_code == 403
    assert _login(anon, *owner_login, headers={}).status_code == 403


def test_forged_and_expired_cookie(anon, store):
    s = server.AUTH_GUARD.settings
    other = auth.AuthSettings(store=store, session_secret="y" * 40, cookie_name=s.cookie_name)
    anon.cookies.set(s.cookie_name, auth.make_session(other, store.user))
    assert anon.get("/api/recipes").status_code == 401
    anon.cookies.set(s.cookie_name, auth.make_session(s, store.user, now=time.time() - s.session_seconds - 5))
    assert anon.get("/api/recipes").status_code == 401
    anon.cookies.set(s.cookie_name, "v2.owner.1.9999999999.abc.def")
    assert anon.get("/api/recipes").status_code == 401


def test_password_change_kills_sessions(anon, owner_login, store):
    assert _login(anon, *owner_login).status_code == 303
    assert anon.get("/api/recipes").status_code == 200
    store.set_password(owner_login[0], owner_login[1])          # same password, new version
    assert anon.get("/api/recipes").status_code == 401
    assert _login(anon, *owner_login).status_code == 303
    assert anon.get("/api/recipes").status_code == 200


def test_rotated_session_secret_kills_cookie(anon, owner_login):
    s = server.AUTH_GUARD.settings
    assert _login(anon, *owner_login).status_code == 303
    old = s.session_secret
    try:
        s.session_secret = old + "-rotated"
        assert anon.get("/api/recipes").status_code == 401
    finally:
        s.session_secret = old


def test_csrf(anon, owner_login):
    _login(anon, *owner_login)
    body = {"kind": "not-a-kind"}                  # past auth the app answers 400 (unknown kind)
    assert anon.post("/api/session/event", json=body).status_code == 403
    assert anon.post("/api/session/event", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert anon.post("/api/session/event", json=body, headers={"Referer": "https://evil.example/x"}).status_code == 403
    assert anon.post("/api/session/event", json=body, headers=ORIGIN).status_code == 400
    assert anon.post("/api/session/event", json=body,
                     headers={"Referer": "http://testserver/index.html"}).status_code == 400


def test_bearer_is_csrf_exempt(api_token):
    assert _bearer(api_token).post("/api/session/event", json={"kind": "not-a-kind"}).status_code == 400


# ---- brute force -----------------------------------------------------------------------------------

def test_limiter_per_ip(anon, api_token):
    for _ in range(server.AUTH_SETTINGS.max_failures):
        assert anon.get("/api/recipes", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = anon.get("/api/recipes", headers={"Authorization": f"Bearer {api_token}"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert anon.get("/health").status_code == 200


def test_limiter_per_username(anon, owner_login):
    for i in range(server.AUTH_SETTINGS.max_failures):
        c = TestClient(server.app, auth=False, client=(f"203.0.113.{i}", 1))   # a new IP each time
        assert _login(c, owner_login[0], f"wrong-password-{i}").status_code == 401
    c = TestClient(server.app, auth=False, client=("198.51.100.7", 1))
    assert _login(c, *owner_login).status_code == 429             # the right password, but locked


def test_limiter_uses_forwarded_ip_only_from_trusted_proxy(store):
    s = auth.AuthSettings(store=store, session_secret="x" * 40, trusted_proxies=("10.0.0.1",))
    hdr = {"x-forwarded-for": "6.6.6.6, 1.2.3.4"}
    assert auth.client_ip(s, {"client": ("10.0.0.1", 1)}, hdr) == "1.2.3.4"
    assert auth.client_ip(s, {"client": ("9.9.9.9", 1)}, hdr) == "9.9.9.9"


def test_limiter_unit(store):
    s = auth.AuthSettings(store=store, session_secret="x" * 40, max_failures=3, failure_window_s=60, lockout_s=100)
    lim = auth.FailureLimiter()
    for i in range(3):
        assert lim.retry_after("ip:1.1.1.1", now=1000 + i) == 0
        lim.fail("ip:1.1.1.1", s, now=1000 + i)
    assert lim.retry_after("ip:1.1.1.1", now=1003) > 0
    assert lim.retry_after("ip:2.2.2.2", now=1003) == 0
    assert lim.retry_after("ip:1.1.1.1", now=1103) == 0


# ---- startup rules, store, CLI -----------------------------------------------------------------------

@pytest.mark.parametrize("argv,ok", [
    ([], True), (["uvicorn", "app:app", "--host", "127.0.0.1"], True), (["--host=::1"], True),
    (["--host", "0.0.0.0"], False), (["--host=192.168.1.5"], False), (["--fd", "3"], False),
    (["--uds", "/tmp/s.sock"], True), (["-b", "0.0.0.0:8000"], False), (["-b", "127.0.0.1:8000"], True),
])
def test_refuse_to_start_without_password(tmp_path, argv, ok):
    s = auth.AuthSettings.from_env(store_path=tmp_path / "auth.json", environ={"DJ_SESSION_SECRET": "s" * 40})
    assert not s.enabled
    if ok:
        auth.check_bind(s, argv=argv, environ={})
    else:
        with pytest.raises(auth.AuthConfigError, match="set-password"):
            auth.check_bind(s, argv=argv, environ={})
    s.store.set_password("owner", "a-long-enough-password")
    auth.check_bind(s, argv=["--host", "0.0.0.0"], environ={})


def test_short_session_secret_rejected(tmp_path):
    with pytest.raises(auth.AuthConfigError):
        auth.AuthSettings.from_env(store_path=tmp_path / "a.json", environ={"DJ_SESSION_SECRET": "short"})


def test_no_password_serves_loopback_only(tmp_path):
    from fastapi import FastAPI

    app = FastAPI()
    app.get("/x")(lambda: {"ok": True})
    auth.install(app, auth.AuthSettings(store=auth.AuthStore(tmp_path / "a.json"), session_secret="x" * 40),
                 log_names=())
    assert TestClient(app, auth=False, base_url="http://127.0.0.1").get("/x").status_code == 200
    assert TestClient(app, auth=False, base_url="http://192.168.1.5").get("/x").status_code == 503


def _no_login_app(tmp_path, **kw):
    from fastapi import FastAPI

    app = FastAPI()
    app.get("/x")(lambda: {"ok": True})
    app.get("/health")(lambda: {"ok": True})
    auth.install(app, auth.AuthSettings(store=auth.AuthStore(tmp_path / "a.json"), session_secret="x" * 40, **kw),
                 log_names=())
    return TestClient(app, auth=False, base_url="http://127.0.0.1")


@pytest.mark.parametrize("hdr", [{"X-Forwarded-For": "203.0.113.7"}, {"X-Forwarded-Proto": "https"},
                                 {"X-Forwarded-Host": "dj.example.com"}, {"Forwarded": "for=203.0.113.7"},
                                 {"X-Real-IP": "203.0.113.7"}])
def test_no_login_behind_a_proxy_fails_closed(tmp_path, hdr):
    """Loopback bind behind Caddy: every request looks local, but carries forwarded headers."""
    c = _no_login_app(tmp_path)
    r = c.get("/x", headers=hdr)
    assert r.status_code == 503 and r.json() == {"detail": "auth not configured"}
    assert c.post("/x", headers=hdr).status_code == 503
    assert c.get("/health", headers=hdr).status_code == 200
    assert c.get("/x").status_code == 200                       # plain local dev still works


def test_no_login_with_public_url_fails_closed(tmp_path):
    c = _no_login_app(tmp_path, public_url="https://dj.example.com:8448")
    assert c.get("/x").status_code == 503
    assert c.get("/health").status_code == 200
    s = auth.AuthSettings.from_env(store_path=tmp_path / "b.json",
                                   environ={"DJ_SESSION_SECRET": "s" * 40, "DJ_PUBLIC_URL": "https://dj.example.com:8448/"})
    assert s.public_url == "https://dj.example.com:8448"
    auth.check_bind(s, argv=["--host", "127.0.0.1"], environ={})   # starts (for /health), but serves 503


def test_csrf_origin_with_a_non_default_port(tmp_path):
    s = auth.AuthSettings(store=auth.AuthStore(tmp_path / "a.json"), session_secret="x" * 40,
                          public_url="https://dj.example.com:8448")
    # Caddy passes the client's Host through, port included
    assert auth.origin_ok(s, {"host": "dj.example.com:8448", "origin": "https://dj.example.com:8448"})
    assert auth.origin_ok(s, {"host": "dj.example.com:8448", "referer": "https://dj.example.com:8448/index.html"})
    # a proxy that rewrote Host: the published origin still counts as same-origin
    assert auth.origin_ok(s, {"host": "127.0.0.1:8000", "origin": "https://dj.example.com:8448"})
    assert not auth.origin_ok(s, {"host": "dj.example.com:8448", "origin": "https://dj.example.com"})
    assert not auth.origin_ok(s, {"host": "dj.example.com:8448", "origin": "https://evil.example.com:8448"})
    hdr = {"x-forwarded-proto": "https", "host": "dj.example.com:8448"}
    assert auth.is_https(s, {"client": ("127.0.0.1", 1), "scheme": "http"}, hdr)       # Secure cookie behind Caddy


def test_store_file_is_private_and_hashed(tmp_path):
    st = auth.AuthStore(tmp_path / "sub" / "auth.json")
    st.set_password("owner", "correct horse battery")
    assert (st.path.stat().st_mode & 0o777) == 0o600
    text = st.path.read_text()
    assert "correct horse battery" not in text and "scrypt$16384$8$1$" in text
    with pytest.raises(ValueError):
        st.set_password("owner", "short")
    with pytest.raises(ValueError):
        st.set_password("bad name", "long enough password")
    assert st.check_login("owner", "correct horse battery")["name"] == "owner"


def test_cli(tmp_path, monkeypatch, capsys):
    f = str(tmp_path / "auth.json")
    assert auth.main(["--file", f, "new-token", "x"]) == 1                    # no password yet
    answers = iter(["a-long-password-1", "a-long-password-1"])
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt="": next(answers))
    assert auth.main(["--file", f, "set-password", "--user", "dj"]) == 0
    capsys.readouterr()
    assert auth.main(["--file", f, "new-token", "sim"]) == 0
    out = capsys.readouterr()
    token = out.out.strip()
    assert token.startswith(auth.TOKEN_PREFIX) and token not in out.err
    assert auth.main(["--file", f, "list-tokens"]) == 0
    listing = capsys.readouterr().out
    assert "sim" in listing and token not in listing
    answers = iter(["a-long-password-2", "different-password"])
    assert auth.main(["--file", f, "set-password"]) == 1


# ---- uploads, logs ------------------------------------------------------------------------------------

def test_upload_limit(api_token):
    c = _bearer(api_token)
    c.get("/health")                                    # builds the middleware stack
    mw = server.app.middleware_stack
    while mw is not None and not isinstance(mw, auth.BodyLimitMiddleware):
        mw = getattr(mw, "app", None)
    assert mw is not None
    key = ("POST", "/api/samples")
    old = mw.limits[key]
    try:
        mw.limits[key] = 1000
        r = c.post("/api/samples", files={"file": ("a.wav", b"\0" * 5000, "audio/wav")})
        assert r.status_code == 413
    finally:
        mw.limits[key] = old


def test_log_redaction(api_token, owner_login):
    flt = auth.RedactFilter(server.AUTH_GUARD)
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/1.1" %d',
                            ("1.2.3.4", "GET", f"/api/recipes?token={api_token}&password={owner_login[1]}", 401), None)
    flt.filter(rec)
    assert api_token not in rec.getMessage() and owner_login[1] not in rec.getMessage()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "headers: Authorization: Bearer %s; Cookie: %s=abc",
                            (api_token, server.AUTH_SETTINGS.cookie_name), None)
    flt.filter(rec)
    assert api_token not in rec.getMessage() and "=abc" not in rec.getMessage()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "body %s", ({"password": owner_login[1]},), None)
    flt.filter(rec)
    assert owner_login[1] not in rec.getMessage()
