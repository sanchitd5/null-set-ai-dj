"""Compat shim, remove after 1.1: this module moved to app.music_brain.audio.audio_convert."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.audio.audio_convert", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.audio.audio_convert import *  # noqa: F401,F403
    from app.music_brain.audio.audio_convert import (  # noqa: F401
        _default_cache, _subtype_for, _stem_wavs, _keylock_wavs, _finish_dir, _clean_tmp, _result,
        _crashed, _stop, _worker_init, _pool_run, _run_parallel, _positive_int, _positive_float,
    )
