"""Admin shell: /admin, owner only (docs/engineering/ADMIN.md).

Access is the existing Google browser sign-in plus an allowlist, DJ_ADMIN_EMAILS (comma separated, the
same addresses as DJ_ALLOWED_EMAILS). Empty or unset: every admin route answers 404, so the feature
leaves no trace. A device bearer token never reaches an admin route (403), nor does a password session.

    GET  /admin, /admin/sessions        server-rendered pages (no script; a 5 s meta refresh)
    GET  /api/admin/me                  {email}
    GET  /api/admin/sessions            {live, closed}: the live set (no lease id) and the last 20 closed sets
    POST /api/admin/sessions/close      end the live set (the holder's next beat answers 409 "closed by admin")
    GET  /api/admin/audit               the newest 100 admin writes
    GET  /admin/jobs                    background jobs (job_registry.py): state, progress, controls
    GET  /api/admin/jobs, /api/admin/jobs/{name}, /api/admin/jobs/{name}/log?tail=n, /api/admin/idle
    POST /api/admin/jobs/{name}/pause|resume|cancel|run     (409 + code when refused)
    PUT  /api/admin/jobs/{name}/schedule                    cron schedule, enabled, max_runtime_s, idle_only (422 bad_schedule)
    PUT  /api/admin/jobs/{name}/settings                    typed, validated (422 on a bad key or value)

Every admin write is stored (actor, action, target, time) in the private user.db and the session log.
"""
from __future__ import annotations

import html
import os
import threading
import time
from pathlib import Path
from typing import FrozenSet, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.ui.services import set_lease

MAX_AUDIT = 5000
NAV = [("Sessions", "/admin/sessions"), ("Phone trace", "/admin/sessions/trace"), ("Jobs", "/admin/jobs"), ("Graph", "/admin/graph"), ("Cache audit", "/admin/cache-audit")]   # later admin pages append here
_PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
STEPS = (
    """CREATE TABLE admin_audit (id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL,
        action TEXT NOT NULL, target TEXT, detail TEXT)""",
)

_guard = None


def configure(guard) -> None:
    """Called once from server.py with the auth Guard (admin.py never imports server). Never raises."""
    global _guard
    _guard = guard
    try:
        allowed = guard.settings.google.allowed_emails
        for e in sorted(admin_emails() - set(allowed)):
            print(f"[admin] DJ_ADMIN_EMAILS has an address that is not in DJ_ALLOWED_EMAILS: {e} (it can never sign in)",
                  flush=True)
    except Exception:                                # startup must never fail on this check
        pass


def admin_emails(environ=None) -> FrozenSet[str]:
    env = os.environ if environ is None else environ
    raw = (env.get("DJ_ADMIN_EMAILS") or "").strip()
    return frozenset(e.strip().lower() for e in raw.split(",") if "@" in e)


def require_admin(request: Request) -> str:
    """FastAPI dependency: the admin's email. 404 when the feature is off, 403 for anyone else."""
    admins = admin_emails()
    if not admins:
        raise HTTPException(404, "Not Found")
    auth = request.scope.get("dj_auth") or {}
    sub = auth.get("sub")
    if auth.get("kind") != "browser" or not sub or _guard is None:
        raise HTTPException(403, "admin only")
    ident = _guard.settings.store.google_identity(sub)
    email = str((ident or {}).get("email") or "").lower()
    if email not in admins:
        raise HTTPException(403, "admin only")
    return email


