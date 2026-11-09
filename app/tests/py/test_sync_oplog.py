"""OpLog (music_brain/sync/oplog.py): idempotent ingest, policy, scope filtering, paging, floor, appliers, compaction."""
import pytest

from app.music_brain.sync import merge, oplog

NOW = 1_800_000_000.0
MS = int(NOW * 1000)


def mk(oid, tbl="edge", kind="incr", delta=None, key="k1", scope="global", ts=MS):
    return {"op_id": oid, "tbl": tbl, "key": key, "kind": kind, "delta": delta or {"n": 1}, "ts": ts, "scope": scope}


@pytest.fixture
def log(tmp_path):
    return oplog.OpLog(tmp_path / "sync.db")


def test_ingest_is_idempotent(log):
    r1 = log.ingest([mk("a1"), mk("a2")], "dev1", "user:u1", now=NOW)
    assert r1["accepted"] == ["a1", "a2"] and r1["duplicate"] == [] and r1["head"] == 2
    r2 = log.ingest([mk("a1"), mk("a3"), mk("a3")], "dev2", "user:u1", now=NOW)
    assert r2["accepted"] == ["a3"] and sorted(r2["duplicate"]) == ["a1", "a3"]
    assert [o["op_id"] for o in log.since({"global"}, 0)] == ["a1", "a2", "a3"]


def test_rejects_leave_the_rest_of_the_batch_alone(log):
    ops = [mk("ok1"), mk("b1", tbl="Bad Table"), mk("b2", kind="nope"), mk("b3", delta={"n": 1.5}), mk("b4", tbl="zzz"),
           mk("b5", scope="galaxy"), mk("b6", ts="now"), mk("b7", key=""), mk("b8", delta={"n": 1, "x": 1}) | {"op_id": "bad id"},
           mk("b9", kind="lww", delta={"note": "mail a@b.com"}, tbl="edge")]
    r = log.ingest(ops, "dev1", "user:u1", now=NOW)
    assert r["accepted"] == ["ok1"]
    assert {x["op_id"] for x in r["rejected"]} == {"b1", "b2", "b3", "b4", "b5", "b6", "b7", "bad id", "b9"}
    assert all(x["why"] for x in r["rejected"])


def test_policy_denies_global_user_tables_and_user_edges(log):
    r = log.ingest([mk("p1", tbl="pref", kind="lww", delta={"a": 1}, scope="global"),
                    mk("p2", tbl="edge", scope="user"),
                    mk("p3", tbl="pref", kind="lww", delta={"a": 1}, scope="user")], "dev1", "user:u1", now=NOW)
    assert r["accepted"] == ["p3"] and {x["op_id"] for x in r["rejected"]} == {"p1", "p2"}


def test_scope_comes_from_the_caller_not_the_op(log):
    log.ingest([mk("s1", tbl="pref", kind="lww", delta={"a": 1}, scope="user")], "dev1", "user:u1", now=NOW)
    assert log.since({"global", "user:u1"}, 0)[0]["scope"] == "user:u1"
    assert log.since({"global", "user:u2"}, 0) == []


def test_scope_filter_and_echo_suppression(log):
    log.ingest([mk("g1"), mk("u1", tbl="pref", kind="lww", delta={"a": 1}, scope="user")], "dev1", "user:u1", now=NOW)
    log.ingest([mk("g2"), mk("u2", tbl="pref", kind="lww", delta={"a": 1}, scope="user")], "dev2", "user:u2", now=NOW)
    ids = lambda scopes, **kw: [o["op_id"] for o in log.since(scopes, 0, **kw)]  # noqa: E731
    assert ids({"global", "user:u1"}) == ["g1", "u1", "g2"]
    assert ids({"global", "user:u2"}) == ["g1", "g2", "u2"]
    assert ids({"global", "user:u1"}, exclude_device="dev1") == ["g2"]


def test_since_limit_order_and_head(log):
    log.ingest([mk(f"o{i}") for i in range(7)], "dev1", "user:u1", now=NOW)
    page = log.since({"global"}, 2, 3)
    assert [o["seq"] for o in page] == [3, 4, 5] and log.head() == 7
    assert log.since({"global"}, 7) == []


