"""The ONE adapter between the public server and the private atlas builder.

The DJ knowledge wiki (builder/kb/DJ) is private for the same reason; KB_DIR points at it.

The code that builds, scores and seeds the pair atlas (pair_atlas, atlas_store, macros,
studied_combos, vetoes and their rules/seed data) lives in the private sanchitd5/dj-atlas repo, under
builder/. That repo is the git submodule at app/music_brain/knowledge, so the builder is at
app/music_brain/knowledge/builder when the submodule is checked out.

This module makes `app.music_brain.atlas.<module>` resolve to builder/atlas/<module>.py when the
submodule is there, and nothing else: a public clone or CI run without the submodule has no atlas
features (AVAILABLE is False), the server still starts, and tests that need the builder skip with
SKIP_REASON. Nothing about how the atlas is built is in this repo.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, List

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"
BUILDER_DIR = KNOWLEDGE_DIR / "builder"
PACKAGE_DIR = BUILDER_DIR / "atlas"
KB_DIR = BUILDER_DIR / "kb" / "DJ"          # the DJ knowledge wiki (markdown), read by knowledge_parser / dj_knowledge

AVAILABLE = (PACKAGE_DIR / "pair_atlas.py").is_file()
SKIP_REASON = ("needs_atlas: the private dj-atlas submodule (app/music_brain/knowledge/builder) "
               "is not checked out")


def extend_package(path: List[str]) -> bool:
    """Called by app.music_brain.atlas: add the builder's atlas/ folder to the package path."""
    if AVAILABLE and str(PACKAGE_DIR) not in path:
        path.append(str(PACKAGE_DIR))
    return AVAILABLE


def is_atlas_import_error(exc: BaseException) -> bool:
    """True for the ModuleNotFoundError raised by importing an atlas module without the builder."""
    name = getattr(exc, "name", None) or ""
    if not AVAILABLE and isinstance(exc, ImportError) and "app.music_brain.atlas" in str(exc):
        return True
    return (not AVAILABLE and isinstance(exc, ModuleNotFoundError)
            and (name == "app.music_brain.atlas" or name.startswith("app.music_brain.atlas.")))


def register_jobs(register: Callable) -> bool:
    """Let the private builder register its background jobs (the job runner passes its `register`). The builder's
    own module, builder/atlas/jobs.py, is optional: without it, or without the submodule, nothing is registered.
    Any failure is logged and swallowed, so the server always starts. True when the module registered jobs."""
    if not AVAILABLE:
        return False
    try:
        import importlib
        mod = importlib.import_module("app.music_brain.atlas.jobs")
    except ModuleNotFoundError as exc:
        if (exc.name or "").startswith("app.music_brain.atlas"):
            return False                           # this builder has no jobs module yet
        print(f"[atlas] jobs module failed to import: {exc}", flush=True)
        return False
    except Exception as exc:                       # noqa: BLE001 -- the server must start
        print(f"[atlas] jobs module failed to import: {type(exc).__name__}: {exc}", flush=True)
        return False
    try:
        mod.register(register)
    except Exception as exc:                       # noqa: BLE001
        print(f"[atlas] job registration failed: {type(exc).__name__}: {exc}", flush=True)
        return False
    return True


def register_appliers(register: Callable) -> bool:
    """Let the private builder register the op-log appliers that materialise synced ops into graph.db:
    register(tbl, fn(op)). The builder's module is builder/atlas/sync_apply.py and is optional; without it, or
    without the submodule, ops are still logged and relayed. Failures are logged and swallowed. True when it registered."""
    if not AVAILABLE:
        return False
    try:
        import importlib
        mod = importlib.import_module("app.music_brain.atlas.sync_apply")
    except ModuleNotFoundError as exc:
        if (exc.name or "").startswith("app.music_brain.atlas"):
            return False                           # this builder has no sync appliers yet
        print(f"[atlas] sync_apply module failed to import: {exc}", flush=True)
        return False
    except Exception as exc:                       # noqa: BLE001 -- the server must start
        print(f"[atlas] sync_apply module failed to import: {type(exc).__name__}: {exc}", flush=True)
        return False
    try:
        mod.register(register)
    except Exception as exc:                       # noqa: BLE001
        print(f"[atlas] applier registration failed: {type(exc).__name__}: {exc}", flush=True)
        return False
    return True