class AuditLog:
    """Admin writes in user.db (private, never exported): who did what to what, when."""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir
        self._lock = threading.Lock()

    def _conn(self):
        from app.music_brain import db
        conn = db.connect(db.db_path(self.cache_dir, db.USER_DB))
        db.ensure(conn, "admin_audit", STEPS)
        return conn

    def add(self, actor: str, action: str, target: str = "", detail: str = "") -> None:
        from app.music_brain import db
        with self._lock:
            conn = self._conn()
            with db.tx(conn):
                conn.execute("INSERT INTO admin_audit (at, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
                             (time.time(), actor, action, target, detail))
                conn.execute("DELETE FROM admin_audit WHERE id <= (SELECT MAX(id) FROM admin_audit) - ?", (MAX_AUDIT,))
        try:
            from app.ui.services import session_log
            session_log.log("admin", actor=actor, action=action, target=target)
        except Exception:                            # the log never blocks an admin action
            pass

    def recent(self, n: int = 100) -> list:
        with self._lock:
            rows = self._conn().execute(
                "SELECT at, actor, action, target, detail FROM admin_audit ORDER BY id DESC LIMIT ?", (int(n),)).fetchall()
        return [{"at": r[0], "actor": r[1], "action": r[2], "target": r[3], "detail": r[4]} for r in rows]


_audit: Optional[AuditLog] = None


def audit() -> AuditLog:
    global _audit
    if _audit is None:
        from app.music_brain.config import CACHE_DIR
        _audit = AuditLog(Path(CACHE_DIR))
    return _audit


def use_audit(a: Optional[AuditLog]) -> None:
    """Swap the process-wide audit log (tests; None = default location on next use)."""
    global _audit
    _audit = a


# ---- pages -------------------------------------------------------------------------------------

_CSS = ("*{box-sizing:border-box}body{font:15px system-ui,sans-serif;margin:0;background:#111;color:#eee}"
        "nav{display:flex;flex-wrap:wrap;align-items:center;gap:4px;padding:6px 12px;background:#1b1b1b}"
        "nav a{color:#9cf;text-decoration:none;display:inline-flex;align-items:center;min-height:44px;padding:0 10px}"
        "nav .muted{margin-left:auto;overflow-wrap:anywhere}"
        "main{padding:12px 16px}h1{font-size:22px;margin:.3em 0}h2{font-size:18px}"
        "table{border-collapse:collapse;width:100%}th,td{padding:6px 10px;border-bottom:1px solid #333;text-align:left;vertical-align:top}"
        "button,input,select{font:inherit;min-height:44px;padding:6px 14px}"
        "td a{display:inline-flex;align-items:center;min-height:44px;padding:0 8px;color:#9cf}"
        ".muted{color:#999}"
        "@media (max-width:640px){main{padding:8px 10px}"
        "table,tbody,tr,td{display:block;width:100%}tr:has(th){display:none}"
        "table:not(.kv) tr{margin:0 0 10px;border:1px solid #333;border-radius:8px;padding:4px 8px;background:#181818}"
        "table:not(.kv) td{border:0;padding:4px 0}"
        "td[data-label]::before{content:attr(data-label);display:block;font-size:12px;color:#999}"
        "table.kv tr{display:flex;gap:10px}table.kv th,table.kv td{display:block}}")


def _t(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts))) if ts else ""


def _dur(sec) -> str:
    sec = int(sec or 0)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _page(title: str, body: str, email: str, refresh: int = 5, head: str = "", csp: str = _PAGE_CSP) -> HTMLResponse:
    e = html.escape
    meta_refresh = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    nav = "".join(f'<a href="{e(u)}">{e(n)}</a>' for n, u in NAV)
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{e(title)}</title>'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'{meta_refresh}<style>{_CSS}</style>{head}</head><body>'
           f'<nav>{nav}<span class="muted">{e(email)}</span></nav><main><h1>{e(title)}</h1>{body}</main></body></html>')
    return HTMLResponse(doc, headers={"content-security-policy": csp, "cache-control": "no-store"})


