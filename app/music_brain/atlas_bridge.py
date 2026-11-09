"""The ONE adapter between the public server and the private atlas builder.

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
from typing import List

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"
BUILDER_DIR = KNOWLEDGE_DIR / "builder"
PACKAGE_DIR = BUILDER_DIR / "atlas"

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
