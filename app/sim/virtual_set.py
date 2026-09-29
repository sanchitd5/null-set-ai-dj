"""Silent virtual DJ set: the real console and the real server, no audio device, a virtual clock.

    python3 -m app.sim.virtual_set --replay lib-s1-long --out app/sim/out/run1
    python3 -m app.sim.virtual_set --record my-run --seed 7 --tracks 10 --mode long   # real LLM + YouTube
    python3 -m app.sim.virtual_set --record lib-1 --library --seed 1 --tracks 10      # offline, StubLLM

What runs (nothing of it is re-implemented for the sim):
  server   the production FastAPI app, on a private port, with its edges (LLM, YouTube, Demucs) served
           by a "world": live, replayed from a fixture, or the offline library (world.py)
  console  index.html's own scripts in node (js/): autopilot.js, dj-mind.js, stem-moves.js, deck-controller.js,
           riff-over-rap.js ... on a virtual clock, a small DOM and a recording Web Audio graph
  audio    synthetic files with the real songs' stem energy (synth.py), so the console's own energy
           measurements and gates see real numbers; a graph sampler estimates what would have been heard
The set is started like a user starts it (seed URL in the START box, START clicked) and stopped after
`--tracks` songs. Everything is deterministic: same fixture, same code, same report.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
from pathlib import Path
from typing import Optional

MODES = ("long", "quick", "hybrid")


def _slug(s: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "song"


def write_outputs(out: Path, world, js: dict, run: dict, report: dict) -> None:
    """events.jsonl (session log shape), songs/NN-slug/steps.jsonl (song log shape), console.jsonl,
    audible.json, run.json, report.json."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in world.events), encoding="utf-8")
    (out / "console.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n" for c in js["console"]), encoding="utf-8")
    (out / "audible.json").write_text(json.dumps(js["audible"], sort_keys=True), encoding="utf-8")
    (out / "net.jsonl").write_text("".join(json.dumps(n, sort_keys=True) + "\n" for n in js["net"]), encoding="utf-8")
    (out / "status.jsonl").write_text("".join(json.dumps(s, sort_keys=True) + "\n" for s in (js.get("status_log") or [])), encoding="utf-8")
    (out / "ui_states.json").write_text(json.dumps({"ui": js.get("ui_states"), "supermoves": js.get("supermoves"), "errors": js["errors"],
                                                    "dom_misses": js.get("dom_misses")}, sort_keys=True), encoding="utf-8")
    by_track: dict = {}
    for st in world.steps:
        by_track.setdefault(st["track_id"], []).append(st)
    from app.ui import server

    for s in run["songs"]:
        tid = next((t for t, n in server._track_names.items() if n == s["name"]), None)
        d = out / "songs" / f"{s['i'] + 1:02d}-{_slug(s['name'])}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps({k: s[k] for k in ("name", "bpm", "key", "level", "duration", "seconds")}, indent=1, sort_keys=True), encoding="utf-8")
        rows = [{"t": st["t"], "at_song": None, "deck": None, "phase": st["phase"] or "planning", "kind": st["kind"], "decision": st["decision"],
                 "why": st["why"], "inputs": st["inputs"], "result": st["result"]} for st in by_track.get(tid, [])]
        (d / "steps.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True, default=str) + "\n" for r in rows), encoding="utf-8")
    (out / "run.json").write_text(json.dumps(run, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    (out / "report.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def run_set(a: argparse.Namespace) -> dict:
    """One set. The private cache dir must be in AIDJ_CACHE_DIR before app.* is imported (main() does it)."""
    from app.music_brain import config

    run_cache = Path(os.environ["AIDJ_CACHE_DIR"]).resolve()
    if config.CACHE_DIR != run_cache:
        raise RuntimeError(f"app.music_brain.config was imported before AIDJ_CACHE_DIR was set "
                           f"({config.CACHE_DIR} != {run_cache}): run the sim as `python -m app.sim.virtual_set`")
    from app.sim import features, runlog, scorer
    from app.sim.headless import run_console, serve
    from app.sim.library import MainLibrary
    from app.sim.pool import Pool
    from app.sim.stubllm import StubLLM
    from app.sim.world import Caps, World

    pool = Pool()
    library = None
    seed, tracks, mode, occasion = a.seed, a.tracks, a.mode, a.occasion or ""
    fixtures = Path(a.fixtures) if a.fixtures else pool.root.parent
    if a.replay:
        rj = json.loads((fixtures / a.replay / "run.json").read_text(encoding="utf-8"))
        seed = a.seed if a.seed is not None else rj["seed"]
        tracks = a.tracks if a.tracks else rj.get("tracks", 10)
        mode = a.mode or rj.get("mode", "long")
        occasion = a.occasion if a.occasion is not None else rj.get("occasion", "")
        world_mode, name = "replay", a.replay
    elif a.library:
        world_mode, name = "library", a.record or "library"
    else:
        world_mode, name = "live", a.record or "live"
    seed = 1 if seed is None else seed
    tracks = tracks or 10
    mode = mode or "long"
    if mode not in MODES:
        raise SystemExit(f"--mode must be one of {MODES}")
    if world_mode != "replay":
        library = MainLibrary()
    llm = None
    if world_mode in ("library", "replay"):
        llm = StubLLM(seed, _catalog_for(world_mode, library, pool))     # the library's model / a replay's fallback
    world = World(world_mode, name, seed, run_cache, pool=pool, record=bool(a.record), llm=llm, library=library,
                  caps=Caps(max_downloads=a.max_downloads), fixtures_dir=Path(a.fixtures) if a.fixtures else None)
    if world_mode == "live":
        # the app's own model, found read-only; down = stop here, before YouTube is touched. Never the stub.
        from app.sim import llm_probe

        cookies = Path.home() / ".config" / "ai-dj" / "youtube-cookies.txt"      # start.sh's default: yt_guard uses it only after a refusal
        if cookies.is_file():
            os.environ.setdefault("YTDLP_COOKIES_FILE", str(cookies))
        ep = llm_probe.resolve()
        world.fx["llm_endpoint"] = ep.as_meta()
        world.fx["ear_server_up"] = llm_probe.ear_up()
        print(f"model: {ep.backend} {ep.model} @ {ep.base_url}; live ear server {'up' if world.fx['ear_server_up'] else 'down (rules answer)'}",
              file=sys.stderr, flush=True)
    world.install()
    try:
        seed_url = world.pick_seed(random.Random(f"sim:{seed}"))
        with serve(world) as port:
            js = run_console(port, {"seedUrl": seed_url, "mode": mode, "tracks": tracks, "seed": seed, "occasion": occasion,
                                    "maxSeconds": max(1500, tracks * 520), "echo": bool(a.echo)})
        if world.fatal:
            raise world.fatal
    finally:
        world.uninstall()
    meta = {"seed": seed, "mode": mode, "tracks_requested": tracks, "world": world.mode, "name": world.name, "occasion": occasion,
            "fixture_source": world.fx.get("source")}
    run = runlog.build_run(js, world, meta)
    try:
        from app.sim import wf_probe

        run["meta"].setdefault("wf", {})["probe"] = wf_probe.probe(world)
    except Exception as exc:                          # informational: never fail a run for it
        run["meta"].setdefault("wf", {})["probe_error"] = f"{type(exc).__name__}: {exc}"[:200]
    run["features"] = features.feature_table(js, world, run)
    report = scorer.score_run(run)
    report["features"] = run["features"]
    if a.record:
        st = world.seed_track or {"hash": None, "name": run["songs"][0]["name"] if run["songs"] else ""}
        world.save_fixture(st, {"tracks": tracks, "mode": mode, "occasion": occasion})
    if a.out:
        write_outputs(Path(a.out), world, js, run, report)
    return report


def _catalog_for(world_mode: str, library, pool) -> list:
    """The songs the stub may suggest: every pool track (replay) or every usable library song (library)."""
    from app.music_brain import energy as en

    cat = []
    if world_mode == "library":
        for t in library.tracks():
            lvl = None
            ep = library.cache / "analysis" / f"{t.hash}.energy.json"
            if ep.exists():
                d = json.loads(ep.read_text(encoding="utf-8"))
                lvl = en.level_from_raw(en._raw(d), library.library_raws())
            cat.append({"name": t.name, "bpm": t.bpm, "key": t.key, "level": lvl, "id": t.hash})
        return cat
    for h in pool.hashes():
        e = pool.load(h)
        lvl = en.level_from_raw(en._raw(e["energy"]), None) if e.get("energy") else None
        cat.append({"name": e["name"], "bpm": float(e["analysis"].get("bpm") or 0),
                    "key": (e["analysis"].get("key") or {}).get("camelot") or "", "level": lvl, "id": h})
    return cat


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m app.sim.virtual_set", description=__doc__.split("\n\n")[0])
    p.add_argument("--seed", type=int, default=None, help="RNG seed (seed track, stub picks); replay: the fixture's")
    p.add_argument("--tracks", type=int, default=0, help="songs in the set (default 10)")
    p.add_argument("--mode", choices=MODES, default=None, help="set mode (default long; replay: the fixture's)")
    p.add_argument("--occasion", default=None)
    p.add_argument("--out", default=None, help="output dir: events.jsonl, report.json, songs/*/steps.jsonl, run.json ...")
    p.add_argument("--replay", metavar="NAME", help="replay app/sim/fixtures/NAME: zero network / LLM / Demucs")
    p.add_argument("--record", metavar="NAME", help="record this run as app/sim/fixtures/NAME (live: real LLM + YouTube)")
    p.add_argument("--library", action="store_true", help="offline world: songs from DATA_DIR, StubLLM (builds fixtures without a model)")
    p.add_argument("--max-downloads", type=int, default=14, help="cap on YouTube downloads in a live run")
    p.add_argument("--fixtures", default=None, help="fixtures dir (default app/sim/fixtures)")
    p.add_argument("--echo", action="store_true", help="print the console's own log lines while it runs (debugging)")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.replay and (a.record or a.library):
        print("--replay cannot be combined with --record / --library", file=sys.stderr)
        return 2
    from app.sim.pool import sim_data_dir

    runs = sim_data_dir() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    cache = Path(tempfile.mkdtemp(prefix="run-", dir=runs))
    os.environ["AIDJ_CACHE_DIR"] = str(cache)
    try:
        report = run_set(a)
    except Exception as exc:
        from app.sim.llm_probe import LLMDown
        from app.sim.world import WorldError

        if isinstance(exc, LLMDown):
            print(f"virtual set stopped: {exc}", file=sys.stderr)
            return 4
        if isinstance(exc, WorldError):
            print(f"virtual set stopped: {exc}", file=sys.stderr)
            return 3
        raise
    finally:
        import shutil

        shutil.rmtree(cache, ignore_errors=True)
    print(json.dumps({"score": report["score"], "metrics": report["metrics"], "worst": report["worst"][:5]}, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
