"""ROMANCE (bedtime) mode on the server: energy rule, suggest prompt and filter, plan gating, request models."""

from app.music_brain.analysis import energy
from app.ui.services import autopilot_service as ap
from app.ui.services import mind_plan as mp
from app.tests.py.test_mind_plan import CANDS, _facts as _plain_facts, _analysis


def _facts(**ctx):
    base = {"now": 0.0, "entry": 0.0, "window_lo": 144.0, "window_hi": 200.0,
            "set_mode": "hybrid", "set_position": 0.3, "mashup_possible": True, "mood": "romance"}
    base.update(ctx)
    return mp.build_facts(_analysis(), {"bpm": 122.0, "key": {"camelot": "9A"}}, CANDS, base)


def test_energy_never_rises_in_romance():
    assert not energy.next_ok(5, 6, romance=True)["ok"]
    assert not energy.next_ok(5, 6, romance=True, force=True)["ok"]
    assert energy.next_ok(5, 4, romance=True)["ok"] and energy.next_ok(5, 5, romance=True)["ok"]
    assert not energy.next_ok(5, 3, romance=True)["ok"]       # at most 1 down a song
    assert energy.allowed_window(5, romance=True) == (4, 5)
    assert energy.next_ok(5, 6, relaxed=True)["ok"]            # plain relaxed is unchanged


def test_romance_arc_falls_through_the_session():
    assert "settling" in ap._romance_arc(0.0) and "deepening" in ap._romance_arc(0.5)
    assert "winding down" in ap._romance_arc(0.9) and "winding down" in ap._romance_arc(1.0)


def test_romance_prompt_text_has_no_examples_and_says_wind_down():
    text = ap.ROMANCE_LINE.lower()
    assert "warm" in text and "intimate" in text and "never higher" in text
    assert "hype" in text and "party" in text
    for phrase, _t in ap.ROMANCE_ARC_PHASES:
        assert phrase  # description only


def test_romance_only_drops_euphoric_dark_and_driving_but_never_empties():
    mk = lambda mood, feel="mid": {"title": mood, "track_profile": {"mood": mood, "tempo_feel": feel}}
    kept = ap._romance_only([mk("euphoric"), mk("chill"), mk("dark"), mk("bittersweet", "driving"), mk("bittersweet")])
    assert [s["title"] for s in kept] == ["chill", "bittersweet"]
    allbad = [mk("euphoric"), mk("dark")]
    assert ap._romance_only(allbad) == allbad


def test_plan_romance_candidates_and_moves():
    f = _facts()
    assert f["mood"] == "romance" and not f["layer_possible"]
    assert all(c["recipe"] in mp.ROMANCE_RECIPES for c in f["candidates"])     # Drop Swap is gone
    assert [c["recipe"] for c in f["candidates"]] == ["Bass Swap", "Echo Out"]
    raw = {"candidate": 0, "exit": 176.0, "moves": [
        {"move": "filter_build", "at": 64.0, "reason": "tension"},
        {"move": "loop_extend", "at": 96.0, "bars": 4, "reason": "x"},
        {"move": "preclear", "at": 160.0, "reason": "ease the sub"}]}
    p = mp.validate_plan(raw, f)
    assert [m["move"] for m in p["moves"]] == ["preclear"]
    assert any("romance" in d for d in p["dropped"])
    # without the mood the same plan keeps its remix moves
    q = mp.validate_plan(raw, _plain_facts())
    assert {"filter_build", "loop_extend"} <= {m["move"] for m in q["moves"]}


def test_plan_prompt_lists_only_smooth_moves():
    prompt = mp.build_prompt(_facts())
    assert "ROMANCE MODE" in prompt
    assert "  hold:" in prompt and "  preclear:" in prompt
    for gone in ("filter_build:", "stutter:", "loop_extend:", "beat_jump:", "subdrop:", "echo_freeze:"):
        assert f"  {gone}" not in prompt
    assert "ROMANCE MODE" not in mp.build_prompt(_plain_facts())


def test_request_models_carry_mood():
    from app.ui import server
    assert server.AutopilotSuggestRequest(track_id="t", mood="romance").mood == "romance"
    assert server.AutopilotSuggestRequest(track_id="t").mood == ""
    assert server.MindPlanRequest(track_a_id="a", track_b_id="b", window_lo=0, window_hi=1, mood="romance").mood == "romance"
