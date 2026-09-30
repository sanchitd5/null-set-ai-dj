import os
import time

from app.music_brain.audio import keylock, keylock_cache as kc

NOW = 1_000_000_000.0
H = 3600.0


def _mk(root, name, size=100, age_h=5.0):
    d = root / name
    d.mkdir(parents=True)
    (d / "bass.wav").write_bytes(b"x" * size)
    t = NOW - age_h * H
    os.utime(d, (t, t))
    return d


def _ev(root, cap, **kw):
    return kc.evict_tempo_sets(root, cap, now=NOW, **kw)


def test_evicts_oldest_first_until_under_cap(tmp_path):
    _mk(tmp_path, "t_old", age_h=10)
    _mk(tmp_path, "t_mid", age_h=5)
    _mk(tmp_path, "t_new", age_h=2)
    r = _ev(tmp_path, 150)
    assert [x["key"] for x in r["removed"]] == ["t_old", "t_mid"]
    assert (r["before"], r["after"]) == (300, 100)
    assert not (tmp_path / "t_old").exists() and (tmp_path / "t_new").exists()


def test_noop_under_cap_and_disabled(tmp_path):
    _mk(tmp_path, "t_a")
    assert _ev(tmp_path, 1000)["removed"] == []
    assert _ev(tmp_path, None)["removed"] == []
    assert (tmp_path / "t_a").exists()


def test_protects_in_use_in_flight_and_young(tmp_path):
    _mk(tmp_path, "t_used", age_h=9)
    _mk(tmp_path, "t_young", age_h=0.1)
    _mk(tmp_path, "t_flight.tmp", age_h=9)
    _mk(tmp_path, "t_old", age_h=8)
    r = _ev(tmp_path, 0, protect=["t_used"])
    assert [x["key"] for x in r["removed"]] == ["t_old"]
    for n in ("t_used", "t_young", "t_flight.tmp"):
        assert (tmp_path / n).exists()


def test_leaves_other_dirs_and_files_alone(tmp_path):
    _mk(tmp_path, "abc123def456", age_h=50)     # riff set (hex key)
    (tmp_path / "t_file").write_bytes(b"x")
    _ev(tmp_path, 0)
    assert (tmp_path / "abc123def456").exists() and (tmp_path / "t_file").exists()


def test_dry_run_deletes_nothing(tmp_path):
    _mk(tmp_path, "t_old", age_h=10)
    r = _ev(tmp_path, 0, apply=False)
    assert len(r["removed"]) == 1 and r["after"] == 0
    assert (tmp_path / "t_old").exists()


def test_tolerates_missing_dir_and_vanishing_set(tmp_path, monkeypatch):
    assert _ev(tmp_path / "nope", 0) == {"before": 0, "after": 0, "removed": []}
    _mk(tmp_path, "t_gone", age_h=10)
    real = kc.shutil.rmtree

    def racing(p, **kw):            # another process removed it first
        real(p, ignore_errors=True)
        return real(p, ignore_errors=True)

    monkeypatch.setattr(kc.shutil, "rmtree", racing)
    assert len(_ev(tmp_path, 0)["removed"]) == 1


def test_env_parsing():
    f = lambda v: kc.max_bytes_from_env({} if v is None else {"KEYLOCK_CACHE_MAX_GB": v})  # noqa: E731
    default = int(20e9)
    assert f(None) == default and f("0") == default and f("junk") == default
    assert f("5") == int(5e9) and f("0.5") == int(0.5e9)
    assert f("-1") is None


def test_touch_bumps_mtime_debounced(tmp_path, monkeypatch):
    monkeypatch.setattr(keylock, "KEYLOCK_DIR", tmp_path)
    monkeypatch.setattr(keylock, "_touched", {})
    d = _mk(tmp_path, "t_x", age_h=9)
    for n in keylock.STEMS:
        (d / f"{n}.wav").write_bytes(b"x")
    assert keylock.stem_path("t_x", "bass") is not None
    assert time.time() - d.stat().st_mtime < 60
    os.utime(d, (NOW, NOW))
    keylock.touch("t_x")                # within a minute: no second bump
    assert d.stat().st_mtime == NOW
