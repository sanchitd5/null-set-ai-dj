"""Device sign-in (RFC 8628 style) in app/ui/services/auth.py: a phone starts, the owner approves
in a signed-in browser, the phone polls for its device token. Own small app with Google
configured (no network: sessions are minted directly). Addresses are example.com only."""

import logging
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI

from app.tests.py.testclient_compat import TestClient
from app.ui.services import auth

PUBLIC = "https://testserver"
ORIGIN = {"Origin": PUBLIC}
OWNER = "owner@example.com"


def _settings(tmp_path, **env):
    base = {"DJ_SESSION_SECRET": "s" * 40, "DJ_GOOGLE_CLIENT_ID": "web.apps.googleusercontent.com",
            "DJ_GOOGLE_CLIENT_SECRET": "web-secret", "DJ_PUBLIC_URL": PUBLIC, "DJ_ALLOWED_EMAILS": OWNER}
    base.update(env)
    return auth.AuthSettings.from_env(store_path=tmp_path / "auth.json", environ=base)


@pytest.fixture
def d(tmp_path):
    app = FastAPI()
    app.get("/x")(lambda: {"ok": True})
    s = _settings(tmp_path)
    guard = auth.install(app, s, log_names=())
    phone = TestClient(app, auth=False, base_url=PUBLIC)
    browser = TestClient(app, auth=False, base_url=PUBLIC)
    ident = s.store.bind_google("1111", OWNER)
    browser.cookies.set(s.cookie_name, auth.make_session(s, {"name": "g-1111", "pw_version": ident["version"]}))
    phone.browser, phone.guard, phone.settings = browser, guard, s
    return phone


def _start(c, name="Pixel phone"):
    r = c.post("/api/auth/device/start", json={"device_name": name})
    assert r.status_code == 200, r.text
    return r.json()


def _poll(c, device_code):
    return c.post("/api/auth/device/poll", json={"device_code": device_code})


def _approve_form(c, user_code):
    r = c.browser.get("/device", params={"code": user_code})
    assert r.status_code == 200, r.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
    return {"code": user_code, "csrf": csrf}


def _decide(c, user_code, decision="approve", headers=ORIGIN):
    form = {**_approve_form(c, user_code), "decision": decision}
    return c.browser.post("/device", data=form, headers=headers)


def _age(c, seconds):
    """Pretend the last poll was `seconds` ago (no sleeping)."""
    for f in c.guard.devices._flows:
        if f.last_poll:
            f.last_poll -= seconds


def test_start_shape(d):
    j = _start(d)
    assert len(j["device_code"]) >= 43                        # token_urlsafe(32): 32 random bytes
    assert re.fullmatch(r"[A-Z]{4}-[A-Z]{4}", j["user_code"])
    assert set(j["user_code"].replace("-", "")) <= set(auth.USER_CODE_ALPHABET)
    assert j["verification_uri"] == PUBLIC + "/device"
    assert j["verification_uri_complete"] == PUBLIC + "/device?code=" + j["user_code"]
    assert j["expires_in"] == 600 and j["interval"] == 3
    flow = d.guard.devices._flows[0]
    assert j["device_code"] not in repr(flow.__dict__)         # stored hashed only


def test_start_needs_a_name(d):
    assert d.post("/api/auth/device/start", json={}).status_code == 400
    assert d.post("/api/auth/device/start", json={"device_name": "   "}).status_code == 400
    assert d.post("/api/auth/device/start", content=b"not json").status_code == 400


def test_full_flow_once(d):
    j = _start(d)
    r = _poll(d, j["device_code"])
    assert r.status_code == 428 and r.json()["error"] == "authorization_pending"
    r = _decide(d, j["user_code"])
    assert r.status_code == 200 and "Approved" in r.text
    _age(d, 10)
    r = _poll(d, j["device_code"])
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith("dj_") and body["token_id"] and body["name"] == "Pixel phone"
    assert d.get("/x", headers={"Authorization": f"Bearer {body['token']}"}).status_code == 200
    rows = d.settings.store.list_tokens()
    assert [t["id"] for t in rows] == [body["token_id"]]
    assert d.settings.store._data["tokens"][0]["sub"] == "1111"     # bound to the approving owner
    # replay: the device code is single use
    _age(d, 10)
    assert _poll(d, j["device_code"]).status_code == 400
    assert len(d.settings.store.list_tokens()) == 1


