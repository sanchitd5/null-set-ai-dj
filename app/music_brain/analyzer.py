"""Compat shim, remove after 1.1: this module moved to app.music_brain.analysis.analyzer."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.analysis.analyzer", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.analysis.analyzer import *  # noqa: F401,F403
    from app.music_brain.analysis.analyzer import (  # noqa: F401
        _CAMELOT_MAJOR, _CAMELOT_MINOR, _MAJOR_PROFILE, _MINOR_PROFILE, _file_hash, _file_hash_impl,
        _GRID_HOP, _norm, _energy_jumps, _cache_path_for, _from_dict, _mem_index, _upgrade_lock,
        _upgrade_queue, _upgrade_started, _upgrade_worker,
    )
