"""Op merge rules (music_brain/sync/merge.py): each kind, order independence with duplicates, golden vectors."""
import json
import random
from pathlib import Path

import pytest

from app.music_brain.sync import merge
from app.tests.fixtures import gen_sync_golden as gen

GOLDEN = Path(gen.OUT)


def op(kind, delta, ts=1000, device="d1", oid="o1", key="k"):
    return {"op_id": oid, "tbl": "edge", "key": key, "kind": kind, "delta": delta, "ts": ts, "scope": "global", "device": device}


def test_incr_adds_and_keeps_newest_updated():
    r = merge.apply_op(None, op("incr", {"n": 2, "good": 1}, ts=50))
    r = merge.apply_op(r, op("incr", {"n": 3}, ts=20, oid="o2"))
    assert (r["n"], r["good"], r["updated"]) == (5, 1, 50)


def test_input_row_is_not_modified():
    row = {"n": 1}
    merge.apply_op(row, op("incr", {"n": 1}))
    assert row == {"n": 1}


def test_lww_newer_wins_and_ties_go_to_the_larger_device():
    r = merge.apply_op(None, op("lww", {"label": "old"}, ts=10, device="dev-a"))
    assert merge.apply_op(r, op("lww", {"label": "new"}, ts=11, device="dev-a"))["label"] == "new"
    assert merge.apply_op(r, op("lww", {"label": "stale"}, ts=9, device="dev-z"))["label"] == "old"
    assert merge.apply_op(r, op("lww", {"label": "tie"}, ts=10, device="dev-b"))["label"] == "tie"
    assert merge.apply_op(r, op("lww", {"label": "tie"}, ts=10, device="dev"))["label"] == "old"


def test_lww_is_per_field_and_delete_is_a_field():
    r = merge.apply_op(None, op("lww", {"a": 1, "b": 1}, ts=10))
    r = merge.apply_op(r, op("lww", {"a": 2}, ts=20, oid="o2"))
    r = merge.apply_op(r, op("lww", {"deleted": 1}, ts=15, oid="o3"))
    assert (r["a"], r["b"], r["deleted"]) == (2, 1, 1)


def test_max_takes_the_larger():
    r = merge.apply_op(None, op("max", {"w": 2.5}))
    assert merge.apply_op(r, op("max", {"w": 1}, oid="o2"))["w"] == 2.5
    assert merge.apply_op(r, op("max", {"w": 4}, oid="o2"))["w"] == 4


def test_union_sorted_and_capped_to_the_smallest():
    r = merge.apply_op(None, op("union", {"field": "tags", "add": ["b", "a"]}))
    r = merge.apply_op(r, op("union", {"field": "tags", "add": ["a", "c"]}, oid="o2"))
    assert r["tags"] == ["a", "b", "c"]
    many = merge.apply_op(None, op("union", {"field": "t", "add": [f"x{i:03d}" for i in range(64)]}))
    many = merge.apply_op(many, op("union", {"field": "t", "add": ["a0"]}, oid="o2"))
    assert len(many["t"]) == merge.UNION_CAP and many["t"][0] == "a0" and "x063" not in many["t"]


@pytest.mark.parametrize("kind,delta", [
    ("incr", {"n": 1.5}), ("incr", {"n": True}), ("incr", {}), ("incr", {"_st": 1}), ("incr", {"updated": 1}),
    ("max", {"w": "x"}), ("lww", {"a": [1]}), ("lww", {"a": "x" * 401}), ("union", {"field": "t", "add": "a"}),
    ("union", {"field": "t", "add": [""]}), ("union", {"field": "t"}), ("nope", {"a": 1}), ("incr", {"a" * 65: 1}),
    ("lww", {"a": "x" * 3000, "b": "y" * 3000}),
])
def test_bad_deltas_are_refused(kind, delta):
    with pytest.raises(ValueError):
        merge.validate_delta(kind, delta)


def test_any_order_with_duplicates_gives_the_same_rows():
    ops = gen.make_ops(seed=3, n=80)
    want = merge.fold(ops)
    rng = random.Random(1)
    for _ in range(25):
        shuffled = ops[:]
        rng.shuffle(shuffled)
        assert merge.fold(shuffled) == want
        assert merge.fold(shuffled + shuffled[:30]) == want


def test_grouping_does_not_matter():
    """Fold two halves separately, then merge the second half onto the first half's rows: same as one fold."""
    ops = gen.make_ops(seed=5, n=60)
    uniq = list({o["op_id"]: o for o in ops}.values())
    half = len(uniq) // 2
    assert merge.fold(uniq[half:], merge.fold(uniq[:half])) == merge.fold(uniq)


def test_golden_file_matches_the_generator():
    g = json.loads(GOLDEN.read_text())
    assert g["ops"] == json.loads(json.dumps(gen.make_ops()))
    assert g["rows"] == json.loads(json.dumps(merge.fold(g["ops"])))
    assert len(g["rows"]) >= 3
