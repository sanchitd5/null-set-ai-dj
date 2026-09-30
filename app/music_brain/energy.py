"""Compat shim, remove after 1.1: this module moved to app.music_brain.analysis.energy."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.analysis.energy", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.analysis.energy import *  # noqa: F401,F403
    from app.music_brain.analysis.energy import (  # noqa: F401
        _norm, _raw, _hash_of, _cache, _lib_cache, _library_raws_impl,
    )
