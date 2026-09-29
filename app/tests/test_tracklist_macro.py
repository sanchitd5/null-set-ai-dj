"""learn-set --macros-only (set_import.learn_tracklist_macro): the tracklist alone becomes the
macro set-<set_id>. Song lookup, set metadata and the atlas build are stubbed: no network,
no Demucs, tmp_path only."""
import json

import pytest

from app.music_brain import agent_bridge as ab, macros as mc, set_import as si, set_learner

SID = "abcdefghijk"
TRACKLIST = """0:00 Anyma - Eternity
3:10 Cassian - SOS x Argy - WIND
6:00 ID - ID
9:00 Adam Beyer - ID
12:00 Missing Artist - Nowhere
15:00 Mau P - Drugs From Amsterdam
18:00 Unanalysed - Song
"""
FOUND = {"Anyma - Eternity": b"eternity", "Cassian - SOS": b"sos", "Mau P - Drugs From Amsterdam": b"drugs",
         "Unanalysed - Song": b"unanalysed"}


def _hex(i: int) -> str:
    return f"{i:x}" * 16


@pytest.fixture
def world(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    (cache / "uploads").mkdir(parents=True)
    songs = tmp_path / "songs"
    songs.mkdir()
    monkeypatch.setattr(set_learner, "SETS_DIR", tmp_path / "sets")
    looked = []

    def find(title, download_dir, download=True, exclude_ids=()):
        looked.append(title)
        if title not in FOUND:
            return None
        p = songs / f"{title.replace(' ', '_')}.mp3"
        p.write_bytes(FOUND[title])
        return p

    ids = {}

    def upload(path, name):
        tid = si.content_id(path)
        ids[name] = tid
        return {"track_id": tid}

    monkeypatch.setattr(si, "_post_upload", upload)
    monkeypatch.setattr(si, "_register_offline", upload)
    monkeypatch.setattr("app.ui.dedup_songs.app_is_running", lambda: False, raising=False)

    def build(cache_dir, seed_macros_to=None, log=print, **kw):
        known = [t for n, t in ids.items() if n != "Unanalysed - Song"]
        return {"tracks": {t: {"name": n} for n, t in ids.items() if t in known}, "pairs": {}}

    return {"cache": cache, "find": find, "build": build, "looked": looked, "ids": ids}


def _run(world, text=TRACKLIST, **kw):
    return si.learn_tracklist_macro("https://youtu.be/" + SID, tracklist=text, cache_dir=world["cache"],
                                    build=world["build"], find=world["find"],
                                    info=lambda s: (SID, "Anyma | Live from Atomium", ""), **kw)


def test_order_kept_and_skips_reported(world):
    out = _run(world)
    assert out["set_id"] == SID and out["macro"] == f"set-{SID}" and out["macros"]["error"] is None
    m = mc.load(f"set-{SID}", world["cache"])
    names = [m["steps"][0]["a_name"]] + [s["b_name"] for s in m["steps"]]
    assert names == ["Anyma - Eternity", "Cassian - SOS", "Mau P - Drugs From Amsterdam"], "tracklist order, locked"
    assert out["songs_found"] == 3 and m["source"] == "tracklist"
    assert m["title"] == "Anyma @ Live from Atomium (tracklist, 3 songs)"
    why = {s["title"]: s["why"] for s in out["songs_skipped"]}
    assert "not downloaded" in why["Missing Artist - Nowhere"]
    assert why["Adam Beyer - ID"].startswith("ID")
    assert why["Unanalysed - Song"] == "not in the atlas (no analysis)"
    assert "Adam Beyer - ID" not in world["looked"], "an ID is never searched for"
    assert "Argy - WIND" not in world["looked"], "a layer is noted, not a slot"
    assert "layered with Argy - WIND" in m["note"] and "1 ID rows" in m["note"]


def test_error_without_a_tracklist(world, capsys, monkeypatch):
    with pytest.raises(ValueError, match="tracklist"):
        _run(world, text="just a description, no timestamps")
    monkeypatch.setattr(set_learner, "set_info", lambda s: (SID, "t", ""))
    from app.music_brain import knowledge
    monkeypatch.setattr(knowledge, "export_safe", lambda **kw: {})
    assert ab.main(["learn-set", "https://youtu.be/" + SID, "--macros-only"]) == 1
    assert "tracklist" in json.loads(capsys.readouterr().out)["error"]


def test_name_never_collides_with_the_studied_set(world):
    studied = {"name": f"studied-set-{SID}", "source": "atlas:studied",
               "steps": [{"a": _hex(1), "b": _hex(2), "recipe": "Echo Out"}]}
    mc.write_seed(studied, world["cache"])
    _run(world)
    _run(world)                                          # a re-run rewrites its own macro
    assert mc.load(f"studied-set-{SID}", world["cache"])["source"] == "atlas:studied"
    assert mc.load(f"set-{SID}", world["cache"])["source"] == "tracklist"


def test_a_users_macro_of_that_name_is_never_overwritten(world):
    mc.save({"name": f"set-{SID}", "source": "console",
             "steps": [{"a": _hex(1), "b": _hex(2), "recipe": "Echo Out"}]}, world["cache"])
    out = _run(world)
    assert out["macro"] is None and "the user's" in out["macros"]["error"]
    assert mc.load(f"set-{SID}", world["cache"])["source"] == "console"