def _sessions_body() -> str:
    e = html.escape
    st = set_lease.store()
    live = st.status_admin()
    if live["active"]:
        v = live["lease"]
        who = " ".join(x for x in (v.get("kind"), v.get("name")) if x)
        body = ("<h2>Live set</h2><table class=\"kv\">"
                f"<tr><th>Client</th><td>{e(str(v.get('client')))}</td></tr>"
                f"<tr><th>Device</th><td>{e(str(v.get('device_name')))}</td></tr>"
                f"<tr><th>Holder</th><td>{e(who)}</td></tr>"
                f"<tr><th>Started</th><td>{e(_t(v.get('since')))}</td></tr>"
                f"<tr><th>Last beat</th><td>{int(time.time() - v['last_beat'])} s ago</td></tr>"
                f"<tr><th>Lapses in</th><td>{max(0, int(v['lapses_in']))} s</td></tr></table>"
                '<form method="post" action="/api/admin/sessions/close"><button type="submit">Force close</button></form>')
    else:
        body = '<h2>Live set</h2><p class="muted">no live set</p>'
    rows = "".join(
        f"<tr><td data-label=\"Client\">{e(str(r.get('client')))}</td><td data-label=\"Device\">{e(str(r.get('device_name')))}</td>"
        f"<td data-label=\"Started\">{e(_t(r.get('started')))}</td><td data-label=\"Ended\">{e(_t(r.get('ended')))}</td>"
        f"<td data-label=\"Duration\">{_dur(r.get('duration_s'))}</td><td data-label=\"Reason\">{e(str(r.get('reason')))}</td></tr>"
        for r in st.history_view(20))
    body += ("<h2>Closed sets</h2><table><tr><th>Client</th><th>Device</th><th>Started</th><th>Ended</th>"
             f"<th>Duration</th><th>Reason</th></tr>{rows}</table>")
    return body


# ---- routes ------------------------------------------------------------------------------------

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
@router.get("/admin/sessions", response_class=HTMLResponse)
def admin_sessions(email: str = Depends(require_admin)):
    return _page("Sessions", _sessions_body(), email)


def _trace_events(session: Optional[str], limit: int = 2000) -> list:
    from app.ui.services import session_log

    evs = session_log.read(session, max(1, min(limit, 5000)))
    return [e for e in evs if e.get("kind") in ("pick_trace", "phone_error")]


@router.get("/admin/sessions/trace", response_class=HTMLResponse)
def admin_trace_page(session: Optional[str] = None, email: str = Depends(require_admin)):
    """The phone's pick / booking trace and caught errors of one server session, oldest first."""
    e = html.escape
    try:
        evs = _trace_events(session)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad session id")
    skip = ("t", "at", "kind", "ev", "source", "set", "stack")
    rows = ""
    for ev in evs:
        detail = e(" ".join(f"{k}={v}" for k, v in ev.items() if k not in skip))
        if ev.get("stack"):
            detail += "<br><small class=\"muted\">" + e(" | ".join(str(x) for x in ev["stack"])) + "</small>"
        rows += (f"<tr><td data-label=\"Time\">{e(str(ev.get('at', '')))}</td>"
                 f"<td data-label=\"Event\">{e(str(ev.get('ev') or ev.get('kind')))}</td>"
                 f"<td data-label=\"Set\">{e(str(ev.get('set', '')))}</td><td data-label=\"Detail\">{detail}</td></tr>")
    body = ("<p class=\"muted\">pick_trace and phone_error events of this server session"
            + (f" ({e(session)})" if session else " (current)") + "; add ?session=&lt;id&gt; for another.</p>"
            f"<table><tr><th>Time</th><th>Event</th><th>Set</th><th>Detail</th></tr>{rows}</table>")
    return _page("Phone pick trace", body, email, refresh=10)


@router.get("/api/admin/sessions/trace")
def admin_trace_api(session: Optional[str] = None, email: str = Depends(require_admin)) -> dict:
    try:
        return {"events": _trace_events(session)}
    except ValueError:
        raise HTTPException(status_code=400, detail="bad session id")


@router.get("/api/admin/me")
def admin_me(email: str = Depends(require_admin)) -> dict:
    return {"email": email}


@router.get("/api/admin/sessions")
def admin_sessions_api(email: str = Depends(require_admin)) -> dict:
    st = set_lease.store()
    return {"live": st.status_admin(), "closed": st.history_view(20)}


