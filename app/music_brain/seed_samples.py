"""Compat shim, remove after 1.1: this module moved to app.music_brain.learning.seed_samples."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.learning.seed_samples", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.learning.seed_samples import *  # noqa: F401,F403
    from app.music_brain.learning.seed_samples import (  # noqa: F401
        _fade, _kick, _clap, _hat, _click, _riser,
    )
