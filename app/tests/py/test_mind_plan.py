"""AI plan for a song pair (app/ui/services/mind_plan.py): facts, prompt, validation."""

import json
import re
from pathlib import Path

import pytest

from app.ui.services import mind_plan as mp

BPM = 120.0
BAR = 2.0  # 240 / 120
PHRASE = 8 * BAR  # 16 s


def _analysis():
    # 0-32 intro, 32-64 verse, 64-80 build, 80-112 drop, 112-144 breakdown,
    # 144-160 build, 160-192 drop, 192-224 outro. 8-bar phrases every 16 s.
    secs = [("intro", 0, 32, 0.3), ("verse", 32, 64, 0.5), ("build", 64, 80, 0.7),
            ("drop", 80, 112, 0.9), ("breakdown", 112, 144, 0.4), ("build", 144, 160, 0.7),
            ("drop", 160, 192, 0.95), ("outro", 192, 224, 0.4)]
    return {
        "bpm": BPM, "duration": 224.0,
        "key": {"camelot": "8A"},
        "sections": [{"label": l, "start": a, "end": b, "energy": e} for l, a, b, e in secs],
        "phrase_boundaries_8bar": [float(t) for t in range(0, 224, 16)],
        "vocal_active_regions": [[32.0, 64.0], [112.0, 144.0]],
    }


CANDS = [
    {"recipe": "Bass Swap", "score": 82.0, "a_time": 176.0, "b_time": 16.0, "overlap_style": "standard", "pre_clear_bars": 8},
    {"recipe": "Drop Swap", "score": 74.0, "a_time": 160.0, "b_time": 32.0, "overlap_style": "instant", "pre_clear_bars": 0},
    {"recipe": "Echo Out", "score": 60.0, "a_time": 192.0, "b_time": 0.0, "overlap_style": "slow", "pre_clear_bars": 16},
]


def _facts(**ctx):
    base = {"now": 0.0, "entry": 0.0, "window_lo": 144.0, "window_hi": 200.0,
            "set_mode": "hybrid", "set_position": 0.3, "mashup_possible": True}
    base.update(ctx)
    return mp.build_facts(_analysis(), {"bpm": 122.0, "key": {"camelot": "9A"}}, CANDS, base)


def test_facts_phrases_and_exit_options():
    f = _facts()
    assert f["exit_options"] == [144.0, 160.0, 176.0, 192.0]
    rows = {r["t"]: r for r in f["phrases"]}
    assert rows[64.0]["pre_drop"] and rows[144.0]["pre_drop"]
    assert not rows[80.0]["pre_drop"]
    assert rows[32.0]["vocal"] == 1.0
    assert len(f["candidates"]) == 3


def test_valid_plan_is_kept():
    f = _facts()
    raw = {"candidate": 0, "exit": 176.0, "candidate_reason": "keys fit",
           "moves": [{"move": "filter_build", "at": 64.0, "reason": "tension"},
                     {"move": "loop_extend", "at": 96.0, "bars": 4, "reason": "ride the drop"},
                     {"move": "preclear", "at": 160.0, "reason": "ease the sub"}]}
    p = mp.validate_plan(raw, f)
    assert p["candidate_index"] == 0 and p["exit"] == 176.0
    assert p["candidate"]["a_time"] == 176.0
    assert [m["move"] for m in p["moves"]] == ["filter_build", "loop_extend", "preclear"]
    assert p["moves"][1]["bars"] == 4
    assert p["dropped"] == [] and p["repaired"] == []


def test_times_snap_to_phrase_within_one_bar():
    p = mp.validate_plan({"candidate": 0, "exit": 177.3,
                          "moves": [{"move": "stutter", "at": 63.1}]}, _facts())
    assert p["exit"] == 176.0
    assert p["moves"][0]["at"] == 64.0


def test_bad_candidate_and_out_of_window_exit_are_repaired():
    p = mp.validate_plan({"candidate": 7, "exit": 40.0, "moves": []}, _facts())
    assert p["candidate_index"] == 0
    assert p["exit"] == 176.0  # nearest exit option to candidate 0's a_time
    assert any("candidate" in d for d in p["dropped"])
    assert any("exit" in d for d in p["dropped"])


