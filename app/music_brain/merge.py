"""Compat shim, remove after 1.1: this module moved to app.music_brain.render.merge."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.render.merge", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.render.merge import *  # noqa: F401,F403
    from app.music_brain.render.merge import (  # noqa: F401
        _slice, _stretch, _ask_omni,
    )
