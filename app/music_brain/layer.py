"""Compat shim, remove after 1.1: this module moved to app.music_brain.render.layer."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.render.layer", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.render.layer import *  # noqa: F401,F403
    from app.music_brain.render.layer import (  # noqa: F401
        _coverage, _label_at, _ENTRY_BONUS, _nearest,
    )
