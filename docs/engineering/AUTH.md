# Console API sign-in

Code: `app/ui/services/auth.py` (one module, stdlib + starlette only, no imports from this
app, so the stem server can reuse it). Tests: `app/tests/py/test_auth.py`. TLS front end:
`deploy/Caddyfile` and `deploy/README-caddy.md`.

## The rule

* **Auth is on once an owner password exists**, for every request, localhost included
  (behind a reverse proxy every request looks local, so loopback is never exempt).
* No password + loopback bind (`127.0.0.1`, `::1`, a unix socket) + no `DJ_PUBLIC_URL` + no
  forwarded headers: auth off, with a warning in the log and at `./start.sh`. Local
  development only.
* **Fail closed behind a proxy:** with no password, a request carrying `X-Forwarded-For`,
  `-Proto`, `-Host`, `X-Real-IP` or `Forwarded` (Caddy always adds them), or any request
  while `DJ_PUBLIC_URL` is set, answers 503 `{"detail": "auth not configured"}`. Only
  `GET /health` still answers. Behind Caddy the console binds 127.0.0.1, so the bind alone
  would look local.
* No password + any other bind (`--host 0.0.0.0`, a LAN address, `--fd`): **the server
  refuses to start** and says to run `set-password`. A request that still arrives on a
  non-loopback address with no password gets 503.

## What is protected

Everything: `/api/*` (including `/api/audio/*`, the `<audio src>` reads), the static
console (`/`, `/index.html`, every JS / CSS file), and any websocket (none exist today; the
middleware closes unauthenticated websocket handshakes with 1008 if one is added). There is
no SSE endpoint. `/docs`, `/redoc`, `/openapi.json` answer 404 while auth is on.

Public: `GET /health` (`{"ok": true}`, nothing else), `GET/POST /login`, `/logout`, and
`POST /api/auth/token` (it needs the password itself).

401 / 403 bodies are `{"detail": "unauthorized"}` and nothing more; a browser asking for a
page gets a redirect to `/login` instead.

## The owner account

One account. Password hashed with `hashlib.scrypt` (n=2^14, r=8, p=1, 16-byte salt), stored
with the device tokens in `data/cache/auth/auth.json` (gitignored, chmod 600, written
atomically; override with `DJ_AUTH_FILE`; follows `AIDJ_CACHE_DIR`). Not in `.env`.

```bash
python -m app.ui.services.auth set-password            # prompts twice, min 12 chars; --user NAME to rename
python -m app.ui.services.auth new-token sim           # prints the token ONCE (stdout only)
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
| Browser (the console) | `/login`: username + password -> session cookie. The console's JS is same-origin and unchanged; `<audio>`, `fetch`, workers all send the cookie. **DEVICES** (bottom-left) lists device tokens with a revoke button, and SIGN OUT. |
| Phone | Once: `POST /api/auth/token` with JSON `{"username", "password", "device_name"}` -> `{"id", "name", "created", "token"}`. Keep `token` in secure storage, send `Authorization: Bearer <token>` on every request. |
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

## Reusing it in the stem server

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
2. Owner password set (`set-password`); `DJ_SESSION_SECRET` in `.env`; `.env` is chmod 600.
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
