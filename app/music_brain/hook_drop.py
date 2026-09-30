"""Compat shim, remove after 1.1: this module moved to app.music_brain.analysis.hook_drop."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.analysis.hook_drop", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.analysis.hook_drop import *  # noqa: F401,F403
    from app.music_brain.analysis.hook_drop import (  # noqa: F401
        _energy,
    )
