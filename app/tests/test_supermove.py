"""$Up3R-M@SS!V3-M0v3: the shipped variant file, from_song slicing, no user.db, the handback pick, the
strict generator gates, the API, and the JS twin's view of the fixture."""
import json
import sqlite3
from pathlib import Path

import pytest

from app.music_brain import supermove as smv
from app.music_brain.render import mashup_mix as mm
from app.tests.test_mashup_mix import _chain

FIX = smv.VARIANTS_DIR / "v1.json"          # the shipped variant file is the fixture


@pytest.fixture
def variant():
    return json.loads(FIX.read_text())


def test_saved_variant_loads_without_after_earth(variant):
    names = [s["name"] for s in variant["songs"]]
    assert len(names) == 7 and names[0] == "Anyma & Chris Avantgarde - Eternity"
    assert not any("Böhmer" in n or "After Earth" in n for n in names)
    assert variant["variant"] == {"move": smv.NAME, "from": 1, "dropped": ["Ben Böhmer - After Earth"]}
    assert smv.validate(variant) == []
    first = variant["songs"][0]
    assert first["enter_bars"] == 0 and first["out"][0] == 0.0 and first["core_out"][0] == 0.0
    assert not [p for p in first["parts"] if p["role"] == "enter"]          # no lead-in over the dropped song
    assert first["src"][0] == first["core"]["start"]
    # nothing hangs where After Earth was: every section names a kept song, the clock starts on Eternity's core
    idx = {s["i"] for s in variant["songs"]}
    assert variant["sections"][0]["start"] == 0.0 and all(x["i"] in idx for sec in variant["sections"] for x in sec["songs"])
    assert variant["duration"] == pytest.approx(variant["songs"][-1]["out"][1])
    assert variant["shape"]["climax_index"] == 6


def test_every_other_step_exactly_as_rendered(variant):
    """Songs 2..7 keep their parts: the fixture equals the source plan's parts shifted by one core."""
    bar = variant["bar_s"]
    for s in variant["songs"][1:]:
        assert s["core_out"][1] - s["core_out"][0] == pytest.approx(s["core"]["bars"] * bar)
        enter = [p for p in s["parts"] if p["role"] == "enter"]
        assert enter and enter[0]["hp_hz"] == 120.0 and enter[0]["env"][0][0] == pytest.approx(s["out"][0])
    # consecutive songs: the next core starts where this one ends (cores back to back)
    for a, b in zip(variant["songs"], variant["songs"][1:]):
        assert b["core_out"][0] == pytest.approx(a["core_out"][1])


def test_from_song_slices_and_shifts(variant):
    v = smv.from_song(variant, 2)
    assert [s["name"] for s in v["songs"]] == [s["name"] for s in variant["songs"][2:]]
    off = variant["songs"][2]["core_out"][0]
    for s, o in zip(v["songs"][1:], variant["songs"][3:]):
        for p, q in zip(s["parts"], o["parts"]):
            assert p["stem"] == q["stem"] and p["role"] == q["role"]
            for (t1, g1), (t2, g2) in zip(p["env"], q["env"]):
                assert t1 == pytest.approx(t2 - off) and g1 == g2
    assert v["variant"]["from"] == 3 and v["variant"]["dropped"][-1] == variant["songs"][1]["name"]
    with pytest.raises(ValueError):
        smv.from_song(variant, len(variant["songs"]) - 1)
    assert smv.from_song(variant, 0) == variant


def test_shipped_variant_is_the_default_and_portable(variant):
    assert FIX.is_file() and FIX.parent == Path(smv.__file__).parent / "variants"
    got = smv.list_variants()
    assert got[0]["name"] == "v1" and got[0]["n"] == 7 and got[0]["first_half"] == 3
    assert smv.load_variant("v1") == variant
    assert smv.load_variant("v1", start=3)["songs"][0]["name"] == variant["songs"][3]["name"]
    raw = FIX.read_text()
    assert "/Users" not in raw and "/home" not in raw
    assert all(s["stems_dir"].startswith("stems/") for s in variant["songs"])
    with pytest.raises(KeyError):
        smv.load_variant("nope")


def test_save_writes_a_file_only(tmp_path, variant):
    abs_plan = json.loads(json.dumps(variant))
    abs_plan["songs"][0]["stems_dir"] = "/Users/x/data/cache/stems/abc_htdemucs_ft"
    f = smv.save_variant(abs_plan, "v2", d=tmp_path)
    assert f == tmp_path / "v2.json" and sorted(p.name for p in tmp_path.iterdir()) == ["v2.json"]
    assert json.loads(f.read_text())["songs"][0]["stems_dir"] == "stems/abc_htdemucs_ft"
    assert [v["name"] for v in smv.list_variants(tmp_path)] == ["v2"]
    with pytest.raises(ValueError):
        smv.save_variant({"kind": "stem_mashup", "songs": []}, "bad", d=tmp_path)
    bad = json.loads(json.dumps(variant))
    bad["songs"][0]["name"] = "/Users/someone/song.mp3"
    with pytest.raises(ValueError, match="absolute path"):
        smv.save_variant(bad, "v3", d=tmp_path)