def build_graph(cache_dir=None, log: Callable[[str], None] = print) -> dict:
    """Rebuild graph.db through the builder's build_graph(cache_dir, log). ValueError when there is no atlas,
    ImportError when the builder does not have the graph module (an older dj-atlas pin)."""
    if not AVAILABLE:
        raise ImportError(SKIP_REASON)
    from app.music_brain.atlas import graph_build
    return graph_build.build_graph(cache_dir, log)


def graph_view(name: str, *args, **kwargs):
    """Read-only graph.db views for the admin graph page (stats, find, node_detail, neighbours in the builder's
    graph_store). None when there is no atlas, no graph.db, or this dj-atlas pin predates the views."""
    if not AVAILABLE:
        return None
    try:
        from app.music_brain.atlas import graph_store
        fn = getattr(graph_store, name)
    except (ImportError, AttributeError):
        return None
    return fn(*args, **kwargs)


# ---------------------------------------------------------------- lyric remix CLI (./build-lyric-remix.sh)

def _lyric_remix_module():
    """The builder's lyric_remix module (ImportError when there is no atlas or this dj-atlas pin predates it)."""
    if not AVAILABLE:
        raise ImportError(SKIP_REASON)
    from app.music_brain.atlas import lyric_remix
    return lyric_remix


def seed_published_plans(cache_dir, out=None, mod=None) -> int:
    """Mark every plan already in the published lyric_remix document as judged-and-accepted in the local resumable state, so
    a machine with no state of its own (the Mac, a fresh checkout) never asks the model about a candidate that is already
    published, and the incremental build keeps those plans instead of dropping them. Only adds; returns how many it added."""
    mod = mod or _lyric_remix_module()
    doc = mod.read_doc(Path(out) if out else mod.default_out()) or {}
    state = mod.load_state(cache_dir)
    added = 0
    for p in doc.get("plans", []):
        if not isinstance(p, dict) or not p.get("id") or p["id"] in state["judged"]:
            continue
        try:
            score = max(1, min(10, int(round(float(p.get("score") or 0.7) * 10))))
        except (TypeError, ValueError):
            score = 7
        state["judged"][p["id"]] = {"ok": True, "score": score, "why": str(p.get("why") or ""), "a": p.get("a"), "b": p.get("b")}
        added += 1
    if added:
        mod.save_state(cache_dir, state)
    return added


def keep_published_plans(before: dict, out, mod) -> int:
    """After a build: put back the published plans (and their song lines) the build dropped, because this machine's library
    lacks one of their songs. The Mac's library and the server's differ, and the two must add up, not replace each other.
    Only when both documents were built on the same rules. Returns how many plans it put back."""
    after = mod.read_doc(out) or {}
    if not before.get("plans") or after.get("v") != before.get("v") or after.get("rules") != before.get("rules"):
        return 0
    have = {p.get("id") for p in after.get("plans", []) if isinstance(p, dict)}
    back = [p for p in before["plans"] if isinstance(p, dict) and p.get("id") and p["id"] not in have]
    if not back:
        return 0
    songs = dict(after.get("songs") or {})
    for p in back:
        for tid in (p.get("a"), p.get("b")):
            if tid and tid not in songs and tid in (before.get("songs") or {}):
                songs[tid] = before["songs"][tid]
    plans = [p for p in after.get("plans", []) if isinstance(p, dict)] + back
    mod.write_doc(out, dict(after, songs=songs, plans=sorted(plans, key=lambda p: p["id"])))
    return len(back)


