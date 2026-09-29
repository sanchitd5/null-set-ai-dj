"""Metric deltas between two virtual-set reports.

    python3 -m app.sim.compare before/report.json after/report.json
    python3 -m app.sim.compare app/sim/baseline.json out/suite/suite.json

Accepts a report.json (scorer.score_run) or a suite.json / baseline.json (suite.py, its
`aggregate`). Prints one line per metric that changed: before, after, delta and a verdict
(BETTER / WORSE by scorer.DIRECTION, `.` for informational metrics). Exit code 1 when the
headline score got worse, so a script can gate on it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.sim.scorer import DIRECTION


def load_metrics(path) -> dict:
    return metrics_of(json.loads(Path(path).read_text(encoding="utf-8")))


def metrics_of(d: dict) -> dict:
    """The flat metric dict of a report or a suite / baseline (its aggregate), `score` included."""
    d = d.get("aggregate", d)
    m = dict(d.get("metrics") or {})
    if "score" in d:
        m["score"] = d["score"]
    return m


def deltas(a: dict, b: dict) -> list:
    """[(metric, before, after, delta, verdict)] for every numeric metric in either, changed or not."""
    rows = []
    for k in sorted(set(a) | set(b)):
        x, y = a.get(k), b.get(k)
        if isinstance(x, dict) or isinstance(y, dict) or isinstance(x, list) or isinstance(y, list):
            continue
        if not isinstance(x, (int, float)) and not isinstance(y, (int, float)):
            continue
        xv, yv = x or 0, y or 0
        d = round(yv - xv, 3)
        way = DIRECTION.get(k, 0)
        verdict = "." if way == 0 or d == 0 else ("BETTER" if d * way > 0 else "WORSE")
        rows.append((k, x, y, d, verdict))
    return rows


def render(rows: list, only_changed: bool = True) -> str:
    out = [f"{'metric':28} {'before':>10} {'after':>10} {'delta':>9}  verdict"]
    for k, x, y, d, v in rows:
        if only_changed and d == 0:
            continue
        out.append(f"{k:28} {str(x):>10} {str(y):>10} {d:>+9.3f}  {v}")
    return "\n".join(out)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        print(__doc__)
        return 2
    a, b = load_metrics(argv[0]), load_metrics(argv[1])
    rows = deltas(a, b)
    print(render(rows, only_changed="--all" not in argv))
    score = next((r for r in rows if r[0] == "score"), None)
    return 1 if score and score[4] == "WORSE" else 0


if __name__ == "__main__":
    sys.exit(main())
