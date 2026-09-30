"""OWNER RULE "never vocal mix a drop line" (live set 2026-09-30_154332), planner half.

The one predicate (app/music_brain/render/drop_line.py) and its console twin (app/ui/static/drop-line.js) run
the same fixture; then every Python planner that layers a vocal uses it: the live mashup (plan_mashup),
riff over rap (keylock.drop_clear) and the pair atlas's mashup scoring. Merges / holds
are not gated: the owner-liked PACS & Ruiz -> Neverland hold sings over A's drop (test_live_fixes_golden.py).
"""
import json
import subprocess
from pathlib import Path

import pytest

from app.music_brain.analysis.analyzer import KeyEstimate, StructureSection, TrackAnalysis
from app.music_brain.audio import keylock
from app.music_brain.render import drop_line, mashup

ROOT = Path(__file__).resolve().parents[3]
FIX = json.loads((ROOT / "app/tests/fixtures/drop_line_cases.json").read_text())


def _sings(s):
    return drop_line.mapped_sings(s["regions"], s["t0"], s["at0"], s["ratio"]) if s else None


def test_fixture_cases():
    ana = FIX["analysis"]
    assert sorted(drop_line.drop_spans(ana)) == [tuple(x) for x in FIX["spans"]]   # the 1.5 s sliver is not a drop
    for c in FIX["cases"]:
        r = drop_line.song_busy(ana, c["t0"], c["t1"], sings=_sings(c.get("sings")))
        assert (r or {}).get("gate") == c["gate"], c["name"]
        if r:
            assert "never vocal mix a drop line" in r["reason"]
    assert drop_line.song_busy(None, 0, 16)["gate"] == "unmeasured"
    assert drop_line.drop_line_busy([], None, 0, 10) is None


def test_parity_with_the_console():
    """drop-line.js answers every fixture case exactly as drop_line.py does (gate and reason)."""
    js = (
        "const DL=require(process.argv[1]);const fx=require(process.argv[2]);"
        "const d={analysis:fx.analysis,bpm:fx.analysis.bpm};"
        "const S=s=>s?DL.mappedSings(s.regions,s.t0,s.at0,s.ratio):null;"
        "console.log(JSON.stringify({spans:DL.dropSpans(fx.analysis,fx.analysis.bpm),"
        "cases:fx.cases.map(c=>DL.deckBusy(d,c.t0,c.t1,S(c.sings)))}))"
    )
    out = subprocess.run(["node", "-e", js, str(ROOT / "app/ui/static/drop-line.js"),
                          str(ROOT / "app/tests/fixtures/drop_line_cases.json")],
                         capture_output=True, text=True, timeout=30, check=True)
    got = json.loads(out.stdout)
    assert sorted(tuple(s) for s in got["spans"]) == sorted(drop_line.drop_spans(FIX["analysis"]))
    for c, j in zip(FIX["cases"], got["cases"]):
        py = drop_line.song_busy(FIX["analysis"], c["t0"], c["t1"], sings=_sings(c.get("sings")))
        assert (j or None) == (py or None), c["name"]


def _track(path, bpm, camelot, duration=240.0, sections=None):
    bar = 240.0 / bpm
    phrases = [i * 8 * bar for i in range(int(duration // (8 * bar)) + 1)]
    return TrackAnalysis(
        path=path, duration=duration, bpm=bpm, phrase_boundaries_8bar=phrases,
        key=KeyEstimate(camelot=camelot, key_name="", is_major=camelot.endswith("B"), confidence=0.8),
        sections=sections or [StructureSection("intro", 0, 20, 0.3), StructureSection("verse", 20, 220, 0.6),
                              StructureSection("outro", 220, duration, 0.3)],
    )


@pytest.fixture
def regions(monkeypatch):
    table = {}
    monkeypatch.setattr(mashup, "vocal_presence_map", lambda p: table[str(p)])
    return table


def test_mashup_never_lays_a_vocal_over_the_host_drop(regions):
    secs = [StructureSection("intro", 0, 20, 0.3), StructureSection("verse", 20, 96, 0.6),
            StructureSection("drop", 96, 112, 0.9), StructureSection("verse", 112, 220, 0.6),
            StructureSection("outro", 220, 240, 0.3)]
    host, guest = _track("h", 120, "8A", sections=secs), _track("g", 120, "9A")
    regions["host.wav"], regions["guest.wav"] = [], [(80.0, 100.0)]
    plan = mashup.plan_mashup(host, guest, lambda: "host.wav", lambda: "guest.wav", bars=8)
    assert plan["ok"]
    assert all(e + plan["host_duration"] <= 96 + 1e-6 or e >= 112 - 1e-6 for e in plan["host_entries"])
    free = mashup.plan_mashup(_track("h", 120, "8A"), guest, lambda: "host.wav", lambda: "guest.wav", bars=8)
    assert set(free["host_entries"]) - set(plan["host_entries"]) == {96.0}, "only the drop window is lost"
    host.vocal_active_regions = [(94.0, 100.0)]      # the host's sung line runs on into its drop at 96 s
    lined = mashup.plan_mashup(host, guest, lambda: "host.wav", lambda: "guest.wav", bars=8)
    assert 80.0 not in lined["host_entries"], "the phrase whose sung line runs into the drop is lost too"
    assert 64.0 in lined["host_entries"]


def test_mashup_refused_when_every_phrase_touches_a_drop(regions):
    # a drop on every phrase line (4 bars each): the guest, singing throughout, has no clean host phrase left
    secs = [StructureSection("intro", 0, 16, 0.3)] + [StructureSection("drop", t, t + 8, 0.9) for t in range(16, 224, 16)] \
        + [StructureSection("outro", 224, 240, 0.3)]
    host, guest = _track("h", 120, "8A", sections=secs), _track("g", 120, "9A")
    regions["host.wav"], regions["guest.wav"] = [], [(80.0, 100.0)]
    plan = mashup.plan_mashup(host, guest, lambda: "host.wav", lambda: "guest.wav", bars=8)
    assert plan["ok"] is False and plan["gate"] == "drop_line"


def test_riff_over_rap_keeps_the_drop_window_clean():
    for m in (keylock.MASHUP_BARS_SHORT, keylock.MASHUP_BARS_LONG):
        assert keylock.drop_clear(keylock.timeline(m)) is None
    bad = dict(keylock.timeline(16), rap=20, mashup=20)
    assert keylock.drop_clear(bad).startswith("drop_line")
    assert keylock.drop_clear({}).startswith("drop_line")

