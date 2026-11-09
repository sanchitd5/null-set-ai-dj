"""/admin/jobs and /api/admin/jobs*: gate (404 off, 403 not admin), controls and their 409/422 answers, audit of
every write. Reuses the admin shell test app; the registry is a private one with an inline spawn."""
import pytest

from app.tests.py.test_admin_shell import ORIGIN, OTHER, _admin, _client, _google_cookie, env  # noqa: F401
from app.ui.services import admin, job_registry as jr
from app.ui.services.job_registry import JobSpec, Registry, SettingSpec


@pytest.fixture
def reg(env, tmp_path):  # noqa: F811
    _, _, _, clock = env
    r = Registry(tmp_path, clock, spawn=lambda name, fn: fn(), environ={"DJ_IDLE_GRACE_S": "30"})
    jr.use(r)
    calls = []
    r.register(JobSpec("work", "Work <i>", fn=lambda ctx: calls.append(ctx.setting("n")) or {"n": 1}, kind="manual",
                       settings={"n": SettingSpec("int", 5, 1, 10)}))
    r.register(JobSpec("idler", "Idler", fn=lambda ctx: None, kind="manual", idle_only=True))
    r.register(JobSpec("ext", "Ext", kind="external", status_fn=lambda: {"busy": None}))
    yield r, calls
    jr.use(None)


def _actions(c):
    return [(a["action"], a["target"]) for a in reversed(c.get("/api/admin/audit").json()["audit"])]


def test_feature_off_is_404(env, reg, monkeypatch):  # noqa: F811
    c = _admin(env)
    monkeypatch.delenv("DJ_ADMIN_EMAILS")
    for path in ("/admin/jobs", "/api/admin/jobs", "/api/admin/idle", "/api/admin/jobs/work", "/api/admin/jobs/work/log"):
        assert c.get(path).status_code == 404, path
    assert c.post("/api/admin/jobs/work/run", headers=ORIGIN).status_code == 404
    assert c.put("/api/admin/jobs/work/settings", json={"n": 2}, headers=ORIGIN).status_code == 404


def test_non_admin_is_403_and_nothing_runs(env, reg):  # noqa: F811
    app, s, *_ = env
    r, calls = reg
    c = _client(app, s, _google_cookie(s, "1002", OTHER))
    assert c.get("/admin/jobs").status_code == 403 and c.get("/api/admin/jobs").status_code == 403
    for act in ("run", "pause", "resume", "cancel"):
        assert c.post(f"/api/admin/jobs/work/{act}", headers=ORIGIN).status_code == 403
    assert c.put("/api/admin/jobs/work/settings", json={"n": 2}, headers=ORIGIN).status_code == 403
    assert calls == [] and not r.view("work")["paused"]


def test_list_view_idle_and_page(env, reg):  # noqa: F811
    c = _admin(env)
    body = c.get("/api/admin/jobs").json()
    assert body["idle"]["idle"] is False and body["idle"]["reason"] == "grace" and body["idle"]["grace_s"] == 30
    assert [j["name"] for j in body["jobs"]] == ["work", "idler", "ext"]
    assert c.get("/api/admin/idle").json()["reason"] == "grace"
    assert c.get("/api/admin/jobs/work").json()["settings"]["n"]["value"] == 5
    assert c.get("/api/admin/jobs/nope").status_code == 404
    page = c.get("/admin/jobs")
    assert page.status_code == 200 and "Work &lt;i&gt;" in page.text and "<i>" not in page.text
    assert page.text.count("<script") == 1 and 'src="/admin-jobs.js"' in page.text   # same-origin script only
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert 'href="/admin/jobs"' in c.get("/admin").text                  # the nav link


def test_run_pause_resume_cancel_and_audit(env, reg):  # noqa: F811
    c = _admin(env)
    r, calls = reg
    v = c.post("/api/admin/jobs/work/run", headers=ORIGIN).json()
    assert v["last"]["status"] == "ok" and calls == [5]
    assert c.post("/api/admin/jobs/work/pause", headers=ORIGIN).json()["state"] == "paused"
    refused = c.post("/api/admin/jobs/work/run", headers=ORIGIN)
    assert refused.status_code == 409 and refused.json()["code"] == "paused"
    assert c.post("/api/admin/jobs/work/resume", headers=ORIGIN).json()["state"] == "idle"
    nr = c.post("/api/admin/jobs/work/cancel", headers=ORIGIN)
    assert nr.status_code == 409 and nr.json()["code"] == "not_running"
    assert c.post("/api/admin/jobs/ext/run", headers=ORIGIN).status_code == 409
    assert c.post("/api/admin/jobs/nope/run", headers=ORIGIN).status_code == 404
    assert c.post("/api/admin/jobs/work/explode", headers=ORIGIN).status_code == 404
    assert _actions(c) == [("job_run", "work"), ("job_pause", "work"), ("job_resume", "work")]   # refused calls are not writes


def test_run_now_when_busy_is_409_not_idle(env, reg):  # noqa: F811
    _, _, store, clock = env
    c = _admin(env)
    clock.t += 60
    store.acquire("phone", "p", {"kind": "device", "key": "device:p1", "name": "phone"})
    r = c.post("/api/admin/jobs/idler/run", headers=ORIGIN)
    assert r.status_code == 409 and r.json()["code"] == "not_idle"
    assert c.get("/api/admin/idle").json()["reason"] == "set_live"


