"""Compat shim, remove after 1.1: this module moved to app.music_brain.matching.dj_knowledge."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.matching.dj_knowledge", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.matching.dj_knowledge import *  # noqa: F401,F403
    from app.music_brain.matching.dj_knowledge import (  # noqa: F401
        _GENRE_PLAYBOOKS, _clean, _section, _cap, _note, _parts,
    )
