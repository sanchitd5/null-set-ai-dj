"""Owner-account auth for an ASGI app. Self-contained: stdlib plus starlette's CORS
middleware, no imports from this app, so another service (the stem server) can reuse it
with its own cookie name, or share the same account store:

    settings = AuthSettings.from_env(store_path=Path(".../auth.json"), cookie_name="stem_session")
    check_bind(settings)                       # refuse to start: public bind with no password set
    install(app, settings, body_limits={...})

One owner account (username + scrypt password hash) and per-device bearer tokens live in
a JSON store (chmod 600, atomic writes), managed by the CLI:

    python -m app.ui.services.auth set-password [--user NAME]
    python -m app.ui.services.auth new-token NAME      # prints the token once
    python -m app.ui.services.auth list-tokens
    python -m app.ui.services.auth revoke-token ID

Ways in:
  * Browser: GET/POST <login_path> with username + password -> session cookie (HttpOnly,
    SameSite=Strict, Secure on HTTPS, HMAC-signed with DJ_SESSION_SECRET, expiry, carries
    the password version so a password change logs out every browser). <logout_path>
    clears it.
  * Machines: `Authorization: Bearer <device token>`. A device gets one from
    POST <token_path> {username, password, device_name}; only its sha256 is stored.
    GET <devices_path> lists them, DELETE <devices_path>/<id> revokes one.
Secrets are never accepted in the query string. Cookie-authenticated unsafe requests must
carry a matching Origin / Referer (CSRF). Failed logins are limited per client IP and per
username. X-Forwarded-For / -Proto are honoured only from a trusted proxy.

Auth is ON once a password is set (localhost included). With no password the app serves
loopback only, with a warning.

Second factor seam: `user["totp"]` is stored (null today) and `_second_factor_ok()` is
called on every password login; a TOTP check slots in there without touching the rest.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("auth")

MIN_PASSWORD_LEN = 12
MIN_SECRET_LEN = 32
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_FORM_BODY_MAX = 4096
_SCRYPT = dict(n=2 ** 14, r=8, p=1)
TOKEN_PREFIX = "dj_"
INVALID_LOGIN = "invalid username or password"


class AuthConfigError(RuntimeError):
    """The auth settings are unsafe; the server must not start."""


def default_store_path() -> Path:
    """DJ_AUTH_FILE, else <cache>/auth/auth.json where <cache> is AIDJ_CACHE_DIR or the
    repo's gitignored data/cache (the same place the server and the CLI both look)."""
    env = os.environ.get("DJ_AUTH_FILE")
    if env:
        return Path(env).expanduser()
    cache = os.environ.get("AIDJ_CACHE_DIR")
    base = Path(cache).expanduser().resolve() if cache else Path(__file__).resolve().parents[3] / "data" / "cache"
    return base / "auth" / "auth.json"


# ---- password hashing (stdlib scrypt) ----------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${n}${r}${p}${s}${h}".format(**_SCRYPT, s=_b64(salt), h=_b64(dk))


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, want = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt), dklen=len(_unb64(want)),
                            n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, _unb64(want))


_DUMMY_HASH: Optional[str] = None


