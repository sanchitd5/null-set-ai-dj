"""One maintenance command: keeps the library's stems, FLAC cache, labels, learned review,
atlas and knowledge export current, reusing each step's own function.

    python3 -m app.music_brain.maintain [--steps stems,flac,labels,review,atlas,export]
                                        [--dry-run] [--jobs N] [--max-minutes M] [--no-network]

Steps always run in the order above, whatever order --steps lists them in. JSON report on
stdout, progress on stderr, and (unless --dry-run) the report is saved to
CACHE_DIR/maintain/<timestamp>.json.

Running app policy: stems and flac REFUSE while the app answers on 127.0.0.1:$PORT (default
8000). The server's own stem worker writes the same stem folders, and flac deletes WAVs the
server may be streaming. labels, review, atlas and export run anyway: each takes its
existing lock (genre_labels merge_save lock, learned store lock, atlas lock).

stems: (a) removes the non-ft folders (<hash>_htdemucs, <hash>_htdemucs_vocals, ...) of every song
that already has a complete htdemucs_ft 4-stem set, (b) separates with htdemucs_ft every song with
no stems or only fast htdemucs stems (files over 15 min skipped), each of which then prunes its
own non-ft folders (stem_service.prune_non_ft). Cleanup only runs with the app down (above), so
no folder a live deck is reading can vanish mid-play.

Resumable: every step is idempotent (stems cached by content hash, flac skips converted
folders, atlas is incremental, labels are missing-only, review only picks sets no model has
reviewed yet), so re-running after a crash picks up where it stopped. Never downloads, never
runs the learner, never deletes user data. The only network use is the optional claudecode
backend for labels / review, which --no-network skips.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

STEPS = ("stems", "flac", "labels", "review", "atlas", "export")
MAX_TRACK_S = 15 * 60          # longer files are full albums / DJ sets: never separated
DEFAULT_PORT = 8000            # start.sh PORT default


def _log(m: str) -> None:
    print(m, file=sys.stderr, flush=True)


def app_running(port: Optional[int] = None) -> bool:
    """True when something accepts connections on the app port (the console server is up)."""
    port = port or int(os.environ.get("PORT") or DEFAULT_PORT)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def parse_steps(text: Optional[str]) -> List[str]:
    """--steps value -> the chosen steps in canonical order; ValueError on an unknown name."""
    if not text:
        return list(STEPS)
    asked = [s.strip() for s in text.split(",") if s.strip()]
    bad = [s for s in asked if s not in STEPS]
    if bad:
        raise ValueError(f"unknown step(s): {', '.join(bad)} (choose from {', '.join(STEPS)})")
    return [s for s in STEPS if s in asked]


# ---------------------------------------------------------------- counts

def _duration(analysis_path: Optional[str]) -> Optional[float]:
    try:
        d = json.loads(Path(analysis_path).read_text(encoding="utf-8")).get("duration")
        return float(d) if d is not None else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def counts(cache: Path) -> dict:
    """Read-only snapshot for the before / after report (never migrates or writes)."""
    from app.music_brain.analysis import genre_labels as gl
    from app.music_brain.atlas import pair_atlas as pa
    from app.music_brain.learning import set_learner as sl

    tracks = pa.Library(cache).tracks()
    from app.music_brain.audio import stem_service as ss

    out = {"tracks": len(tracks), "tracks_without_stems": sum(1 for t in tracks if not t["stems"])}
    ft, fast, voc = ss.DEMUCS_MODEL, ss.FAST_MODEL, f"{ss.FAST_MODEL}_vocals"
    st = {"ft_only": 0, "fast_only": 0, "both": 0, "fast_vocals_on_4stem": 0}
    for vs in stem_hashes(Path(cache) / "stems").values():
        kind = "both" if ft in vs and fast in vs else "ft_only" if ft in vs else "fast_only" if fast in vs else "other"
        st[kind] = st.get(kind, 0) + 1
        st["fast_vocals_on_4stem"] += voc in vs and (ft in vs or fast in vs)
    out["stem_sets"] = st
    root = pa._root(cache, None)
    meta = pa._meta(root) or {}
    c = {"pairs": 0, "merge": 0, "mashup": 0, "riff": 0, "double_drop": 0, "combos": 0, "merge_blocked_by_stems": 0}
    for a in meta.get("shards") or []:
        for p in pa.pairs_for(a, path=root).values():
            c["pairs"] += 1
            merge = p.get("merge") or {}
            c["merge"] += bool(merge.get("ok"))
            c["merge_blocked_by_stems"] += (not merge.get("ok")) and merge.get("gate") == "stems"
            c["mashup"] += bool((p.get("mashup") or {}).get("ok"))
            c["riff"] += bool(pa.move_of(p, "riff").get("ok"))
            c["double_drop"] += bool(pa.move_of(p, "double_drop").get("ok"))
            c["combos"] += bool(p.get("combo"))
    out["atlas"] = c
    obs = [o for e in sl.load_learned(Path(cache) / "learned_techniques.json").values() if isinstance(e, dict)
           for o in e.get("observations") or [] if isinstance(o, dict)]
    out["learned_observations"] = len(obs)
    out["learned_reviewed"] = sum(1 for o in obs if (o.get("detail") or {}).get("ai_rule"))
    genres, eras = gl.load(gl.path(cache))
    out["labels"] = {"genre": len(genres), "era": len(eras)}
    return out


# ---------------------------------------------------------------- backends

def backend_state(no_network: bool) -> dict:
    """{"backend", "ok", "note"} for the labels / review model backend."""
    from app.music_brain.llm import claudecode as cc
    from app.music_brain.learning import set_ai

    b = cc.backend(None)
    if b == "claudecode":
        if no_network:
            return {"backend": b, "ok": False, "note": "claudecode backend skipped (--no-network)"}
        try:
            cc.find_cli()
        except cc.ClaudeCodeError as exc:
            return {"backend": b, "ok": False, "note": str(exc)}
        return {"backend": b, "ok": True, "note": None}
    if os.environ.get("OLLAMA_BASE_URL") or set_ai.find_model():
        return {"backend": b, "ok": True, "note": None}
    return {"backend": b, "ok": False, "note": "no local model answering (start ./start.sh or set AI_REVIEW_BACKEND=claudecode)"}


# ---------------------------------------------------------------- steps

def separate_one(path: str) -> None:
    """4-stem Demucs, FLAC, cached by content hash (the same call agent_bridge.separate makes)."""
    from app.music_brain.audio import stem_service

    stem_service.separate(path)


def stem_hashes(stems_dir: Path) -> Dict[str, List[str]]:
    """{audio hash: [variant, ...]} from the stem folder names <sha256>_<model>[_<two_stems>]."""
    out: Dict[str, List[str]] = {}
    if Path(stems_dir).is_dir():
        for d in sorted(Path(stems_dir).iterdir()):
            h, sep, variant = d.name.partition("_")
            if d.is_dir() and sep and len(h) == 64:
                out.setdefault(h, []).append(variant)
    return out


def cleanup_non_ft(stems_dir: Path, dry_run: bool, out_of_time=lambda: False) -> dict:
    """(a) every song that already has a complete htdemucs_ft set loses its non-ft folders."""
    from app.music_brain.audio import stem_service as ss

    out = {"songs": 0, "folders": [], "bytes": 0, "errors": []}
    for h, variants in stem_hashes(stems_dir).items():
        if all(v.startswith(ss.DEMUCS_MODEL) for v in variants):
            continue
        if out_of_time():
            out["note"] = "time budget reached"
            break
        r = ss.prune_non_ft(h, stems_dir, dry_run=dry_run)
        if r.get("removed"):
            out["songs"] += 1
            out["folders"].extend(r["removed"])
            out["bytes"] += r["bytes"]
        out["errors"].extend(r.get("errors") or [])
    trash = Path(stems_dir).parent / "stems_trash"        # leftovers of a crash mid-delete
    if not dry_run and trash.is_dir():
        import shutil
        for d in trash.iterdir():
            shutil.rmtree(d, ignore_errors=True)
    out["gb"] = round(out["bytes"] / 1e9, 2)
    return out


def step_stems(ctx: dict) -> dict:
    """(a) drop non-ft stem folders of songs that have a complete ft set; (b) separate with
    htdemucs_ft every song with no stems or only fast (htdemucs) stems, which then prunes its
    non-ft folders itself (stem_service.separate -> prune_non_ft)."""
    from app.music_brain.atlas import pair_atlas as pa
    from app.music_brain.audio import stem_service as ss

    stems_dir = Path(ctx["cache"]) / "stems"
    out = {"cleanup": cleanup_non_ft(stems_dir, ctx["dry_run"], ctx["out_of_time"])}
    missing, upgrade, long_ = [], [], []
    for t in pa.Library(ctx["cache"]).tracks():
        if ss.complete_ft(t["digest"], stems_dir):
            continue
        d = _duration(t["analysis"])
        if d is not None and d > MAX_TRACK_S:
            long_.append(t)
        else:
            (upgrade if t["stems"] else missing).append(t)
    fast_bytes = sum(ss.dir_bytes(p) for t in upgrade for p in ss.non_ft_dirs(t["digest"], stems_dir))
    out |= {"missing": len(missing), "upgrade": len(upgrade), "upgrade_replaces_gb": round(fast_bytes / 1e9, 2),
            "skipped_long": [t["name"] for t in long_], "separated": 0, "failed": [], "not_started": 0}
    if ctx["dry_run"]:
        return out
    todo = missing + upgrade
    for i, t in enumerate(todo):
        if ctx["out_of_time"]():
            out["not_started"] = len(todo) - i
            out["note"] = "time budget reached"
            break
        _log(f"stems {i + 1}/{len(todo)} ({'upgrade' if t['stems'] else 'new'}): {t['name']}")
        try:
            separate_one(t["path"])
            out["separated"] += 1
        except Exception as exc:  # noqa: BLE001 -- one bad file must not stop the rest
            out["failed"].append({"name": t["name"], "error": f"{type(exc).__name__}: {exc}"[:200]})
    return out


def step_flac(ctx: dict) -> dict:
    from app.music_brain.audio import audio_convert as ac

    timer = None
    left = ctx["seconds_left"]()
    if not ctx["dry_run"] and left is not None:
        timer = threading.Timer(max(left, 0.0), ac.request_stop)
        timer.daemon = True
        timer.start()
    try:
        return ac.run(ctx["cache"], apply=not ctx["dry_run"], jobs=ctx["jobs"], log=_log)
    finally:
        if timer:
            timer.cancel()


def step_labels(ctx: dict) -> dict:
    from app.music_brain.analysis import genre_labels as gl

    be = ctx["backend"]
    if not ctx["dry_run"] and not be["ok"]:
        return {"skipped": be["note"]}
    return gl.label_library(missing_only=True, backend=be["backend"], dry_run=ctx["dry_run"],
                            cache_dir=ctx["cache"], log=_log)


def unreviewed_sets(learned_path: Path) -> List[str]:
    """Set ids none of whose stored observations carries an ai_rule (never reviewed by a model)."""
    from app.music_brain.learning import set_learner as sl

    by_set = sl._stored_by_set(sl.load_learned(learned_path))
    return sorted(s for s, rows in by_set.items()
                  if not any((o.get("detail") or {}).get("ai_rule") for o in rows))


def step_review(ctx: dict) -> dict:
    from app.music_brain.learning import set_learner as sl

    be = ctx["backend"]
    path = Path(ctx["cache"]) / "learned_techniques.json"
    sets = unreviewed_sets(path)
    out = {"unreviewed_sets": len(sets), "sets": []}
    if not sets:
        return out
    if not ctx["dry_run"] and not be["ok"]:
        out["skipped"] = be["note"]
        return out
    for sid in sets:
        if ctx["out_of_time"]():
            out["note"] = "time budget reached"
            break
        r = sl.review_learned(set_id=sid, backend=be["backend"], dry_run=ctx["dry_run"], path=path,
                              sidecar_dir=Path(ctx["cache"]) / "learned_review", log=_log)
        out["sets"].extend(r.get("sets") or [])
    return out


def step_atlas(ctx: dict) -> dict:
    from app.music_brain.atlas import pair_atlas as pa

    meta = pa._meta(pa._root(ctx["cache"], None)) or {}
    rh = pa.rules_hash()
    plan = {"rules_changed": meta.get("rules") != rh,
            "rescore": "every pair" if meta.get("rules") != rh else "new or changed pairs only",
            "atlas_tracks": len(meta.get("track_index") or {})}
    if ctx["dry_run"]:
        return plan
    # same as `pair_atlas build`: incremental, rescored in full by build itself when the rules hash moved
    doc = pa.build(Path(ctx["cache"]), workers=ctx["jobs"], log=_log, seed_macros_to=Path(ctx["cache"]))
    return plan | {"stats": doc.get("stats"), "written": doc.get("written")}


def step_export(ctx: dict) -> dict:
    from app.music_brain.matching import knowledge as kn

    if ctx["dry_run"]:
        return {"note": "would export macros, learned store, names, labels and slim atlas to knowledge/"}
    rep = kn.export_safe(ctx["cache"], ctx.get("export_out"), log=_log)
    return rep | {"privacy_check": "refused" if "error" in rep else "passed"}


STEP_FNS: Dict[str, Callable[[dict], dict]] = {
    "stems": step_stems, "flac": step_flac, "labels": step_labels,
    "review": step_review, "atlas": step_atlas, "export": step_export,
}
NEEDS_IDLE_APP = ("stems", "flac")


# ---------------------------------------------------------------- run

def run(steps: Sequence[str], cache: Optional[Path] = None, dry_run: bool = False, jobs: Optional[int] = None,
        max_minutes: Optional[float] = None, no_network: bool = False, export_out: Optional[Path] = None,
        save: bool = True) -> dict:
    from app.music_brain.config import CACHE_DIR

    cache = Path(cache or CACHE_DIR)
    t0 = time.monotonic()
    deadline = None if max_minutes is None else t0 + max_minutes * 60

    def seconds_left() -> Optional[float]:
        return None if deadline is None else deadline - time.monotonic()

    running = app_running()
    ctx = {"cache": cache, "dry_run": dry_run, "jobs": jobs, "export_out": export_out,
           "seconds_left": seconds_left, "out_of_time": lambda: deadline is not None and time.monotonic() >= deadline}
    if {"labels", "review"} & set(steps):
        ctx["backend"] = backend_state(no_network)
    report = {"dry_run": dry_run, "steps": list(steps), "app_running": running,
              "backend": ctx.get("backend"), "before": counts(cache), "results": {}}
    for s in steps:
        if s in NEEDS_IDLE_APP and running and not dry_run:
            report["results"][s] = {"skipped": "app is running (stop it first: stems/flac write files the server uses)"}
            continue
        if ctx["out_of_time"]():
            report["results"][s] = {"skipped": "time budget reached"}
            continue
        _log(f"== {s}")
        t = time.monotonic()
        try:
            res = STEP_FNS[s](ctx)
        except Exception as exc:  # noqa: BLE001 -- a failed step is reported, the next ones still run
            res = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        res["seconds"] = round(time.monotonic() - t, 1)
        report["results"][s] = res
    report["after"] = report["before"] if dry_run else counts(cache)
    report["seconds"] = round(time.monotonic() - t0, 1)
    if save and not dry_run:
        d = cache / "maintain"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{time.strftime('%Y-%m-%d_%H%M%S')}.json"
        p.write_text(json.dumps(report, indent=2), encoding="utf-8")
        report["saved"] = str(p)
    return report


def _positive(kind):
    def f(s: str):
        v = kind(s)
        if not v > 0:
            raise argparse.ArgumentTypeError("must be > 0")
        return v
    return f


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m app.music_brain.maintain", description=__doc__.splitlines()[0])
    ap.add_argument("--steps", default=None, help=f"comma list from {','.join(STEPS)} (default: all, in that order)")
    ap.add_argument("--dry-run", action="store_true", help="print what each step would do and the counts, no writes")
    ap.add_argument("--jobs", type=_positive(int), default=None, help="flac converters and atlas workers")
    ap.add_argument("--max-minutes", type=_positive(float), default=None, help="stop starting new work after M minutes")
    ap.add_argument("--no-network", action="store_true", help="skip the claudecode backend for labels / review")
    ap.add_argument("--cache-dir", type=Path, default=None, help="default: data/cache (config.CACHE_DIR)")
    a = ap.parse_args(argv)
    if a.cache_dir and "app.music_brain.config" not in sys.modules:
        os.environ["AIDJ_CACHE_DIR"] = str(a.cache_dir)   # so separation writes the same cache it reads
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    try:
        steps = parse_steps(a.steps)
        res = run(steps, cache=a.cache_dir, dry_run=a.dry_run, jobs=a.jobs,
                  max_minutes=a.max_minutes, no_network=a.no_network)
    except Exception as exc:  # noqa: BLE001 -- one JSON error line, never a traceback on stdout
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"[:400]}, indent=2))
        return 1
    print(json.dumps(res, indent=2))
    return 1 if any("error" in r for r in res["results"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
