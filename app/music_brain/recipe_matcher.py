"""Compat shim, remove after 1.1: this module moved to app.music_brain.matching.recipe_matcher."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.matching.recipe_matcher", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.matching.recipe_matcher import *  # noqa: F401,F403
    from app.music_brain.matching.recipe_matcher import (  # noqa: F401
        _scene_profile, _CAMELOT_RE, _EXIT_SECTIONS, _ENTRY_SECTIONS, _HIGH_ENERGY_RECIPES,
        _LOW_ENERGY_RECIPES, _INSTANT_RECIPES, _SLOW_RECIPES, _CUT_RE, _DROP_RECIPES,
        _BREAKDOWN_ECHO_RECIPES, _EXIT_ROLES, _parse_camelot, _section_at, _on_grid, _is_entry_role,
        _phrase_transition_window, _explain,
    )