def _burn_hash(password: str) -> None:
    """Same scrypt cost as a real check, for an unknown username (no timing oracle)."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _DUMMY_HASH)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---- the account store -----------------------------------------------------------------------

class AuthStore:
    """{"version": 1, "user": {name, hash, pw_version, totp}|null, "tokens": [{id, name, sha256,
    created, last_used}]}. Re-read when the file changes on disk, so CLI edits (a revoke, a new
    password) apply to a running server at its next request."""

    LAST_USED_FLUSH_S = 300

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = {"version": 1, "user": None, "tokens": []}
        self._stamp: Optional[Tuple[int, int]] = None
        self._dirty_at = 0.0
        self._load()

    # -- io
    def _load(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._data, self._stamp = {"version": 1, "user": None, "tokens": []}, None
            return
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._stamp:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AuthConfigError(f"auth store {self.path} is unreadable: {exc}") from exc
        data.setdefault("user", None)
        data.setdefault("tokens", [])
        self._data, self._stamp = data, stamp

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(prefix=".auth-", dir=str(self.path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        st = self.path.stat()
        self._stamp = (st.st_mtime_ns, st.st_size)

    def refresh(self) -> None:
        with self._lock:
            self._load()

    # -- account
    @property
    def user(self) -> Optional[dict]:
        with self._lock:
            self._load()
            return self._data.get("user")

    @property
    def has_password(self) -> bool:
        u = self.user
        return bool(u and u.get("hash"))

    def set_password(self, username: str, password: str) -> None:
        if not USERNAME_RE.match(username or ""):
            raise ValueError("username: 1-64 of letters, digits, _ and -")
        if len(password) < MIN_PASSWORD_LEN:
            raise ValueError(f"password must be at least {MIN_PASSWORD_LEN} characters")
        with self._lock:
            self._load()
            old = self._data.get("user") or {}
            self._data["user"] = {"name": username, "hash": hash_password(password),
                                  "pw_version": int(old.get("pw_version", 0)) + 1,
                                  "totp": old.get("totp")}
            self._save()

    def check_login(self, username: str, password: str) -> Optional[dict]:
        u = self.user
        if not u or not u.get("hash") or not hmac.compare_digest(u["name"].encode(), (username or "").encode()):
            _burn_hash(password)
            return None
        return u if verify_password(password, u["hash"]) else None

    # -- device tokens
    def new_token(self, name: str) -> Tuple[str, dict]:
        name = " ".join((name or "").split())[:64]
        if not name:
            raise ValueError("device name required")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        row = {"id": secrets.token_hex(4), "name": name, "sha256": _sha256(token),
               "created": int(time.time()), "last_used": None}
        with self._lock:
            self._load()
            self._data["tokens"].append(row)
            self._save()
        return token, {k: v for k, v in row.items() if k != "sha256"}

    def list_tokens(self) -> List[dict]:
        with self._lock:
            self._load()
            return [{k: v for k, v in t.items() if k != "sha256"} for t in self._data["tokens"]]

    def revoke(self, token_id: str) -> bool:
        with self._lock:
            self._load()
            keep = [t for t in self._data["tokens"] if t["id"] != token_id]
            if len(keep) == len(self._data["tokens"]):
                return False
            self._data["tokens"] = keep
            self._save()
            return True

    def check_token(self, token: str) -> Optional[dict]:
        digest = _sha256(token).encode()
        hit = None
        with self._lock:
            self._load()
            for t in self._data["tokens"]:            # every row compared: no early exit
                if hmac.compare_digest(t["sha256"].encode(), digest):
                    hit = t
            if hit is not None:
                now = int(time.time())
                hit["last_used"] = now
                if now - self._dirty_at > self.LAST_USED_FLUSH_S:
                    self._dirty_at = now
                    try:
                        self._save()
                    except OSError:
                        pass
        return hit


# ---- settings --------------------------------------------------------------------------------

def _split(value: str) -> Tuple[str, ...]:
    return tuple(p.strip() for p in (value or "").split(",") if p.strip())


@dataclass
class AuthSettings:
    store: AuthStore
    session_secret: str = ""
    cookie_name: str = "dj_session"
    session_seconds: int = 30 * 86400
    # Caddy (or another TLS terminator) on the same host: 127.0.0.1 / ::1.
    trusted_proxies: Tuple[str, ...] = ("127.0.0.1", "::1")
    # Extra origins allowed for cookie-authenticated unsafe requests (CSRF) and CORS.
    allowed_origins: Tuple[str, ...] = ()
    public_paths: Tuple[str, ...] = ("/health",)
    hidden_paths: Tuple[str, ...] = ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json")
    login_path: str = "/login"
    logout_path: str = "/logout"
    token_path: str = "/api/auth/token"
    devices_path: str = "/api/auth/devices"
    max_failures: int = 10
    failure_window_s: int = 600
    lockout_s: int = 900
    title: str = "Sign in"

    @classmethod
    def from_env(cls, store_path: Optional[Path] = None, prefix: str = "DJ_",
                 environ: Optional[Dict[str, str]] = None, **overrides) -> "AuthSettings":
        env = os.environ if environ is None else environ
        kw: dict = {"store": AuthStore(store_path or default_store_path())}
        secret = (env.get(prefix + "SESSION_SECRET") or "").strip()
        if secret and len(secret) < MIN_SECRET_LEN:
            raise AuthConfigError(f"{prefix}SESSION_SECRET is shorter than {MIN_SECRET_LEN} characters")
        if not secret:
            log.warning("%sSESSION_SECRET is not set: browser sessions end when the server restarts "
                        "(start.sh writes one into .env)", prefix)
            secret = secrets.token_urlsafe(32)
        kw["session_secret"] = secret
        if env.get(prefix + "SESSION_DAYS"):
            kw["session_seconds"] = int(float(env[prefix + "SESSION_DAYS"]) * 86400)
        if (prefix + "TRUSTED_PROXIES") in env:
            kw["trusted_proxies"] = _split(env[prefix + "TRUSTED_PROXIES"])
        if env.get(prefix + "ALLOWED_ORIGINS"):
            kw["allowed_origins"] = tuple(o.rstrip("/") for o in _split(env[prefix + "ALLOWED_ORIGINS"]))
        kw.update(overrides)
        s = cls(**kw)
        s.validate()
        return s

    def validate(self) -> None:
        if len(self.session_secret) < MIN_SECRET_LEN:
            raise AuthConfigError(f"session secret must be at least {MIN_SECRET_LEN} characters")
        if "*" in self.allowed_origins:
            raise AuthConfigError("allowed origins must be an explicit list, not '*'")
        for p in self.trusted_proxies:
            ipaddress.ip_network(p, strict=False)       # raises ValueError on a typo

    @property
    def enabled(self) -> bool:
        return self.store.has_password


# ---- bind check ------------------------------------------------------------------------------

def _is_loopback(host: Optional[str]) -> bool:
    if not host:
        return False
    host = host.strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def bind_host_from_argv(argv: Sequence[str], environ: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The address uvicorn / gunicorn was told to bind (None = unknown, e.g. --fd).
    A unix socket counts as local."""
    env = os.environ if environ is None else environ
    host: Optional[str] = env.get("UVICORN_HOST") or "127.0.0.1"
    args = list(argv)
    for i, a in enumerate(args):
        if a == "--uds" or a.startswith("--uds="):
            return "localhost"
        if a == "--fd" or a.startswith("--fd="):
            return None
        for flag in ("--host", "--bind", "-b"):
            val = args[i + 1] if a == flag and i + 1 < len(args) else (
                a.split("=", 1)[1] if a.startswith(flag + "=") else None)
            if val is None:
                continue
            if val.startswith("unix:"):
                return "localhost"
            if flag != "--host":                      # gunicorn host:port / [v6]:port
                val = val.rsplit(":", 1)[0] if (val.count(":") == 1 or val.startswith("[")) else val
            host = val
    return host