@router.post("/api/admin/sessions/close")
def admin_close(request: Request, email: str = Depends(require_admin)):
    view = set_lease.force_close_once("admin")
    if view is None:
        return JSONResponse({"detail": "no live set", "code": "no_live_set"}, status_code=409)
    audit().add(email, "force_close", f"{view.get('client')}:{view.get('device_name')}")
    # a browser form post goes back to the page; an API caller gets the closed set
    if "text/html" in request.headers.get("accept", ""):
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/admin/sessions", status_code=303)
    return {"closed": view}


@router.get("/api/admin/audit")
def admin_audit(email: str = Depends(require_admin)) -> dict:
    return {"audit": audit().recent(100)}


# ---- jobs (job_registry.py) ---------------------------------------------------------------------

def _reg():
    from app.ui.services import job_registry
    return job_registry.registry


def _job_err(exc) -> JSONResponse:
    return JSONResponse({"detail": exc.detail, "code": exc.code}, status_code=exc.status)


def _fmt_state(v) -> str:
    return "" if v is None else str(v)


def _rel(ts, now: float) -> str:
    """'in 5 min' / '3 h ago' for an epoch time."""
    d = float(ts) - now
    n, fut = abs(d), d >= 0
    txt = f"{int(n)} s" if n < 90 else f"{int(n // 60)} min" if n < 5400 else f"{n / 3600:.1f} h" if n < 172800 else f"{int(n // 86400)} d"
    return f"in {txt}" if fut else f"{txt} ago"


def _hist_row(h: dict) -> str:
    e = html.escape
    dur = f" {_dur((h['ended'] or h['started']) - h['started'])}" if h.get("started") and h.get("status") != "skipped" else ""
    return f"<li>{e(_t(h.get('started')))} {e(str(h.get('status') or ''))}{e(dur)} <span class=\"muted\">{e(str(h.get('summary') or '')[:120])}</span></li>"


def _sched_form(v: dict) -> str:
    e = html.escape
    s = v["schedule"]
    if not s["editable"]:
        return ""
    chk = lambda b: " checked" if b else ""   # noqa: E731
    return (f'<form class="sched" data-job="{e(v["name"])}"><label>Cron (min hour day month weekday, server local time)'
            f'<input type="text" name="schedule" value="{e(s["expr"] or "")}" placeholder="none" autocomplete="off" spellcheck="false"></label>'
            f'<label>Max runtime, minutes (0 = no limit)<input type="number" name="max_min" min="0" max="10080" step="1" '
            f'value="{int((s["max_runtime_s"] or 0) // 60)}"></label>'
            f'<label class="chk"><input type="checkbox" name="enabled"{chk(s["enabled"])}> Enabled</label>'
            f'<label class="chk"><input type="checkbox" name="idle_only"{chk(s["idle_only"])}> Only when idle</label>'
            f'<button type="submit">Save schedule</button> <span class="msg muted" role="status"></span></form>')


