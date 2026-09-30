"""Pre-planned transitions: timing candidates in both directions, scoring, the ear's re-rank."""
import numpy as np
import soundfile as sf

from app.music_brain import preplan

BPM = 120.0
BAR = 240 / BPM


def _stems(tmp_path, name, secs, levels):
    sr = preplan.SR
    out = {}
    for i, r in enumerate(("drums", "bass", "vocals", "other")):
        p = tmp_path / f"{name}_{r}.wav"
        t = np.arange(int(secs * sr)) / sr
        sf.write(p, levels[r] * np.sin(2 * np.pi * (110 + 50 * i) * t), sr)
        out[r] = str(p)
    return out


def _ana(secs, vocal_at):
    return {"duration": secs, "phrase_boundaries_8bar": [float(x) for x in np.arange(0, secs, 8 * BAR)],
            "vocal_active_regions": [[vocal_at, vocal_at + 30]],
            "energy_times": [float(x) for x in np.arange(0, secs, 1.0)],
            "energy_curve": [0.3 if x < 48 else 0.9 for x in np.arange(0, secs, 1.0)]}


def test_both_directions_are_searched_and_the_ear_reranks(tmp_path):
    lv = {"drums": 0.2, "bass": 0.2, "vocals": 0.1, "other": 0.1}
    sa, sb = _stems(tmp_path, "a", 240, lv), _stems(tmp_path, "b", 240, lv)
    a, b = _ana(240, 20.0), _ana(240, 32.0)
    eA, eB = preplan.energies(sa, BAR), preplan.energies(sb, BAR)
    cands = preplan.candidates(a, b, BPM, BPM, lo=140, hi=200, now=60, eA=eA, eB=eB, key_score=1.0)
    dirs = {c["direction"] for c in cands}
    assert "B's intro under A's outro" in dirs and any(d.startswith("A's tail over") for d in dirs)
    for c in cands:                                    # on the grid, inside A, B starts before the line
        assert abs((c["handover"] - c["a_in"]) - c["bars"] * BAR) < 1e-6 and c["a_in"] >= 60 + preplan.MIN_LEAD_S
        assert c["handover"] in a["phrase_boundaries_8bar"] and c["b_start"] in b["phrase_boundaries_8bar"]
    worst = cands[preplan.EAR_TOP - 1]
    ask = lambda system, wav, text: '{"score": 10, "why": "locks"}' if worst["label"] in text and worst["direction"] in text else '{"score": 1, "why": "no"}'
    res = preplan.preplan(a, b, sa, sb, BPM, BPM, 140, 200, 60, 1.0, ask=ask, cache_key="t", cache_dir=tmp_path / "c")
    assert res["ok"] and res["ear"] and res["plan"]["ear"]["score"] == 10               # the ear's favourite wins
    again = preplan.preplan(a, b, sa, sb, BPM, BPM, 140, 200, 60, 1.0, ask=lambda *x: 1 / 0, cache_key="t", cache_dir=tmp_path / "c")
    assert again["plan"] == res["plan"]                                                  # heard once, cached


def test_nothing_fits_says_why(tmp_path):
    lv = {"drums": 0.2, "bass": 0.2, "vocals": 0.1, "other": 0.1}
    sa, sb = _stems(tmp_path, "a", 120, lv), _stems(tmp_path, "b", 120, lv)
    res = preplan.preplan(_ana(120, 20.0), _ana(120, 32.0), sa, sb, BPM, BPM, 110, 118, 100, 1.0,
                          ask=lambda *x: None, cache_dir=tmp_path / "c")
    assert not res["ok"] and "no phrase line" in res["why"]


def test_no_transition_while_a_is_at_its_energy_high(tmp_path):
    lv = {"drums": 0.2, "bass": 0.2, "vocals": 0.1, "other": 0.1}
    sa, sb = _stems(tmp_path, "a", 240, lv), _stems(tmp_path, "b", 240, lv)
    a, b = _ana(240, 20.0), _ana(240, 32.0)
    a["energy_curve"] = [0.9 if 150 <= x < 190 else 0.3 for x in np.arange(0, 240, 1.0)]   # A's high: 150-190 s
    spans = preplan.high_spans(a, BAR)
    assert spans and abs(spans[0][0] - (150 - preplan.HIGH_LEAD_BARS * BAR)) < 1.01 and spans[0][1] >= 189   # build protected
    eA, eB = preplan.energies(sa, BAR), preplan.energies(sb, BAR)
    cands = preplan.candidates(a, b, BPM, BPM, lo=100, hi=230, now=30, eA=eA, eB=eB, key_score=1.0)
    assert cands
    for c in cands:                                   # nothing from B's entry to 8 bars after the line touches it
        assert not preplan.in_high(spans, c["a_in"], c["handover"] + 8 * BAR)


def test_quick_window_still_gets_a_plan(tmp_path):
    """QUICK window (60-120 s) with the ear only at 37 s: a 16-bar overlap alone fit nowhere."""
    lv = {"drums": 0.2, "bass": 0.2, "vocals": 0.1, "other": 0.1}
    sa, sb = _stems(tmp_path, "a", 240, lv), _stems(tmp_path, "b", 240, lv)
    a, b = _ana(240, 20.0), _ana(240, 32.0)
    for x in (a, b):
        x["energy_curve"] = [0.5] * len(x["energy_times"])         # flat: no high point to dodge
    eA, eB = preplan.energies(sa, BAR), preplan.energies(sb, BAR)
    cands = preplan.candidates(a, b, BPM, BPM, lo=60, hi=120, now=37, eA=eA, eB=eB, key_score=1.0)
    assert cands and {c["bars"] for c in cands} <= set(preplan.SHORT_OVERLAPS)
    assert any(c["bars"] == 8 for c in cands)
    for c in cands:
        assert c["a_in"] >= 37 + preplan.MIN_LEAD_S + preplan.PLAN_BUDGET_S
    # a long window keeps the 16/32-bar overlaps only
    long_c = preplan.candidates(a, b, BPM, BPM, lo=100, hi=200, now=37, eA=eA, eB=eB, key_score=1.0)
    assert long_c and {c["bars"] for c in long_c} <= set(preplan.OVERLAPS)


def test_flat_or_mostly_loud_songs_have_no_high_to_protect():
    flat = {"energy_times": list(range(100)), "energy_curve": [0.5] * 100}
    mostly = {"energy_times": list(range(100)), "energy_curve": [0.9] * 90 + [0.1] * 10}
    assert preplan.high_spans(flat, BAR) == [] and preplan.high_spans(mostly, BAR) == []


def test_session_event_accepts_glitch_reports(tmp_path, monkeypatch):
    import app.ui.server as s
    from app.ui import session_log
    monkeypatch.setattr(session_log, "SESSIONS_DIR", tmp_path)
    r = s.post_session_event(s.SessionEvent(kind="glitch", data={"kind": "silence", "where": "A x @ 1:00", "decks": [{"deck": "a"}]}))
    e = session_log.read()[-1]
    assert r["ok"] and e["kind"] == "glitch" and e["kind_"] == "silence" and e["where"] == "A x @ 1:00"
