"""Compat shim, remove after 1.1: this module moved to app.music_brain.learning.learn_cleanup."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.learning.learn_cleanup", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.learning.learn_cleanup import *  # noqa: F401,F403
    from app.music_brain.learning.learn_cleanup import (  # noqa: F401
        _LEFTOVER, _AUDIO, _inside, _size,
    )