def test_no_user_db_for_supermove(monkeypatch, variant):
    """The variants never touch user.db: no code path names it, and loading opens no database."""
    root = Path(smv.__file__).parent
    api = Path(__file__).resolve().parents[1] / "ui" / "services" / "supermove_api.py"
    for f in [*root.glob("*.py"), api]:
        t = f.read_text()
        assert "user.db" not in t and "USER_DB" not in t and "supermove_variants" not in t, f
    opened = []
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: opened.append(a) or (_ for _ in ()).throw(AssertionError("db")))
    smv.load_all(fresh=True)
    assert smv.load_variant("v1", start=1)["songs"][0]["name"] == variant["songs"][1]["name"] and not opened


def _atlas(tmp_path, rows, levels):
    db = sqlite3.connect(tmp_path / "app.db")
    db.execute("CREATE TABLE atlas_tracks (id TEXT PRIMARY KEY, name TEXT, level INTEGER)")
    db.execute("CREATE TABLE atlas_pairs (a TEXT, b TEXT, works REAL, best TEXT, data TEXT, PRIMARY KEY (a, b))")
    for t, lv in levels.items():
        db.execute("INSERT INTO atlas_tracks VALUES (?, ?, ?)", (t, f"song {t}", lv))
    for a, b, works, bass in rows:
        db.execute("INSERT INTO atlas_pairs VALUES (?, ?, ?, 'Long Blend', ?)",
                   (a, b, works, json.dumps({"moves": {"bass_swap": bass}})))
    db.commit()
    db.close()


def test_handback_bass_swap_one_level_lower(tmp_path):
    _atlas(tmp_path, [("L", "a", 90, [1, 70]), ("L", "b", 80, [1, 95]), ("L", "c", 99, [0, 0]), ("L", "d", 95, [1, 99]),
                      ("L", "e", 99, [1, 99])],
           {"L": 10, "a": 9, "b": 9, "c": 9, "d": 8, "e": 10})
    p = smv.handback_pick("L", 10, cache_dir=tmp_path)
    assert p["b"] == "b" and p["level"] == 9 and p["recipe"] == "Bass Swap"          # best Bass Swap at level 9
    assert smv.handback_pick("L", 10, {"b", "a"}, cache_dir=tmp_path)["b"] == "d"     # nearest lower level next
    assert smv.handback_pick("L", 8, cache_dir=tmp_path) is None                       # nothing under 8 swaps
    assert smv.handback_pick("nope", 5, cache_dir=tmp_path) is None


def test_generator_gates_thresholds():
    ok = {"works": 97.0, "best": "Merge → Hold"}
    assert mm.handover_gate(ok) is None
    assert "works" in mm.handover_gate({"works": 84.0, "best": "Long Blend"})
    assert "not a layered" in mm.handover_gate({"works": 99.0, "best": "Echo Out"})
    assert mm.handover_gate(None) == "no atlas row for the pair"
    after_earth = {"level": 9, "genre": "melodic house", "bpm": 123.02}
    eternity = {"level": 7, "genre": "melodic techno", "bpm": 125.0}
    assert "slow to high" in mm.energy_step_gate(after_earth, eternity)                # the owner's complaint
    argy, sunday = {"level": 7, "genre": "melodic techno", "bpm": 124.0}, {"level": 10, "genre": "melodic house", "bpm": 124.0}
    assert mm.energy_step_gate(argy, sunday) is None                                    # "other is all good"
    assert "energy level step" in mm.energy_step_gate({"level": 10, "genre": "x", "bpm": 124}, {"level": 6, "genre": "x", "bpm": 124})


def test_saved_variant_passes_the_generator_gates(variant):
    songs = variant["songs"]
    for a, b in zip(songs, songs[1:]):
        assert mm.energy_step_gate(a, b) is None, (a["name"], b["name"])
        assert a["next"]["works"] >= 80


def test_build_plan_strict_mode():
    ch = _chain(4)
    rows = {(a["id"], b["id"]): {"works": 90.0, "best": "Long Blend"} for a, b in zip(ch, ch[1:])}
    p = mm.build_plan(ch, 120.5, atlas=rows)
    assert [s["next"]["atlas_move"] for s in p["songs"][:-1]] == ["Long Blend"] * 3
    with pytest.raises(ValueError, match="strict: 3 songs"):
        mm.build_plan(_chain(3), 120.5, atlas={})
    bad = dict(rows)
    bad[("s1", "s2")] = {"works": 70.0, "best": "Long Blend"}
    with pytest.raises(ValueError, match="atlas works"):
        mm.build_plan(_chain(4), 120.5, atlas=bad)
    ch = _chain(4)
    ch[2]["genre"], ch[2]["bpm"] = "melodic techno", ch[1]["bpm"] + 2
    ch[3]["genre"] = "melodic techno"
    with pytest.raises(ValueError, match="slow to high"):
        mm.build_plan(ch, 120.5, atlas=rows)
    # default (no atlas): unchanged behaviour
    assert mm.build_plan(_chain(3), 120.5)["songs"][1]["next"]["atlas_move"] == "Bass Swap"


def test_api_lists_and_slices(tmp_path, variant, monkeypatch):
    from app.ui.services import supermove_api as api

    monkeypatch.setattr(api, "VARIANTS_DIR", tmp_path)
    assert api.supermoves() == {"move": smv.NAME, "variants": []}
    smv.save_variant(variant, "v1", d=tmp_path)
    got = api.supermoves()["variants"]
    assert [v["name"] for v in got] == ["v1"] and got[0]["title"] == smv.NAME
    assert api.supermove_plan("v1", start=2)["plan"]["songs"][0]["name"] == variant["songs"][2]["name"]
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        api.supermove_plan("nope")