def test_settings_put_validates_and_audits(env, reg):  # noqa: F811
    c = _admin(env)
    r, calls = reg
    ok = c.put("/api/admin/jobs/work/settings", json={"n": 7}, headers=ORIGIN)
    assert ok.status_code == 200 and ok.json()["settings"]["n"]["value"] == 7
    for bad in ({"zz": 1}, {"n": 99}, {"n": "x"}):
        resp = c.put("/api/admin/jobs/work/settings", json=bad, headers=ORIGIN)
        assert resp.status_code == 422, bad
    assert c.put("/api/admin/jobs/nope/settings", json={"n": 1}, headers=ORIGIN).status_code == 404
    c.post("/api/admin/jobs/work/run", headers=ORIGIN)
    assert calls == [7]
    assert _actions(c) == [("job_settings", "work"), ("job_run", "work")]


def test_log_tail(env, reg):  # noqa: F811
    c = _admin(env)
    r, _ = reg
    r.register(JobSpec("chatty", "Chatty", fn=lambda ctx: [ctx.log(f"l{i}") for i in range(5)], kind="manual"))
    c.post("/api/admin/jobs/chatty/run", headers=ORIGIN)
    assert [x["msg"] for x in c.get("/api/admin/jobs/chatty/log?tail=2").json()["log"]] == ["l3", "l4"]
    assert c.get("/api/admin/jobs/nope/log").status_code == 404


def test_form_post_redirects_back_to_the_page(env, reg):  # noqa: F811
    c = _admin(env)
    r = c.post("/api/admin/jobs/work/run", headers={**ORIGIN, "Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/jobs"
    assert admin.audit().recent(1)[0]["action"] == "job_run"


def _sched_reg(reg):
    r, _ = reg
    r.register(JobSpec("cronj", "Cron job", fn=lambda ctx: None, kind="manual", schedule="0 * * * *", idle_only=True))
    return r


def test_schedule_put_validates_and_audits(env, reg):  # noqa: F811
    c = _admin(env)
    _sched_reg(reg)
    v = c.put("/api/admin/jobs/cronj/schedule", json={"schedule": "*/5 * * * *", "enabled": False, "max_runtime_s": 600},
              headers=ORIGIN)
    assert v.status_code == 200
    s = v.json()["schedule"]
    assert s["expr"] == "*/5 * * * *" and s["enabled"] is False and s["max_runtime_s"] == 600 and s["default"] == "0 * * * *"
    for bad in ({"schedule": "61 * * * *"}, {"schedule": "* * *"}, {"enabled": "yes"}, {"max_runtime_s": -1}, {"nope": 1}, {}):
        r = c.put("/api/admin/jobs/cronj/schedule", json=bad, headers=ORIGIN)
        assert r.status_code == 422 and r.json()["code"] == "bad_schedule", bad
    assert c.put("/api/admin/jobs/cronj/schedule", json={"schedule": None}, headers=ORIGIN).json()["schedule"]["expr"] is None
    assert c.put("/api/admin/jobs/ext/schedule", json={"enabled": True}, headers=ORIGIN).status_code == 409
    assert c.put("/api/admin/jobs/nope/schedule", json={"enabled": True}, headers=ORIGIN).status_code == 404
    assert [a for a in _actions(c) if a[0] == "job_schedule"] == [("job_schedule", "cronj")] * 2


def test_schedule_put_gate(env, reg, monkeypatch):  # noqa: F811
    app, s, *_ = env
    _sched_reg(reg)
    c = _client(app, s, _google_cookie(s, "1002", OTHER))
    assert c.put("/api/admin/jobs/cronj/schedule", json={"enabled": False}, headers=ORIGIN).status_code == 403
    assert reg[0].view("cronj")["schedule"]["enabled"] is True
    monkeypatch.delenv("DJ_ADMIN_EMAILS")
    assert _admin(env).put("/api/admin/jobs/cronj/schedule", json={"enabled": False}, headers=ORIGIN).status_code == 404


def test_page_shows_schedule_and_is_phone_sized(env, reg):  # noqa: F811
    c = _admin(env)
    r = _sched_reg(reg)
    r.set_schedule("cronj", {"max_runtime_s": 3000})
    page = c.get("/admin/jobs").text
    for want in ('name="schedule" value="0 * * * *"', 'name="max_min"', 'value="50"', 'name="enabled" checked',
                 "Save schedule", "cron 0 * * * *", "server local time", 'data-label="Next run"', "not scheduled"):
        assert want in page, want
    assert 'name="viewport" content="width=device-width,initial-scale=1"' in page      # 360 px: one column, no side scroll
    assert "@media (max-width:640px)" in page and "table,tbody,tr,td{display:block;width:100%}" in page
    assert "max-width:24em" in page and "min-height:44px" in page
    assert "<script" in page and "onclick" not in page and "javascript:" not in page
    assert 'http-equiv="refresh"' not in page                      # a refresh would wipe a half-edited form
