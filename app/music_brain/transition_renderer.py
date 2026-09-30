"""Compat shim, remove after 1.1: this module moved to app.music_brain.render.transition_renderer."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.render.transition_renderer", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.render.transition_renderer import *  # noqa: F401,F403
    from app.music_brain.render.transition_renderer import (  # noqa: F401
        _load_window, _linear_crossfade, _render_bass_swap, _render_echo_out, _render_cut,
        _render_filter_transition, _generic_eq_blend, _RECIPE_RENDERERS, _resolve_renderer,
    )
