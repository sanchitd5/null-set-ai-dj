"""Validation and deterministic Obsidian export for ``djset-v1`` logs.

The browser recorder emits a small metadata/event log, while the documented
format also permits track and transition journey data.  Both forms are
validated here before they are rendered into the ``DJ/17 - Set Logs`` style.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import math
from typing import Any, Mapping

SCHEMA = "https://pulse.dj/schemas/djset-v1.json"
_TOP_LEVEL = {"$schema", "metadata", "track_quest_journey", "transitions", "control_event_stream"}


def _error(path: str, message: str) -> ValueError:
    return ValueError(f"Invalid djset-v1 {path}: {message}")


def _number(value: Any, path: str, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _error(path, "must be a finite number")
    if integer and not isinstance(value, int):
        raise _error(path, "must be an integer")
    return value


def _text(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise _error(path, "must be a non-empty string")
    return value


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(path, "must be an object")
    return value


def _iso_date(value: Any, path: str) -> str:
    value = _text(value, path)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error(path, "must be an ISO-8601 date/time") from exc
    return value


def validate_set_log(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a detached ``djset-v1`` payload.

    ``ValueError`` is raised for malformed or unrecognized schema data.  The
    returned copy can safely be normalized or rendered by callers.
    """
    root = _object(payload, "root")
    unknown = set(root) - _TOP_LEVEL
    if unknown:
        raise _error("root", f"unrecognized field(s): {', '.join(sorted(unknown))}")
    if root.get("$schema") != SCHEMA:
        raise _error("$schema", f"must equal {SCHEMA!r}")
    metadata = _object(root.get("metadata"), "metadata")
    allowed_metadata = {
        "session_id", "date", "created_at", "duration_seconds", "total_tracks",
        "total_transitions", "harmonic_coherence_score", "energy_arc", "location",
        "audience", "genre", "energy", "recording", "track_a_id", "track_b_id",
    }
    unknown = set(metadata) - allowed_metadata
    if unknown:
        raise _error("metadata", f"unrecognized field(s): {', '.join(sorted(unknown))}")
    date_key = "date" if "date" in metadata else "created_at"
    _iso_date(metadata.get(date_key), f"metadata.{date_key}")
    _number(metadata.get("duration_seconds"), "metadata.duration_seconds")
    if metadata["duration_seconds"] < 0:
        raise _error("metadata.duration_seconds", "must not be negative")
    if "session_id" in metadata:
        _text(metadata["session_id"], "metadata.session_id")
    for key in ("total_tracks", "total_transitions"):
        if key in metadata:
            _number(metadata[key], f"metadata.{key}", integer=True)
            if metadata[key] < 0:
                raise _error(f"metadata.{key}", "must not be negative")
    for key in ("harmonic_coherence_score", "energy"):
        if key in metadata:
            _number(metadata[key], f"metadata.{key}")
    if "energy_arc" in metadata:
        _text(metadata["energy_arc"], "metadata.energy_arc")
    for key in ("location", "audience", "genre", "recording", "track_a_id", "track_b_id"):
        if key in metadata and not isinstance(metadata[key], str):
            raise _error(f"metadata.{key}", "must be a string")

    tracks = root.get("track_quest_journey", [])
    if not isinstance(tracks, list):
        raise _error("track_quest_journey", "must be an array")
    track_fields = {"index", "track_id", "title", "deck", "bpm", "key", "energy_score", "start_time_in_set", "exit_time_in_set", "recommendation_reason"}
    for i, track in enumerate(tracks):
        track = _object(track, f"track_quest_journey[{i}]")
        unknown = set(track) - track_fields
        if unknown:
            raise _error(f"track_quest_journey[{i}]", f"unrecognized field(s): {', '.join(sorted(unknown))}")
        for key in ("index", "track_id", "title", "deck", "bpm", "key", "energy_score", "start_time_in_set"):
            if key not in track:
                raise _error(f"track_quest_journey[{i}]", f"missing {key!r}")
        _number(track["index"], f"track_quest_journey[{i}].index", integer=True)
        for key in ("track_id", "title", "deck", "key"):
            _text(track[key], f"track_quest_journey[{i}].{key}")
        for key in ("bpm", "energy_score", "start_time_in_set", "exit_time_in_set"):
            if key in track:
                _number(track[key], f"track_quest_journey[{i}].{key}")
        if "recommendation_reason" in track:
            _text(track["recommendation_reason"], f"track_quest_journey[{i}].recommendation_reason")

    transitions = root.get("transitions", [])
    if not isinstance(transitions, list):
        raise _error("transitions", "must be an array")
    transition_fields = {"transition_index", "outgoing_track", "incoming_track", "recipe_name", "recipe_category", "mix_start_time", "swap_point_time", "mix_end_time", "bars_length", "performance_type", "steps_log"}
    for i, transition in enumerate(transitions):
        transition = _object(transition, f"transitions[{i}]")
        unknown = set(transition) - transition_fields
        if unknown:
            raise _error(f"transitions[{i}]", f"unrecognized field(s): {', '.join(sorted(unknown))}")
        required = ("transition_index", "outgoing_track", "incoming_track", "recipe_name", "mix_start_time", "mix_end_time")
        for key in required:
            if key not in transition:
                raise _error(f"transitions[{i}]", f"missing {key!r}")
        _number(transition["transition_index"], f"transitions[{i}].transition_index", integer=True)
        for key in ("outgoing_track", "incoming_track", "recipe_name"):
            _text(transition[key], f"transitions[{i}].{key}")
        for key in ("mix_start_time", "swap_point_time", "mix_end_time", "bars_length"):
            if key in transition:
                _number(transition[key], f"transitions[{i}].{key}")
        if "steps_log" in transition:
            if not isinstance(transition["steps_log"], list):
                raise _error(f"transitions[{i}].steps_log", "must be an array")
            for j, step in enumerate(transition["steps_log"]):
                step = _object(step, f"transitions[{i}].steps_log[{j}]")
                if set(step) != {"time", "action"}:
                    raise _error(f"transitions[{i}].steps_log[{j}]", "must contain only time and action")
                _number(step["time"], f"transitions[{i}].steps_log[{j}].time")
                _text(step["action"], f"transitions[{i}].steps_log[{j}].action")

    events = root.get("control_event_stream")
    if not isinstance(events, list):
        raise _error("control_event_stream", "must be an array")
    for i, event in enumerate(events):
        event = _object(event, f"control_event_stream[{i}]")
        if set(event) != {"t", "param", "val"}:
            raise _error(f"control_event_stream[{i}]", "must contain exactly t, param, and val")
        _number(event["t"], f"control_event_stream[{i}].t")
        if event["t"] < 0:
            raise _error(f"control_event_stream[{i}].t", "must not be negative")
        _text(event["param"], f"control_event_stream[{i}].param")
        if isinstance(event["val"], (dict, list)):
            raise _error(f"control_event_stream[{i}].val", "must be a scalar")

    return deepcopy(dict(root))


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def export_set_log_markdown(payload: Mapping[str, Any]) -> str:
    """Return deterministic Obsidian Markdown for a validated set log."""
    log = validate_set_log(payload)
    meta = log["metadata"]
    date = meta.get("date", meta.get("created_at"))
    date_day = date[:10]
    location = meta.get("location", "")
    lines = ["---", "type: dj-set-log", f"date: {date_day}", f"duration: {_clock(meta['duration_seconds'])}", f"location: {location}", f"audience: {meta.get('audience', '')}", f"genre: {meta.get('genre', '')}", f"energy: {meta.get('energy', '')}", f"recording: {meta.get('recording', '')}", "tags:", "  - dj/set-log", "---", "", f"# Set Log: {date_day} — {location}", "", "## Set Metadata", f"* **Date:** {date_day}", f"* **Duration:** {_clock(meta['duration_seconds'])}", f"* **Location / Venue:** {location}", f"* **Audience Profile:** {meta.get('audience', '')}", f"* **Primary Genres:** {meta.get('genre', '')}", f"* **Peak Energy Reached:** {meta.get('energy', '')} / 10", f"* **Audio Recording Link:** {meta.get('recording', '')}", "", "---", "", "## 1. Set Objective & Intended Narrative", "* *What was the artistic and crowd goal before pressing play?*", "* *What energy curve did I plan to execute?*", "", "## 2. Complete Tracklist & Key Milestones"]
    tracks = log.get("track_quest_journey", [])
    if tracks:
        for track in tracks:
            lines.append(f"{track['index']}. `[{_clock(track['start_time_in_set'])}]` {track['title']} ({track['bpm']:g} BPM, {track['key']})")
    else:
        lines.append("*No track journey recorded.*")
    lines += ["", "## 3. Highs & Lows Post-Mortem", "", "### Best Moments (The Peaks)", "* *Which tracks ignited the biggest cheers, sing-alongs, or mosh pits?*", "", "### Worst Moments / Energy Dips (The Friction)", "* *Where did the dancefloor thin out or lose physical momentum?*", "", "## 4. Technical Performance Breakdown", "", "### Best Transitions Executed"]
    transitions = log.get("transitions", [])
    if transitions:
        for transition in transitions:
            lines.append(f"* **Transition {transition['transition_index']}:** {transition['outgoing_track']} → {transition['incoming_track']} using [[{transition['recipe_name']}]].")
    else:
        lines.append("*No transition journey recorded.*")
    lines += ["", "### Failed / Sloppy Transitions", "* *What happened, and how did I recover?*", "", "## 5. Crowd Response & Bio-Feedback Analysis", "* *How did the room react to tempo shifts?*", "", "## 6. Energy Curve Evaluation", "* *Did the set follow the intended wave/three-peak structure?*", "", "## 7. Key Takeaways & What I'll Try Next Time", "* **What I Learned Tonight:** ", "* **Tactical Adjustments for Next Set:** ", "", "## Related Notes", "* [[Set Construction & Architecture]]", "* [[Reading the Room & Crowd Psychology]]", "* [[Energy Management & Dynamics]]", ""]
    return "\n".join(lines)


def to_obsidian_markdown(payload: Mapping[str, Any]) -> str:
    """Alias for :func:`export_set_log_markdown`."""
    return export_set_log_markdown(payload)