def check_bind(settings: AuthSettings, argv: Optional[Sequence[str]] = None,
               environ: Optional[Dict[str, str]] = None) -> None:
    """Fail closed: no password and a non-loopback bind -> refuse to start."""
    if settings.enabled:
        return
    host = bind_host_from_argv(sys.argv if argv is None else argv, environ)
    if not _is_loopback(host):
        raise AuthConfigError(f"refusing to start: binding {host or 'an unknown address'} with no owner password. "
                              "Run: python -m app.ui.services.auth set-password   (or bind 127.0.0.1 only)")
    log.warning("AUTH OFF: no owner password set - serving loopback only, with no login. "
                "Run: python -m app.ui.services.auth set-password")


# ---- session cookie --------------------------------------------------------------------------

def _session_sig(settings: AuthSettings, body: str) -> str:
    key = hmac.new(settings.session_secret.encode(), b"session-v2:" + settings.cookie_name.encode(),
                   hashlib.sha256).digest()
    return _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())


def make_session(settings: AuthSettings, user: dict, now: Optional[float] = None) -> str:
    exp = int((time.time() if now is None else now) + settings.session_seconds)
    body = f"v2.{user['name']}.{int(user.get('pw_version', 0))}.{exp}.{secrets.token_urlsafe(12)}"
    return f"{body}.{_session_sig(settings, body)}"


