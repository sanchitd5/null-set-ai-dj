"""Writes sync_merge_golden.json: ops (with duplicates, shuffled) and the rows `merge.fold` yields. The same file is
copied to dj-core test/sync/fixtures/ and applied there with applyOp. Opaque ids only. Run:
    python -m app.tests.fixtures.gen_sync_golden
"""
import json
import random
from pathlib import Path

from app.music_brain.sync import merge

OUT = Path(__file__).with_name("sync_merge_golden.json")
DEVICES = ("dev-a", "dev-b", "dev-c")
KEYS = ("k1", "k2", "k3", "k4")


def make_ops(seed: int = 9, n: int = 120) -> list:
    rng = random.Random(seed)
    ops = []
    for i in range(n):
        kind = rng.choice(merge.KINDS)
        if kind == "incr":
            delta = {f: rng.randint(0, 5) for f in rng.sample(("n", "good", "bad"), rng.randint(1, 3))}
        elif kind == "lww":
            delta = {"label": rng.choice(("x", "y", "z", None)), "rank": rng.randint(0, 9)}
            if rng.random() < 0.2:
                delta = {"deleted": 1}
        elif kind == "max":
            delta = {"w": rng.choice((0, 1, 2.5, 7, 3.25))}
        else:
            delta = {"field": "tags", "add": rng.sample(("t1", "t2", "t3", "t4", "t5", "t6"), rng.randint(1, 3))}
        ops.append({"op_id": f"{rng.choice(DEVICES)}:{i}", "tbl": "edge", "key": rng.choice(KEYS), "kind": kind,
                    "delta": delta, "ts": 1_700_000_000_000 + rng.randint(0, 40) * 1000,   # many ts ties
                    "scope": "global", "device": rng.choice(DEVICES)})
    dups = rng.sample(ops, 25)
    allops = ops + [dict(o) for o in dups]
    rng.shuffle(allops)
    return allops


def main() -> None:
    ops = make_ops()
    OUT.write_text(json.dumps({"ops": ops, "rows": merge.fold(ops)}, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({len(ops)} ops)")


if __name__ == "__main__":
    main()