def test_ts_is_clamped_to_a_day_of_server_time(log):
    log.ingest([mk("t1", ts=MS + 10 * 86_400_000), mk("t2", ts=0)], "dev1", "user:u1", now=NOW)
    ts = [o["ts"] for o in log.since({"global"}, 0)]
    assert ts == [MS + 86_400_000, MS - 86_400_000]


def test_batch_limit(log):
    with pytest.raises(ValueError):
        log.ingest([mk(f"o{i}") for i in range(oplog.MAX_BATCH + 1)], "dev1", "user:u1")


def test_survives_restart_and_seq_is_never_reused(tmp_path):
    p = tmp_path / "sync.db"
    a = oplog.OpLog(p)
    a.ingest([mk("o1"), mk("o2")], "dev1", "user:u1", now=NOW)
    b = oplog.OpLog(p)
    assert b.head() == 2 and [o["op_id"] for o in b.since({"global"}, 0)] == ["o1", "o2"]
    assert b.ingest([mk("o1"), mk("o3")], "dev1", "user:u1", now=NOW)["accepted"] == ["o3"]
    assert b.head() == 3


def test_listener_runs_after_commit_only_for_new_ops(log):
    seen = []
    off = log.subscribe(lambda: seen.append(log.head()))
    log.ingest([mk("o1")], "dev1", "user:u1", now=NOW)
    log.ingest([mk("o1")], "dev1", "user:u1", now=NOW)
    off()
    log.ingest([mk("o2")], "dev1", "user:u1", now=NOW)
    assert seen == [1]
    log.subscribe(lambda: 1 / 0)
    assert log.ingest([mk("o3")], "dev1", "user:u1", now=NOW)["accepted"] == ["o3"]    # a bad listener never fails it


def _rows_applier(rows):
    def fn(o):
        k = o["key"]
        rows[k] = merge.apply_op(rows.get(k), o)
    return fn


def test_applier_materialises_and_a_late_one_replays(log):
    log.ingest([mk("o1", delta={"n": 2}), mk("o2", delta={"n": 3}, key="k2")], "dev1", "user:u1", now=NOW)
    rows = {}
    log.register_applier("edge", _rows_applier(rows))
    assert log.apply_pending() == 2 and rows["k1"]["n"] == 2 and rows["k2"]["n"] == 3 and log.applied("edge") == 2
    log.ingest([mk("o3", delta={"n": 4})], "dev1", "user:u1", now=NOW)           # live: applied right after ingest
    assert rows["k1"]["n"] == 6 and log.applied("edge") == 3
    assert log.apply_pending() == 0


def test_failing_applier_keeps_its_cursor_and_retries(log):
    calls = {"n": 0}

    def fn(o):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
    log.register_applier("edge", fn)
    log.ingest([mk("o1"), mk("o2"), mk("o3")], "dev1", "user:u1", now=NOW)
    assert log.applied("edge") == 1
    assert log.apply_pending() == 2 and log.applied("edge") == 3


def test_compact_drops_only_old_applied_ops_and_raises_the_floor(log):
    old, new = NOW - 40 * 86400, NOW - 1 * 86400
    log.ingest([mk("o1")], "dev1", "user:u1", now=old)
    log.ingest([mk("o2"), mk("u1", tbl="pref", kind="lww", delta={"a": 1}, scope="user")], "dev1", "user:u1", now=old)
    log.ingest([mk("o3")], "dev1", "user:u1", now=new)
    rows = {}
    log.register_applier("edge", _rows_applier(rows))
    res = log.compact(30, now=NOW)
    assert res == {"deleted": 2, "floor": 2}
    assert [o["op_id"] for o in log.since({"global", "user:u1"}, 0)] == ["u1", "o3"]   # pref has no applier: kept
    assert rows["k1"]["n"] == 3                                                         # materialised before the delete
    assert log.head() == 4 and log.floor() == 2
    assert log.compact(30, now=NOW)["deleted"] == 0
