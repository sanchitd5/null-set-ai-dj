"""The sim's informational report on the batch C artist moves (app/sim/features.py artist_moves_report)."""
from collections import defaultdict

from app.sim import features


def _F():
    return defaultdict(features._F)


def test_artist_report_counts_runs_checks_and_release():
    js = {
        "events": [
            {"type": "ai-activity", "detail": {"kind": "artist_move", "move": "slip_loop", "grid_err_s": 0.0, "beats": 16, "cap_beats": 16}},
            {"type": "ai-activity", "detail": {"kind": "artist_move_release", "move": "slip_loop", "shadow_err_s": 0.0, "line_err_s": 0.004}},
            {"type": "ai-activity", "detail": {"kind": "artist_move", "move": "cue_tease", "grid_err_s": 0.05, "beats": 4, "cap_beats": 8}},
            {"type": "ai-activity", "detail": {"kind": "artist_move_release", "move": "slip_loop", "shadow_err_s": 0.3, "line_err_s": 0.3}},
            {"type": "ai-activity", "detail": {"kind": "learned_move", "move": "vocal_loop"}},
        ],
        "audible": {"artist": [{"move": "slip_loop", "dead_air_s": 0.0, "bass_overlap_s": 0.0},
                               {"move": "cue_tease", "dead_air_s": 1.5, "bass_overlap_s": 2.0}]},
    }
    F = _F()
    rep = features.artist_moves_report(js, F)
    assert rep["fired"] == {"slip_loop": 1, "cue_tease": 1, "roll": 0, "perc_bridge": 0, "pad_lead": 0, "chant_gate": 0}
    s, t = F["artist_slip_loop"], F["artist_cue_tease"]
    assert s.checks["release_on_shadow"] == {"pass": 1, "fail": 1}
    assert s.checks["within_cap"] == {"pass": 1, "fail": 0}
    assert t.checks["on_beat_grid"] == {"pass": 0, "fail": 1}
    assert t.checks["no_dead_air"] == {"pass": 0, "fail": 1} and t.checks["one_sub_bass_owner"] == {"pass": 0, "fail": 1}
    assert rep["dead_air_introduced_s"] == 1.5 and rep["sub_overlap_s"] == 2.0


def test_artist_moves_are_catalogued_but_not_required():
    for k in features.ARTIST_MOVES:
        assert f"artist_{k}" in features.CATALOG
    required = {n for names in features.REQUIRED.values() for n in names}
    assert not any(n.startswith("artist_") for n in required), "informational only: no coverage gate"
