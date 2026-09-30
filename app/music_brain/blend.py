"""Compat shim, remove after 1.1: this module moved to app.music_brain.render.blend."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.render.blend", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.render.blend import *  # noqa: F401,F403
    from app.music_brain.render.blend import (  # noqa: F401
        _EXIT_LABEL_BONUS, _ENTRY_LABEL_BONUS, _coverage, _label_at, _mean_energy, _curve_mean,
        _vocal_in_bars, _breakdown_drops,
    )
