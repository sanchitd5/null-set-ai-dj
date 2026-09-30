"""learn-set ends by importing the studied set and writing its macros (set_import.learn_macros).
The learn step, import and atlas build are stubbed: no network, no Demucs, tmp_path only."""
import json

import pytest

from app.music_brain import agent_bridge as ab, set_import as si, set_learner

SID = "S1"
REPORT = {"set_id": SID, "set_path": "/x", "observations": [], "learned": {}}


@pytest.fixture
def learned(monkeypatch):
    from app.music_brain.matching import knowledge

    exported = []
    monkeypatch.setattr(set_learner, "learn_set", lambda source, **kw: dict(REPORT))
    monkeypatch.setattr(knowledge, "export_safe", lambda **kw: exported.append(1) or {"macros": 0})
    return exported


def test_learn_set_ends_with_the_knowledge_export(monkeypatch, learned):
    monkeypatch.setattr(si, "learn_macros", lambda set_id, **kw: {"written": []})
    monkeypatch.setattr(si, "learn_tracklist_macro", lambda source, **kw: {"set_id": SID, "macro": f"set-{SID}"})
    assert ab.learn_set("set.mp3")["knowledge"] == {"macros": 0}
    assert ab.learn_set("set.mp3", macros_only=True)["knowledge"] == {"macros": 0}
    assert learned == [1, 1]


@pytest.fixture
def imported(monkeypatch):
    calls = []
    monkeypatch.setattr(si, "plan", lambda cache_dir, set_id: calls.append(set_id) or
                        [{"position": 1, "title": "A - B", "action": "import", "why": None}])
    monkeypatch.setattr(si, "apply", lambda rows: rows)
    return calls


def _fake_build(seen):
    def build(cache_dir, seed_macros_to=None, log=print, **kw):
        seen.append({"cache_dir": cache_dir, "seed_macros_to": seed_macros_to, **kw})
        # real macro names are slugs (macros.slug lowercases), whatever the set id's case
        return {"seeded": [{"name": n} for n in (f"studied-{SID.lower()}-2", f"studied-set-{SID.lower()}",
                                                 "studied-s10-2", "studied-other-3", "atlas-top-1")]}
    return build


def test_learn_macros_imports_then_builds_incrementally(tmp_path, imported):
    seen = []
    out = si.learn_macros(SID, tmp_path, build=_fake_build(seen))
    assert imported == [SID]
    assert seen == [{"cache_dir": tmp_path, "seed_macros_to": tmp_path}], "incremental: no full=True, no only="
    assert out == {"imported": 1, "skipped": 0, "written": [f"studied-{SID.lower()}-2", f"studied-set-{SID.lower()}"],
                   "error": None}
    assert (tmp_path / "pair_atlas.lock").exists()


def test_learn_macros_never_raises(tmp_path, imported):
    def boom(*a, **kw):
        raise RuntimeError("atlas broke")

    out = si.learn_macros(SID, tmp_path, build=boom)
    assert out["error"] == "RuntimeError: atlas broke" and out["written"] == [] and out["imported"] == 1


def test_learn_set_runs_macros_for_its_set(monkeypatch, learned):
    got = []
    monkeypatch.setattr(si, "learn_macros", lambda set_id, **kw: got.append(set_id) or
                        {"imported": 2, "skipped": 1, "written": [f"studied-set-{SID}"], "error": None})
    rep = ab.learn_set("set.mp3")
    assert got == [SID] and rep["macros"]["written"] == [f"studied-set-{SID}"] and rep["set_id"] == SID


def test_no_macros_skips_the_step(monkeypatch, learned, capsys):
    monkeypatch.setattr(si, "learn_macros", lambda *a, **kw: pytest.fail("--no-macros must skip import + build"))
    assert "macros" not in ab.learn_set("set.mp3", macros=False)
    assert ab.main(["learn-set", "set.mp3", "--no-macros"]) == 0
    assert "macros" not in json.loads(capsys.readouterr().out)


def test_failing_build_keeps_the_learn_result(tmp_path, monkeypatch, learned, imported, capsys):
    from app.music_brain.atlas import pair_atlas

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(pair_atlas, "build", boom)
    monkeypatch.setattr("app.music_brain.config.CACHE_DIR", tmp_path)
    assert ab.main(["learn-set", "set.mp3"]) == 0, "a macro failure never fails the learn"
    rep = json.loads(capsys.readouterr().out)
    assert rep["set_id"] == SID and rep["macros"]["error"] == "OSError: disk full"
