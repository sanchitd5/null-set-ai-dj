"""mashup_mix: full-groove core pick, pure JSON plan (layering, one sub owner, one vocal), render QC, energy steps."""
import json

import numpy as np
import pytest
import soundfile as sf

from app.music_brain.render import mashup_mix as mm
from app.music_brain.render import swap_mix as sm

BPM = 120.0
BAR = 240.0 / BPM


def _song(n_bars=120, full=(32, 64), dip=None, other_quiet_before=None):
    lv = np.full(n_bars, 0.2)
    lv[full[0]:full[1]] = 1.0
    if dip:
        lv[dip[0]:dip[1]] = 0.3
    other = lv.copy()
    if other_quiet_before is not None:
        other[:other_quiet_before] = 0.0
    dur = n_bars * BAR
    an = {"bpm": BPM, "duration": dur, "phrase_boundaries_8bar": [i * 8 * BAR for i in range(n_bars // 8)],
          "energy_times": [], "energy_curve": [], "vocal_active_regions": []}
    stem = {"anchor": 0.0, "bar": BAR, "bars": {"drums": list(lv), "bass": list(lv), "other": list(other)}}
    return an, stem


def test_core_is_full_groove_not_a_dip():
    an, st = _song(full=(32, 64))
    c = mm.pick_core(an, st)
    assert c["bars"] == 16 and 32 * BAR <= c["start"] <= 48 * BAR and c["share"] >= mm.BAND
    an, st = _song(full=(32, 64), dip=(40, 48))       # a breakdown inside: only 48-64 is a clean 16-bar run
    assert mm.pick_core(an, st)["start"] == pytest.approx(48 * BAR)


def test_long_core_for_liked_song():
    an, st = _song(full=(32, 64))
    c = mm.pick_core(an, st, long=True)
    assert c["bars"] == 32 and c["start"] == pytest.approx(32 * BAR)


def test_enter_refused_when_other_has_no_energy():
    an, st = _song(other_quiet_before=32)
    assert not mm.enter_ok(an, st, 32 * BAR, 16)
    an, st = _song()
    assert mm.enter_ok(an, st, 32 * BAR, 16)


def _chain(n=8, stems_dir=None):
    out = []
    for i in range(n):
        an, st = _song()
        out.append({"id": f"s{i}", "name": f"s{i}", "genre": "melodic house", "cam": "8A", "bpm": BPM + (i % 2),
                    "an": an, "stem_bars": st, "core": mm.pick_core(an, st), "stems": stems_dir and str(stems_dir(i)),
                    "move": "Bass Swap" if i < n - 1 else None, "key_score": 1.0, "works": 90})
    return out


def test_plan_is_json_and_layers_by_stem():
    p = mm.build_plan(_chain(), 120.5)
    json.dumps(p)
    counts = [len(s["songs"]) for s in p["sections"]]
    assert max(counts) == 3 and sum(c >= 2 for c in counts) >= len(counts) - 2
    for sec in p["sections"]:
        assert sum("bass" in x["stems"] for x in sec["songs"]) <= 1          # one sub owner
        assert sum("vocals" in x["stems"] for x in sec["songs"]) <= 1        # never two vocals
    for s in p["songs"][1:]:
        roles = {(pt["stem"], pt["role"]) for pt in s["parts"]}
        assert ("other", "enter") in roles
        assert all(pt["hp_hz"] == sm.SUB_HZ for pt in s["parts"] if pt["role"] != "core")
        assert not any(pt["stem"] in ("bass", "vocals") and pt["role"] != "core" for pt in s["parts"])


def test_stretch_cap_refused():
    ch = _chain(8)
    ch[3]["bpm"] = 100.0
    with pytest.raises(ValueError):
        mm.build_plan(ch, 120.0)


def _stems(tmp, i, sr=8000, dur=120 * BAR):
    d = tmp / f"s{i}"
    d.mkdir()
    t = np.arange(int(dur * sr)) / sr
    f0 = 400 + 50 * i
    parts = {"bass": 0.3 * np.sin(2 * np.pi * 50 * t), "drums": 0.2 * np.sin(2 * np.pi * f0 * t),
             "other": 0.2 * np.sin(2 * np.pi * 1.5 * f0 * t), "vocals": 0.05 * np.sin(2 * np.pi * 2 * f0 * t)}
    for n, x in parts.items():
        sf.write(d / f"{n}.flac", np.stack([x, x], 1).astype(np.float32), sr)
    return d


def test_render_plan_qc(tmp_path):
    p = mm.build_plan(_chain(8, lambda i: _stems(tmp_path, i)), BPM)
    for s in p["songs"]:
        s["ratio"] = 1.0                  # keep the test off Rubber Band
    rep = mm.render_plan(p, tmp_path / "mix.wav", tmp_path / "tmp")
    assert rep["peak_dbfs"] == pytest.approx(sm.PEAK_DBFS, abs=0.05)
    assert not rep["silent_seconds"] and rep["sub_clash_blocks"] == 0
    assert rep["energy"]["max_step_db"] <= 2.0


def test_energy_steps_and_leveller():
    sr, blk = 1000, 8 * BAR
    x = np.concatenate([np.full(int(blk * sr), a, np.float32) for a in (0.1, 0.1, 0.4, 0.1, 0.1)])[:, None]
    e = mm.energy_steps(x, sr, BPM, drop_last=False)
    assert e["max_step_db"] == pytest.approx(12.04, abs=0.05) and e["range_db"] == pytest.approx(12.04, abs=0.05)
    g = mm.level(x, sr, blk)
    assert g[2] == -mm.LEVEL_MAX_DB and g[0] == 0.0
