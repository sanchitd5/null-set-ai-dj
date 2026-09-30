"""The sim's counters for waveform-derived parameters (runlog.wf_summary, scorer metrics)."""
from app.sim import runlog, scorer


def _events():
    return [
        {"kind": "derived_params", "move": "riff_lines", "measured": 2, "fallback": 0,
         "fit": {"hold_active": [1.0, 0.9], "hold_active_fixed": [0.5, 0.7], "dropout_active": 1.0, "dropout_active_fixed": 0.6}},
        {"kind": "derived_params", "move": "mashup_layer", "measured": 1, "fallback": 1},
        {"kind": "track", "data": {}},
    ]


def test_wf_summary_counts_measured_and_fallback_and_the_gap_fit():
    s = runlog.wf_summary(_events())
    assert s["plans"] == 2 and s["measured"] == 3 and s["fallback"] == 1
    assert s["by_move"]["mashup_layer"] == {"plans": 1, "measured": 1, "fallback": 1}
    assert s["hold_fit"] == 0.95 and s["hold_fit_fixed"] == 0.6                  # the picked bars carry more rap than the fixed ones
    assert s["dropout_fit"] == 1.0 and s["dropout_fit_fixed"] == 0.6
    assert runlog.wf_summary([])["plans"] == 0


def test_scorer_reports_them_as_informational_metrics():
    run = {"songs": [], "transitions": [{"i": 1, "from": "a", "to": "b", "bass_overlap_s": 2.5, "vocal_clash_s": 1.0,
                                          "recipe_planned": "x", "recipe_executed": "x", "plan_parsed": True}],
           "counters": {}, "meta": {"wf": {**runlog.wf_summary(_events()), "probe": {"hold_fit": 0.9, "level_spread": 0.12, "measured": 5, "fallback": 0}}}}
    m = scorer.score_run(run, artists_of=lambda n: set(), identity=lambda n: n)["metrics"]
    assert (m["wf_params_measured"], m["wf_params_fallback"], m["wf_measured_share"]) == (3, 1, 0.75)
    assert m["bass_overlap_seconds"] == 2.5 and m["vocal_clash_seconds"] == 1.0
    assert m["wfp_hold_fit"] == 0.9 and m["wfp_level_spread"] == 0.12
    plain = scorer.score_run({**run, "meta": {}}, artists_of=lambda n: set(), identity=lambda n: n)
    assert plain["metrics"]["wf_measured_share"] == 0.0
    assert plain["score"] == scorer.score_run(run, artists_of=lambda n: set(), identity=lambda n: n)["score"]   # never in the score