@pytest.mark.parametrize("move, why", [
    ({"move": "moonwalk", "at": 64.0}, "unknown"),
    ({"move": "stutter", "at": 55.0}, "not a listed phrase"),
    ({"move": "stutter", "at": 96.0}, "drop lead-in"),
    ({"move": "loop_extend", "at": 16.0}, "first 16 bars"),
    ({"move": "loop_extend", "at": 144.0}, "exit"),
    ({"move": "beat_jump", "at": 80.0}, "drop"),
    ({"move": "preclear", "at": 64.0}, "8-16 bars"),
    ({"move": "subdrop", "at": 96.0}, "verse/breakdown"),
    ({"move": "hold", "at": 64.0}, "just before the exit"),
    ({"move": "loop_extend", "at": 192.0}, "after the exit"),
])
def test_invalid_moves_dropped(move, why):
    p = mp.validate_plan({"candidate": 0, "exit": 176.0, "moves": [move]}, _facts())
    assert p["moves"] == []
    assert why in p["dropped"][0]


def test_same_phrase_neighbours_and_repeat_kinds_dropped():
    raw = {"candidate": 0, "exit": 176.0, "moves": [
        {"move": "filter_build", "at": 64.0},
        {"move": "subdrop", "at": 64.0},         # same phrase
        {"move": "loop_extend", "at": 80.0},     # neighbouring remix phrase
        {"move": "filter_build", "at": 144.0},   # kind again (also exit guard)
        {"move": "loop_extend", "at": 96.0},     # fine: 2 phrases after 64
    ]}
    p = mp.validate_plan(raw, _facts(window_hi=224.0))
    assert [(m["move"], m["at"]) for m in p["moves"]] == [("filter_build", 64.0), ("loop_extend", 96.0)]
    assert len(p["dropped"]) == 3


def test_remix_cap_counts_moves_already_played():
    f = _facts(now=60.0, remix_used=["loop_extend", "beat_jump", "stutter", "echo_freeze"])
    p = mp.validate_plan({"candidate": 0, "exit": 176.0,
                          "moves": [{"move": "filter_build", "at": 64.0}]}, f)
    assert p["moves"] == [] and "cap" in p["dropped"][0]


def test_instant_recipe_never_preclears_and_beat_layer_needs_mashup():
    p = mp.validate_plan({"candidate": 1, "exit": 160.0,
                          "moves": [{"move": "preclear", "at": 144.0}]}, _facts())
    assert p["moves"] == [] and "instant" in p["dropped"][0]
    p = mp.validate_plan({"candidate": 0, "exit": 176.0,
                          "moves": [{"move": "beat_layer", "at": 96.0}]}, _facts(mashup_possible=False))
    assert p["moves"] == [] and "beat layer" in p["dropped"][0]
    p = mp.validate_plan({"candidate": 0, "exit": 176.0,
                          "moves": [{"move": "beat_layer", "at": 96.0}]}, _facts())
    assert p["moves"][0]["move"] == "beat_layer"


def test_subdrop_on_vocal_breakdown_ok_but_not_after_last_track():
    raw = {"candidate": 0, "exit": 192.0, "moves": [{"move": "subdrop", "at": 112.0}]}
    assert mp.validate_plan(raw, _facts())["moves"][0]["move"] == "subdrop"
    assert mp.validate_plan(raw, _facts(subdrop_last_track=True))["moves"] == []


def test_non_object_inputs():
    for raw in (None, [], "x"):
        p = mp.validate_plan(raw, _facts())
        assert p["candidate_index"] == 0 and p["exit"] == 176.0 and p["moves"] == []
    p = mp.validate_plan({"candidate": "zero", "moves": "lots"}, _facts())
    assert p["moves"] == [] and "moves is not a list" in p["dropped"]


# -- LLM round trip with fake model output -----------------------------------
def _fake(text):
    seen = {}

    def llm(system, user):
        seen["system"], seen["user"] = system, user
        return text
    return llm, seen


def test_plan_pair_parses_fenced_json_and_prompt_lists_options():
    body = {"candidate": 2, "exit": 192, "moves": [{"move": "echo_freeze", "at": 144, "reason": "cut"}]}
    llm, seen = _fake("Sure!\n```json\n" + json.dumps(body) + "\n```")
    p = mp.plan_pair(_facts(window_hi=224.0), llm=llm)
    assert p["parsed"] and p["candidate_index"] == 2 and p["exit"] == 192.0
    assert p["moves"] == [{"move": "echo_freeze", "at": 144.0, "reason": "cut"}]
    assert "exit_options: [144.0, 160.0, 176.0, 192.0, 208.0]" in seen["user"]
    assert "pre_drop" in seen["user"] and "JSON" in seen["system"]


def test_plan_pair_malformed_output_falls_back():
    llm, _ = _fake("I think you should loop the chorus")
    p = mp.plan_pair(_facts(), llm=llm)
    assert not p["parsed"] and p["moves"] == [] and p["candidate_index"] == 0
    llm, _ = _fake('{"candidate": 1, "exit": 999, "moves": [{"move": "stutter", "at": 64}')  # truncated
    p = mp.plan_pair(_facts(), llm=llm)
    assert not p["parsed"]


