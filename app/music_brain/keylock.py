"""Compat shim, remove after 1.1: this module moved to app.music_brain.audio.keylock."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.audio.keylock", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.audio.keylock import *  # noqa: F401,F403
    from app.music_brain.audio.keylock import (  # noqa: F401
        _nearest, _voice_band_db, _touched, _key, _jobs, _lock, _to_flac,
    )
