"""Compat shim, remove after 1.1: this module moved to app.music_brain.learning.set_learner."""

if __name__ == "__main__":
    import runpy

    runpy.run_module("app.music_brain.learning.set_learner", run_name="__main__", alter_sys=True)
else:
    from app.music_brain.learning.set_learner import *  # noqa: F401,F403
    from app.music_brain.learning.set_learner import (  # noqa: F401
        _scene_profile, _TS, _secs, _ARTIST_SEP, _layers, _is_url, _ydl, _fold, _norm, _words,
        _norm_words, _MASHUP, _load, _rates, _candidates, _score_at, _track_pass, _owner_grid,
        _only_sung, _drop_tracks, _vocal_jump, _merge_locked, _store_lock, _save,
        _add_user_rule_locked, _set_tempo_fn, _attach_lyrics, _set_title, _parallel, _Checkpoint,
        _plan_key, _read_tracklist, _learn_set, _final_cleanup, _study_part,
    )