def check_session(settings: AuthSettings, value: str, now: Optional[float] = None) -> str:
    """'ok' | 'expired' | 'stale' (password changed / user gone) | 'bad' (forged, malformed)."""
    parts = value.split(".")
    if len(parts) != 6 or parts[0] != "v2" or not parts[2].isdigit() or not parts[3].isdigit():
        return "bad"
    body = ".".join(parts[:5])
    if not hmac.compare_digest(_session_sig(settings, body).encode(), parts[5].encode()):
        return "bad"
    if int(parts[3]) <= (time.time() if now is None else now):
        return "expired"
    u = settings.store.user
    if not u or u["name"] != parts[1] or int(u.get("pw_version", 0)) != int(parts[2]):
        return "stale"
    return "ok"


# ---- failed-attempt limiter ------------------------------------------------------------------

class FailureLimiter:
    """In-memory failure counter per key ("ip:..." / "user:..."): max_failures inside the
    window -> that key is locked for lockout_s."""

    def __init__(self, max_keys: int = 10000):
        self._fails: "OrderedDict[str, deque]" = OrderedDict()
        self._locked: Dict[str, float] = {}
        self._lock = threading.Lock()
        self.max_keys = max_keys

    def retry_after(self, key: str, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            until = self._locked.get(key)
            if until is None:
                return 0
            if until <= now:
                del self._locked[key]
                return 0
            return max(1, int(until - now))

    def fail(self, key: str, settings: AuthSettings, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            q = self._fails.pop(key, None) or deque()
            q.append(now)
            while q and q[0] <= now - settings.failure_window_s:
                q.popleft()
            if len(q) >= settings.max_failures:
                self._locked[key] = now + settings.lockout_s
            else:
                self._fails[key] = q
            while len(self._fails) > self.max_keys:
                self._fails.popitem(last=False)
            if len(self._locked) > self.max_keys:
                for k in [k for k, v in self._locked.items() if v <= now]:
                    del self._locked[k]

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()
            self._locked.clear()


# ---- request helpers -------------------------------------------------------------------------

def _headers(scope) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in scope.get("headers") or []:
        name, val = k.decode("latin-1").lower(), v.decode("latin-1")
        out[name] = f"{out[name]}{'; ' if name == 'cookie' else ', '}{val}" if name in out else val
    return out


def _trusted(settings: AuthSettings, ip: Optional[str]) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip.strip("[]"))
    except ValueError:
        return False
    return any(addr in ipaddress.ip_network(p, strict=False) for p in settings.trusted_proxies)


def client_ip(settings: AuthSettings, scope, headers: Dict[str, str]) -> str:
    peer = (scope.get("client") or ("unknown", 0))[0]
    if not _trusted(settings, peer) or "x-forwarded-for" not in headers:
        return peer
    hops = [h.strip() for h in headers["x-forwarded-for"].split(",") if h.strip()]
    for hop in reversed(hops):                     # rightmost hop not added by a trusted proxy
        if not _trusted(settings, hop):
            return hop
    return hops[0] if hops else peer


def is_https(settings: AuthSettings, scope, headers: Dict[str, str]) -> bool:
    scheme = scope.get("scheme", "http")
    peer = (scope.get("client") or ("", 0))[0]
    if _trusted(settings, peer) and headers.get("x-forwarded-proto"):
        scheme = headers["x-forwarded-proto"].split(",")[0].strip().lower()
    return scheme in ("https", "wss")


def _cookie(headers: Dict[str, str], name: str) -> Optional[str]:
    for part in headers.get("cookie", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


def origin_ok(settings: AuthSettings, headers: Dict[str, str]) -> bool:
    origin = headers.get("origin")
    if not origin or origin == "null":
        ref = headers.get("referer")
        if not ref:
            return False
        p = urlsplit(ref)
        origin = f"{p.scheme}://{p.netloc}"
    origin = origin.rstrip("/")
    if origin in settings.allowed_origins:
        return True
    host = headers.get("host", "").lower()
    return bool(host) and urlsplit(origin).netloc.lower() == host


# ---- ASGI middleware -------------------------------------------------------------------------

_LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{font:16px system-ui,sans-serif;background:#111;color:#eee;display:grid;place-items:center;min-height:90vh}}
form{{display:grid;gap:.6rem;min-width:18rem}}input,button{{font:inherit;padding:.5rem}}p{{color:#f77;margin:0}}</style>
</head><body><form method="post" action="{action}"><h1>{title}</h1>{error}
<input name="username" autocomplete="username" placeholder="username" required autofocus>
<input type="password" name="password" autocomplete="current-password" placeholder="password" required>
<button type="submit">Sign in</button></form></body></html>"""
_JSON = (b"content-type", b"application/json")
_UNAUTHORIZED = b'{"detail":"unauthorized"}'


class _BodyTooLarge(Exception):
    pass


async def _read_body(receive, cap: int) -> Optional[bytes]:
    body, more = b"", True
    while more:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            return None
        body += msg.get("body", b"")
        more = msg.get("more_body", False)
        if len(body) > cap:
            raise _BodyTooLarge()
    return body


class TokenAuthMiddleware:
    """Pure ASGI (http + websocket). Reads `guard.settings` and the store on every request,
    so a password change or a revoke applies without a restart."""

    def __init__(self, app, guard: "Guard"):
        self.app = app
        self.guard = guard

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        s = self.guard.settings
        path = scope.get("path", "")
        method = scope.get("method", "GET") if scope["type"] == "http" else "GET"
        if not s.enabled:
            server_host = (scope.get("server") or ("", 0))[0]
            if not _is_loopback(server_host):          # fail closed: auth off is loopback only
                return await self._deny(scope, send, 503, b'{"detail":"no owner password set"}')
            return await self.app(scope, receive, send)
        if path in s.public_paths and method in ("GET", "HEAD"):
            return await self.app(scope, receive, send)
        if path in s.hidden_paths:
            return await self._send(send, 404, b'{"detail":"Not Found"}', [_JSON])

        headers = _headers(scope)
        ip = client_ip(s, scope, headers)
        wait = self.guard.limiter.retry_after("ip:" + ip)
        if wait:
            return await self._deny(scope, send, 429, b'{"detail":"too many attempts"}',
                                    [(b"retry-after", str(wait).encode())])
        try:
            if scope["type"] == "http" and path == s.login_path:
                if method == "POST":
                    return await self._login(scope, receive, send, headers, ip)
                if method in ("GET", "HEAD"):
                    return await self._login_page(send, 200)
            if scope["type"] == "http" and path == s.token_path and method == "POST":
                return await self._issue_token(receive, send, ip)
        except _BodyTooLarge:
            return await self._send(send, 413, b'{"detail":"too large"}', [_JSON])
        if scope["type"] == "http" and path == s.logout_path and method in ("GET", "POST"):
            return await self._send(send, 303, b"", [(b"location", b"login"),
                                                     (b"set-cookie", self._cookie_header(headers, scope, "", 0))])

        auth = headers.get("authorization", "")
        if auth:
            scheme, _, cred = auth.partition(" ")
            if scheme.lower() == "bearer" and cred.strip() and s.store.check_token(cred.strip()):
                return await self._authed(scope, receive, send, path, method)
            self.guard.limiter.fail("ip:" + ip, s)
            return await self._deny(scope, send, 401)

        cookie = _cookie(headers, s.cookie_name)
        if cookie:
            state = check_session(s, cookie)
            if state == "ok":
                if (scope["type"] == "websocket" or method not in _SAFE_METHODS) and not origin_ok(s, headers):
                    return await self._deny(scope, send, 403)
                return await self._authed(scope, receive, send, path, method)
            if state == "bad":
                self.guard.limiter.fail("ip:" + ip, s)
            return await self._deny(scope, send, 401,
                                    extra=[(b"set-cookie", self._cookie_header(headers, scope, "", 0))])

        if (scope["type"] == "http" and method in ("GET", "HEAD") and not path.startswith("/api/")
                and "text/html" in headers.get("accept", "")):
            return await self._send(send, 303, b"", [(b"location", s.login_path.encode())])
        return await self._deny(scope, send, 401)

    # -- authenticated routes owned by this module --------------------------------------------
    async def _authed(self, scope, receive, send, path, method):
        s = self.guard.settings
        if scope["type"] == "http" and path == s.devices_path and method == "GET":
            body = json.dumps({"devices": s.store.list_tokens()}).encode()
            return await self._send(send, 200, body, [_JSON])
        if scope["type"] == "http" and path.startswith(s.devices_path + "/") and method == "DELETE":
            ok = s.store.revoke(path[len(s.devices_path) + 1:])
            return await self._send(send, 200 if ok else 404,
                                    b'{"ok":true}' if ok else b'{"detail":"unknown device"}', [_JSON])
        return await self.app(scope, receive, send)

    # -- password logins ----------------------------------------------------------------------
    def _password_login(self, ip: str, username: str, password: str) -> Tuple[Optional[dict], int]:
        """(user, retry_after). Limited per IP (checked by the caller) and per username."""
        s = self.guard.settings
        ukey = "user:" + (username or "").lower()[:64]
        wait = self.guard.limiter.retry_after(ukey)
        if wait:
            return None, wait
        user = s.store.check_login(username, password) if username and password else None
        if user is None and not (username and password):
            _burn_hash(password or "")
        if user is not None and not self._second_factor_ok(user):
            user = None
        if user is None:
            self.guard.limiter.fail("ip:" + ip, s)
            self.guard.limiter.fail(ukey, s)
        return user, 0

    def _second_factor_ok(self, user: dict) -> bool:
        """Seam for TOTP: today no account has one, so the password alone signs in."""
        return not user.get("totp")

    async def _login(self, scope, receive, send, headers, ip):
        s = self.guard.settings
        if not origin_ok(s, headers):                 # login CSRF: a foreign page can't sign you in
            return await self._deny(scope, send, 403)
        body = await _read_body(receive, _FORM_BODY_MAX)
        if body is None:
            return
        form = parse_qs(body.decode("utf-8", "replace"))
        user, wait = self._password_login(ip, (form.get("username") or [""])[0].strip(),
                                          (form.get("password") or [""])[0])
        if wait:
            return await self._send(send, 429, b'{"detail":"too many attempts"}',
                                    [_JSON, (b"retry-after", str(wait).encode())])
        if user is None:
            return await self._login_page(send, 401, error=True)
        value = make_session(s, user)
        return await self._send(send, 303, b"", [(b"location", b"./"),
                                                 (b"set-cookie", self._cookie_header(headers, scope, value,
                                                                                     s.session_seconds))])

    async def _issue_token(self, receive, send, ip):
        body = await _read_body(receive, _FORM_BODY_MAX)
        if body is None:
            return
        try:
            req = json.loads(body or b"{}")
            assert isinstance(req, dict)
        except (ValueError, AssertionError):
            return await self._send(send, 400, b'{"detail":"JSON body: username, password, device_name"}', [_JSON])
        user, wait = self._password_login(ip, str(req.get("username") or "").strip(), str(req.get("password") or ""))
        if wait:
            return await self._send(send, 429, b'{"detail":"too many attempts"}',
                                    [_JSON, (b"retry-after", str(wait).encode())])
        if user is None:
            return await self._send(send, 401, json.dumps({"detail": INVALID_LOGIN}).encode(), [_JSON])
        try:
            token, row = self.guard.settings.store.new_token(str(req.get("device_name") or ""))
        except ValueError as exc:
            return await self._send(send, 400, json.dumps({"detail": str(exc)}).encode(), [_JSON])
        return await self._send(send, 200, json.dumps({**row, "token": token}).encode(), [_JSON])

    async def _login_page(self, send, status, error=False):
        s = self.guard.settings
        action = html.escape(s.login_path.rsplit("/", 1)[-1] or "login")   # relative: works under a path prefix
        page = _LOGIN_HTML.format(title=html.escape(s.title), action=action,
                                  error=f"<p>{INVALID_LOGIN}</p>" if error else "")
        return await self._send(send, status, page.encode("utf-8"), [
            (b"content-type", b"text/html; charset=utf-8"),
            (b"content-security-policy",
             b"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"),
        ])

    def _cookie_header(self, headers, scope, value: str, max_age: int) -> bytes:
        s = self.guard.settings
        parts = [f"{s.cookie_name}={value}", "Path=/", f"Max-Age={max_age}", "HttpOnly", "SameSite=Strict"]
        if is_https(s, scope, headers):
            parts.append("Secure")
        return "; ".join(parts).encode("latin-1")

    # -- responses -----------------------------------------------------------------------------
    async def _deny(self, scope, send, status, body=_UNAUTHORIZED, extra=None):
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        return await self._send(send, status, body, [_JSON, *(extra or [])])

    @staticmethod
    async def _send(send, status, body, extra=None):
        hdrs = [(b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store"), *(extra or [])]
        await send({"type": "http.response.start", "status": status, "headers": hdrs})
        await send({"type": "http.response.body", "body": body})


class _TooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Caps request bodies: Content-Length checked up front, streamed bytes counted
    (chunked uploads). `limits` maps (method, path) -> bytes; `default` covers the rest."""

    def __init__(self, app, limits: Dict[Tuple[str, str], int], default: Optional[int] = None):
        self.app = app
        self.limits = limits
        self.default = default

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        cap = self.limits.get((scope.get("method", ""), scope.get("path", "")), self.default)
        if cap is None:
            return await self.app(scope, receive, send)
        cl = _headers(scope).get("content-length", "")
        if cl.isdigit() and int(cl) > cap:
            return await self._too_large(send, cap)
        seen, over, started = 0, False, False

        async def counted():
            nonlocal seen, over
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > cap:
                    over = True
                    raise _TooLarge()
            return msg

        async def guarded_send(msg):
            nonlocal started
            if over:                                   # the app turned our abort into its own error
                return
            started = started or msg["type"] == "http.response.start"
            await send(msg)

        try:
            await self.app(scope, counted, guarded_send)
        except _TooLarge:
            pass
        except Exception:
            if not over:
                raise
        if over and not started:
            await self._too_large(send, cap)

    @staticmethod
    async def _too_large(send, cap):
        body = json.dumps({"detail": f"request body too large (max {cap // (1024 * 1024)} MB)"}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [_JSON, (b"content-length", str(len(body)).encode()), (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})


# ---- log redaction ---------------------------------------------------------------------------

class RedactFilter(logging.Filter):
    """Strips bearer tokens, device tokens, cookies, passwords and query-string secrets."""

    def __init__(self, guard: "Guard"):
        super().__init__()
        self.guard = guard
        self._pats = [
            (re.compile(r"(?i)(bearer\s+)[^\s'\",;]+"), r"\1[redacted]"),
            (re.compile(re.escape(TOKEN_PREFIX) + r"[A-Za-z0-9_-]{20,}"), "[redacted]"),
            (re.compile(r"(?i)((?:^|[\s'\"{,])(?:authorization|cookie|set-cookie)['\"]?\s*[:=]\s*['\"]?)[^'\"\r\n]+"),
             r"\1[redacted]"),
            (re.compile(r"(?i)([?&](?:token|access_token|api_token|api_key|password)=)[^&\s'\"]+"), r"\1[redacted]"),
            (re.compile(r"(?i)(['\"]?password['\"]?\s*[:=]\s*['\"]?)[^'\"&\s,}]+"), r"\1[redacted]"),
        ]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 -- a broken record is the logger's problem, not ours
            return True
        name = re.escape(self.guard.settings.cookie_name)
        red = re.sub(rf"({name}=)[^;\s'\"]+", r"\1[redacted]", msg)
        for pat, rep in self._pats:
            red = pat.sub(rep, red)
        if red != msg:
            record.msg, record.args = red, ()
        return True


# ---- wiring ----------------------------------------------------------------------------------

@dataclass
class Guard:
    settings: AuthSettings
    limiter: FailureLimiter = field(default_factory=FailureLimiter)


def install(app, settings: AuthSettings, body_limits: Optional[Dict[Tuple[str, str], int]] = None,
            default_body_limit: Optional[int] = None,
            log_names: Iterable[str] = ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi", "")) -> Guard:
    """Adds (inside -> out) body limit, auth, CORS (only with an allow-list), and log
    redaction. Returns the Guard holding the live settings and the limiter."""
    guard = Guard(settings)
    if body_limits or default_body_limit:
        app.add_middleware(BodyLimitMiddleware, limits=dict(body_limits or {}), default=default_body_limit)
    app.add_middleware(TokenAuthMiddleware, guard=guard)
    if settings.allowed_origins:
        from starlette.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.allowed_origins),
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["Authorization", "Content-Type"], allow_credentials=False)
    flt = RedactFilter(guard)
    for name in log_names:
        logging.getLogger(name).addFilter(flt)
    return guard


def env_bytes(name: str, default: int, environ: Optional[Dict[str, str]] = None) -> int:
    env = os.environ if environ is None else environ
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError as exc:
        raise AuthConfigError(f"{name} must be a whole number of bytes") from exc
    if val <= 0:
        raise AuthConfigError(f"{name} must be positive")
    return val


# ---- CLI -------------------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.ui.services.auth",
                                 description="Owner password and device tokens for the console API.")
    ap.add_argument("--file", type=Path, default=None, help="account store (default: DJ_AUTH_FILE or data/cache/auth/auth.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("set-password", help="set or change the owner password (logs out every browser)")
    sp.add_argument("--user", default=None, help="username (default: the current one, else 'owner')")
    sub.add_parser("list-tokens", help="list device tokens")
    rv = sub.add_parser("revoke-token", help="revoke a device token by id")
    rv.add_argument("id")
    nt = sub.add_parser("new-token", help="create a device token (printed once)")
    nt.add_argument("name")
    a = ap.parse_args(argv)
    store = AuthStore(a.file or default_store_path())

    if a.cmd == "set-password":
        cur = store.user
        user = a.user or (cur["name"] if cur else "owner")
        if not USERNAME_RE.match(user):
            print("username: 1-64 of letters, digits, _ and -", file=sys.stderr)
            return 2
        pw = getpass.getpass(f"new password for {user}: ")
        if pw != getpass.getpass("again: "):
            print("passwords differ - nothing changed", file=sys.stderr)
            return 1
        try:
            store.set_password(user, pw)
        except ValueError as exc:
            print(f"{exc} - nothing changed", file=sys.stderr)
            return 1
        print(f"password set for {user} in {store.path} (every browser session is now signed out)")
        return 0
    if a.cmd == "list-tokens":
        rows = store.list_tokens()
        if not rows:
            print("no device tokens")
        for t in rows:
            used = time.strftime("%Y-%m-%d %H:%M", time.localtime(t["last_used"])) if t.get("last_used") else "never"
            made = time.strftime("%Y-%m-%d", time.localtime(t["created"]))
            print(f"{t['id']}  {t['name']:<24} created {made}  last used {used}")
        return 0
    if a.cmd == "revoke-token":
        if store.revoke(a.id):
            print(f"revoked {a.id}")
            return 0
        print(f"no token with id {a.id}", file=sys.stderr)
        return 1
    if a.cmd == "new-token":
        if not store.has_password:
            print("set the owner password first: python -m app.ui.services.auth set-password", file=sys.stderr)
            return 1
        try:
            token, row = store.new_token(a.name)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"device token {row['id']} ({row['name']}) - shown once, store it now:", file=sys.stderr)
        print(token)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
