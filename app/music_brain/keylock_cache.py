"""Compat shim, remove after 1.1: this module moved to app.music_brain.audio.keylock_cache."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.audio.keylock_cache", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.audio.keylock_cache import *  # noqa: F401,F403
    from app.music_brain.audio.keylock_cache import (  # noqa: F401
        _size, _run_lock, _last_run, _run,
    )
