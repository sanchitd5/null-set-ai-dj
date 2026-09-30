"""Compat shim, remove after 1.1: this module moved to app.music_brain.analysis.tempo."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.analysis.tempo", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.analysis.tempo import *  # noqa: F401,F403
    from app.music_brain.analysis.tempo import (  # noqa: F401
        _beat_track, _beat_regression_bpm, _lag_corr,
    )