def plan_pair_on_demand(a: str, b: str, cache_dir, chat, log: Callable[[str], None] = print, mod=None,
                        names: Callable[[str], str] = lambda t: t, max_batches: int = 2, should_stop: Callable[[], bool] = lambda: False) -> dict:
    """Lyric remix plans for ONE pair a -> b, made now (the song menu asked and no plan was precomputed). Same candidates, same
    judge and same plan store as the nightly build: the builder's cross_candidates, its judge prompt (`chat` is the server's own LLM
    call), judged answers into the resumable state, accepted plans added to lyric_remix.json.gz. Only cached line tables are used
    (no audio is read). {"ok": True, "plans": [...]} or {"ok": False, "reason": plain words}."""
    import fcntl

    mod = mod or _lyric_remix_module()
    cache_dir = Path(cache_dir)

    def no(why: str) -> dict:
        return {"ok": False, "reason": why}

    tracks = {t["id"]: t for t in mod._tracks(cache_dir) if t.get("id") in (a, b)}
    songs = {}
    for tid in (a, b):
        s = mod.load_song(tracks[tid], cache_dir, False, log, cache_only=True) if tid in tracks else None
        if s is None:
            return no(f"no lyrics transcribed yet for {names(tid)}")
        if len(mod._clean(s)) < mod.MIN_CLEAN_LINES:
            return no(f"{names(tid)} has too few clear sung lines")
        songs[tid] = s
    sa, sb = songs[a], songs[b]
    if not sa["lang"] or sa["lang"] != sb["lang"]:
        return no("the two songs are not sung in the same language")
    if not sa["bpm"] or not sb["bpm"] or abs(sa["bpm"] / sb["bpm"] - 1) > mod.CROSS_GAP_MAX:
        return no("the tempos are too far apart")
    pair = (mod._pairs(a, cache_dir) or {}).get(f"{a}>{b}") or {}
    if isinstance(pair.get("key"), (int, float)) and pair["key"] < mod.CROSS_KEY_MIN:
        return no("the keys do not fit")
    cands = mod.cross_candidates(sa, sb)
    if not cands:
        return no("no combination of their lines fits the beat")

    state = mod.load_state(cache_dir)
    todo = [c for c in cands if c["id"] not in state["judged"]]
    calls = failed = 0
    for k in range(0, len(todo), mod.JUDGE_BATCH):
        if calls >= max_batches or should_stop() or any(state["judged"].get(c["id"], {}).get("ok") for c in cands):
            break
        batch = todo[k:k + mod.JUDGE_BATCH]
        calls += 1
        ans = mod._judge(batch, chat, log)
        if ans is None:
            failed += 1
            continue
        for n, c in enumerate(batch):
            r = ans.get(n + 1)
            if r is not None:
                state["judged"][c["id"]] = {"ok": r["ok"], "score": r["score"], "why": r["why"], "a": c["a"], "b": c["b"]}
    state["calls"] = int(state.get("calls", 0)) + calls
    for tid, s in songs.items():
        state["songs"].setdefault(tid, {"sig": s["sig"], "lines": len(s["lines"])})
    state = mod.save_state(cache_dir, state)
    good = [c for c in cands if state["judged"].get(c["id"], {}).get("ok")]
    if not good:
        if failed and not any(c["id"] in state["judged"] for c in cands):
            return no("the language model did not answer in time, try again in a minute")
        return no("no lyric remix of these two songs reads well enough")
    out = mod.default_out()
    with open(cache_dir / "lyric_remix" / "state.lock", "w") as lk:           # the nightly import takes the same lock
        fcntl.flock(lk, fcntl.LOCK_EX)
        doc = mod.read_doc(out) or {"v": mod.DATA_V, "built": 0, "rules": mod.rules_hash(), "songs": {}, "plans": [], "rejected": []}
        have = {p.get("id") for p in doc.get("plans", []) if isinstance(p, dict)}
        plans = [p for p in doc.get("plans", []) if isinstance(p, dict)]
        plans += [mod._plan(c, state["judged"][c["id"]]) for c in good if c["id"] not in have]
        doc_songs = dict(doc.get("songs") or {})
        for tid, s in songs.items():
            doc_songs.setdefault(tid, {"bpm": round(s["bpm"], 2), "lang": s["lang"], "lines": s["lines"]})
        rejected = sorted(set(doc.get("rejected") or []) | {c["id"] for c in cands if state["judged"].get(c["id"], {}).get("ok") is False})
        mod.write_doc(out, dict(doc, songs=doc_songs, plans=sorted(plans, key=lambda p: p["id"]), rejected=rejected, built=int(time.time())))
    return {"ok": True, "plans": [mod._plan(c, state["judged"][c["id"]]) for c in good]}