def test_plan_pair_out_of_window_exit_repaired():
    llm, _ = _fake('{"candidate": 1, "exit": 30, "moves": []}')
    p = mp.plan_pair(_facts(), llm=llm)
    assert p["parsed"] and p["candidate_index"] == 1 and p["exit"] == 160.0
    assert p["repaired"]


def test_plan_model_env(monkeypatch):
    monkeypatch.delenv("AUTOPILOT_PLAN_MODEL", raising=False)
    monkeypatch.setenv("AUTOPILOT_MODEL", "gemma3:27b")
    assert mp.plan_model() == "gemma3:27b"
    monkeypatch.setenv("AUTOPILOT_PLAN_MODEL", "gemma3:4b")
    assert mp.plan_model() == "gemma3:4b"


def test_grounding_is_compact_and_from_the_wiki():
    g = mp.grounding(["Bass Swap", "Echo Out", "Drop Swap"])
    assert 0 < len(g) <= mp.GROUNDING_MAX_CHARS
    assert "Loops" in g and "Bass Swap" in g


def test_loop_transition_step_one_caps_cycles():
    # DJ/05 Loop Transition step 1 reaches the plan prompt with its 2-cycle cap intact
    step1 = mp._recipe_lines("Loop Transition", 2)[0]
    assert "2 cycles" in step1 and "indefinitely" not in step1
    assert "2 cycles" in mp.grounding(["Loop Transition"])


def test_vocal_layer_recipes_carry_drop_line_guard():
    # DJ/05 Acapella Overlay / Live Mashup / Double Drop: the guard reaches the plan prompt
    ao = " ".join(mp._recipe_lines("Acapella Overlay", 2))
    lm = " ".join(mp._recipe_lines("Live Mashup", 2))
    dd = mp._recipe_lines("Double Drop", 2)[0]
    assert "no vocal for the whole" in ao and "before A's drop" in ao
    assert "no vocal for the whole" in lm and "end before Deck 1's drop" in lm
    assert "drop is instrumental" in dd and "Drop Swap" in dd


def test_loop_hold_rule_is_capped_not_open_ended():
    # DJ/04 Loops & Beat Jumps: a hold lasts at most one 8-bar phrase; short outro -> Echo Out / breakdown
    line = next(ln for ln in mp.move_rules().splitlines() if ln.startswith("- loop_extend"))
    assert "at most one 8-bar phrase" in line and "Echo Out" in line
    assert not re.search(r"infinite|indefinite", mp.move_rules(), re.I)


def test_constants_match_dj_mind_js():
    js = (Path(mp.__file__).parents[1] / "static" / "dj-mind.js").read_text(encoding="utf-8")
    for name in ("PHRASE_BARS", "MIN_SECTION_BARS", "MIN_BARS_ON_TRACK", "EXIT_GUARD_BARS",
                 "HOLD_BARS", "PRECLEAR_SLACK_BARS", "SUBDROP_MAX_ENERGY", "REMIX_MAX_PER_SONG",
                 "REMIX_GAP_PHRASES", "BEAT_LAYER_MIN_SCORE", "LAYER_MIN_KEY"):
        m = re.search(rf"\b{name} = ([0-9.]+)", js)
        assert m, name
        assert float(m.group(1)) == float(getattr(mp, name)), name
    m = re.search(r"const REMIX_MOVES = \[([^\]]+)\]", js)
    assert tuple(x.strip().strip('"') for x in m.group(1).split(",")) == mp.REMIX_MOVES


def test_phrase_label_uses_majority_of_short_sections():
    secs = [{"label": "verse", "start": 0, "end": 3, "energy": 0.4},
            {"label": "drop", "start": 3, "end": 9, "energy": 0.9},
            {"label": "verse", "start": 9, "end": 11, "energy": 0.4}]
    assert mp.phrase_label(secs, 0, 11) == ("drop", 0.67)
    assert mp.phrase_label([], 0, 11) == (None, None)


def test_pre_drop_matches_js():
    assert mp.is_pre_drop("verse", "drop") and mp.is_pre_drop("build", "verse")
    assert not mp.is_pre_drop("build", "build") and not mp.is_pre_drop("chorus", "drop")
    assert not mp.is_pre_drop("verse", None)


# ── PEAK moves in the LLM plan (same caps as dj-mind.js) ─────────────────────
def _peak(moves, **ctx):
    f = _facts(**{"peak_moves": True, "big_moment_ok": True, **ctx})
    return mp.validate_plan({"candidate": 0, "exit": 176.0, "moves": moves}, f)


