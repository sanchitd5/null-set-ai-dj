"""Conflict-free merge of one op into one row. Pure functions, no I/O; mirrored by dj-core lib/sync/op_merge.dart
and checked against the shared golden app/tests/fixtures/sync_merge_golden.json.

Op (wire JSON, v1): {"op_id", "tbl", "key", "kind", "delta", "ts", "scope", "device"?}. `device` is stamped by the
server from the authenticated device, never taken from a client body.

A row is a plain dict of fields. Last-writer-wins stamps live in row["_st"] = {field: [ts, device]}.

    incr   delta = {field: int, ...}                 every field is added; row["updated"] = max(ts)
    lww    delta = {field: scalar, ...}              a field is set when (ts, device) beats its stamp
    max    delta = {field: number, ...}              every field = max(old, new)
    union  delta = {"field": name, "add": [str...]}  set merge, kept sorted and capped to the smallest UNION_CAP

Deletes are lww with {"deleted": 1}. Merging is commutative and associative once duplicates (same op_id) are
dropped, which `fold` does; the log itself deduplicates by op_id.
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

KINDS = ("incr", "lww", "max", "union")
UNION_CAP = 64
MAX_DELTA_BYTES = 4096
_FIELD_MAX = 64
_STR_MAX = 400


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _field(f) -> bool:
    return isinstance(f, str) and 0 < len(f) <= _FIELD_MAX and not f.startswith("_") and f != "updated"


def validate_delta(kind: str, delta) -> None:
    """ValueError when `delta` is not a valid payload for `kind`."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if not isinstance(delta, dict) or not delta:
        raise ValueError("delta must be a non-empty object")
    if len(json.dumps(delta, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_DELTA_BYTES:
        raise ValueError(f"delta over {MAX_DELTA_BYTES} bytes")
    if kind == "union":
        if set(delta) != {"field", "add"} or not _field(delta["field"]) or not isinstance(delta["add"], list):
            raise ValueError('union delta must be {"field": name, "add": [strings]}')
        if len(delta["add"]) > UNION_CAP or not all(isinstance(x, str) and 0 < len(x) <= _STR_MAX for x in delta["add"]):
            raise ValueError(f"union add: at most {UNION_CAP} non-empty strings")
        return
    for f, v in delta.items():
        if not _field(f):
            raise ValueError(f"bad field name {str(f)[:20]!r}")
        if kind == "incr" and not _int(v):
            raise ValueError(f"incr {f}: integer required")
        if kind == "max" and not _num(v):
            raise ValueError(f"max {f}: number required")
        if kind == "lww" and not (v is None or isinstance(v, (str, bool)) or _num(v)):
            raise ValueError(f"lww {f}: scalar required")
        if kind == "lww" and isinstance(v, str) and len(v) > _STR_MAX:
            raise ValueError(f"lww {f}: string too long")


def apply_op(row: Optional[dict], op: dict) -> dict:
    """Return the row after `op` (the input row is not modified). None starts an empty row."""
    new = json.loads(json.dumps(row)) if row else {}
    kind, delta, ts = op["kind"], op["delta"], int(op.get("ts") or 0)
    validate_delta(kind, delta)
    if kind == "union":
        cur = new.get(delta["field"])
        merged = set(cur if isinstance(cur, list) else []) | set(delta["add"])
        new[delta["field"]] = sorted(merged)[:UNION_CAP]
    elif kind == "incr":
        for f, v in delta.items():
            new[f] = (new[f] if _num(new.get(f)) else 0) + v
    elif kind == "max":
        for f, v in delta.items():
            new[f] = max(new[f], v) if _num(new.get(f)) else v
    else:
        st = new.setdefault("_st", {})
        stamp = [ts, str(op.get("device") or "")]
        for f, v in delta.items():
            old = st.get(f)
            if old is None or stamp > list(old):
                new[f] = v
                st[f] = stamp
    new["updated"] = max(int(new.get("updated") or 0), ts)
    return new


def fold(ops: Iterable[dict], rows: Optional[dict] = None) -> dict:
    """{(tbl, key) as 'tbl\\x1fkey': row} after applying `ops`, each op_id once. Order does not matter."""
    out = dict(rows or {})
    seen = set()
    for op in ops:
        if op["op_id"] in seen:
            continue
        seen.add(op["op_id"])
        k = f"{op['tbl']}\x1f{op['key']}"
        out[k] = apply_op(out.get(k), op)
    return out
