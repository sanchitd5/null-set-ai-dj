"""Google sign-in (app/ui/services/auth.py) against a mocked Google: a local RSA key signs the
ID tokens, a fake fetch serves the JWKS and the token endpoint. Its own small app, so the
shared console server (Google off) is untouched. Addresses are example.com only."""

import base64
import hashlib
import logging
import time
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from app.tests.py.testclient_compat import TestClient
from app.ui.services import auth

WEB_ID = "web-client.apps.googleusercontent.com"
ANDROID_ID = "android-client.apps.googleusercontent.com"
OWNER = "owner@example.com"
PUBLIC = "https://testserver"
ORIGIN = {"Origin": PUBLIC}


def _rsa():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeGoogle:
    """JWKS + token endpoint. `claims` overrides what the next ID token says."""

    def __init__(self):
        self.keys = {"k1": _rsa()}
        self.kid = "k1"
        self.claims = {}
        self.codes = {}              # code -> (challenge, nonce) as the start redirect set them
        self.jwks_fetches = 0
        self.exchanged = []

    def jwks(self):
        out = []
        for kid, k in self.keys.items():
            j = jwt.algorithms.RSAAlgorithm.to_jwk(k.public_key(), as_dict=True)
            out.append({**j, "kid": kid, "alg": "RS256", "use": "sig"})
        return {"keys": out}

    def id_token(self, aud=WEB_ID, nonce=None, **over):
        now = int(time.time())
        c = {"iss": "https://accounts.google.com", "aud": aud, "sub": "1111", "email": OWNER,
             "email_verified": True, "iat": now, "exp": now + 3600}
        if nonce is not None:
            c["nonce"] = nonce
        c.update(over)
        c = {k: v for k, v in c.items() if v is not None}
        return jwt.encode(c, self.keys[self.kid], algorithm="RS256", headers={"kid": self.kid})

    def fetch(self, url, data=None):
        if url == auth.GOOGLE_JWKS_URL:
            self.jwks_fetches += 1
            return self.jwks(), {"cache-control": "public, max-age=3600"}
        assert url == auth.GOOGLE_TOKEN_URL
        challenge, nonce = self.codes.pop(data["code"])          # a code works once
        got = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest()).rstrip(b"=")
        if got.decode() != challenge:
            raise auth.GoogleAuthError("PKCE mismatch")
        assert data["client_secret"] == "web-secret" and data["redirect_uri"] == PUBLIC + "/auth/google/callback"
        self.exchanged.append(data["code"])
        over = dict(self.claims)
        return {"id_token": self.id_token(nonce=over.pop("nonce", nonce), **over)}, {}


def _settings(tmp_path, **env):
    base = {"DJ_SESSION_SECRET": "s" * 40, "DJ_GOOGLE_CLIENT_ID": WEB_ID, "DJ_GOOGLE_CLIENT_SECRET": "web-secret",
            "DJ_PUBLIC_URL": PUBLIC, "DJ_ALLOWED_EMAILS": f"Someone@Example.com, {OWNER.upper()}",
            "DJ_GOOGLE_MOBILE_CLIENT_IDS": ANDROID_ID}
    base.update(env)
    return auth.AuthSettings.from_env(store_path=tmp_path / "auth.json", environ={k: v for k, v in base.items() if v is not None})


@pytest.fixture
def g(tmp_path):
    app = FastAPI()
    app.get("/x")(lambda: {"ok": True})
    s = _settings(tmp_path)
    guard = auth.install(app, s, log_names=())
    fake = FakeGoogle()
    guard.google = auth.GoogleVerifier(fetch=fake.fetch)
    c = TestClient(app, auth=False, base_url=PUBLIC)
    c.fake, c.guard, c.settings = fake, guard, s
    return c


