"""Compat shim, remove after 1.1: this module moved to app.music_brain.learning.set_import."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.learning.set_import", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.learning.set_import import *  # noqa: F401,F403
    from app.music_brain.learning.set_import import (  # noqa: F401
        _ID_TITLE, _NOT_THE_SONG, _VERSION_MIX, _why_skip, _set_audio, _safe_name, _post_upload,
        _register_offline, _analysis_via_app, _analysis_offline, _info, _BARE_ID, _HELD,
        _HELD_GUARD, _atlas_lock,
    )
