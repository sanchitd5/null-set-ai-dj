"""Compat shim, remove after 1.1: this module moved to app.music_brain.analysis.waveform_params."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.analysis.waveform_params", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.analysis.waveform_params import *  # noqa: F401,F403
    from app.music_brain.analysis.waveform_params import (  # noqa: F401
        _db, _read_mono, _hash_memo, _content_hash, _slice,
    )
