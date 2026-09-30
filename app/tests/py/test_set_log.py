import pytest

from app.music_brain.learning.set_log import SCHEMA, export_set_log_markdown, validate_set_log


def sample():
    return {
        "$schema": SCHEMA,
        "metadata": {"created_at": "2026-09-10T02:00:00Z", "duration_seconds": 125.4, "location": "The Loft", "energy": 8},
        "control_event_stream": [{"t": 0.0, "param": "recording-start", "val": "A"}],
    }


def test_validate_minimal_browser_log_and_detaches_data():
    payload = sample()
    validated = validate_set_log(payload)
    assert validated == payload and validated is not payload


def test_rejects_unknown_schema_and_fields():
    with pytest.raises(ValueError):
        validate_set_log({"$schema": "other", "metadata": {}, "control_event_stream": []})
    payload = sample()
    payload["unexpected"] = 1
    with pytest.raises(ValueError):
        validate_set_log(payload)


def test_markdown_is_deterministic_and_contains_obsidian_sections():
    payload = sample()
    first = export_set_log_markdown(payload)
    assert first == export_set_log_markdown(payload)
    assert "type: dj-set-log" in first
    assert "# Set Log: 2026-09-10 — The Loft" in first
    assert "## 2. Complete Tracklist & Key Milestones" in first


def test_full_track_and_transition_export():
    payload = sample()
    payload["track_quest_journey"] = [{"index": 1, "track_id": "a", "title": "Artist — Tune", "deck": "A", "bpm": 128.0, "key": "8A", "energy_score": 7.0, "start_time_in_set": 0.0}]
    payload["transitions"] = [{"transition_index": 1, "outgoing_track": "Artist A", "incoming_track": "Artist B", "recipe_name": "Bass Swap", "mix_start_time": 10.0, "mix_end_time": 20.0}]
    markdown = export_set_log_markdown(payload)
    assert "`[00:00]` Artist — Tune (128 BPM, 8A)" in markdown
    assert "using [[Bass Swap]]" in markdown

