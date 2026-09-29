"""The virtual set's scorer, compare and suite aggregation (pure: no server, no audio)."""
import copy
import json

from app.sim import compare, scorer, suite


def _art(name):
    return {name.split(" - ")[0].lower()}


def _id(name):
    return name.split(" - ", 1)[-1].lower()


def _t(i, **kw):
    t = {"i": i, "from": f"A{i} - x{i}", "to": f"B{i} - y{i}", "from_key": "8A", "to_key": "9A", "key_score": 0.9,
         "beat_locked": True, "tempo_pct": 0.0, "tempo_jump": False, "from_bpm": 124, "to_bpm": 126,
         "recipe_planned": "Long Blend", "recipe_executed": "Long Blend", "path": "stem_blend", "kind": "blend",
         "refused": "", "degraded": False, "dead_air_s": 0.0, "min_db": -3.0, "intro": "other", "silent_intro": False,
         "unlocked_overlap_s": 0.0, "plan_parsed": True, "key_rewrite": None, "blend_dropped": False}
    t.update(kw)
    return t


def _run(trans, names=None, levels=None, secs=None, counters=None):
    n = len(trans) + 1
    names = names or [f"Artist{i} - Song {i}" for i in range(n)]
    levels = levels or [5] * n
    secs = secs or [200.0] * (n - 1) + [None]
    c = {"picks_empty": 0, "stalls": 0, "http_errors": 0, "download_failures": 0}
    c.update(counters or {})
    return {"meta": {"seed": 1, "mode": "long", "tracks_played": n, "world": "replay", "name": "t", "stalled": False,
                     "replay_misses": 0},
            "songs": [{"i": i, "name": names[i], "level": levels[i], "seconds": secs[i]} for i in range(n)],
            "transitions": trans, "preps": [{"rounds": 1}], "rejects": [], "counters": c}


def _score(run):
    return scorer.score_run(run, artists_of=_art, identity=_id)


def test_clean_set_scores_zero():
    r = _score(_run([_t(1), _t(2), _t(3)]))
    assert r["score"] == 0.0
    assert r["worst"] == []
    assert r["metrics"]["key_clash_rate"] == 0.0


def test_key_clash_blend_is_the_worst_offence():
    clash = _t(1, key_score=0.0, from_key="6A", to_key="1A")
    echo = _t(1, key_score=0.0, from_key="6A", to_key="1A", recipe_planned="Echo Out", recipe_executed="Echo Out",
              kind="echo", path="eq")
    blend = _score(_run([clash, _t(2)]))
    safe = _score(_run([echo, _t(2)]))
    assert blend["metrics"]["key_clash_blends"] == 1
    assert safe["metrics"]["key_clash_blends"] == 0
    assert blend["score"] > safe["score"] > 0
    assert blend["worst"][0]["transition"] == 1
    assert any("tonal" in r for r in blend["worst"][0]["reasons"])


def test_kb_table_decides_what_a_clash_is():
    # the console scores a diagonal move 0, the KB 0.75: only a KB clash (3+ hours) is a hard clash
    diag = _t(1, key_score=0.0, key_kb=0.75, from_key="11B", to_key="12A", recipe_planned="Long Blend",
              recipe_executed="Echo Out", kind="echo", path="eq", key_rewrite={"from": "Long Blend", "to": "Echo Out"})
    r = _score(_run([diag, _t(2)]))
    m = r["metrics"]
    assert m["key_hard_clash_count"] == 0 and m["key_clash_count"] == 1        # console score 0 < 0.8, KB says no clash
    assert m["key_false_rewrites"] == 1
    assert r["breakdown"]["key_false_rewrite"] == scorer.WEIGHTS["key_false_rewrite"]
    real = _t(1, key_score=0.0, key_kb=0.0, from_key="6A", to_key="1A")
    rm = _score(_run([real, _t(2)]))["metrics"]
    assert rm["key_hard_clash_count"] == 1 and rm["key_clash_blends"] == 1 and rm["key_false_rewrites"] == 0


def test_breakdown_ranks_the_categories():
    r = _score(_run([_t(1, dead_air_s=4.0), _t(2, unlocked_overlap_s=2.0)]))
    assert list(r["breakdown"])[0] == "dead_air"
    assert r["breakdown"]["unlocked_overlap"] == 1.0


def test_tempo_dead_air_and_mismatch_metrics():
    t = _t(1, tempo_pct=9.0, dead_air_s=2.0, silent_intro=True, recipe_planned="Stem Bridge", recipe_executed="EQ blend",
           degraded=True, refused="stem bridge refused: master -13.1 dB", beat_locked=True)
    m = _score(_run([t, _t(2)]))["metrics"]
    assert m["tempo_over_cap"] == 1 and m["tempo_stretch_max_pct"] == 9.0
    assert m["dead_air_seconds"] == 2.0 and m["dead_air_transitions"] == 1
    assert m["silent_intros"] == 1 and m["plan_exec_mismatch"] == 1 and m["degraded_moves"] == 1
    assert m["recipes"] == {"EQ blend": 1, "Long Blend": 1}


