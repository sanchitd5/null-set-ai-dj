"""Late-entry rule, Python twin parity with autopilot.js (shared fixture) plus semantics."""
import json
import math
from pathlib import Path

import numpy as np

from app.music_brain.render import entry_lines as el

FX = json.loads((Path(__file__).parent / "fixtures" / "entry_line_cases.json").read_text())


def _js_key(t):
    return str(int(t)) if float(t).is_integer() else repr(t)


def _lvl(o, m, arr):
    if arr:
        return lambda t: el.low_level_at(arr, o["anchor"], o["bar"], t)
    if m:
        return lambda t: m.get(_js_key(t))
    return None


def _py_args(o):
    return dict(
        lines=o.get("lines"), include=o.get("include"), energy_times=o.get("energyTimes") or [],
        energy_curve=o.get("energyCurve") or [], vocals=o.get("vocals"), drops=o.get("drops"),
        bar=o.get("bar"), end=o.get("end"), room_s=o.get("roomS") or 0, overlap_bars=o.get("overlapBars"),
        band=o.get("band"), intro_ok=bool(o.get("introOk")),
        drums=_lvl(o, o.get("drums"), o.get("drumBars")), low=_lvl(o, o.get("low"), o.get("lowBars")),
    )


def _case(name):
    return next(k for k in FX["lineCases"] if k["name"] == name)


def test_entry_lines_parity():
    for k in FX["lineCases"]:
        got = el.entry_lines(**_py_args(k["o"]))
        exp = k["expect"]
        assert got["lines"] == exp["lines"], k["name"]
        assert got["rejected"] == exp["rejected"], k["name"]
        assert got["main_drop"] == exp["mainDrop"], k["name"]


def test_nocturnal_skips_its_quiet_intro_with_and_without_stems():
    for nm in ("Nocturnal with stems (drum bars)", "Nocturnal without stems (mix low band)"):
        got = el.entry_lines(**_py_args(_case(nm)["o"]))["lines"]
        assert not any(abs(t - 3.785) < 0.01 for t in got), nm
        assert any(abs(t - 18.785) < 0.01 for t in got), nm
    # Neverland's liked entry (7.64, its drop on 22.64) stays a candidate
    got = el.entry_lines(**_py_args(_case("Neverland without stems (mix low band)")["o"]))["lines"]
    assert any(abs(t - 7.64) < 0.01 for t in got)


def test_low_band_parity():
    k = FX["lowCases"][0]
    n = k["sr"] * k["secs"]
    i = np.arange(n)
    amp = np.array([0.05, 0.05, 0.8, 1, 0.8, 1])[(i // int(k["sr"] * 0.5)) % 6]
    s1 = (amp * np.sin(2 * np.pi * 60 * i / k["sr"]) + 0.3 * np.sin(2 * np.pi * 3000 * i / k["sr"])).astype(np.float32)
    s2 = (0.5 * s1).astype(np.float32)
    got = el.low_band_bars([s1, s2], k["sr"], k["anchor"], k["bar"])
    assert len(got) == len(k["expect"])
    for a, b in zip(got, k["expect"]):
        assert math.isclose(a, b, rel_tol=1e-4, abs_tol=1e-6)


def test_hash_and_rng_parity():
    for k in FX["hashCases"]:
        assert el.hash_seed(k["s"]) == k["h"], k["s"]
    for k in FX["rngCases"]:
        r = el.seeded_rng(k["seed"])
        assert [r() for _ in k["first"]] == k["first"], k["seed"]


def test_pick_parity():
    for k in FX["pickCases"]:
        r = el.seeded_rng(k["seed"])
        assert [el.pick_entry_line(k["cands"], k["setLevel"], r) for _ in k["expect"]] == k["expect"], k["name"]


def test_uniform_distribution():
    cands = [{"t": 7, "level": 8}, {"t": 37, "level": 8}, {"t": 95, "level": 7}, {"t": 150, "level": 6},
             {"t": 120, "level": 1}]
    r = el.seeded_rng(el.hash_seed("dist"))
    hits = {}
    n = 8000
    for _ in range(n):
        t = el.pick_entry_line(cands, 8, r)["t"]
        hits[t] = hits.get(t, 0) + 1
    assert 120 not in hits
    for t in (7, 37, 95, 150):
        assert abs(hits[t] - n / 4) < 0.15 * n / 4


def test_main_drop_by_band_and_quiet_intro():
    bar = 240 / 128
    times = list(range(190))
    curve = [0.1 if t < 30 else 0.95 if 90 <= t < 105 else 0.6 for t in times]
    kw = dict(lines=[0, 15, 30, 45, 60, 75, 90, 105, 120], include=0, energy_times=times, energy_curve=curve,
              drops=[{"t": 90, "energy": 0.95}], bar=bar, end=190, room_s=60)
    rel = el.entry_lines(band="relaxed", **kw)
    assert 0 not in rel["lines"] and 15 in rel["lines"] and all(t < 90 for t in rel["lines"])
    hi = el.entry_lines(band="high", **kw)
    assert 90 in hi["lines"] and 120 in hi["lines"]
    assert 0 in el.entry_lines(band="high", intro_ok=el.intro_recipe("Reverb Transition"), **kw)["lines"]
    # stems: drums decide over the low band; no stems: the low band decides over the energy curve
    assert 0 in el.entry_lines(band="high", drums=lambda t: 1.0, low=lambda t: 0.0, **kw)["lines"]
    lo = el.entry_lines(band="high", low=lambda t: 0.3 if t < 30 else 1.1, **kw)["lines"]
    assert 0 not in lo and 15 in lo and 30 in lo
