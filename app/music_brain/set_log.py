"""Compat shim, remove after 1.1: this module moved to app.music_brain.learning.set_log."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.learning.set_log", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.learning.set_log import *  # noqa: F401,F403
    from app.music_brain.learning.set_log import (  # noqa: F401
        _TOP_LEVEL, _error, _number, _text, _object, _iso_date, _clock,
    )
