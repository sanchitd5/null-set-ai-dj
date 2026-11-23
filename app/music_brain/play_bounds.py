"""Playable window of a song (CLEANLIB): where the real music starts and ends, from the dj-core `song_bounds` rule.

This module holds no rule. It sends the analysis records to the dj-core service (see djcore_bridge.py) and attaches the answer as
`play_bounds` {play_start, play_end, play_duration, intro_end, outro_start} (null = unbounded). The console starts decks at
play_start and plans transitions inside [play_start, play_end]. When the service is unavailable the field is left out and the
console plays the song unbounded (the bridge already logs the outage).
"""
from __future__ import annotations

from typing import List, Optional

from app.music_brain import djcore_bridge


def bounds_for(records: List[dict]) -> List[Optional[dict]]:
    """One service call for the whole list; [None, ...] when dj-core is unavailable."""
    if not records:
        return []
    try:
        out = djcore_bridge.call("song_bounds", {"items": records})
    except (djcore_bridge.DjCoreUnavailable, djcore_bridge.DjCoreError):
        return [None] * len(records)
    return out if isinstance(out, list) and len(out) == len(records) else [None] * len(records)


def attach(payloads: List[Optional[dict]]) -> None:
    """Add play_bounds in place to every non-null analysis payload."""
    live = [p for p in payloads if p]
    for p, b in zip(live, bounds_for(live)):
        if b is not None:
            p["play_bounds"] = b
