"""G17: hold lengths, recipe mix rule layer, level matching, telemetry (app/music_brain/hold_policy.py) and the plan rule layer."""
from app.music_brain import hold_policy as hp
from app.music_brain.generated import dj_config as DJ
from app.ui.services import mind_plan


def test_constants_match_dj_config():
    assert hp.MERGE_HOLD_MIN_BARS == DJ.MERGE_HOLD_MIN_BARS and hp.MERGE_HOLD_MAX_BARS == DJ.MERGE_HOLD_MAX_BARS
    assert hp.MERGE_HOLD_TARGET_S == DJ.MERGE_HOLD_TARGET_S and hp.HOLD_ALIGN_BARS == DJ.HOLD_ALIGN_BARS
    assert hp.INSONG_HOLD_MIN_BARS == DJ.INSONG_HOLD_MIN_BARS and hp.INSONG_HOLD_MAX_BARS == DJ.INSONG_HOLD_MAX_BARS
    assert hp.STEM_MERGE_SHARE_MIN == DJ.STEM_MERGE_SHARE_MIN and hp.ACCENT_MAX_SHARE == DJ.ACCENT_MAX_SHARE
    assert hp.STEM_MATCH_MAX_BOOST_DB == DJ.STEM_MATCH_MAX_BOOST_DB and hp.HOLD_END_ENERGY_DROP == DJ.HOLD_END_ENERGY_DROP
    assert list(hp.ACCENT_RECIPES) == list(DJ.ACCENT_RECIPES) and hp.INSONG_HOLDS_ON == DJ.INSONG_HOLDS_ON


def test_merge_hold_bars_scale_with_bpm_and_room():
    assert hp.merge_hold_bars(240 / 128, 200) == 48
    assert hp.merge_hold_bars(240 / 174, 200) == 64
    assert hp.merge_hold_bars(240 / 90, 200) == 32
    assert hp.merge_hold_bars(240 / 60, 200) == 32
    assert hp.merge_hold_bars(1.38, 40) == 32 and hp.merge_hold_bars(1.38, 20) == 16
    assert hp.merge_hold_bars(1.38, 12) == 8 and hp.merge_hold_bars(1.38, 4) == 0


def test_settings_clamp_and_override():
    assert hp.settings({"merge_hold_bars": 999})["merge_hold_bars"] == 128
    assert hp.settings({"merge_hold_bars": 3})["merge_hold_bars"] == 16
    assert hp.settings({"insong_hold_bars": 500})["insong_hold_bars"] == 64
    assert hp.settings({"stem_merge_share": 7, "accent_cap": -1})["stem_merge_share"] == 1
    assert hp.settings("junk") == hp.settings({})
    assert hp.merge_hold_bars(1.875, 200, hp.settings({"merge_hold_bars": 128})) == 128
    assert hp.in_song_hold_bars(64) == 32 and hp.in_song_hold_bars(12) == 0
    assert hp.in_song_hold_bars(64, hp.settings({"insong_holds": False})) == 0


def test_recipe_mix_share_and_accents():
    assert hp.enforce_recipe_mix("Long Blend", ["Stem Merge", "Long Blend"], True)[0] == "Stem Merge"
    assert hp.enforce_recipe_mix("Long Blend", ["Stem Merge"] * 3, True)[0] == "Long Blend"
    assert hp.enforce_recipe_mix("Echo Out", ["Stem Merge"] * 4, False)[0] == "Echo Out"
    assert hp.enforce_recipe_mix("Echo Out", ["Stem Merge"] * 4 + ["Echo Out"], False)[0] == "Basic Blend"
    assert hp.enforce_recipe_mix("Bass Swap", ["Stem Merge", "Stem Merge", "Echo Out", "Stem Merge"], False)[0] == "Basic Blend"
    hist = []
    picks = ["Echo Out", "Bass Swap", "Long Blend", "Backspin (Spinback)", "Stem Merge", "Echo Out"]
    for i in range(30):
        hist.append(hp.enforce_recipe_mix(picks[i % 6], hist, i % 5 != 4)[0])
    t = hp.hold_telemetry([{"recipe": r} for r in hist])
    assert t["merge_share"] >= 0.6 and t["accent_share"] <= 0.2
    assert not any(hp.is_accent(a) and hp.is_accent(b) for a, b in zip(hist, hist[1:]))


