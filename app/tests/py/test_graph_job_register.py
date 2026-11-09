"""The private builder's graph_expand job reaches the job runner only through atlas_bridge.register_jobs.
Without the builder the server registers nothing; with it the job is idle-only and off by default."""
import pytest

from app.music_brain import atlas_bridge
from app.ui.services.job_registry import Registry


def test_without_the_builder_nothing_registers(monkeypatch, tmp_path):
    monkeypatch.setattr(atlas_bridge, "AVAILABLE", False)
    reg = Registry(cache_dir=tmp_path)
    assert atlas_bridge.register_jobs(reg.register) is False
    assert "graph_expand" not in reg._jobs


@pytest.mark.skipif(not atlas_bridge.AVAILABLE, reason=atlas_bridge.SKIP_REASON)
def test_graph_expand_registers_idle_only_and_off(tmp_path, monkeypatch):
    monkeypatch.delenv("DJ_GRAPH_EXPAND", raising=False)
    reg = Registry(cache_dir=tmp_path)
    assert atlas_bridge.register_jobs(reg.register) is True
    spec = reg._jobs["graph_expand"].spec
    assert spec.idle_only and spec.kind == "idle_worker"
    assert reg._jobs["graph_expand"].spec.settings["enabled"].default is False
