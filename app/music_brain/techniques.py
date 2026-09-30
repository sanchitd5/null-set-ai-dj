"""Compat shim, remove after 1.1: this module moved to app.music_brain.matching.techniques."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.matching.techniques", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.matching.techniques import *  # noqa: F401,F403
    from app.music_brain.matching.techniques import (  # noqa: F401
        _riff_over_rap, _strip_rebuild, _vocal_handoff, _full_mashup, _eq_blend, _fold_gap,
        _KEY_SENSITIVE, _median, _spans, _move_params,
    )
