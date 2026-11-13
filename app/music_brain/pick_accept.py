"""G23: the ONE candidate-acceptance function, as a client.

The gates (order, hard / soft, refusal text) are data in dj-config PICK_RULES and are evaluated by dj-core's acceptCandidate
(lib/booking/accept.dart), served by the dj-core service (see djcore_bridge.py). This module holds no rule: it only sends
the candidate and the set state over the bridge. If the service is down the call raises DjCoreUnavailable and logs; there is
no Python fallback.

accept_candidate(candidate, state) -> (ok, reason). reason is None when the candidate may be picked.
accept_many([(candidate, state), ...]) -> [(ok, reason), ...] in one round trip.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from app.music_brain import djcore_bridge


def accept_many(pairs: Iterable[Tuple[dict, dict]]) -> List[Tuple[bool, Optional[str]]]:
    items = [[c or {}, s or {}] for c, s in pairs]
    if not items:
        return []
    return [(bool(ok), why) for ok, why in djcore_bridge.call("accept_batch", {"items": items})]


def accept_candidate(candidate: dict, state: dict) -> Tuple[bool, Optional[str]]:
    return accept_many([(candidate, state)])[0]


def resolve_genre(inp: dict) -> dict:
    """The artist decides the genre (PICK_RULES.genre_resolution): {"genre", "source"}."""
    return djcore_bridge.call("resolve_genre", {"inputs": [inp or {}]})[0]


def genre_distance(pairs: Iterable[Tuple[str, str]]) -> List[Optional[int]]:
    """HF13: steps between each (pinned scene genre, candidate genre) on dj-core's one genre graph (0 = same family, None =
    unlabelled or no path). For RANKING only (closer first); whether a distance is acceptable is accept_candidate's call."""
    ps = [[a or "", b or ""] for a, b in pairs]
    if not ps:
        return []
    return djcore_bridge.call("genre_distance", {"pairs": ps})
