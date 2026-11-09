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

## Jobs (`/admin/jobs`)

`app/ui/services/job_registry.py` is a generic runner: a `JobSpec` is `periodic` (every `interval_s`), `idle_worker`
(runs whenever idle, loops until `ctx.should_stop()`), `manual` (run now only) or `external` (another module's own
thread; status plus optional pause/resume). Built-in jobs are registered by `jobs_builtin.py` at startup:
`quarantine_cleanup` (cron `15 4 * * *`), `sync_compact` (cron `30 3 * * *`), `lang_id` (pause/resume + status), `knowledge_publish`
(observe only) and `graph_rebuild` (manual, only when the private builder is present; never automatic). Private jobs
arrive through `atlas_bridge.register_jobs`: `graph_expand` (cron `0 * * * *`, idle-only, on by default; `DJ_GRAPH_EXPAND=0` is the kill switch) downloads, analyses and pairs the best YouTube-related songs the library lacks; its candidates
queued / done / failed and the measured-pair count are in the job's last result. Downloaded songs join the library.

* Idle = no active set lease for `DJ_IDLE_GRACE_S` (30 s). A lease change calls a `set_lease` listener, so a running
  `idle_only` job sees `ctx.should_stop()` true at once. Stopping is cooperative; a run that ignores it for
  `DJ_JOB_CANCEL_GRACE_S` (30 s) shows `stuck`.
* Controls: `POST /api/admin/jobs/{name}/pause|resume|cancel|run` (409 with a `code`: `running`, `paused`, `not_idle`,
  `not_running`, `not_supported`, `disabled`), `PUT .../settings` (typed keys, 422 on a bad key or value),
  `GET /api/admin/jobs`, `/api/admin/jobs/{name}`, `.../log?tail=n`, `/api/admin/idle`. Every write is audited.
* State persists in `user.db` (`job_state`, `job_run`, newest 200 runs per job): a pause survives a restart; a job's
  checkpoint (`ctx.checkpoint`, `ctx.save_checkpoint()`) is handed to its next run.
* Cron (`app/ui/services/cron.py`, stdlib only): a job of any non-external kind may have a 5-field `schedule`
  (`minute hour day-of-month month day-of-week`; `*`, lists, ranges, `*/n`, `a-b/n`, jan..dec, sun..sat, `@hourly`
  `@daily` ...). Read in the SERVER'S LOCAL TIME (naive wall clock, so DST never shifts a fire; a time skipped by
  spring-forward fires when the clock jumps, a repeated one fires once). Day-of-month and day-of-week are OR-ed when
  both are restricted. An invalid expression is rejected at register time and by the API (422 `bad_schedule`).
  Per job: `enabled`, `max_runtime_s` (0 = none; at the limit the job is asked to stop, status `timeout`, so it ends at its
  next safe point), `idle_only` (a fire during a set waits for idle until the next fire or one hour, then is
  skipped and logged `skipped: busy`), overlap = skip (a fire due while still running is logged `skipped: still
  running`), missed run = once at startup (never a backlog). `PUT /api/admin/jobs/{name}/schedule`
  `{schedule, enabled, max_runtime_s, idle_only}` (audited as `job_schedule`); the page has a form for it.
  `last_fire` / `next_fire` / the newest 20 runs are in each job view (`schedule`, `history`).
* `DJ_JOBS=0` turns the scheduler off (built-in external jobs are still listed), `DJ_JOBS_TICK_S` (2 s) is the tick.

## Graph view (/admin/graph)

Search a song by name or YouTube id; it is drawn with its top-k neighbours per edge kind (cytoscape, vendored in
`app/ui/static/vendor/` with its licence, no CDN). Tap a node to expand one hop and open its details (artist, title,
bpm, key, energy, language, scene, source, library, edges grouped by kind); long-press also opens the details.
Filters: edge kinds, minimum weight, language. Read-only JSON: `GET /api/admin/graph/stats`, `/node?q=|id=`,
`/neighbours?id=&k=&kinds=&min_weight=&lang=`. "Trending only" (`/node?trending=1[&q=]`) lists the songs on a region chart now; trending
songs carry a `trend` badge (best chart place and per-region places) and the song detail shows the daily chart history.
The `graph_trending` job (private, daily, idle only; regions from its `regions` setting or the library's languages) fills
it, and the idle expansion takes charting songs as candidates. Data is read from graph.db through `atlas_bridge.graph_view` (the
`graph_store` views in the private dj-atlas); without a graph.db the page says so and the API answers empty/404.
The admin pages use one responsive stylesheet: readable at 360 px, 44 px tap targets, tables turn into cards.