def _jobs_body() -> str:
    e = html.escape
    reg = _reg()
    idle = reg.idle()
    now = reg.clock()
    reason = {"set_live": "a set is live", "grace": "a set just ended", "idle": "idle"}.get(idle.reason, idle.reason)
    body = (f'<p>Server: <b>{e(reason)}</b> <span class="muted">(idle after {int(reg.grace_s)} s without a set; '
            f'cron times are server local time)</span>'
            + ("" if reg.enabled else ' <span class="muted">job runner off (DJ_JOBS=0)</span>') + "</p>")
    rows = []
    for v in reg.views():
        n, c, last, p, sc = e(v["name"]), v["controls"], v["last"], v["progress"], v["schedule"]
        total = float(p.get("total") or 0)
        bar = f'<progress max="{total}" value="{float(p.get("done") or 0)}"></progress> ' if total > 0 else ""
        btns = "".join(
            f'<form method="post" action="/api/admin/jobs/{n}/{act}" style="display:inline"><button type="submit">{label}</button></form> '
            for act, label in (("run", "Run now"), ("pause", "Pause"), ("resume", "Resume"), ("cancel", "Cancel")) if c.get(act))
        sets = "<br>".join(f"{e(k)} = {e(str(x['value']))}" for k, x in v["settings"].items())
        err = f'<br><span class="muted">{e(str(last["error"]))}</span>' if last.get("error") else ""
        dur = f" in {_dur(last['ended'] - last['started'])}" if last.get("ended") and last.get("started") else ""
        nxt = (f'{e(_t(v["next_run"]))}<br><span class="muted">{e(_rel(v["next_run"], now))}</span>' if v["next_run"] else
               '<span class="muted">not scheduled</span>' if not sc["expr"] else '<span class="muted">off</span>')
        cr = f'<br><span class="muted">cron {e(sc["expr"])}</span>' if sc["expr"] else ""
        hist = "".join(_hist_row(h) for h in v["history"])
        hist = f"<details><summary>History ({len(v['history'])})</summary><ul>{hist}</ul></details>" if hist else ""
        rows.append(
            f'<tr><td data-label="Job">{e(v["title"])}<br><span class="muted">{n} - {e(v["kind"])}{" - idle only" if v["idle_only"] else ""}</span>{cr}</td>'
            f'<td data-label="State">{e(v["state"])}</td><td data-label="Progress">{bar}{e(str(p.get("note") or ""))}</td>'
            f'<td data-label="Last run">{e(_fmt_state(last.get("status")))} {e(_t(last.get("ended")))}{e(dur)}{err}{hist}</td>'
            f'<td data-label="Next run">{nxt}</td>'
            f'<td data-label="Settings">{sets}</td><td data-label="Actions">{btns}<a href="/api/admin/jobs/{n}/log?tail=100">log</a>'
            f'{_sched_form(v)}</td></tr>')
    return body + ("<table><tr><th>Job</th><th>State</th><th>Progress</th><th>Last run</th><th>Next run</th>"
                   f"<th>Settings</th><th></th></tr>{''.join(rows)}</table>")


_JOBS_CSP = "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'"
_JOBS_HEAD = ('<style>form.sched{margin-top:8px}form.sched label{display:block;margin:6px 0}form.sched label.chk{display:flex;'
              'align-items:center;gap:8px;min-height:44px}form.sched input[type=text],form.sched input[type=number]{width:100%;'
              'max-width:24em}form.sched input[type=checkbox]{width:22px;height:22px;min-height:0}details ul{padding-left:18px;'
              'margin:4px 0;font-size:13px}</style><script src="/admin-jobs.js" defer></script>')


def _cache_audit_body() -> str:
    from app.music_brain.config import CACHE_DIR
    from app.ui import server
    from app.ui.services import cache_audit

    e = html.escape
    flags = cache_audit.load(CACHE_DIR)
    names = server._track_names
    s = cache_audit.summary(flags)
    head = (f'<p>{s["flagged"]} suspect files {e(str(s["by_class"]))}. Flag only: nothing is deleted. Re-fetch downloads the '
            f'song again from its source video; the flagged file stays. Run the scan from <a href="/admin/jobs">Jobs</a> '
            f'(cache_audit).</p>')
    rows = []
    for tid, f in sorted(flags.items()):
        refetch = (f'<form method="post" action="/api/admin/cache-audit/{e(tid)}/refetch"><button type="submit">Re-fetch</button></form>'
                   if f.get("video") and f.get("status") == "open" else "")
        new = f' -> {e(str(f["new_id"]))}' if f.get("new_id") else ""
        rows.append(f'<tr><td data-label="Song">{e(names.get(tid) or tid)}<br><span class="muted">{e(tid)}</span></td>'
                    f'<td data-label="Why">{e(", ".join(f.get("classes", [])))}<br><span class="muted">{e(str(f.get("detail") or ""))}</span></td>'
                    f'<td data-label="Source">{e(str(f.get("video") or ""))}</td>'
                    f'<td data-label="Status">{e(str(f.get("status") or ""))}{new}</td><td data-label="">{refetch}</td></tr>')
    return head + ("<table><tr><th>Song</th><th>Why</th><th>Source video</th><th>Status</th><th></th></tr>"
                   f"{''.join(rows)}</table>" if rows else "<p>Nothing flagged.</p>")


