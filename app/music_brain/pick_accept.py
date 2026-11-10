"""G23: the ONE candidate-acceptance function.

The gates (order, hard / soft, parameters, refusal text) are data in dj-config PICK_RULES (generated into
generated/dj_config.py); this file only evaluates them. It is a mechanical port of dj-core lib/booking/accept.dart, and
the vectors in app/tests/fixtures/pick_rules_goldens.json fail the tests when any port drifts. No other code decides
whether a candidate may be picked.

accept_candidate(candidate, state, rules=None, hooks=None) -> (ok, reason). reason is None when the candidate may be
picked; the first refusal in the rules' gate order wins. Hard gates ignore state["relax"].
"""
from __future__ import annotations

import math
import re
from typing import Callable, Optional, Tuple

from app.music_brain.generated import dj_config as _cfg


class PickHooks:
    """The host's genre knowledge: leaves_scene(scene, genre, profile) -> bool, era_gap(a, b) -> Optional[int]."""

    def __init__(self, leaves_scene: Callable[[str, Optional[str], str], bool],
                 era_gap: Callable[[Optional[str], Optional[str]], Optional[int]]):
        self.leaves_scene = leaves_scene
        self.era_gap = era_gap


def _leaves_scene_default(scene: str, genre: Optional[str], profile: str) -> bool:
    from app.music_brain.analysis import scene_profile as sp
    from app.music_brain.analysis.genre import family_jump, genre_families

    if not genre_families(scene):
        return False
    off = not genre_families(genre) or family_jump(scene, genre)
    return bool(off and not (sp.selection_active(profile, scene) and sp.in_scene_pair(scene, genre)))


def _era_gap_default(a, b):
    from app.music_brain.analysis.genre import era_gap

    return era_gap(a, b)


def default_hooks() -> PickHooks:
    return PickHooks(_leaves_scene_default, _era_gap_default)


def _norm(s) -> str:
    return " ".join(str(s or "").lower().split())


def _set(xs) -> set:
    return {_norm(x) for x in (xs or [])}


def _hits(ids, known: set) -> bool:
    return bool(known) and any(i and i in known for i in ids)


def _min(s: float) -> str:
    """'N min': whole minutes, halves rounded up (same in every language)."""
    return f"{math.floor(s / 60 + 0.5)} min"


def _fmt(t: str, v: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: v.get(m.group(1), m.group(0)), t)


def _why(reason, gate: dict) -> str:
    return reason if reason else gate["why_default"]


def accept_candidate(candidate: dict, state: dict, rules: Optional[dict] = None,
                     hooks: Optional[PickHooks] = None) -> Tuple[bool, Optional[str]]:
    r = rules if rules is not None else _cfg.PICK_RULES
    hooks = hooks or default_hooks()
    c, s = candidate or {}, state or {}
    ids = [_norm(i) for i in (c.get("ids") or [])]
    playing, played, vetoed = _set(s.get("playing_ids")), _set(s.get("played")), _set(s.get("vetoed"))
    relax = {str(x) for x in (s.get("relax") or [])}
    limit = s.get("max_length_s")
    limit = float(limit) if limit is not None else float(r["max_track_secs"])
    dur = c.get("duration_s")
    dur = float(dur) if dur is not None else None
    v, lreason = c.get("lang_verdict"), c.get("lang_reason")
    profile = s.get("punjabi_profile") or "off"
    for g in r["gates"]:
        gid = g["id"]
        if g.get("hard") is not True and gid in relax:
            continue
        vars_: dict = {}
        if gid == "playing":
            refuse = _hits(ids, playing)
        elif gid == "played":
            refuse = _hits(ids, played)
        elif gid == "vetoed":
            refuse = _hits(ids, vetoed)
        elif gid == "not_music":
            refuse = c.get("not_music") is True
        elif gid == "length_unknown":
            refuse = dur is not None and dur <= 0
        elif gid == "too_long":
            refuse = dur is not None and dur > 0 and dur >= limit
            if refuse:
                vars_ = {"dur": _min(dur), "limit": _min(limit)}
        elif gid == "language_block":
            refuse = v == "block"
            vars_["why"] = _why(lreason, g)
        elif gid == "language_unverified":
            refuse = v == "unverified" and s.get("language_strict") is True
            vars_["why"] = _why(lreason, g)
        elif gid == "language_penalty":
            refuse = v == "penalty"
            vars_["why"] = _why(lreason, g)
        elif gid == "scene":
            scene = s.get("scene_genre")
            refuse = bool(scene) and bool(hooks.leaves_scene(scene, c.get("genre"), profile))
            vars_ = {"scene": scene or "", "genre": c.get("genre") or g["genre_default"]}
        elif gid == "era_jump":
            gap = hooks.era_gap(s.get("cur_era"), c.get("era"))
            refuse = gap is not None and gap > r["max_era_gap"]
            vars_ = {"era": c.get("era") or "", "cur_era": s.get("cur_era") or ""}
        else:
            raise ValueError(f"pick_rules: unknown gate {gid}")
        if refuse:
            return False, _fmt(g["reason"], vars_)
    return True, None