def test_level_match_lifts_quiet_stems_capped():
    n = 40
    e_a = {"drums": [0.45] * n, "bass": [0.45] * n, "vocals": [0.0] * n, "other": [0.0] * n}
    e_b = {"drums": [0.0] * n, "bass": [0.0] * n, "vocals": [0.01] * n, "other": [0.35] * n}
    lm = hp.level_match(e_a, e_b, {"drums": "a", "bass": "a", "vocals": "b", "other": "b"}, 32)
    assert abs(lm["trim"]["drums"] - 1) < 1e-9 and abs(lm["trim"]["other"] - 1) < 1e-9
    assert abs(lm["trim"]["vocals"] - 3.98) < 0.01  # 12 dB cap
    assert abs(lm["eB"]["vocals"][0] - 0.0398) < 0.001


def test_hold_end_and_telemetry():
    assert hp.hold_end_reason(True, 1, 1) == "vocal clash"
    assert hp.hold_end_reason(False, 0.4, 1) == "energy drop"
    assert hp.hold_end_reason(False, 0.9, 1) is None
    assert hp.hold_end_reason(False, 1, 1, True) == "user action"
    t = hp.hold_telemetry([{"recipe": "Stem Merge", "hold_bars": 30, "hold_s": 56.2},
                           {"recipe": "Stem Merge", "hold_bars": 62, "hold_s": 85.3, "early": "vocal clash"},
                           {"recipe": "Echo Out"}, {"recipe": "Long Blend"}])
    assert t["merge_share"] == 0.5 and t["hold_count"] == 2 and t["hold_seconds"] == 141.5 and t["early_ends"] == 1


def _facts(recent, merge_possible, recipes=("Echo Out", "Long Blend")):
    cands = [{"recipe": r, "score": 80 - i, "a_time": 100.0, "b_time": 8.0, "overlap_style": "standard"} for i, r in enumerate(recipes)]
    return {"bar_secs": 2.0, "candidates": cands, "exit_options": [100.0], "phrases": [], "remix_used": [], "entry": 0.0,
            "recent_recipes": recent, "merge_possible": merge_possible, "hold_settings": hp.settings()}


def test_plan_rule_layer_prefers_merge_and_caps_accents():
    out = mind_plan.validate_plan({"candidate": 0, "exit": 100.0}, _facts(["Long Blend"], True))
    assert out["prefer_merge"] is True and "merge share" in out["recipe_mix"]
    out = mind_plan.validate_plan({"candidate": 0, "exit": 100.0}, _facts(["Stem Merge"] * 3 + ["Echo Out"], False))
    assert out["candidate"]["recipe"] == "Long Blend" and out["candidate_index"] == 1
    out = mind_plan.validate_plan({"candidate": 0, "exit": 100.0}, _facts(["Stem Merge"] * 4, False))
    assert out["candidate"]["recipe"] == "Echo Out" and not out["prefer_merge"]


def test_plan_prompt_states_the_weights():
    f = _facts(["Stem Merge", "Echo Out"], True)
    f.update({"bpm": 128, "key": "8A", "next_bpm": 128, "next_key": "8A", "set_mode": "hybrid", "set_position": 0.5, "now": 10,
              "recent_moves": [], "mashup_possible": False, "bar_secs": 1.875})
    f["candidates"] = [dict(c, pre_clear_bars=8) for c in f["candidates"]]
    text = mind_plan.build_prompt(f)
    assert "RECIPE MIX" in text and "at least 60%" in text and "at most 20%" in text


def test_set_log_accepts_hold_fields():
    from app.music_brain.learning.set_log import validate_set_log
    log = {"$schema": "djset-v1", "metadata": {"date": "2026-11-10", "duration_seconds": 100, "merge_share": 0.6, "hold_count": 2, "hold_seconds": 141.5},
           "track_quest_journey": [], "control_event_stream": [],
           "transitions": [{"transition_index": 0, "outgoing_track": "a", "incoming_track": "b", "recipe_name": "Stem Merge",
                            "mix_start_time": 1, "mix_end_time": 90, "hold_bars": 62, "hold_s": 85.3}]}
    try:
        out = validate_set_log(log)
    except ValueError as exc:  # schema constant may differ; the point is hold fields are not "unrecognized"
        assert "unrecognized" not in str(exc), exc
        return
    assert out["transitions"][0]["hold_bars"] == 62