def test_peak_moves_kept_on_their_phrases():
    p = _peak([{"move": "fakeout", "at": 64.0, "reason": "silence then slam"},
               {"move": "beat_boost", "at": 96.0, "reason": "heavier drums"},
               {"move": "peak_roll", "at": 144.0, "reason": "roll into drop 2"}])
    assert [m["move"] for m in p["moves"]] == ["fakeout", "beat_boost"]
    assert "no remix in the last 24 bars" in p["dropped"][0]      # 144 is 16 bars before 176


@pytest.mark.parametrize("moves, ctx, why", [
    ([{"move": "fakeout", "at": 64.0}], {"peak_moves": False}, "peak moves are off"),
    ([{"move": "peak_roll", "at": 64.0}], {"peak_moves": False}, "peak moves are off"),
    ([{"move": "fakeout", "at": 64.0}], {"big_moment_ok": False}, "big-moment cap"),
    ([{"move": "fakeout", "at": 80.0}], {}, "right before a drop"),
    ([{"move": "fakeout", "at": 16.0}], {}, "too close to song start"),
    ([{"move": "fakeout", "at": 64.0}, {"move": "fakeout", "at": 144.0}], {}, "already used"),
    ([{"move": "beat_boost", "at": 32.0}], {}, "only inside a drop"),
    ([{"move": "peak_roll", "at": 32.0}], {}, "only on the run-up right before a drop"),
])
def test_peak_moves_vetoed(moves, ctx, why):
    p = _peak(moves, **ctx)
    assert any(why in d for d in p["dropped"]), p["dropped"]


def test_peak_help_only_when_allowed():
    on = mp.build_prompt(_facts(peak_moves=True, big_moment_ok=True))
    off = mp.build_prompt(_facts())
    assert "fakeout" in on and "peak_roll" in on
    assert "fakeout" not in off and "beat_boost" not in off


def test_peak_roll_kept_on_a_build_before_a_drop():
    p = _peak([{"move": "peak_roll", "at": 64.0, "reason": "roll into drop 1"}])
    assert [m["move"] for m in p["moves"]] == ["peak_roll"], p["dropped"]


def test_drop_hit_from_energy_lets_peak_moves_through_sliver_labels():
    a = _analysis()
    for s in a["sections"]:
        s["label"] = "verse"                          # labels useless
    a["energy_times"] = [x * 0.5 for x in range(448)]
    a["energy_curve"] = [0.9 if 80 <= t < 112 else 0.4 for t in a["energy_times"]]
    f = mp.build_facts(a, {"bpm": 122.0}, CANDS, {"window_lo": 144.0, "window_hi": 200.0,
                                                   "peak_moves": True, "big_moment_ok": True})
    assert {r["t"] for r in f["phrases"] if r["drop_hit"]} == {80.0}
    p = mp.validate_plan({"candidate": 0, "exit": 176.0,
                          "moves": [{"move": "fakeout", "at": 64.0}, {"move": "beat_boost", "at": 80.0}]}, f)
    assert [m["move"] for m in p["moves"]] == ["fakeout", "beat_boost"], p["dropped"]


def test_layer_possible_needs_key_tempo_and_restraint():
    assert _facts()["layer_possible"]                      # 8A -> 9A, 120 -> 122 BPM
    assert not _facts(recent_moves=["ride", "layer"])["layer_possible"]
    a = _analysis()
    assert not mp.layer_possible(a, {"bpm": 122.0, "key": {"camelot": "3B"}}, {})
    assert not mp.layer_possible(a, {"bpm": 140.0, "key": {"camelot": "8A"}}, {})
    assert mp.layer_possible(a, {"bpm": 60.5, "key": {"camelot": "8A"}}, {})   # half time


def test_layer_proposal_validated():
    f = _facts()
    p = mp.validate_plan({"candidate": 0, "exit": 176.0, "layer": True, "layer_reason": "grooves ride"}, f)
    assert p["layer"] and p["layer_reason"] == "grooves ride"
    assert not mp.validate_plan({"candidate": 0, "exit": 176.0, "layer": "yes"}, f)["layer"]
    p = mp.validate_plan({"candidate": 1, "exit": 176.0, "layer": True}, f)   # instant recipe
    assert not p["layer"] and any("instant" in d for d in p["dropped"])
    f2 = _facts(recent_moves=["layer"])
    p = mp.validate_plan({"candidate": 0, "exit": 176.0, "layer": True}, f2)
    assert not p["layer"] and "Layer possible" not in mp.build_prompt(f2)
    assert "Layer possible: yes" in mp.build_prompt(f)
