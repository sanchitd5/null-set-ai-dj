"""Compat shim, remove after 1.1: this module moved to app.music_brain.analysis.vibe."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.analysis.vibe", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.analysis.vibe import *  # noqa: F401,F403
    from app.music_brain.analysis.vibe import (  # noqa: F401
        _file_hash, _cache_path_for,
    )
