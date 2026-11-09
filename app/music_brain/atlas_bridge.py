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