def lyric_remix_cli(argv: List[str], mod=None, log: Callable[[str], None] = print) -> int:
    """The Mac / local entry of the lyric remix builder: `python -m app.music_brain.atlas_bridge lyric-remix [options]`.
    Resumable; plans are written to the knowledge checkout's lyric_remix.json.gz (see --help)."""
    import argparse
    import json
    import os

    ap = argparse.ArgumentParser(
        prog="build-lyric-remix.sh",
        description="Judge candidate lyric re-orderings on this machine's library and store the accepted plans.",
        epilog="Results: the accepted plans go to app/music_brain/knowledge/lyric_remix.json.gz (the dj-atlas checkout; --out "
               "changes it), the resumable state (every judged candidate) to <cache>/lyric_remix/state.json. Plans already in "
               "that file are kept and never judged again. To ship them: see docs/engineering/MAC_TOOLS.md (publish to dj-atlas).")
    ap.add_argument("--backend", choices=["local", "claudecode"], default="local",
                    help="local: the omni server start.sh runs (or OMNI_SERVER_URL); claudecode: the logged-in claude CLI")
    ap.add_argument("--queue", choices=["within", "pair", "both"], default="both",
                    help="within: one song's own lines; pair: lines of two songs; both (default)")
    ap.add_argument("--max-calls", type=int, default=None, help="stop after N judge calls (default: the builder's, 40)")
    ap.add_argument("--dry-run", action="store_true", help="counts only: what a build would work with; no model, no writes")
    ap.add_argument("--model", default=None, help="CLAUDECODE_MODEL for --backend claudecode (default haiku)")
    ap.add_argument("--fallback-local", action="store_true", help="claudecode unusable: use the local model instead of stopping")
    ap.add_argument("--only", default="", help="comma separated track ids (A songs)")
    ap.add_argument("--cache-dir", default=None, help="library cache (default: AIDJ_CACHE_DIR or data/cache)")
    ap.add_argument("--out", default=None, help="plans file (default: app/music_brain/knowledge/lyric_remix.json.gz)")
    a = ap.parse_args(argv)
    try:
        mod = mod or _lyric_remix_module()
    except ImportError as exc:
        log(f"lyric remix builder not available: {exc}")
        log("check out the private dj-atlas submodule:  git submodule update --init app/music_brain/knowledge")
        return 2
    cache = Path(a.cache_dir) if a.cache_dir else mod.default_cache_dir()
    if a.dry_run:
        mod.coverage(cache, log)
        pub = mod.read_doc(Path(a.out) if a.out else mod.default_out()) or {}
        log(f"published plans already in the file: {len(pub.get('plans', []))} (kept, never judged again)")
        return 0
    out = Path(a.out) if a.out else mod.default_out()
    before = mod.read_doc(out) or {}
    seeded = seed_published_plans(cache, out, mod)
    if seeded:
        log(f"{seeded} already published plans marked as judged (not asked again)")
    if a.model:
        os.environ["CLAUDECODE_MODEL"] = a.model
    kw = {} if a.max_calls is None else {"max_calls": a.max_calls}
    r = mod.build(cache, out=out, only=[x for x in a.only.split(",") if x] or None, backend=a.backend,
                  fallback_local=a.fallback_local, queue="all" if a.queue == "both" else a.queue, log=log, **kw)
    kept = keep_published_plans(before, out, mod)
    if kept:
        log(f"{kept} published plans of songs not in this library were kept")
    log(json.dumps({k: v for k, v in r.items() if k != "batches"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    import sys

    if sys.argv[1:2] == ["lyric-remix"]:
        raise SystemExit(lyric_remix_cli(sys.argv[2:]))
    print("usage: python -m app.music_brain.atlas_bridge lyric-remix [options]  (see ./build-lyric-remix.sh --help)")
    raise SystemExit(2)
