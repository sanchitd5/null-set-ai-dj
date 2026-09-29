"""The coverage gate: a live feature that the baseline triggered and a new run does not is a failure,
whatever the headline score says."""
import json

from app.sim import compare, features, suite


def _report(triggered, viable=("Bass Swap",), score=10.0):
    return {"score": score, "metrics": {"score": score}, "features": {
        "triggered": list(triggered), "table": {n: {"triggered": 1, "executed": 1} for n in triggered},
        "cookbook": {"viable": list(viable), "top1": [], "total": 28}}}


def _suite(triggered, viable=("Bass Swap",), score=10.0):
    return {"aggregate": {"score": score, "metrics": {"stalls": 0}, "features": {
        "triggered": sorted(triggered), "viable_recipes": sorted(viable), "never_triggered": [], "executed": {}}}}


def test_required_groups_name_catalog_features():
    for group, names in features.REQUIRED.items():
        assert names, group
        for n in names:
            assert n in features.CATALOG, (group, n)


def test_lost_feature_and_recipe():
    before = _report(["riff_over_rap", "hook_drop", "dj_mind"], viable=("Bass Swap", "Echo Out"))
    after = _report(["hook_drop", "dj_mind"], viable=("Bass Swap",))
    assert features.lost(before, after) == ["riff_over_rap", "recipe:Echo Out"]
    assert features.lost(after, before) == []             # a feature gained is not a loss


def test_lost_reads_suite_and_report_alike():
    assert features.lost(_suite(["live_ear", "auto_sampler"]), _suite(["live_ear"])) == ["auto_sampler"]
    assert features.lost(_report(["live_ear"]), _report(["live_ear"])) == []


def test_compare_fails_when_a_feature_is_lost_even_if_the_score_improves(tmp_path, capsys):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(_report(["stem_merge", "hook_drop"], score=10.0)))
    b.write_text(json.dumps(_report(["hook_drop"], score=5.0)))
    assert compare.main([str(a), str(b)]) == 1
    assert "FEATURE LOST: stem_merge" in capsys.readouterr().out
    b.write_text(json.dumps(_report(["hook_drop", "stem_merge"], score=5.0)))
    assert compare.main([str(a), str(b)]) == 0


def test_suite_check_fails_on_a_lost_feature():
    base, now = _suite(["mashup_layer", "tempo_stems"]), _suite(["tempo_stems"])
    bad = suite.check(now, base)
    assert any("mashup_layer" in line for line in bad)
    assert suite.check(base, base) == []


def test_coverage_table_lists_never_triggered_groups():
    rows = features.coverage(_suite(["riff_over_rap", "stem_bridge"]))
    assert rows["riff x rap"]["never"] == []
    assert rows["sampler"]["never"] == ["auto_sampler"]
    assert "| sampler |" in features.coverage_markdown(_suite(["stem_bridge"]))
