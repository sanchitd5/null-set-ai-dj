"""quarantine_cleanup: audio 7 d, notes 30 d, 2 GB cap oldest first, summary counts; it runs as a job, not a thread."""
import threading

from app.ui.services import quarantine_cleanup as qc

NOW = 1_000_000_000.0
D = qc.DAY


def _mk(q, age_days, size=10, ext=".m4a"):
    ts = int(NOW - age_days * D)
    p = q / f"{ts}-song{ext}"
    p.write_bytes(b"x" * size)
    return p


def test_old_audio_goes_notes_stay_until_30_days(tmp_path):
    a_old, a_new = _mk(tmp_path, 8), _mk(tmp_path, 1)
    n_mid, n_old = _mk(tmp_path, 10, ext=".m4a.json"), _mk(tmp_path, 31, ext=".m4a.json")
    r = qc.cleanup(tmp_path, now=NOW)
    assert not a_old.exists() and a_new.exists()
    assert n_mid.exists() and not n_old.exists()
    assert r["audio"] == 1 and r["notes"] == 1


def test_cap_removes_oldest_audio_first(tmp_path, monkeypatch):
    monkeypatch.setattr(qc, "CAP_BYTES", 25)
    a3, a2, a1 = _mk(tmp_path, 3, 10), _mk(tmp_path, 2, 10), _mk(tmp_path, 1, 10)
    qc.cleanup(tmp_path, now=NOW)
    assert not a3.exists() and a2.exists() and a1.exists()


def test_summary_and_missing_dir(tmp_path):
    _mk(tmp_path, 1, 5)
    _mk(tmp_path, 1, 3, ext=".m4a.json")
    s = qc.summary(tmp_path)
    assert (s["audio_files"], s["audio_bytes"], s["notes"]) == (1, 5, 1)
    assert qc.summary(tmp_path / "nope")["audio_files"] == 0
    assert qc.cleanup(tmp_path / "nope", now=NOW) == {"audio": 0, "notes": 0, "bytes": 0}


def test_no_thread_of_its_own_and_runs_as_a_registered_job(tmp_path, monkeypatch):
    from app.ui.services import job_registry, jobs_builtin

    assert not hasattr(qc, "start") and not hasattr(qc, "_loop")
    before = {t.name for t in threading.enumerate()}
    seen = []
    monkeypatch.setattr(qc, "cleanup", lambda *a, **k: seen.append(1) or {"audio": 0, "notes": 0, "bytes": 0})
    now = [1000.0]
    reg = job_registry.Registry(tmp_path, lambda: now[0], spawn=lambda name, fn: fn(), environ={})
    jobs_builtin.register_builtin(reg)
    spec = reg._jobs["quarantine_cleanup"].spec
    assert spec.schedule == "15 4 * * *" and not spec.idle_only
    reg.tick()                                         # cron: nothing before the first fire, no thread
    assert seen == []
    now[0] = reg.view("quarantine_cleanup")["next_run"]
    reg.tick()
    assert seen == [1] and reg.view("quarantine_cleanup")["last"]["status"] == "ok"
    assert {t.name for t in threading.enumerate()} == before
