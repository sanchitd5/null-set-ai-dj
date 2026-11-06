# Console API sign-in

Code: `app/ui/services/auth.py` (one module, stdlib + starlette, plus PyJWT only when Google
sign-in is configured; no imports from this app, so the stem server can reuse it). Tests:
`app/tests/py/test_auth.py`, `app/tests/py/test_auth_google.py`. TLS front end:
`deploy/Caddyfile` and `deploy/README-caddy.md`.

## The rule

* **Auth is on once Google sign-in is configured or an owner password exists**, for every
  request, localhost included (behind a reverse proxy every request looks local, so loopback
  is never exempt).
* Google sign-in is the primary way in; see [Google sign-in](#google-sign-in).
* No password, no Google + loopback bind (`127.0.0.1`, `::1`, a unix socket) + no `DJ_PUBLIC_URL` + no
  forwarded headers: auth off, with a warning in the log and at `./start.sh`. Local
  development only.
* **Fail closed behind a proxy:** with no password and no Google, a request carrying `X-Forwarded-For`,
  `-Proto`, `-Host`, `X-Real-IP` or `Forwarded` (Caddy always adds them), or any request
  while `DJ_PUBLIC_URL` is set, answers 503 `{"detail": "auth not configured"}`. Only
  `GET /health` still answers. Behind Caddy the console binds 127.0.0.1, so the bind alone
  would look local.
* No password, no Google + any other bind (`--host 0.0.0.0`, a LAN address, `--fd`): **the
  server refuses to start**. It also refuses when `DJ_GOOGLE_CLIENT_ID` is set but
  `DJ_ALLOWED_EMAILS` is empty, or the client secret / `DJ_PUBLIC_URL` is missing. A request that still arrives on a
  non-loopback address with no password gets 503.

## What is protected

Everything: `/api/*` (including `/api/audio/*`, the `<audio src>` reads), the static
console (`/`, `/index.html`, every JS / CSS file), and any websocket (none exist today; the
middleware closes unauthenticated websocket handshakes with 1008 if one is added). There is
no SSE endpoint. `/docs`, `/redoc`, `/openapi.json` answer 404 while auth is on.

Public: `GET /health` (`{"ok": true}`, nothing else), `GET/POST /login`, `/logout`,
`POST /api/auth/token` (it needs the password itself), and with Google:
`GET /auth/google/start`, `GET /auth/google/callback`, `POST /api/auth/google/token` (each
needs a valid Google sign-in).
`POST /api/auth/device/start` and `POST /api/auth/device/poll` are public too (device sign-in,
below); the `/device` approval page needs the browser login.

401 / 403 bodies are `{"detail": "unauthorized"}` and nothing more; a browser asking for a
page gets a redirect to `/login` instead.

One set at a time: on top of auth, the set-scoped routes need the set lease while a set runs
(`SET_LEASE.md`). The middleware hands the caller's identity to the app as `scope["dj_auth"]`
(`device:<token id>` or a hash of the browser session cookie, never the secret itself); the lease
uses it to name the holder. CORS (when `DJ_ALLOWED_ORIGINS` is set) allows the `X-Set-Lease` header.

## The owner account

One account. Password hashed with `hashlib.scrypt` (n=2^14, r=8, p=1, 16-byte salt), stored
with the device tokens in one JSON store (chmod 600, written atomically). Not in `.env`.

Where the store lives:
* `DJ_AUTH_FILE` if set: in the environment, or in the repo `.env`. The server and the CLI
  both read `.env`, so `set-password` finds the same file without `--file`.
* Else `$XDG_STATE_HOME/null-set/auth.json`, i.e. `~/.local/state/null-set/auth.json`
  (directory 0700).
* It is **never under `data/`**. `data/` may be a shared mount, e.g. the mergerfs pool on
  CT116 with 777 directories, where the file can't be made private.
* A store left at the old default `data/cache/auth/auth.json` is still used, with a warning,
  until you move it.

The store's directory must be private: owned by the user running the server, and not group-
or world-writable. Otherwise the server refuses to start and the CLI exits with one line
saying to set `DJ_AUTH_FILE`; there is no traceback. Someone else who can write there could
swap the file. The same refusal happens when the file can't be chmod 600, which some FUSE /
network filesystems don't allow. On CT116:

```bash
sudo install -d -m 700 -o nullset -g nullset /var/lib/null-set
echo 'DJ_AUTH_FILE=/var/lib/null-set/auth.json' >> /opt/null-set-ai-dj/.env      # and in the stem server's env file
```

```bash
python -m app.ui.services.auth set-password            # prompts twice, min 12 chars; --user NAME to rename
python -m app.ui.services.auth new-token sim           # prints the token ONCE (stdout only); no password needed
python -m app.ui.services.auth list-google             # Google accounts pinned to an allowed address
python -m app.ui.services.auth forget-google SUB       # unpin one: ends its sessions, revokes its device tokens
python -m app.ui.services.auth list-tokens             # id, name, created, last used (never the token)
python -m app.ui.services.auth revoke-token ID
```

Changes apply to a running server at its next request (it re-reads the file when it
changes). **Changing the password signs out every browser** (the session cookie carries the
password version). Device tokens are not affected by a password change; revoke them
separately.

## How each client signs in

| client | how |
|---|---|
| Browser (the console) | `/login` -> **Sign in with Google** (or username + password where that is on) -> session cookie. The console's JS is same-origin and unchanged; `<audio>`, `fetch`, workers all send the cookie. **DEVICES** (bottom-left) lists device tokens with a revoke button, and SIGN OUT. |
| Phone | Primary: **Sign in with browser** (device sign-in, below). Fallback: Google sign-in in the app, `POST /api/auth/google/token` with `{"id_token", "device_name"}` -> a device token (see below). The password route below still exists where password login is on: `POST /api/auth/token` with JSON `{"username", "password", "device_name"}` -> `{"id", "name", "created", "token"}`. Keep `token` in secure storage, send `Authorization: Bearer <token>` on every request. |
| Sim (`app/sim`: virtual_set, suite, stem_capture) | `python -m app.ui.services.auth new-token sim`, put it in `.env` as `DJ_API_TOKEN=...`. The sim's node side sends it as a Bearer header. |
| Scripts / curl | Same: a named device token, `curl -H "Authorization: Bearer $DJ_API_TOKEN" ...`. |
| `start.sh` readiness | `GET /health`, no credentials. |

Never in a query string: `?token=` etc. is ignored (401) and redacted from logs.

### Session cookie

`dj_session`: `HttpOnly`, `SameSite=Strict`, `Path=/`, `Max-Age` 30 days (`DJ_SESSION_DAYS`),
`Secure` when the request is HTTPS (directly, or `X-Forwarded-Proto: https` from a trusted
proxy). Value: username, password version, expiry, nonce, HMAC-SHA256 signature with a key
derived from `DJ_SESSION_SECRET`. No password or token inside. `start.sh` writes a random
`DJ_SESSION_SECRET` into `.env` the first time (never printed). Without one the server makes
a per-process secret (sessions end at restart) and warns.

To sign out everywhere: change the password, or replace `DJ_SESSION_SECRET` and restart.

### CSRF and CORS

A cookie-authenticated request that is not GET / HEAD / OPTIONS (and any websocket
handshake) must carry an `Origin` (or `Referer`) whose host equals the `Host` header, or an
origin listed in `DJ_ALLOWED_ORIGINS`. Bearer requests are exempt (a browser never adds that
header on its own). The login form is checked the same way.

CORS: off unless `DJ_ALLOWED_ORIGINS` lists origins (comma separated, no `*`). The console is
served by the app itself and needs none.

### Brute force

In memory, per client IP and per username: 10 failures in 10 minutes -> 429 for 15 minutes.
A failure is a wrong password, a wrong bearer token, or a forged cookie. The client IP comes
from `X-Forwarded-For` only when the TCP peer is in `DJ_TRUSTED_PROXIES` (default
`127.0.0.1,::1`: Caddy on the same host). Behind a proxy that is not listed, every client
shares the proxy's IP; list it. The per-username lock means someone guessing can lock the
browser login for 15 minutes; device tokens and existing sessions keep working.

### Upload caps

`POST /api/tracks` 300 MB (`DJ_MAX_TRACK_BYTES`), `/api/samples` 26 MB (`DJ_MAX_SAMPLE_BYTES`),
`/api/recordings` 1 GB (`DJ_MAX_RECORDING_BYTES`), anything else 64 MB (`DJ_MAX_BODY_BYTES`).
Values in bytes. Over the cap -> 413, checked on `Content-Length` and on the bytes as they
arrive. Keep `deploy/Caddyfile`'s `request_body` sizes in step.

### Logs

A filter on the uvicorn / fastapi / root loggers redacts bearer tokens, device tokens,
`Cookie` / `Authorization` headers, the session cookie value, passwords, and secret query
parameters. start.sh runs uvicorn without the access log (`LOG_LEVEL=debug` brings it back,
redacted). Caddy redacts the same headers in its own log.

### Second factor (later)

`user.totp` is stored (null today) and every password login calls
`TokenAuthMiddleware._second_factor_ok(user)`. A TOTP check goes there, plus a field on the
login form and `POST /api/auth/token`. Nothing else changes.

## Google sign-in

Browser: OIDC authorization-code flow with PKCE, all server side.

1. `GET /auth/google/start` makes a random `state`, `nonce` and PKCE verifier, keeps them in
   server memory (10 minutes, each handed out once), sets a short pre-auth cookie `dj_oauth`
   (the state, HMAC-signed; `HttpOnly`, `SameSite=Lax` because Google's redirect back is a
   cross-site navigation, `Secure`, 10 minutes, path `/auth/google/`) and redirects to Google
   with `scope=openid email`, `prompt=select_account`, `code_challenge_method=S256`.
2. `GET /auth/google/callback` checks the state against the cookie and the server copy
   (a replay finds nothing), exchanges the code at Google's token endpoint with the client
   secret and verifier, and checks the ID token with PyJWT: RS256 signature against Google's
   JWKS (cached for Google's max-age, refetched when a token names a new key), `iss` is
   Google, `aud` is `DJ_GOOGLE_CLIENT_ID`, `exp` / `iat` with 60 s skew, `nonce`,
   `email_verified`, the address in `DJ_ALLOWED_EMAILS`, and `hd` if `DJ_GOOGLE_HD` is set.
3. The address is **pinned to the Google account (`sub`)** that first signed in with it. A
   different Google account presenting the same address (a recycled or re-registered
   address) is refused until the owner runs `forget-google`.
4. Success: the usual session cookie, set on a small page that forwards to the console (a
   same-site step, so the `SameSite=Strict` cookie is sent). Failure: a plain "not
   authorised" page (403); the reason goes to the log, never a code or token, and counts
   toward the per-IP limiter.

Removing an address from `DJ_ALLOWED_EMAILS` (and restarting) ends its browser sessions and
stops the device tokens it issued on the console. On the stem server, revoke them instead
(DEVICES panel, `revoke-token`, or `forget-google`).

**Phone**: the app signs in with Google itself and posts the ID token to
`POST /api/auth/google/token` `{"id_token", "device_name"}`. Same checks, except `aud` must be
one of `DJ_GOOGLE_MOBILE_CLIENT_IDS` or the web client id (Android's `serverClientId` makes
Google issue the token for the web client) and there is no nonce. Answer:
`{"id", "name", "created", "token"}`, the token shown once. Failures: 401
`{"detail": "not authorised"}`, rate-limited per IP. The same token works on the stem server
(`/stems/*` behind Caddy), which reads the same account store.

**Password login** is off once Google is configured: no password form on `/login`, and
`POST /login` / `POST /api/auth/token` answer 403. `DJ_PASSWORD_LOGIN=1` turns it back on.
Break-glass either way:
* `python -m app.ui.services.auth new-token NAME` over SSH, then use that Bearer token;
* password login from a loopback client, e.g. `ssh -L 8000:127.0.0.1:8000 ct116` and
  `http://localhost:8000/login` (behind Caddy the client IP is the real remote address, so
  this never opens up through the proxy).

### Device sign-in (phone, no native Google Sign-In)

RFC 8628 style, for when the phone's own Google Sign-In fails. The phone needs no Google
setup at all; the owner approves in any browser that can reach the console.

1. Phone: `POST /api/auth/device/start` `{"device_name"}` -> `{"device_code", "user_code",
   "verification_uri", "verification_uri_complete", "expires_in": 600, "interval": 3}`.
   `device_code` is 32 random bytes and stays on the phone. `user_code` is 8 letters from
   `BCDFGHJKMNPQRSTVWXZ` shown as `XXXX-XXXX`. `verification_uri` is `DJ_PUBLIC_URL/device`.
2. The phone opens `verification_uri_complete` in the browser and shows the code big.
   `/device` needs the normal browser login; signed out, it goes to `/login?next=/device?code=...`
   and Google (or the password form) brings the owner back. `next` accepts the device page
   only, so it is not an open redirect.
3. The page shows the device name, the code, the requesting IP, and APPROVE / DENY. The
   POST must be same-origin and carry a form token (HMAC of the session cookie and the code).
   A bearer token can't use the page.
4. Phone: `POST /api/auth/device/poll` `{"device_code"}` every `interval` seconds:
   428 `authorization_pending`, 429 `slow_down` (the interval grows by 5 s), 403
   `access_denied`, 410 `expired_token`, 400 `invalid_grant` (unknown or already used), or
   200 `{"token", "token_id", "name", "created"}` exactly once. The device token is minted at
   that moment, bound to the approving owner (a Google approver's `sub`, so `forget-google`
   revokes it too), and listed under DEVICES. Same token on the console and the stem server.

Limits: per IP 10 starts per 10 minutes and 5 undecided flows; 200 flows in flight in total.
A wrong device code or user code counts toward the per-IP failure limiter (10 -> 429 for 15
minutes). Codes are kept in memory as sha256 only, compared with `compare_digest`, single
use, and gone 10 minutes after expiry; a restart ends every flow in flight. Neither code nor
the token is logged (the redaction filter also strips `device_code` / `user_code` fields).

### Settings (`.env`; secrets never in the repo)

| variable | |
|---|---|
| `DJ_GOOGLE_CLIENT_ID` | the Web client's id. Setting it turns Google sign-in on. |
| `DJ_GOOGLE_CLIENT_SECRET` | the Web client's secret (secret: `.env` / `/etc` env file only, chmod 600) |
| `DJ_PUBLIC_URL` | `https://<name>.duckdns.org[:port]` (keep a non-default port, e.g. when the router forwards 8448 to Caddy's 443), no path. Also counts as same-origin for the CSRF check. The redirect URI is this + `/auth/google/callback`, exactly. |
| `DJ_ALLOWED_EMAILS` | comma-separated addresses allowed in; exact address, case-insensitive. Required. |
| `DJ_GOOGLE_MOBILE_CLIENT_IDS` | comma-separated Android / iOS client ids (optional) |
| `DJ_GOOGLE_HD` | optional Google Workspace domain lock |
| `DJ_PASSWORD_LOGIN` | `1` keeps password login on with Google configured |

### Google Cloud Console steps (owner)

1. <https://console.cloud.google.com/> -> create (or pick) a project.
2. **APIs & Services -> OAuth consent screen** (Google Auth Platform -> Branding /
   Audience): User type **External**; app name, support email; publishing status
   **Testing**. Under **Test users**, add your address. In Testing mode only listed test
   users can sign in at all, which is an extra lock on top of `DJ_ALLOWED_EMAILS`.
3. **Data access / Scopes**: `openid` and `.../auth/userinfo.email` only. These are
   non-sensitive, so no verification review is needed.
4. **Credentials -> Create credentials -> OAuth client ID -> Web application**.
   Authorised redirect URI: `https://<name>.duckdns.org[:port]/auth/google/callback` (exactly,
   the same as `DJ_PUBLIC_URL` + `/auth/google/callback`). No JavaScript origins are
   needed. Copy the client id and secret into `.env`.
5. **Android client** (for the phone app): OAuth client ID -> Android; package name of the
   app and the SHA-1 of the signing key (`keytool -list -v -keystore <keystore>`, or
   `./gradlew signingReport`; debug and release keys differ, add one client per key).
   Its id goes into `DJ_GOOGLE_MOBILE_CLIENT_IDS`. The app passes the **Web** client id as
   `serverClientId`, so Android tokens carry the web id as `aud`.
6. **iOS client**: OAuth client ID -> iOS; the app's bundle id. Its id goes into
   `DJ_GOOGLE_MOBILE_CLIENT_IDS` and into the app (see the mobile repo's notes).
7. Set `DJ_ALLOWED_EMAILS` to your address and restart.

Testing mode limits (refresh tokens expiring after 7 days, the 100-test-user cap) don't
matter here: the server uses Google's ID token once at sign-in and keeps its own session /
device token after that; it never stores a Google access or refresh token.

## Reusing it in the stem server

Today the stem server keeps its own small auth (`STEM_SERVER_TOKEN` plus the console's
limiter) and **also accepts the console's device tokens** by reading the same store
(`DJ_AUTH_FILE`, else the same default as above; set `DJ_AUTH_FILE` in both env files).
Both services must run as the same user. A full switch to this module would look like:

```python
from app.ui.services import auth
settings = auth.AuthSettings.from_env(cookie_name="stem_session", title="Stem server")  # same owner store
auth.check_bind(settings)
auth.install(app, settings, body_limits={("POST", "/split"): 200 * 1024 * 1024})
```

The same `auth.json` means one password and one device list for both services. Its
`STEM_SERVER_TOKEN` can then become a device token (`new-token stem-client`).

## Internet exposure checklist

1. TLS terminator in front: Caddy in CT116 (`deploy/README-caddy.md`). Cloudflare Tunnel or
   Tailscale Funnel also work; then set `DJ_TRUSTED_PROXIES` to the address the tunnel
   daemon connects from (127.0.0.1 when it runs in the same container).
2. Google sign-in configured (`DJ_GOOGLE_*`, `DJ_PUBLIC_URL`, `DJ_ALLOWED_EMAILS`) and tried
   once; or an owner password (`set-password`). `DJ_SESSION_SECRET` in `.env`; `.env` is
   chmod 600.
3. Console bound to `127.0.0.1` with `--no-proxy-headers` (start.sh); stem server
   `STEM_SERVER_HOST=127.0.0.1`.
4. `DJ_TRUSTED_PROXIES` = the proxy's address only; `DJ_ALLOWED_ORIGINS` empty unless a
   different-origin web client really needs it.
5. No plain-HTTP port forward to 8000 or the stem port; the router forwards 80/443 to the
   Proxmox host, which DNATs them on `vmbr0` to CT116 and nothing else.
6. CT116 firewall: inbound from the proxy path (80/443) only.
7. Check from outside: `/health` 200, `/api/recipes` 401, `/docs` 404, `http://` redirects
   to `https://`, the login sets a `Secure` cookie.
8. Device tokens: one per device, named; revoke lost ones (DEVICES panel or `revoke-token`).
