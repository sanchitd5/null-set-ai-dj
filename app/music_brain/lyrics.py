"""Compat shim, remove after 1.1: this module moved to app.music_brain.analysis.lyrics."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.analysis.lyrics", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.analysis.lyrics import *  # noqa: F401,F403
    from app.music_brain.analysis.lyrics import (  # noqa: F401
        _LRC, _LRC_TAG, _CJK, _CREDIT, _JUNK, _FEAT, _clean_title, _fold, _tokens, _get,
        _lrclib_search, _VERSION, _cache_path, _norm,
    )
