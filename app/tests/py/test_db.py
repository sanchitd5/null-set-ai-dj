"""app.music_brain.db: one SQLite file per cache dir, WAL, per-store schema versions, retire()."""
import threading

import pytest

from app.music_brain import db


def test_connect_is_wal_and_per_thread(tmp_path):
    p = tmp_path / db.APP_DB
    c = db.connect(p)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.connect(p) is c
    other = []
    t = threading.Thread(target=lambda: other.append(db.connect(p)))
    t.start()
    t.join()
    assert other[0] is not c, "one connection per thread"
    assert db.db_path(tmp_path) == tmp_path / db.APP_DB
    assert db.beside(tmp_path / "x.json") == tmp_path.resolve() / db.APP_DB


def test_schema_steps_run_once_in_order_and_upgrade(tmp_path):
    c = db.connect(tmp_path / db.APP_DB)
    v1 = ("CREATE TABLE t (a INTEGER)",)
    assert db.ensure(c, "s", v1) == 1 and db.ensure(c, "s", v1) == 1
    calls = []
    v2 = v1 + (lambda conn: calls.append(conn.execute("INSERT INTO t VALUES (7)")),)
    assert db.ensure(c, "s", v2) == 2 and db.ensure(c, "s", v2) == 2
    assert len(calls) == 1 and c.execute("SELECT a FROM t").fetchall() == [(7,)]
    assert db.version(c, "other") == 0


def test_a_newer_db_is_refused_and_a_failed_step_rolls_back(tmp_path):
    c = db.connect(tmp_path / db.APP_DB)
    db.ensure(c, "s", ("CREATE TABLE t (a INTEGER)", "CREATE TABLE u (a INTEGER)"))
    with pytest.raises(ValueError, match="newer"):
        db.ensure(c, "s", ("CREATE TABLE t (a INTEGER)",))
    with pytest.raises(Exception):
        db.ensure(c, "s2", ("CREATE TABLE v (a INTEGER); INSERT INTO nope VALUES (1)",))
    assert db.version(c, "s2") == 0
    assert c.execute("SELECT name FROM sqlite_master WHERE name = 'v'").fetchone() is None


def test_tx_nests_and_rolls_back(tmp_path):
    c = db.connect(tmp_path / db.APP_DB)
    db.ensure(c, "s", ("CREATE TABLE t (a INTEGER)",))
    with pytest.raises(RuntimeError):
        with db.tx(c):
            c.execute("INSERT INTO t VALUES (1)")
            with db.tx(c):
                c.execute("INSERT INTO t VALUES (2)")
            raise RuntimeError
    assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 0


def test_app_and_user_db_are_separate_files(tmp_path):
    assert db.db_path(tmp_path, db.USER_DB) == tmp_path / "user.db" != db.db_path(tmp_path)
    assert db.connect(tmp_path / db.APP_DB) is not db.connect(tmp_path / db.USER_DB)


def test_retire_never_overwrites(tmp_path):
    a = tmp_path / "x.json"
    a.write_text("1")
    assert db.retire(a).name == "x.json.migrated"
    a.write_text("2")
    second = db.retire(a)
    assert second.name.startswith("x.json.migrated.") and (tmp_path / "x.json.migrated").read_text() == "1"