def test_token_dies_with_the_owner_identity(d):
    j = _start(d)
    _decide(d, j["user_code"])
    tok = _poll(d, j["device_code"]).json()["token"]
    assert d.settings.store.forget_google("1111")
    assert d.get("/x", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_password_owner_can_approve(tmp_path):
    app = FastAPI()
    app.get("/x")(lambda: {"ok": True})
    s = _settings(tmp_path, DJ_GOOGLE_CLIENT_ID="", DJ_GOOGLE_CLIENT_SECRET="", DJ_ALLOWED_EMAILS="")
    s.store.set_password("owner", "correct horse battery")
    guard = auth.install(app, s, log_names=())
    c = TestClient(app, auth=False, base_url=PUBLIC)
    c.browser = TestClient(app, auth=False, base_url=PUBLIC)
    c.browser.cookies.set(s.cookie_name, auth.make_session(s, s.store.user))
    c.guard, c.settings = guard, s
    j = _start(c)
    assert _decide(c, j["user_code"]).status_code == 200
    r = _poll(c, j["device_code"])
    assert r.status_code == 200 and "sub" not in s.store._data["tokens"][0]
    assert c.get("/x", headers={"Authorization": f"Bearer {r.json()['token']}"}).status_code == 200


def test_deny(d):
    j = _start(d)
    r = _decide(d, j["user_code"], "deny")
    assert r.status_code == 200 and "Denied" in r.text
    r = _poll(d, j["device_code"])
    assert r.status_code == 403 and r.json()["error"] == "access_denied"
    _age(d, 10)
    assert _poll(d, j["device_code"]).status_code == 400          # gone after the answer
    assert d.settings.store.list_tokens() == []


def test_decided_code_cannot_be_decided_again(d):
    j = _start(d)
    form = {**_approve_form(d, j["user_code"]), "decision": "deny"}
    assert d.browser.post("/device", data=form, headers=ORIGIN).status_code == 200
    form["decision"] = "approve"
    assert d.browser.post("/device", data=form, headers=ORIGIN).status_code == 404
    assert _poll(d, j["device_code"]).json()["error"] == "access_denied"


def test_expiry(d):
    j = _start(d)
    for f in d.guard.devices._flows:
        f.expires -= 601
    r = _poll(d, j["device_code"])
    assert r.status_code == 410 and r.json()["error"] == "expired_token"
    assert d.browser.get("/device", params={"code": j["user_code"]}).status_code == 404


def test_approved_but_collected_too_late(d):
    j = _start(d)
    _decide(d, j["user_code"])
    for f in d.guard.devices._flows:
        f.expires -= 601
    assert _poll(d, j["device_code"]).status_code == 410
    assert d.settings.store.list_tokens() == []


def test_slow_down(d):
    j = _start(d)
    assert _poll(d, j["device_code"]).status_code == 428
    r = _poll(d, j["device_code"])                                 # right away: too fast
    assert r.status_code == 429 and r.json() == {"error": "slow_down", "interval": 8}
    _age(d, 4)
    assert _poll(d, j["device_code"]).status_code == 429             # 8 s now, and it grows again (13 s)
    _age(d, 14)
    assert _poll(d, j["device_code"]).status_code == 428


def test_wrong_device_code_counts_toward_the_ip_limit(d):
    for _ in range(d.settings.max_failures):
        assert _poll(d, "not-a-real-code").status_code == 400
    assert _poll(d, "not-a-real-code").status_code == 429
    assert d.post("/api/auth/device/start", json={"device_name": "x"}).status_code == 429


def test_wrong_user_code(d):
    _start(d)
    r = d.browser.get("/device", params={"code": "BBBB-BBBB"})
    assert r.status_code == 404 and "wrong" in r.text
    assert d.browser.get("/device", params={"code": "nonsense"}).status_code == 404
    form = {"code": "BBBB-BBBB", "csrf": "x", "decision": "approve"}
    assert d.browser.post("/device", data=form, headers=ORIGIN).status_code == 403


def test_user_code_is_forgiving_about_format(d):
    j = _start(d)
    loose = j["user_code"].replace("-", " ").lower()
    assert d.browser.get("/device", params={"code": loose}).status_code == 200


def test_entry_form_without_code(d):
    r = d.browser.get("/device")
    assert r.status_code == 200 and 'name="code"' in r.text


def test_csrf_token_and_origin(d):
    j = _start(d)
    form = {**_approve_form(d, j["user_code"]), "decision": "approve"}
    assert d.browser.post("/device", data=form).status_code == 403                   # no Origin
    assert d.browser.post("/device", data=form, headers={"Origin": "https://evil.example.com"}).status_code == 403
    bad = {**form, "csrf": form["csrf"][:-2] + "AA"}
    assert d.browser.post("/device", data=bad, headers=ORIGIN).status_code == 403
    # a CSRF token is tied to the browser session that saw the page
    other = TestClient(d.app, auth=False, base_url=PUBLIC)
    s = d.settings
    other.cookies.set(s.cookie_name, auth.make_session(s, {"name": "g-1111", "pw_version": 1}))
    assert other.post("/device", data=form, headers=ORIGIN).status_code == 403
    assert _poll(d, j["device_code"]).status_code == 428                              # still undecided
    assert d.browser.post("/device", data=form, headers=ORIGIN).status_code == 200


def test_unauthenticated_page_and_approve_go_to_login(d):
    j = _start(d)
    r = d.get("/device", params={"code": j["user_code"]}, follow_redirects=False)
    assert r.status_code == 303
    loc = urlsplit(r.headers["location"])
    assert loc.path == "/login" and parse_qs(loc.query)["next"] == ["/device?code=" + j["user_code"]]
    r = d.post("/device", data={"code": j["user_code"], "decision": "approve", "csrf": "x"},
               headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and urlsplit(r.headers["location"]).path == "/login"
    assert _poll(d, j["device_code"]).status_code == 428


def test_bearer_cannot_approve(d):
    tok, _ = d.settings.store.new_token("cli")
    j = _start(d)
    r = d.get("/device", params={"code": j["user_code"]}, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_login_next_round_trip(d):
    r = d.get("/login", params={"next": "/device?code=BCDF-GHJK"})
    assert "auth/google/start?next=%2Fdevice%3Fcode%3DBCDF-GHJK" in r.text
    r = d.get("/auth/google/start", params={"next": "/device?code=BCDF-GHJK"}, follow_redirects=False)
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    assert d.guard.pending.pop_with_next(state)[2] == "/device?code=BCDF-GHJK"


@pytest.mark.parametrize("nxt", ["https://evil.example.com", "//evil.example.com", "/x", "/device/../x",
                                 "/device?code=<script>"])
def test_login_next_is_not_an_open_redirect(d, nxt):
    assert auth._safe_next(nxt) == ""
    r = d.get("/auth/google/start", params={"next": nxt}, follow_redirects=False)
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    assert d.guard.pending.pop_with_next(state)[2] == ""


def test_start_rate_limit_per_ip(d):
    for i in range(auth.DeviceFlows.MAX_LIVE_PER_IP):
        _start(d, f"phone {i}")
    r = d.post("/api/auth/device/start", json={"device_name": "one more"})
    assert r.status_code == 429


def test_codes_never_logged(d, caplog):
    caplog.set_level(logging.DEBUG, logger="auth")
    j = _start(d)
    _decide(d, j["user_code"])
    tok = _poll(d, j["device_code"]).json()["token"]
    text = caplog.text
    for secret in (j["device_code"], tok, j["user_code"], j["user_code"].replace("-", "")):
        assert secret not in text


def test_redact_filter_strips_device_codes(d):
    flt = auth.RedactFilter(d.guard)
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, '{"device_code": "abc123secret", "user_code": "BCDF-GHJK"}',
                            (), None)
    flt.filter(rec)
    assert "abc123secret" not in rec.getMessage() and "BCDF-GHJK" not in rec.getMessage()
