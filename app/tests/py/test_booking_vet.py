"""Live set 2026-09-30_154332: FOLLOW SET / macro / studied bookings obey the picks' rules, and the OWNER VETO.

Replay fixture: app/tests/fixtures/live_set_154332.json (songs in play order with their genre labels, the FOLLOW
SET "performing" moves and the transitions, extracted read-only from the session and genre_labels.json).
"""
import json
from pathlib import Path

import pytest

from app.music_brain.atlas import vetoes as vt
from app.ui.services import booking_vet as bv

ROOT = Path(__file__).resolve().parents[3]
REPLAY = json.loads((ROOT / "app/tests/fixtures/live_set_154332.json").read_text())
SONGS = REPLAY["songs"]
def _seed():
    """The tracked veto seed, loaded when a test runs (the conftest keeps the cache private), never at import."""
    return vt.load(None)
BODYROCK, WORK = "TH;EN - Bodyrock", "Masters At Work - Work (Skytech Remix)"
HACKNEY, SANTI = "Sammy Virji - Hackney Pigeon", "Santigold - You’ll Find a Way (Official Audio)"


def _song(name_start):
    return next(s for s in SONGS if s["name"].startswith(name_start))


def _sets():
    """The autopilot restarted (a new set) at ~16:24: Faust came back at 16:25."""
    return [s for s in SONGS if s["at"] < "16:24"], [s for s in SONGS if s["at"] >= "16:24"]


def test_the_session_shows_the_double_booking():
    perf = [e for e in REPLAY["events"] if e["kind"] == "follow_set" and "Masters At Work" in e["why"]]
    assert [e["at"] for e in perf] == ["16:08:23"], "FOLLOW SET performed TH;EN - Bodyrock -> Work (16:08 booking, in at 16:09)"
    starts = [e for e in REPLAY["events"] if e["kind"] == "transition_start" and (e.get("to") or "").startswith("Masters At Work")]
    assert len(starts) == 2, "Work was played twice in one session"
    assert any(e["kind"] == "suggest_reject" and e["reason"] == "played in an earlier set" for e in REPLAY["events"])


def test_replay_1637_follow_set_refuses_work_heard_in_the_earlier_set():
    set1, set2 = _sets()
    loofy = _song("Loofy")
    hist = [s["name"] for s in set2 if s["at"] < "16:37"]
    earlier = [s["name"] for s in set1]
    w = _song("Masters At Work")
    r = bv.vet_one(hist[-1], w["name"], a_genre=loofy["genre"], b_genre=w["genre"], a_era=loofy["era"], b_era=w["era"],
                   history=hist, earlier=earlier, vetoes=[], stored=True)
    assert r == {"gate": "earlier_set", "why": "played in an earlier set"}
    # the same song as a plain pick: the picks' own filter handles it (fresh alternatives), the vet does not
    assert bv.vet_one(hist[-1], w["name"], history=hist, earlier=earlier, vetoes=[], stored=False) is None


def test_replay_1608_the_seeded_veto_refuses_bodyrock_to_work():
    th, w = _song("TH;EN"), _song("Masters At Work")
    r = bv.vet_one(th["name"], w["name"], a_genre=th["genre"], b_genre=w["genre"], vetoes=_seed(), stored=True)
    assert r["gate"] == "veto" and "vibe killer" in r["why"]
    r = bv.vet_one(HACKNEY, SANTI, vetoes=_seed())      # any path (the 16:40 atlas backup was not a stored move)
    assert r["gate"] == "veto"


def test_repeat_in_the_same_set_by_title_not_id():
    hist = ["Argy, Son of Son - Faust", "Masters At Work - Work (Skytech Remix) [HQ]"]
    r = bv.vet_one("Argy, Son of Son - Faust", WORK, history=hist, stored=True)
    assert r == {"gate": "repeat", "why": "already played this set"}


def test_scene_as_the_picks_score_it():
    # one family (electronic): house <-> melodic techno is kept, as for the picks
    assert bv.vet_one("A - x", "B - y", a_genre="melodic techno", b_genre="progressive house", stored=True) is None
    assert bv.vet_one("A - x", "B - y", a_genre="electronic", b_genre="house", stored=True) is None
    r = bv.vet_one("A - x", "B - y", a_genre="melodic techno", b_genre="punjabi", stored=True)
    assert r["gate"] == "scene" and "genre jump" in r["why"]
    # the Punjabi profile's own pairs are one scene while it is active
    assert bv.vet_one("A - x", "B - y", a_genre="punjabi", b_genre="bollywood", stored=True, punjabi_profile="auto") is None
    # unknown passes (the studied pair is the evidence)
    assert bv.vet_one("A - x", "B - y", a_genre=None, b_genre="punjabi", stored=True) is None
    # era is not a stored-move rule (owner-liked Adapter - Catchaman -> PACS & Ruiz is 2000s -> 2020s)
    assert bv.vet_one("A - x", "B - y", a_genre="house", b_genre="house", a_era="2000s", b_era="2020s", stored=True) is None


