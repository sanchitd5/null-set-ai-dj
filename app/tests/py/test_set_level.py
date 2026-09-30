"""Set-level habits (research/notes/artist-signature-techniques.md S16-S22): sim metrics and rules."""
from app.sim import features, scorer

BPM = 128.0


def _song_with_breakdown():
    et = [i * 0.5 for i in range(600)]
    ec = [0.1 if t < 10 or t >= 280 else 0.2 if (100 <= t < 140 or 220 <= t < 260)
          else 1.0 if 140 <= t < 200 else 0.6 for t in et]
    return {"analysis": {"bpm": BPM, "duration": 300.0, "energy_times": et, "energy_curve": ec}}


def test_set_level_report_counts_variety_fx_breakdown_exits_and_overlap():
    by_name = {"A": _song_with_breakdown(), "B": _song_with_breakdown(), "C": _song_with_breakdown()}
    run = {"transitions": [{"recipe_executed": "Stem Merge", "seconds": 30.0},
                           {"recipe_executed": "Stem Merge", "seconds": 20.0},
                           {"recipe_executed": "Echo Out", "seconds": 10.0}],
           "songs": [{"seconds": 600.0}, {"seconds": 600.0}, {"seconds": 600.0}]}
    js = {"console": [{"text": "fx budget: spent wet: fx wet ok"}, {"text": "fx budget: refused wet: too many"},
                      {"text": "fx budget: refused vocal: song budget spent"}],
          "session_events": [
              {"kind": "track", "data": {"event": "transition_start", "from": "A", "to": "B", "a_pos": 120.0}},   # in the breakdown
              {"kind": "track", "data": {"event": "transition_start", "from": "B", "to": "C", "a_pos": 90.0}},    # before it
              {"kind": "track", "data": {"event": "transition_start", "from": "C", "to": "D"}}]}                  # no a_pos: skipped
    r = features.set_level_report(js, run, by_name)
    assert r["max_recipe_repeat_run"] == 2 and r["distinct_recipes"] == 2 and r["recipe_variety"] == round(2 / 3, 3)
    assert r["fx_budget_spent"] == 1 and r["fx_budget_refused"] == 2
    assert r["fx_budget_by_kind"] == {"spent": {"wet": 1}, "refused": {"vocal": 1, "wet": 1}}
    assert r["fx_density_per_30min"] == 1.0
    assert r["exits_checked"] == 2 and r["exits_in_breakdown"] == 1 and r["exits_in_breakdown_at"] == [120.0]
    assert r["overlap_seconds"] == {"n": 3, "min": 10.0, "p50": 20.0, "p90": 30.0, "max": 30.0, "mean": 20.0}


def test_s16_hit_share_target_and_note():
    from app.ui.services import autopilot_service as ap

    assert ap.hit_share_target("late night club") is None
    assert ap.hit_share_target("Wedding: all the HITS please") == ap.HIT_SHARE_HITS
    assert ap.hit_share_target("underground warehouse, fresh music") == ap.HIT_SHARE_NEW
    assert ap.hit_share_target("hits and underground") is None          # both: the brief does not choose
    assert ap.hit_share_note("anthems", 0, 1) == ""                      # too few measured songs
    assert "songs this crowd already knows" in ap.hit_share_note("anthems and bangers", 1, 4)
    assert ap.hit_share_note("anthems", 3, 4) == ""                      # 0.75, on target
    assert "lesser-known" in ap.hit_share_note("new music only", 3, 4)
    assert ap.hit_share_note("club", 0, 8) == ""                         # no target: no steering


def test_s16_fame_counts_only_uses_cached_answers(monkeypatch, tmp_path):
    from app.ui import server

    monkeypatch.setattr(server, "FAME_PATH", tmp_path / "fame.json")
    monkeypatch.setattr(server, "_fame", {"a": {"famous": True}, "b": {"famous": False}})
    monkeypatch.setattr(server, "_track_names", {"a": "X - Hit", "b": "Y - Deep", "c": "Z - Unknown"})
    assert server._fame_counts(["X - Hit", "Y - Deep", "Z - Unknown", "Not loaded"]) == (1, 2)


def test_familiar_and_edits_share():
    run = {"transitions": [], "songs": [{"name": "A - Song (Extended Mix)"}, {"name": "B - Song"}, {"name": "C - Song (VIP)"}]}
    r = features.set_level_report({"console": [], "session_events": []}, run, {}, {"A - Song (Extended Mix)": True, "B - Song": False})
    assert r["familiar_share"] == 0.5 and r["familiar_known"] == 2
    assert r["edits_share"] == round(2 / 3, 3)


def test_set_level_metrics_are_informational_not_scored():
    """The headline score must not move with the new metrics."""
    for k in scorer.SET_LEVEL_KEYS:
        assert k not in scorer.WEIGHTS
    assert scorer.DIRECTION["exits_in_breakdown"] == -1
