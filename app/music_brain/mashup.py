"""Compat shim, remove after 1.1: this module moved to app.music_brain.render.mashup."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.render.mashup", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.render.mashup import *  # noqa: F401,F403
    from app.music_brain.render.mashup import (  # noqa: F401
        _coverage, _touches_edge_section, _layer_mix,
    )
