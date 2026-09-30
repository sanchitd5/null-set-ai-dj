"""Compat shim, remove after 1.1: this module moved to app.music_brain.matching.knowledge_parser."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.matching.knowledge_parser", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.matching.knowledge_parser import *  # noqa: F401,F403
    from app.music_brain.matching.knowledge_parser import (  # noqa: F401
        _CAMELOT_BYPASS_RECIPES, _STEMS_REQUIRED_RECIPES, _UNLIMITED_BPM_DELTA_RECIPES,
        _strip_markdown, _parse_frontmatter, _split_sections, _derive_prerequisites, _slugify,
    )
