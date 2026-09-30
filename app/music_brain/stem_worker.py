"""Compat shim, remove after 1.1: this module moved to app.music_brain.audio.stem_worker."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.audio.stem_worker", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.audio.stem_worker import *  # noqa: F401,F403
