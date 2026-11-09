"""The private builder's graph_expand job reaches the job runner only through atlas_bridge.register_jobs.
Without the builder the server registers nothing; with it graph_expand is idle-only, on by default and runs
hourly by cron (owner decision 2026-11-09), and graph_trending runs daily."""
import pytest

from app.music_brain import atlas_bridge
from app.ui.services.job_registry import Registry


def test_without_the_builder_nothing_registers(monkeypatch, tmp_path):
    monkeypatch.setattr(atlas_bridge, "AVAILABLE", False)
    reg = Registry(cache_dir=tmp_path)
    assert atlas_bridge.register_jobs(reg.register) is False
    assert "graph_expand" not in reg._jobs


@pytest.mark.skipif(not atlas_bridge.AVAILABLE, reason=atlas_bridge.SKIP_REASON)
def test_graph_jobs_register_on_cron(tmp_path, monkeypatch):
    monkeypatch.delenv("DJ_GRAPH_EXPAND", raising=False)
    monkeypatch.delenv("DJ_GRAPH_TRENDING", raising=False)
    reg = Registry(cache_dir=tmp_path)
    assert atlas_bridge.register_jobs(reg.register) is True
    expand = reg._jobs["graph_expand"].spec
    assert expand.idle_only and expand.schedule == "0 * * * *"
    assert expand.settings["enabled"].default is True
    trending = reg._jobs["graph_trending"].spec
    assert trending.idle_only and trending.schedule == "20 5 * * *"