def test_vet_rows_carry_the_scene_clash_for_the_mashup_gate():
    rows = bv.vet(HACKNEY, [{"track_id": "s", "name": SANTI, "genre": "indie pop"}, {"track_id": "n", "name": "X - y", "genre": None}],
                  a_genre="uk garage", vetoes=[])
    assert [r["scene_clash"] for r in rows] == [True, False]
    assert all(r["ok"] for r in rows)


def test_server_endpoints_store_and_every_path_reads(tmp_path):
    from app.tests.py.testclient_compat import TestClient
    from app.ui import server

    c = TestClient(server.app)
    r = c.post("/api/vetoes", json={"kind": "pair", "a_name": "A - one", "b_name": "B - two"})
    assert r.status_code == 200 and r.json()["added"] is True
    assert c.post("/api/vetoes", json={"kind": "pair", "a_name": "A - one", "b_name": "B - two"}).json()["added"] is False
    assert c.post("/api/vetoes", json={"kind": "pair", "b_name": "B - two"}).status_code == 400
    names = [v["b"] for v in c.get("/api/vetoes").json()["vetoes"]]
    assert "B - two" in names and any("Masters At Work" in n for n in names), "the seed is always loaded"
    res = c.post("/api/autopilot/vet", json={"a_name": "A - one", "history": ["A - one"],
                                             "cands": [{"name": "B - two"}, {"name": "C - three", "stored": True}]}).json()
    assert [x["ok"] for x in res["results"]] == [False, True]
    assert res["results"][0]["gate"] == "veto"
    res = c.post("/api/autopilot/vet", json={"a_name": BODYROCK, "cands": [{"name": WORK, "stored": True}]}).json()
    assert res["results"][0]["gate"] == "veto", "the seeded pair"


# ---- the mashup's scene gate (render/mashup.py scene_gate, narrow) -----------------------------------------

from app.music_brain.analysis.analyzer import KeyEstimate, StructureSection, TrackAnalysis  # noqa: E402
from app.music_brain.render import mashup  # noqa: E402


def _track(path, bpm, camelot, duration=240.0):
    bar = 240.0 / bpm
    phrases = [i * 8 * bar for i in range(int(duration // (8 * bar)) + 1)]
    return TrackAnalysis(
        path=path, duration=duration, bpm=bpm, phrase_boundaries_8bar=phrases,
        key=KeyEstimate(camelot=camelot, key_name="", is_major=camelot.endswith("B"), confidence=0.8),
        sections=[StructureSection("intro", 0, 20, 0.3), StructureSection("verse", 20, 220, 0.6),
                  StructureSection("outro", 220, duration, 0.3)],
    )


@pytest.fixture
def regions(monkeypatch):
    table = {}
    monkeypatch.setattr(mashup, "vocal_presence_map", lambda p: table[str(p)])
    return table


def test_mashup_scene_gate_is_narrow(regions):
    """Session events #401 / #228: Santigold (indie pop) over Sammy Virji - Hackney Pigeon (uk garage), and a
    melodic house vocal over the Punjabi "Sadi Gali". Only a CLEAR mismatch refuses; unknown passes."""
    assert mashup.scene_gate("uk garage", "indie pop")
    assert mashup.scene_gate("punjabi", "melodic house")
    assert mashup.scene_gate("melodic techno", "melodic house") is None
    assert mashup.scene_gate("punjabi", "bhangra") is None
    assert mashup.scene_gate("punjabi", "bollywood") is None, "the Punjabi profile's neighbour"
    assert mashup.scene_gate(None, "indie pop") is None and mashup.scene_gate("uk garage", "") is None
    assert mashup.scene_gate("electronic", "weird unlisted label") is None
    host, guest = _track("h", 120, "8A"), _track("g", 120, "9A")
    regions["host.wav"], regions["guest.wav"] = [], [(80.0, 100.0)]
    no = mashup.plan_mashup(host, guest, lambda: "host.wav", lambda: "guest.wav", bars=8, genres=("uk garage", "indie pop"))
    assert no["ok"] is False and no["gate"] == "scene"
    assert mashup.plan_mashup(host, guest, lambda: "host.wav", lambda: "guest.wav", bars=8, genres=(None, None))["ok"]
    assert mashup.plan_mashup(host, guest, lambda: "host.wav", lambda: "guest.wav", bars=8)["ok"], "no genres: not gated"
