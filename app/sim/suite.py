"""Run the fixed panel of recorded sets and score them; the regression gate of the improvement loop.

    python3 -m app.sim.suite                       # replay every run in app/sim/panel.json
    python3 -m app.sim.suite --check               # ... and exit 1 when it is worse than baseline.json
    python3 -m app.sim.suite --update-baseline     # accept this result as the new baseline.json
    python3 -m app.sim.suite --record-panel        # (re)record the panel with the REAL model + YouTube (the baseline)
    python3 -m app.sim.suite --build-panel         # offline panel (library world, StubLLM): tests / a machine without the model

Each panel entry is a fixture under app/sim/fixtures/<name>/ replayed with zero network. The
sets are independent processes (parallel, --jobs). Output (default app/sim/out/suite/):
suite.json (per-run scores and metrics + the aggregate), suite.md (a short table),
<name>/report.json and events.jsonl per run.

The aggregate is the mean of each numeric metric over the runs (`score` too). `--check` fails when
  * the mean score is worse than the baseline's by more than TOL_SCORE (absolute) or TOL_REL (relative), or
  * a hard metric got worse at all: tempo_over_cap, key_clash_blends, repeat_songs, stalls, or
  * a feature the baseline triggered somewhere in the panel is not triggered any more (features.lost).
`replay_misses > 0` is reported: the set left its recording (a rule change picked other songs and
no recorded reply fits), so its score is measured on a different set than the baseline's.
`replay_drift` counts replies recovered by subject after a prompt was reworded: harmless.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.sim import compare, features

SIM = Path(__file__).resolve().parent
PANEL = SIM / "panel.json"
BASELINE = SIM / "baseline.json"
DEFAULT_OUT = SIM / "out" / "suite"
TOL_SCORE, TOL_REL = 0.15, 0.03
HARD = ("tempo_over_cap", "key_clash_blends", "repeat_songs", "stalls")


def load_panel(path: Path = PANEL) -> list:
    return json.loads(path.read_text(encoding="utf-8"))["runs"]


def _run(entry: dict, out: Path) -> dict:
    cmd = [sys.executable, "-m", "app.sim.virtual_set", "--replay", entry["name"], "--out", str(out / entry["name"])]
    p = subprocess.run(cmd, cwd=SIM.parent.parent, capture_output=True, text=True)
    rep = out / entry["name"] / "report.json"
    if p.returncode != 0 or not rep.exists():
        raise RuntimeError(f"{entry['name']}: virtual set failed (rc {p.returncode}): {p.stderr.strip()[-400:]}")
    return json.loads(rep.read_text(encoding="utf-8"))


def aggregate(reports: dict) -> dict:
    """Mean of every numeric metric over the runs; recipe counts summed."""
    keys, sums, n = [], {}, len(reports)
    recipes: dict = {}
    for r in reports.values():
        for k, v in r["metrics"].items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                sums[k] = sums.get(k, 0.0) + v
            elif k == "recipes":
                for name, c in v.items():
                    recipes[name] = recipes.get(name, 0) + c
    metrics = {k: round(v / n, 3) for k, v in sorted(sums.items())}
    bd: dict = {}
    for r in reports.values():
        for k, v in (r.get("breakdown") or {}).items():
            bd[k] = bd.get(k, 0.0) + v
    breakdown = {k: round(v / n, 3) for k, v in sorted(bd.items(), key=lambda kv: (-kv[1], kv[0]))}
    return {"runs": n, "score": round(sum(r["score"] for r in reports.values()) / n, 3), "metrics": metrics,
            "breakdown": breakdown, "recipes": dict(sorted(recipes.items())), "features": feature_union(reports)}


def feature_union(reports: dict) -> dict:
    """Which live features the panel exercised as a whole: triggered / executed totals per catalog feature,
    the cookbook recipes the matcher scored on any played pair, and the features no seed reached."""
    trig: dict = {}
    execd: dict = {}
    viable: set = set()
    for r in reports.values():
        f = r.get("features") or {}
        for name, row in (f.get("table") or {}).items():
            trig[name] = trig.get(name, 0) + row["triggered"]
            execd[name] = execd.get(name, 0) + row["executed"]
        viable |= set((f.get("cookbook") or {}).get("viable") or [])
    return {"triggered": sorted(k for k, v in trig.items() if v), "executed": dict(sorted(execd.items())),
            "never_triggered": sorted(k for k in features.CATALOG if not trig.get(k)), "viable_recipes": sorted(viable)}


def run_panel(panel: list, out: Path, jobs: int = 4) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        results = list(ex.map(lambda e: _run(e, out), panel))
    reports = {e["name"]: r for e, r in zip(panel, results)}
    suite = {"version": 2, "score_direction": "lower is better", "aggregate": aggregate(reports),
             "runs": {n: {"score": r["score"], "metrics": r["metrics"], "worst": r["worst"][:3], "fixture_source": (r.get("meta") or {}).get("fixture_source"),
                          "llm": (r.get("meta") or {}).get("llm"),
                          "triggered": (r.get("features") or {}).get("triggered") or []} for n, r in sorted(reports.items())}}
    return suite


def markdown(suite: dict, baseline: dict | None) -> str:
    agg = suite["aggregate"]
    lines = ["| run | score | key clash | dead air s | tempo jumps | unlocked s | mismatch | stalls | misses |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, r in suite["runs"].items():
        m = r["metrics"]
        lines.append(f"| {name} | {r['score']} | {m['key_clash_count']} | {m['dead_air_seconds']} | {m['tempo_jumps']} | "
                     f"{m['unlocked_overlap_seconds']} | {m['plan_exec_mismatch']} | {m['stalls']} | {m['replay_misses']} |")
    am = agg["metrics"]
    lines.append(f"| **mean** | **{agg['score']}** | {am['key_clash_count']} | {am['dead_air_seconds']} | {am['tempo_jumps']} | "
                 f"{am['unlocked_overlap_seconds']} | {am['plan_exec_mismatch']} | {am['stalls']} | {am['replay_misses']} |")
    lines += ["", "Feature coverage (union over the panel):", "", features.coverage_markdown(suite)]
    never = agg.get("features", {}).get("never_triggered") or []
    if never:
        lines += [f"Never triggered by any seed: {', '.join(never)}", ""]
    if baseline:
        rows = compare.deltas(compare.metrics_of(baseline), compare.metrics_of(suite))
        lines += ["", "Against baseline.json:", "", "```", compare.render(rows), "```"]
    return "\n".join(lines) + "\n"


def check(suite: dict, baseline: dict) -> list:
    """Reasons the suite is worse than the baseline (empty = fine)."""
    bad = []
    a, b = baseline["aggregate"], suite["aggregate"]
    if b["score"] > a["score"] + max(TOL_SCORE, TOL_REL * a["score"]):
        bad.append(f"mean score {a['score']} -> {b['score']} (tolerance {max(TOL_SCORE, TOL_REL * a['score']):.2f})")
    for k in HARD:
        if b["metrics"].get(k, 0) > a["metrics"].get(k, 0) + 1e-9:
            bad.append(f"{k} {a['metrics'].get(k, 0)} -> {b['metrics'].get(k, 0)}")
    for name in features.lost(baseline, suite):
        bad.append(f"feature lost: {name} was triggered in the baseline and is not any more")
    return bad


def record_panel(panel: list) -> int:
    """Record every panel fixture with the app's real model, real YouTube search / download and Demucs.
    One run at a time (YouTube is rate limited; yt_guard and the per-run download cap still apply). Refuses to
    start when the model server is down: it never starts the model and never falls back to the stub."""
    from app.sim import llm_probe

    try:
        ep = llm_probe.resolve(publish=False)
    except llm_probe.LLMDown as exc:
        print(f"record-panel stopped: {exc}", file=sys.stderr)
        return 4
    print(f"recording {len(panel)} sets with {ep.backend} {ep.model} @ {ep.base_url}", flush=True)
    for e in panel:
        cmd = [sys.executable, "-m", "app.sim.virtual_set", "--record", e["name"], "--seed", str(e["seed"]),
               "--tracks", str(e.get("tracks", 10)), "--mode", e["mode"], "--max-downloads", str(e.get("max_downloads", 14))]
        p = subprocess.run(cmd, cwd=SIM.parent.parent, capture_output=True, text=True)
        if p.returncode != 0:
            print(f"{e['name']}: record failed (rc {p.returncode}): {p.stderr.strip()[-500:]}", file=sys.stderr)
            return p.returncode
        print(f"recorded {e['name']}", flush=True)
    return 0


def build_panel(panel: list, jobs: int = 2) -> None:
    """Record every panel fixture offline (library world: songs from DATA_DIR, seeded StubLLM)."""
    def one(e):
        cmd = [sys.executable, "-m", "app.sim.virtual_set", "--library", "--record", e["name"], "--seed", str(e["seed"]),
               "--tracks", str(e.get("tracks", 10)), "--mode", e["mode"]]
        p = subprocess.run(cmd, cwd=SIM.parent.parent, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"{e['name']}: {p.stderr.strip()[-400:]}")
        print(f"recorded {e['name']}")
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        list(ex.map(one, panel))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m app.sim.suite", description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--panel", default=str(PANEL))
    ap.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    ap.add_argument("--check", action="store_true", help="exit 1 when worse than baseline.json")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--build-panel", action="store_true", help="(re)record the panel offline first (library world, StubLLM)")
    ap.add_argument("--record-panel", action="store_true", help="(re)record the panel with the real model + YouTube first")
    ap.add_argument("--allow-stub", action="store_true", help="let --update-baseline accept a panel recorded with the StubLLM")
    a = ap.parse_args(argv)
    panel = load_panel(Path(a.panel))
    if a.record_panel:
        rc = record_panel(panel)
        if rc:
            return rc
    if a.build_panel:
        build_panel(panel, jobs=min(a.jobs, 2))
    suite = run_panel(panel, Path(a.out), a.jobs)
    base = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else None
    out = Path(a.out)
    (out / "suite.json").write_text(json.dumps(suite, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    md = markdown(suite, base)
    (out / "suite.md").write_text(md, encoding="utf-8")
    print(md)
    if a.update_baseline:
        sources = sorted({r.get("fixture_source") or "?" for r in suite["runs"].values()})
        if sources != ["live"] and not a.allow_stub:
            print(f"refusing to write baseline.json: the panel's fixtures come from {sources}, not the real model "
                  "(--record-panel; --allow-stub writes a provisional baseline)", file=sys.stderr)
            return 5
        suite["fixture_sources"] = sources
        BASELINE.write_text(json.dumps(suite, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline written: {BASELINE}")
        return 0
    if a.check:
        if base is None:
            print("no baseline.json to check against; run with --update-baseline first")
            return 2
        bad = check(suite, base)
        for line in bad:
            print("REGRESSION:", line)
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
