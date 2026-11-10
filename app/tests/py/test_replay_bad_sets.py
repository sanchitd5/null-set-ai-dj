"""Replay regression: the bad sets of 2026-11-10, read from anonymised session-event fixtures
(app/tests/py/fixtures/replay/). The events hold the picks and why each was dropped or kept, not the
model's raw reply, so each test rebuilds the suggest input from those events and runs the real filters
(_filter_suggestions, pick_pool.ground, the language gate, artist spacing).
Song names are real context songs from the owner's sets; nothing here is ever sent to an LLM."""
import json
import re
from pathlib import Path

import pytest

from app.music_brain.analysis import lang_id
from app.ui.services import pick_pool as pp
from app.ui.services.autopilot_service import _filter_suggestions, artist_spacing

FX = Path(__file__).parent / "fixtures" / "replay"
SEED = "Coke Studio Season 9｜ Afreen Afreen｜ Rahat Fateh Ali Khan & Momina Mustehsan"
REASON = re.compile(r"genre (?P<genre>.+?) \(set: (?P<set>.+?)\); era (?P<era>\S+?);")


def events(sid: str, kind: str | None = None) -> list[dict]:
    rows = [json.loads(line) for line in (FX / f"2026-11-10_{sid}.jsonl").read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if kind is None or r["kind"] == kind]


def split(song: str) -> tuple[str, str]:
    artist, _, title = song.partition(" - ")
    return artist.strip(), title.strip()


def pick(song: str, **kw) -> dict:
    artist, title = split(song)
    return {"artist": artist, "title": title, **kw}


def served(sid: str) -> list[dict]:
    """The picks the old build let through: each pick_reason event carries the label it was served under."""
    out = []
    for e in events(sid, "pick_reason"):
        m = REASON.search(e["reason"])
        assert m, e["reason"]
        out.append(pick(e["song"], genre=m["genre"], era=m["era"], _set=m["set"]))
    return out


# ------------------------------------------------------------ scene: calm Urdu set -> melodic house

def _assert_all_rejected(picks):
    data = {"current_genre": "Urdu pop", "current_era": "2020s", "steering": "stay", "suggestions": picks}
    assert _filter_suggestions(data, [SEED], current_artist="Rahat Fateh Ali Khan", current_title="Afreen Afreen",
                               scene_genre="Urdu pop") == []


def test_melodic_house_picks_that_were_served_are_now_rejected():
    off = [p for p in served("050652") if p["genre"] == "melodic house"]
    assert len(off) == 3
    for p in off:                                   # one at a time: nothing is kept as a last resort
        _assert_all_rejected([dict(p)])
    _assert_all_rejected([dict(p) for p in off])


def test_african_vocal_and_house_jumps_of_the_second_set_are_rejected():
    jumps = [e["song"] for e in events("060012", "suggest_reject") if e["reason"] == "genre jump (2)"]
    assert len(jumps) == 2
    labels = {"CKay - Love Nwantiti (Ah Ah Ah)": "afrobeats", "Tourist - Run": "melodic house"}
    for song in jumps:
        _assert_all_rejected([pick(song, genre=labels[song], era="2020s")])
    _assert_all_rejected([pick(s, genre=labels[s], era="2020s") for s in jumps])


def test_in_scene_pick_survives_next_to_the_rejected_ones():
    inscene = [p for p in served("060012") if p["genre"] == "Urdu pop"]
    assert [p["title"] for p in inscene] == ["Tonelabira"]
    rejected = [pick(e["song"], genre="melodic house", era="2020s") for e in events("050652", "suggest_reject")
                if e["reason"].startswith("genre jump")]
    assert rejected
    data = {"current_genre": "Urdu pop", "current_era": "2020s", "steering": "stay",
            "suggestions": rejected + [dict(inscene[0])]}
    kept = _filter_suggestions(data, [SEED], current_artist="Rahat Fateh Ali Khan", scene_genre="Urdu pop")
    assert [k["title"] for k in kept] == ["Tonelabira"]


def test_era_jump_from_the_calm_set_is_rejected():
    ev = [e for e in events("050652", "suggest_reject") if e["reason"].startswith("era jump")]
    assert ev
    data = {"current_genre": "Urdu pop", "current_era": "2020s", "suggestions": [pick(ev[0]["song"], genre="Urdu pop", era="2001")]}
    assert _filter_suggestions(data, [], scene_genre="Urdu pop") == []


def test_other_writing_system_is_rejected_for_a_latin_set():
    ev = [e for e in events("050652", "suggest_reject") if e["reason"] == "writing system far from the set"]
    assert ev
    data = {"current_genre": "Urdu pop", "suggestions": [pick(ev[0]["song"])]}
    assert _filter_suggestions(data, [], current_artist="Rahat Fateh Ali Khan", current_title="Afreen Afreen") == []


# ------------------------------------------------------------ energy scale

