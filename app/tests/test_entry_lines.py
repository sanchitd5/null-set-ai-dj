"""Late-entry rule, Python twin parity with autopilot.js (shared fixture) plus semantics."""
import json
from pathlib import Path

from app.music_brain.render import entry_lines as el

FX = json.loads((Path(__file__).parent / "fixtures" / "entry_line_cases.json").read_text())


def _py_args(o):
    m = o.get("drums")
    return dict(
        lines=o.get("lines"), include=o.get("include"), energy_times=o.get("energyTimes") or [],
        energy_curve=o.get("energyCurve") or [], vocals=o.get("vocals"), drops=o.get("drops"),
        bar=o.get("bar"), end=o.get("end"), room_s=o.get("roomS") or 0, overlap_bars=o.get("overlapBars"),
        band=o.get("band"), intro_ok=bool(o.get("introOk")),
        drums=(lambda t: m.get(_js_key(t))) if m else None,
    )


def _js_key(t):
    return str(int(t)) if float(t).is_integer() else repr(t)


def test_entry_lines_parity():
    for k in FX["lineCases"]:
        got = el.entry_lines(**_py_args(k["o"]))
        exp = k["expect"]
        assert got["lines"] == exp["lines"], k["name"]
        assert got["rejected"] == exp["rejected"], k["name"]
        assert got["main_drop"] == exp["mainDrop"], k["name"]


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
    curve = [0.1 if t < 15 else 0.95 if 90 <= t < 105 else 0.6 for t in times]
    kw = dict(lines=[0, 15, 30, 45, 60, 75, 90, 105, 120], include=0, energy_times=times, energy_curve=curve,
              drops=[{"t": 90, "energy": 0.95}], bar=bar, end=190, room_s=60)
    rel = el.entry_lines(band="relaxed", **kw)
    assert 0 not in rel["lines"] and all(t < 90 for t in rel["lines"])
    hi = el.entry_lines(band="high", **kw)
    assert 90 in hi["lines"] and 120 in hi["lines"]
    assert 0 in el.entry_lines(band="high", intro_ok=el.intro_recipe("Reverb Transition"), **kw)["lines"]
