"""atlas_bridge.register_jobs / build_graph: the only way the public server reaches the private builder's jobs.
Never raises; the server must start with or without the submodule."""
import sys
import types

import pytest

from app.music_brain import atlas_bridge as ab


def test_without_the_builder_nothing_registers_and_build_graph_says_so(monkeypatch):
    monkeypatch.setattr(ab, "AVAILABLE", False)
    got = []
    assert ab.register_jobs(got.append) is False and got == []
    with pytest.raises(ImportError):
        ab.build_graph()


def test_builder_jobs_module_registers(monkeypatch):
    monkeypatch.setattr(ab, "AVAILABLE", True)
    mod = types.ModuleType("app.music_brain.atlas.jobs")
    mod.register = lambda register: register("a-job")
    monkeypatch.setitem(sys.modules, "app.music_brain.atlas.jobs", mod)
    got = []
    assert ab.register_jobs(got.append) is True and got == ["a-job"]


def test_a_failing_jobs_module_is_swallowed(monkeypatch, capsys):
    monkeypatch.setattr(ab, "AVAILABLE", True)
    mod = types.ModuleType("app.music_brain.atlas.jobs")

    def boom(register):
        raise RuntimeError("bad")

    mod.register = boom
    monkeypatch.setitem(sys.modules, "app.music_brain.atlas.jobs", mod)
    assert ab.register_jobs(lambda s: None) is False
    assert "job registration failed" in capsys.readouterr().out


def test_builder_without_a_jobs_module_is_fine(monkeypatch):
    monkeypatch.setattr(ab, "AVAILABLE", True)
    monkeypatch.setitem(sys.modules, "app.music_brain.atlas.jobs", None)     # import raises ImportError
    assert ab.register_jobs(lambda s: None) is False


def test_register_appliers_follows_the_same_never_raise_rules(monkeypatch, capsys):
    monkeypatch.setattr(ab, "AVAILABLE", False)
    assert ab.register_appliers(lambda t, f: None) is False
    monkeypatch.setattr(ab, "AVAILABLE", True)
    monkeypatch.setitem(sys.modules, "app.music_brain.atlas.sync_apply", None)      # builder without the module
    assert ab.register_appliers(lambda t, f: None) is False
    mod = types.ModuleType("app.music_brain.atlas.sync_apply")
    mod.register = lambda register: register("edge", "fn")
    monkeypatch.setitem(sys.modules, "app.music_brain.atlas.sync_apply", mod)
    got = []
    assert ab.register_appliers(lambda t, f: got.append((t, f))) is True and got == [("edge", "fn")]

    def boom(register):
        raise RuntimeError("bad")

    mod.register = boom
    assert ab.register_appliers(lambda t, f: None) is False
    assert "applier registration failed" in capsys.readouterr().out