@pytest.mark.parametrize("measured,model", [(1, 1), (2, 1), (2, 2), (2, 0.2), (1, 0.1)])
def test_calm_pick_after_a_calm_song_is_not_an_energy_gap(measured, model):
    """The calm Urdu song was logged "energy gap 9" against every calm pick: a model 1 was read as 0-1 scale = 10."""
    ev = [e for e in events("050652", "suggest_reject") if e["reason"] == "energy gap 9"]
    assert ev
    p = pick(ev[0]["song"], track_profile={"energy": model, "tempo_feel": "mid", "mood": "chill"})
    kept = _filter_suggestions({"suggestions": [p]}, [], measured_energy=measured)
    assert len(kept) == 1


def test_loud_pick_after_a_calm_song_is_still_a_gap():
    p = pick("Talwiinder - KAMMO JI", track_profile={"energy": 9, "tempo_feel": "mid", "mood": "chill"})
    assert _filter_suggestions({"suggestions": [p]}, [], measured_energy=2) == []


# ------------------------------------------------------------ cid grounding

def _pool():
    rows = [("Noor", "Aarzu"), ("Tourist", "Run"), ("An-Known", "Tonelabira"), ("Jind Universe", "High On You")]
    rows += [(f"Artist {i}", f"Song {i}") for i in range(1, 13)]
    return pp.build_pool(atlas=[{"artist": a, "title": t, "source": "library", "track_id": f"t{i}"}
                                for i, (a, t) in enumerate(rows)], cat=[], exclude=[], seed_title="Seed")


def test_cid_written_into_the_artist_field_still_grounds():
    """060012: "C12 - Noor - Aarzu" came back as a free-text reject although C12 was in the list."""
    pool = _pool()
    cid = next(c["cid"] for c in pool if c["title"] == "Aarzu")
    ev = [e for e in events("060012", "suggest_reject") if e["song"].startswith("C12 - ")]
    assert ev
    artist, title = split(ev[0]["song"])
    ok, free = pp.ground([{"artist": cid, "title": title}], pool)
    assert not free and (ok[0]["artist"], ok[0]["title"]) == ("Noor", "Aarzu")
    ok, free = pp.ground([{"artist": f"{cid} - Noor", "title": "Aarzu"}], pool)
    assert not free and ok[0]["title"] == "Aarzu"


@pytest.mark.parametrize("sid", ["163310", "164231"])
def test_free_text_picks_stay_rejected(sid):
    ev = [e for e in events(sid, "suggest_reject") if e["reason"].startswith("free-text pick")]
    assert ev
    picks = [pick(e["song"]) for e in ev]
    ok, free = pp.ground(picks, _pool())
    if sid == "164231":                      # "Jind Universe - High On You" is a pool row here: it grounds by name
        assert [p["title"] for p in ok] == ["High On You"] and not free
    else:
        assert not ok and [p["title"] for p in free] == ["So Supreme"]


# ------------------------------------------------------------ language gate

def _gate(lang):
    profile = {"n": 3, "share": {"hi": 1.0}, "top": "hi", "pure": True}
    return lambda s: lang_id.verdict({"v": 1, "lang": lang(s), "conf": 0.9, "instr": False}, profile)


@pytest.mark.parametrize("sid", ["050652", "060012"])
def test_english_picks_blocked_in_a_pure_hindi_urdu_set_and_in_set_pick_survives(sid):
    ev = [e for e in events(sid, "suggest_reject") if e["reason"].startswith("language jump")]
    assert ev
    bad = [pick(e["song"].lstrip(" -")) for e in ev]
    good = pick("Artist Ok - Song In Set")
    chk = _gate(lambda s: "hi" if s["title"] == "Song In Set" else "en")
    kept = _filter_suggestions({"suggestions": bad + [good]}, [], lang_check=chk)
    assert [k["title"] for k in kept] == ["Song In Set"]
    assert _filter_suggestions({"suggestions": bad}, [], lang_check=chk) == []


# ------------------------------------------------------------ same song again / artist spacing

def test_the_playing_song_is_never_suggested_back():
    """164231: the same song sat on both decks. A pick equal to what is playing (in history) is dropped."""
    playing = "Shubh - We Rollin"
    data = {"suggestions": [pick("Shubh - We Rollin (Official Audio)"), pick("Other Artist - Other Song")]}
    kept = _filter_suggestions(data, [playing])
    assert [k["title"] for k in kept] == ["Other Song"]


def test_artist_that_just_played_is_not_picked_again():
    ev = [e for e in events("163310", "suggest_reject") if e["reason"] == "artist played too recently"]
    assert len(ev) >= 3
    recent = ["Shubh - Cheques"]
    picks = [pick(e["song"]) for e in ev] + [pick("Other Artist - Other Song")]
    assert [p["title"] for p in artist_spacing(picks, recent)] == ["Other Song"]


# ------------------------------------------------------------ fixtures hold no atlas / device data

def test_fixtures_are_anonymised():
    for f in FX.glob("*.jsonl"):
        text = f.read_text(encoding="utf-8")
        assert "device" not in text and "graph" not in text and "client" not in text
