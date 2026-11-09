# Admin shell

`/admin` is an owner-only page on the existing Google sign-in, gated by the allowlist `DJ_ADMIN_EMAILS`
(comma separated; set by the owner in the server `.env`, then restart). Code: `app/ui/services/admin.py`.

* `DJ_ADMIN_EMAILS` empty or unset: `/admin` and `/api/admin/*` answer 404 (feature off). An admin address that is not
  in `DJ_ALLOWED_EMAILS` can never sign in; startup prints a warning.
* Allowed: a Google browser session whose email is in the list (re-read on every request). Everything else is 403:
  other Google users, password sessions, any device bearer token (also Google-issued). Unauthenticated requests get the
  normal auth answer (303 to /login for pages, 401 for the API). Unsafe requests need the normal Origin check.
* Pages (no script, CSP `default-src 'none'`, 5 s meta refresh): `/admin`, `/admin/sessions` (live set, last 20 closed
  sets, Force close). More pages append to `admin.NAV`.
* API: `GET /api/admin/me|sessions|audit`, `POST /api/admin/sessions/close` (409 `no_live_set` when nothing is live).
  `GET /api/admin/quarantine` is behind the same gate.
* Audit: every admin write (actor email, action, target, time) goes to table `admin_audit` in the private `user.db`
  (newest 5000 kept, never exported) and the session log (kind `admin`).
* `scope["dj_auth"]` now carries `sub` (Google subject for a Google browser session or device token, else None).