@router.get("/admin/cache-audit", response_class=HTMLResponse)
def admin_cache_audit_page(email: str = Depends(require_admin)):
    return _page("Cache audit", _cache_audit_body(), email, refresh=0)


@router.get("/admin/jobs", response_class=HTMLResponse)
def admin_jobs_page(email: str = Depends(require_admin)):
    return _page("Jobs", _jobs_body(), email, refresh=0, head=_JOBS_HEAD, csp=_JOBS_CSP)


@router.get("/api/admin/idle")
def admin_idle(email: str = Depends(require_admin)) -> dict:
    reg = _reg()
    st = reg.idle()
    return {"idle": st.idle, "since": st.since, "reason": st.reason, "grace_s": reg.grace_s}


@router.get("/api/admin/jobs")
def admin_jobs(email: str = Depends(require_admin)) -> dict:
    reg = _reg()
    st = reg.idle()
    return {"idle": {"idle": st.idle, "since": st.since, "reason": st.reason, "grace_s": reg.grace_s},
            "jobs": reg.views()}


@router.get("/api/admin/jobs/{name}")
def admin_job(name: str, email: str = Depends(require_admin)):
    from app.ui.services.job_registry import JobError
    try:
        return _reg().view(name)
    except JobError as exc:
        return _job_err(exc)


@router.get("/api/admin/jobs/{name}/log")
def admin_job_log(name: str, tail: int = 100, email: str = Depends(require_admin)):
    from app.ui.services.job_registry import JobError
    try:
        return {"log": _reg().log_tail(name, max(0, min(tail, 200)))}
    except JobError as exc:
        return _job_err(exc)


@router.post("/api/admin/jobs/{name}/{action}")
def admin_job_control(name: str, action: str, request: Request, email: str = Depends(require_admin)):
    from app.ui.services.job_registry import JobError
    reg = _reg()
    fn = {"pause": reg.pause, "resume": reg.resume, "cancel": reg.cancel, "run": reg.run_now}.get(action)
    if fn is None:
        raise HTTPException(404, "Not Found")
    try:
        view = fn(name)
    except JobError as exc:
        if "text/html" in request.headers.get("accept", ""):          # a form post: back to the page
            from fastapi.responses import RedirectResponse
            return RedirectResponse("/admin/jobs", status_code=303)
        return _job_err(exc)
    audit().add(email, f"job_{action}", name)
    if "text/html" in request.headers.get("accept", ""):
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/admin/jobs", status_code=303)
    return view


@router.put("/api/admin/jobs/{name}/schedule")
def admin_job_schedule(name: str, values: dict, email: str = Depends(require_admin)):
    """schedule (5-field cron, server local time; null = none), enabled, max_runtime_s, idle_only. 422 on a bad value."""
    import json
    from app.ui.services.job_registry import JobError
    try:
        view = _reg().set_schedule(name, values)
    except JobError as exc:
        return _job_err(exc)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc), "code": "bad_schedule"}, status_code=422)
    audit().add(email, "job_schedule", name, json.dumps(values, default=str)[:300])
    return view


@router.put("/api/admin/jobs/{name}/settings")
def admin_job_settings(name: str, values: dict, email: str = Depends(require_admin)):
    import json
    from app.ui.services.job_registry import JobError
    try:
        view = _reg().set_settings(name, values)
    except JobError as exc:
        return _job_err(exc)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc), "code": "bad_setting"}, status_code=422)
    audit().add(email, "job_settings", name, json.dumps(values, default=str)[:300])
    return view


from app.ui.services import admin_graph as _admin_graph  # noqa: E402 -- /admin/graph + /api/admin/graph/*

_admin_graph.register(router, require_admin, _page)
