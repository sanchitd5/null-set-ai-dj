# Set lease: one set at a time

Owner rule: the server supports one running set at a time. Its set state (session log, LLM gate,
live ear, set memory, booking / preplan state, stem priority, prerender list) assumes one set.
A client that starts a set takes the server's **set lease**; anyone else is told who holds it.

Code: `app/ui/services/set_lease.py` (store, gate, routes), `app/ui/static/set-lease.js` (console and
phone web Lite view), phone app `lib/server/set_lease.dart` (null-set-dj-mobile).

## Routes

All behind the owner auth (cookie or device token, `AUTH.md`), so every caller is the owner.

| route | answer |
|---|---|
| `POST /api/set/lease` `{client: console\|phone, device_name}` | 200 `{lease: {id, client, device_name, holder: {kind, name}, started, deadline, ttl_s, beat_s}}`, or 409 `{detail, code: "set_lease", holder: {client, device_name, kind, name, since, deadline}, since, same_holder}` |
| `POST /api/set/lease/{id}/beat` | 200 `{lease}`; 409 `{detail: "taken over" \| "lease ended", holder?, since?}` |
| `DELETE /api/set/lease/{id}`, `POST /api/set/lease/{id}/release` | 200 `{ok, released}` (the POST form is for `sendBeacon`) |
| `POST /api/set/lease/takeover` `{client, device_name}` | 200 `{lease, previous}`; the old holder's next beat gets 409 "taken over" |
| `GET /api/set/lease` | `{active, lease}` (public view, never the id) |

* Heartbeat every `beat_s` (15 s). With no beat for `ttl_s` (60 s) the lease lapses (a crashed
  client frees the set by itself). A beat on a lapsed lease that nobody else took revives it, so a
  network blip does not end a set.
* `holder` is the device token (`device:<token id>`, its name) or the browser session (a hash of the
  session cookie); auth off on loopback = `local`. The auth middleware puts it in `scope["dj_auth"]`;
  the token itself is never stored. `same_holder` in a 409 means "this same device / browser session"
  (a stale lease of its own, another tab).
* The id is the capability. It is returned only to the client that got the lease.
* State: `<cache>/set_lease.json` (atomic write), so a restart keeps the holder. A corrupt file = no lease.
* **Closed** (idle backstop): a lease with no beat for `SESSION_PAUSE_CLOSE_MIN` minutes (dj-config, 15) is removed
  by a sweep that runs every 60 s (`LeaseStore.sweep`). The set counts as over: `knowledge_publish.on_set_end()` runs
  once, the session log gets action `closed`. A later beat from that lease answers 409 `lease ended` with no `holder`
  (unless another set holds the lease now). The phone treats that as "set closed" (idle, a new lease on the next play),
  not as "server busy".
* **Force close** (admin shell, `ADMIN.md`): `LeaseStore.force_close` ends the live set like the sweep (publish, history entry);
  the holder's next beat answers 409 `closed by admin`.
* **History**: the last `MAX_HISTORY` (50) closed sets (`id_hash` = 8 hex of the lease id's sha256, client, device, holder,
  started, ended, reason `released` / `taken_over` / `lapsed` / `paused_timeout` / `admin`) live in `set_lease.json`
  and feed the admin sessions page. The lease id itself is never stored there.
* Session log: kind `set_lease`, action `grant` / `release` / `takeover` / `lapsed` / `revived` / `closed` / `force_close`.

## Gate

`set_lease.Gate` (pure ASGI, installed inside the auth middleware, so a stranger gets 401 and never
learns the holder). While a lease is active, these routes need `X-Set-Lease: <id>`, else 409 with
the holder:

`POST /api/autopilot/suggest|plan|vet`, `POST /api/transition/preplan`, `POST /api/merge/audition`,
`GET /api/learned/pick`, `POST /api/riff/plan`, `POST /api/riff/{key}/balance`, `POST /api/live/ear`,
`POST /api/session/event|events|steps`, `POST /api/prerender` (`set_lease.SET_SCOPED`).

Everything else stays open with no lease: library, analysis, stems for the cache, downloads, sync,
macros, previews, set logs, recordings. The phone must still prefetch and browse during someone
else's set. One downgrade: a stem split asked (track upload, `?separate=1`) by a request without the
active lease is queued at **backfill** priority, never ahead of the running set's decks
(`set_lease.may_lead()` in `server._queue_stems`).

With no active lease every route is open (plain local use, the sim, tests).

## Clients

* **Console** (`set-lease.js`, loaded first in `index.html`): START SET / START FROM CURRENT take the
  lease (`autopilot.js` `leased()`); a 409 shows "A set is running on <device> since <time>" with
  TAKE OVER / CANCEL. While held, a `window.fetch` wrapper adds `X-Set-Lease` to every same-origin
  `/api/` request, a beat runs every 15 s, STOP SET releases, and `pagehide` sends a beacon to the
  release route. A 409 on a beat stops this console's set ("Set taken over by <device>"). A server
  that is down or has no lease route lets the set start (nothing to share). The phone web Lite view
  is the same page and runs the same code.
* **Phone app**: the set is AI Mix playback. Start takes the lease, 409 asks TAKE OVER / CANCEL
  (CANCEL plays on in offline mode), beats every 15 s, releases on AI Mix off and app termination. A set
  paused for `SESSION_PAUSE_CLOSE_MIN` minutes (AI Mix on, a song loaded, signed in) is closed by the phone: set log
  queued and flushed, lease released; the next play starts a new set and lease.
  Losing the lease mid-set keeps the music playing on dj_core rules, shows "server busy - offline
  mode", and makes no set-scoped call until the next start (mobile `docs/THIN_CLIENT.md`).

## Tests

`app/tests/py/test_set_lease.py` (grant, conflict, expiry, revive, takeover, restart persistence,
route gating, read-only allowed, 401 before 409, browser holder, backfill downgrade),
`app/tests/js/set_lease_check.js`, mobile `test/server/set_lease_test.dart`.