def _start(c):
    r = c.get("/auth/google/start", follow_redirects=False)
    assert r.status_code == 303
    loc = urlsplit(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == auth.GOOGLE_AUTH_URL
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    sc = r.headers["set-cookie"]
    assert "HttpOnly" in sc and "SameSite=Lax" in sc and "Secure" in sc and "Max-Age=600" in sc
    return q


def _code_for(c, q, code="code-1"):
    c.fake.codes[code] = (q["code_challenge"], q["nonce"])
    return code


def _callback(c, q, code=None, state=None):
    code = code or _code_for(c, q)
    return c.get("/auth/google/callback", params={"state": state or q["state"], "code": code}, follow_redirects=False)


# ---- browser flow ------------------------------------------------------------------------------

def test_good_login(g):
    q = _start(g)
    assert q["response_type"] == "code" and q["scope"] == "openid email" and q["prompt"] == "select_account"
    assert q["code_challenge_method"] == "S256" and q["client_id"] == WEB_ID
    assert q["redirect_uri"] == PUBLIC + "/auth/google/callback" and q["nonce"] and q["state"]
    assert g.get("/x").status_code == 401
    r = _callback(g, q)
    assert r.status_code == 200 and 'http-equiv="refresh"' in r.text
    assert any(h.startswith("dj_session=") and "SameSite=Strict" in h for h in r.headers.get_list("set-cookie"))
    assert g.get("/x").status_code == 200
    ident = g.settings.store.list_google()
    assert [(i["sub"], i["email"]) for i in ident] == [("1111", OWNER)]


@pytest.mark.parametrize("claims,why", [
    ({"aud": "someone-else.apps.googleusercontent.com"}, "wrong aud"),
    ({"iss": "https://evil.example.com"}, "wrong iss"),
    ({"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, "expired"),
    ({"iat": int(time.time()) + 3600, "exp": int(time.time()) + 7200}, "issued in the future"),
    ({"nonce": "not-the-nonce"}, "bad nonce"),
    ({"email_verified": False}, "unverified"),
    ({"email": "intruder@example.com"}, "not allowed"),
])
def test_refused_tokens(g, claims, why):
    g.fake.claims = claims
    r = _callback(g, _start(g))
    assert r.status_code == 403, why
    assert auth.NOT_AUTHORISED in r.text
    assert not any(h.startswith("dj_session=") and "Max-Age=0" not in h for h in r.headers.get_list("set-cookie"))
    assert g.get("/x").status_code == 401


def test_bad_state_and_missing_cookie(g):
    q = _start(g)
    assert _callback(g, q, state="forged-state").status_code == 403
    q = _start(g)
    g.cookies.clear()                                         # callback in a browser that never started
    assert _callback(g, q).status_code == 403
    assert g.fake.exchanged == []                             # never reached Google's token endpoint


def test_replayed_state(g):
    q = _start(g)
    pre = g.cookies.get(g.settings.preauth_cookie)
    assert _callback(g, q).status_code == 200
    g.cookies.clear()
    g.cookies.set(g.settings.preauth_cookie, pre, path="/auth/google/")
    assert _callback(g, q, code=_code_for(g, q, "code-2")).status_code == 403
    assert g.fake.exchanged == ["code-1"]


def test_google_error_param(g):
    q = _start(g)
    r = g.get("/auth/google/callback", params={"state": q["state"], "error": "access_denied"})
    assert r.status_code == 403


def test_address_pinned_to_first_account(g):
    assert _callback(g, _start(g)).status_code == 200
    g.cookies.clear()
    g.fake.claims = {"sub": "2222"}                           # same address, another Google account
    assert _callback(g, _start(g)).status_code == 403
    assert g.get("/x").status_code == 401
    assert g.settings.store.forget_google("1111")             # owner unpins over SSH
    g.cookies.clear()
    assert _callback(g, _start(g)).status_code == 200


def test_session_ends_when_address_leaves_the_allow_list(g):
    assert _callback(g, _start(g)).status_code == 200
    g.guard.settings = _settings(g.settings.store.path.parent, DJ_ALLOWED_EMAILS="someone@example.com")
    assert g.get("/x").status_code == 401


def test_jwks_rotation(g):
    assert _callback(g, _start(g)).status_code == 200
    assert g.fake.jwks_fetches == 1
    g.fake.keys["k2"] = _rsa()
    g.fake.kid = "k2"
    g.guard.google._last_fetch -= 60                          # past the refetch floor
    g.cookies.clear()
    assert _callback(g, _start(g)).status_code == 200
    assert g.fake.jwks_fetches == 2


def test_unknown_kid_refused(g):
    g.fake.keys["k9"] = _rsa()
    g.fake.kid = "k9"
    tok = g.fake.id_token(nonce="n")
    g.fake.keys.pop("k9")
    g.fake.kid = "k1"
    forged = jwt.encode({"iss": "https://accounts.google.com", "aud": WEB_ID, "sub": "1", "email": OWNER,
                         "email_verified": True, "iat": int(time.time()), "exp": int(time.time()) + 60},
                        _rsa(), algorithm="RS256", headers={"kid": "k1"})
    with pytest.raises(auth.GoogleAuthError):
        g.guard.google.verify(g.settings.google, forged, [WEB_ID])          # right kid, wrong key
    with pytest.raises(auth.GoogleAuthError):
        g.guard.google.verify(g.settings.google, tok, [WEB_ID])             # kid Google doesn't publish


# ---- phone ----------------------------------------------------------------------------------------

def test_mobile_token_exchange(g):
    r = g.post("/api/auth/google/token", json={"id_token": g.fake.id_token(aud=ANDROID_ID), "device_name": "phone"})
    assert r.status_code == 200
    tok = r.json()["token"]
    assert tok.startswith(auth.TOKEN_PREFIX) and "sub" not in r.json()
    c = TestClient(g.app, auth=False, base_url=PUBLIC, headers={"Authorization": f"Bearer {tok}"})
    assert c.get("/x").status_code == 200
    # the web client id is accepted too (Android serverClientId pattern)
    r = g.post("/api/auth/google/token", json={"id_token": g.fake.id_token(aud=WEB_ID), "device_name": "tablet"})
    assert r.status_code == 200
    # forgetting the Google account revokes the tokens it issued
    assert g.settings.store.forget_google("1111")
    assert c.get("/x").status_code == 401


@pytest.mark.parametrize("over", [{"aud": "other.apps.googleusercontent.com"}, {"email": "intruder@example.com"},
                                  {"email_verified": "false"}, {"iss": "https://evil.example.com"}])
def test_mobile_token_refused(g, over):
    r = g.post("/api/auth/google/token", json={"id_token": g.fake.id_token(**{"aud": ANDROID_ID, **over}),
                                                 "device_name": "phone"})
    assert r.status_code == 401 and r.json() == {"detail": auth.NOT_AUTHORISED}
    assert g.settings.store.list_tokens() == []


def test_mobile_token_rate_limited(g):
    for _ in range(g.settings.max_failures):
        g.post("/api/auth/google/token", json={"id_token": "not.a.jwt", "device_name": "x"})
    r = g.post("/api/auth/google/token", json={"id_token": g.fake.id_token(aud=ANDROID_ID), "device_name": "x"})
    assert r.status_code == 429


def test_mobile_bad_body(g):
    assert g.post("/api/auth/google/token", json={"device_name": "x"}).status_code == 400


# ---- password login when Google is on ---------------------------------------------------------

def test_password_login_off_when_google_configured(g):
    g.settings.store.set_password("owner", "a-long-enough-password")
    page = g.get("/login")
    assert "Sign in with Google" in page.text and 'name="password"' not in page.text
    r = g.post("/login", data={"username": "owner", "password": "a-long-enough-password"}, headers=ORIGIN,
               follow_redirects=False)
    assert r.status_code == 403
    r = g.post("/api/auth/token", json={"username": "owner", "password": "a-long-enough-password", "device_name": "x"})
    assert r.status_code == 403


def test_password_login_break_glass_from_loopback(g):
    g.settings.store.set_password("owner", "a-long-enough-password")
    c = TestClient(g.app, auth=False, base_url=PUBLIC, client=("127.0.0.1", 5000))
    assert 'name="password"' in c.get("/login").text
    r = c.post("/login", data={"username": "owner", "password": "a-long-enough-password"}, headers=ORIGIN,
               follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/x").status_code == 200


def test_password_login_env_reenables(tmp_path):
    s = _settings(tmp_path, DJ_PASSWORD_LOGIN="1")
    s.store.set_password("owner", "a-long-enough-password")
    assert s.password_login_ok("203.0.113.9")
    assert not _settings(tmp_path).password_login_ok("203.0.113.9")
    assert _settings(tmp_path).password_login_ok("127.0.0.1")


# ---- config and startup --------------------------------------------------------------------------

def test_config_rules(tmp_path):
    with pytest.raises(auth.AuthConfigError, match="ALLOWED_EMAILS"):
        _settings(tmp_path, DJ_ALLOWED_EMAILS="")
    with pytest.raises(auth.AuthConfigError):
        _settings(tmp_path, DJ_GOOGLE_CLIENT_SECRET="")
    with pytest.raises(auth.AuthConfigError, match="https"):
        _settings(tmp_path, DJ_PUBLIC_URL="http://dj.example.com")
    with pytest.raises(auth.AuthConfigError):
        _settings(tmp_path, DJ_PUBLIC_URL="https://dj.example.com/sub")
    with pytest.raises(auth.AuthConfigError):
        _settings(tmp_path, DJ_GOOGLE_CLIENT_ID="")              # mobile ids without a web client
    s = _settings(tmp_path)
    assert s.enabled and not s.store.has_password
    auth.check_bind(s, argv=["--host", "0.0.0.0"], environ={})   # Google alone may bind publicly
    assert s.google.allowed_emails == {"someone@example.com", OWNER}


def test_logs_never_carry_google_secrets(g):
    flt = auth.RedactFilter(g.guard)
    tok = g.fake.id_token(nonce="n")
    for msg, args in [('%s - "%s %s HTTP/1.1" %d', ("1.2.3.4", "GET", "/auth/google/callback?state=abc&code=4/0Secret", 200)),
                      ("token response %s", ({"id_token": tok},)),
                      ("raw %s", (tok,)),
                      ("cookie %s", (f"{g.settings.preauth_cookie}=state.sig",))]:
        rec = logging.LogRecord("x", logging.INFO, __file__, 1, msg, args, None)
        flt.filter(rec)
        out = rec.getMessage()
        assert "4/0Secret" not in out and tok not in out and "state.sig" not in out, out


def test_public_url_with_a_port(tmp_path):
    """Router port 8448 -> Caddy :443: the redirect URI and the CSRF origin keep the port."""
    s = _settings(tmp_path, DJ_PUBLIC_URL="https://dj.example.com:8448")
    assert s.google.redirect_uri == "https://dj.example.com:8448/auth/google/callback"
    assert s.public_url == "https://dj.example.com:8448"
    app = FastAPI()
    auth.install(app, s, log_names=())
    c = TestClient(app, auth=False, base_url="http://127.0.0.1:8000",
                   headers={"X-Forwarded-Proto": "https", "Host": "dj.example.com:8448"}, client=("127.0.0.1", 1))
    r = c.get("/auth/google/start", follow_redirects=False)
    q = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
    assert q["redirect_uri"] == "https://dj.example.com:8448/auth/google/callback"
    assert "Secure" in r.headers["set-cookie"]
