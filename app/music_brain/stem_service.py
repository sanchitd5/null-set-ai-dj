"""Compat shim, remove after 1.1: this module moved to app.music_brain.audio.stem_service."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.audio.stem_service", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.audio.stem_service import *  # noqa: F401,F403
    from app.music_brain.audio.stem_service import (  # noqa: F401
        _cache_dir_for, _manifest_path, _load_from_cache, _demucs_out, _detect_device,
    )