def test_set_level_penalties():
    names = ["Same - a", "Same - b", "Same - c", "Other - d", "Other - d"]
    r = _score(_run([_t(i) for i in range(1, 5)], names=names, levels=[8, 4, 3, 9, 2],
                    secs=[60.0, 500.0, 200.0, 200.0, None], counters={"picks_empty": 2, "stalls": 1}))
    m = r["metrics"]
    assert m["max_artist_run"] == 3 and m["same_artist_repeats"] >= 2
    assert m["repeat_songs"] == 1
    assert m["energy_max_fall"] == 7 and m["energy_falls_ge3"] == 2
    assert m["song_min_s"] == 60.0 and m["song_max_s"] == 500.0
    assert m["empty_picks"] == 2 and m["stalls"] == 1
    assert r["penalties"]["stall"] == scorer.WEIGHTS["stall"]
    assert r["penalties"]["short_song"] == scorer.WEIGHTS["short_song"]


def test_score_is_deterministic_and_normalised():
    run = _run([_t(1, dead_air_s=1.0), _t(2)])
    assert _score(copy.deepcopy(run)) == _score(copy.deepcopy(run))
    one = _score(_run([_t(1, dead_air_s=2.0)]))
    two = _score(_run([_t(1, dead_air_s=2.0), _t(2)]))
    assert two["score"] < one["score"]              # per transition: a longer clean set dilutes the same flaw
    assert one["score_direction"] == "lower is better"


def test_worst_lists_at_most_five_with_reasons():
    trans = [_t(i, dead_air_s=float(i)) for i in range(1, 9)]
    w = _score(_run(trans))["worst"]
    assert len(w) == 5 and w[0]["transition"] == 8
    assert all(x["reasons"] for x in w)


def test_compare_verdicts_and_exit_code(tmp_path, capsys):
    a = _score(_run([_t(1, dead_air_s=2.0), _t(2)]))
    b = _score(_run([_t(1), _t(2)]))
    pa, pb = tmp_path / "a.json", tmp_path / "b.json"
    pa.write_text(json.dumps(a))
    pb.write_text(json.dumps(b))
    assert compare.main([str(pa), str(pb)]) == 0
    out = capsys.readouterr().out
    assert "dead_air_seconds" in out and "BETTER" in out and "score" in out
    assert compare.main([str(pb), str(pa)]) == 1        # got worse: non-zero exit
    assert "WORSE" in capsys.readouterr().out


def test_suite_aggregate_and_check():
    good = _score(_run([_t(1), _t(2)]))
    bad = _score(_run([_t(1, key_score=0.0, from_key="6A", to_key="1A"), _t(2)]))

    def mk(*reports):
        return {"aggregate": suite.aggregate({f"r{i}": r for i, r in enumerate(reports)})}

    base, same, worse = mk(good, good), mk(good, good), mk(good, bad)
    assert base["aggregate"]["score"] == 0.0
    assert suite.check(same, base) == []
    reasons = suite.check(worse, base)
    assert any("mean score" in r for r in reasons) and any("key_clash_blends" in r for r in reasons)
    assert base["aggregate"]["recipes"] == {"Long Blend": 4}


def test_merge_hold_metrics_are_informational():
    """merged_play_share / hold seconds / refusal histogram report, but never move the score."""
    plain = _score(_run([_t(1), _t(2), _t(3), _t(4)]))
    held = _run([_t(1, merge_outcome="hold", hold_s=30.0, hold_bars=14), _t(2, merge_outcome="hold", hold_s=14.0, hold_bars=6),
                 _t(3, merge_outcome="refused", merge_gate="key"), _t(4, merge_outcome="classic", merge_gate="unclean")])
    r = _score(held)
    m = r["metrics"]
    assert r["score"] == plain["score"]
    assert m["merged_play_share"] == 0.5 and m["classic_merge_share"] == 0.25
    assert m["hold_seconds_mean"] == 22.0 and m["hold_seconds_max"] == 30.0
    assert m["merge_refusals"] == {"key": 1, "unclean": 1}
    assert _score(_run([_t(1)]))["metrics"]["merged_play_share"] == 0.0


def test_merge_facts_read_the_step_log():
    from app.sim import runlog

    def st(kind, t, **kw):
        return {"kind": kind, "t": 1_790_000_000.0 + t, "decision": kw.pop("decision", None), "result": kw}

    steps = [st("merge_gate", 5, decision="refused", gate="key"),
             st("hold", 61, decision="hold", phrases=2, bars=14, seconds=28.0)]
    held = runlog._merge_facts(steps, 0.0, 60.0, 90.0, "Stem Merge")
    assert held["merge_outcome"] == "hold" and held["hold_s"] == 28.0 and held["hold_bars"] == 14 and held["merge_gate"] == ""
    refused = runlog._merge_facts(steps[:1], 0.0, 60.0, 90.0, "Echo Out")
    assert refused["merge_outcome"] == "refused" and refused["merge_gate"] == "key"
    classic = runlog._merge_facts([st("merge_gate", 5, decision="classic merge", gate="unclean")], 0.0, 60.0, 90.0, "Stem Merge")
    assert classic["merge_outcome"] == "classic" and classic["merge_gate"] == "unclean"
    assert runlog._merge_facts([], 0.0, 60.0, 90.0, "Long Blend")["merge_outcome"] == "n/a"
    # another transition's hold step (outside the window) is not this one's
    other = runlog._merge_facts([st("hold", 200, phrases=2, bars=14, seconds=28.0)], 0.0, 60.0, 90.0, "Long Blend")
    assert other["merge_outcome"] == "n/a"
