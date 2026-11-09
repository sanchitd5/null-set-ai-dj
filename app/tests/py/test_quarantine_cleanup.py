"""quarantine_cleanup: audio 7 d, notes 30 d, 2 GB cap oldest first, summary counts."""
import os

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
