"""Admin shell: /admin, owner only (docs/engineering/ADMIN.md).

Access is the existing Google browser sign-in plus an allowlist, DJ_ADMIN_EMAILS (comma separated, the
same addresses as DJ_ALLOWED_EMAILS). Empty or unset: every admin route answers 404, so the feature
leaves no trace. A device bearer token never reaches an admin route (403), nor does a password session.

    GET  /admin, /admin/sessions        server-rendered pages (no script; a 5 s meta refresh)
    GET  /api/admin/me                  {email}
    GET  /api/admin/sessions            {live, closed}: the live set (no lease id) and the last 20 closed sets
    POST /api/admin/sessions/close      end the live set (the holder's next beat answers 409 "closed by admin")
    GET  /api/admin/audit               the newest 100 admin writes

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
NAV = [("Sessions", "/admin/sessions")]             # later admin pages append here (jobs: G5)
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

_CSS = ("body{font:14px system-ui,sans-serif;margin:0;background:#111;color:#eee}"
        "nav{padding:10px 16px;background:#1b1b1b}nav a{color:#9cf;margin-right:14px;text-decoration:none}"
        "main{padding:16px}table{border-collapse:collapse}th,td{padding:4px 10px;border-bottom:1px solid #333;text-align:left}"
        "button{padding:6px 12px}.muted{color:#999}")


def _t(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts))) if ts else ""


def _dur(sec) -> str:
    sec = int(sec or 0)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _page(title: str, body: str, email: str) -> HTMLResponse:
    e = html.escape
    nav = "".join(f'<a href="{e(u)}">{e(n)}</a>' for n, u in NAV)
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{e(title)}</title>'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<meta http-equiv="refresh" content="5"><style>{_CSS}</style></head><body>'
           f'<nav>{nav}<span class="muted">{e(email)}</span></nav><main><h1>{e(title)}</h1>{body}</main></body></html>')
    return HTMLResponse(doc, headers={"content-security-policy": _PAGE_CSP, "cache-control": "no-store"})


def _sessions_body() -> str:
    e = html.escape
    st = set_lease.store()
    live = st.status_admin()
    if live["active"]:
        v = live["lease"]
        who = " ".join(x for x in (v.get("kind"), v.get("name")) if x)
        body = ("<h2>Live set</h2><table>"
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
        f"<tr><td>{e(str(r.get('client')))}</td><td>{e(str(r.get('device_name')))}</td><td>{e(_t(r.get('started')))}</td>"
        f"<td>{e(_t(r.get('ended')))}</td><td>{_dur(r.get('duration_s'))}</td><td>{e(str(r.get('reason')))}</td></tr>"
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
