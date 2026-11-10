"""G17 long merges + holds: hold lengths, the recipe mix rule layer, stem level matching, hold safety, set telemetry.

Mirror of dj-core lib/console/hold_policy.dart and app/ui/static/hold-policy.js (same numbers, same functions).
Every number is a dj-config key; the module constants below are checked against the config by dj-config's
check_consumers.py. Pure functions, no I/O.
"""
from __future__ import annotations

import math
from typing import Any, Optional

MERGE_HOLD_MIN_BARS = 32
MERGE_HOLD_MAX_BARS = 64
MERGE_HOLD_TARGET_S = 90
HOLD_ALIGN_BARS = 16
INSONG_HOLD_MIN_BARS = 16
INSONG_HOLD_MAX_BARS = 32
STEM_MERGE_SHARE_MIN = 0.6
ACCENT_MAX_SHARE = 0.2
STEM_MATCH_MAX_BOOST_DB = 12
HOLD_END_ENERGY_DROP = 0.5
INSONG_HOLDS_ON = True
ACCENT_RECIPES = ("Backspin (Spinback)", "Echo Out", "Bass Swap")
STEM_MERGE = "Stem Merge"

_SILENT_BIN, _DROPOUT_FLOOR = 0.002, 0.5
_MERGE_BARS_RANGE = (16, 128)
_INSONG_BARS_RANGE = (8, 64)
_STEMS = ("drums", "bass", "vocals", "other")
_SHARE = {"drums": 0.45, "bass": 0.45, "vocals": 0.35, "other": 0.35}


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def settings(j: Any = None) -> dict:
    """The per-device override (phone Settings "Merges & holds", sent as `hold_settings`). Malformed -> defaults, bounds clamped."""
    o = j if isinstance(j, dict) else {}

    def bars(v, r):
        return int(_clamp(round(v), r[0], r[1])) if _num(v) and v > 0 else 0

    def share(v, d):
        return float(_clamp(v, 0, 1)) if _num(v) else d

    s = {
        "merge_hold_bars": bars(o.get("merge_hold_bars"), _MERGE_BARS_RANGE),
        "insong_hold_bars": bars(o.get("insong_hold_bars"), _INSONG_BARS_RANGE),
        "stem_merge_share": share(o.get("stem_merge_share"), STEM_MERGE_SHARE_MIN),
        "accent_cap": share(o.get("accent_cap"), ACCENT_MAX_SHARE),
        "insong_holds": o["insong_holds"] if isinstance(o.get("insong_holds"), bool) else INSONG_HOLDS_ON,
    }
    s["merge_min"] = s["merge_hold_bars"] or MERGE_HOLD_MIN_BARS
    s["merge_max"] = s["merge_hold_bars"] or MERGE_HOLD_MAX_BARS
    s["insong_min"] = s["insong_hold_bars"] or INSONG_HOLD_MIN_BARS
    s["insong_max"] = s["insong_hold_bars"] or INSONG_HOLD_MAX_BARS
    return s


def merge_hold_bars(bar_s: float, room_bars: float, s: Optional[dict] = None) -> int:
    s = s or settings()
    if not bar_s > 0 or not room_bars >= 8:
        return 0
    want = int(MERGE_HOLD_TARGET_S // bar_s)
    bars = int(_clamp((want // HOLD_ALIGN_BARS) * HOLD_ALIGN_BARS, s["merge_min"], s["merge_max"]))
    cap = int(room_bars // HOLD_ALIGN_BARS) * HOLD_ALIGN_BARS
    if bars <= cap:
        return bars
    return cap if cap >= HOLD_ALIGN_BARS else int(room_bars // 8) * 8


def in_song_hold_bars(section_bars: float, s: Optional[dict] = None) -> int:
    s = s or settings()
    if not s["insong_holds"]:
        return 0
    fit = int(section_bars // 8) * 8
    return 0 if fit < s["insong_min"] else min(s["insong_max"], fit)


def is_accent(recipe: Optional[str]) -> bool:
    return bool(recipe) and recipe in ACCENT_RECIPES


def enforce_recipe_mix(proposed: str, history: list, merge_fits: bool, s: Optional[dict] = None,
                       accent_fallback: str = "Basic Blend") -> tuple:
    """Rule layer over the LLM / matcher pick -> (recipe, why|None). history = recipes so far, oldest first."""
    s = s or settings()
    n = len(history) + 1
    merges = sum(1 for r in history if r == STEM_MERGE)
    accents = sum(1 for r in history if is_accent(r))
    last_accent = bool(history) and is_accent(history[-1])
    if proposed != STEM_MERGE and merge_fits and merges / n < s["stem_merge_share"]:
        return STEM_MERGE, f"merge share {round(100 * merges / n)} % under {round(100 * s['stem_merge_share'])} %"
    if is_accent(proposed) and (last_accent or accents + 1 > math.ceil(s["accent_cap"] * n - 1e-9)):
        why = "accent twice in a row" if last_accent else f"accent cap {round(100 * s['accent_cap'])} %"
        return (STEM_MERGE if merge_fits else accent_fallback), why
    return proposed, None


def _mean(arr, a: int, b: int) -> float:
    if not isinstance(arr, list) or not arr:
        return 0.0
    v = arr[min(a, len(arr)):min(b, len(arr))]
    return sum(v) / len(v) if v else 0.0


def level_match(e_a: dict, e_b: dict, combo: dict, bars: int, max_boost_db: float = STEM_MATCH_MAX_BOOST_DB) -> dict:
    """Lift quiet held stems toward the loudest (relative to typical mix share), at most max_boost_db. -> {eA, eB, trim}."""
    cap = 10 ** (max_boost_db / 20)
    rel = {}
    for n in _STEMS:
        m = _mean((e_a if combo.get(n) == "a" else e_b).get(n), 0, bars)
        if m > 0:
            rel[n] = m / _SHARE[n]
    target = max(rel.values(), default=0.0)
    trim = {n: (_clamp(target / rel[n], 1, cap) if n in rel else 1.0) for n in _STEMS}

    def lift(k: str, arr: list) -> list:
        # a dropout bin (silent while the stem's section is loud) is matched to a fraction of the stem's own level
        floor = rel.get(k, 0.0) * _SHARE[k] * trim[k] * _DROPOUT_FLOOR
        return [floor if v <= _SILENT_BIN else v * trim[k] for v in arr]

    def scaled(src: dict, deck: str) -> dict:
        return {k: (lift(k, src[k]) if k in _STEMS and combo.get(k) == deck and isinstance(src[k], list) else src[k])
                for k in src}

    return {"eA": scaled(e_a, "a"), "eB": scaled(e_b, "b"), "trim": trim}


def hold_end_reason(vocal_clash: bool, energy_now: float, energy_at_start: float, user_action: bool = False) -> Optional[str]:
    if user_action:
        return "user action"
    if vocal_clash:
        return "vocal clash"
    if energy_at_start > 0 and energy_now < energy_at_start * HOLD_END_ENERGY_DROP:
        return "energy drop"
    return None


def hold_telemetry(log: list) -> dict:
    """[{recipe, hold_bars, hold_s, early}] -> merge share, hold count, hold seconds (set log fields)."""
    n = len(log)
    merges = sum(1 for t in log if t.get("recipe") == STEM_MERGE)
    holds = [t for t in log if (t.get("hold_bars") or 0) > 0]
    return {
        "transitions": n,
        "merge_share": round(merges / n, 3) if n else 0.0,
        "hold_count": len(holds),
        "hold_seconds": round(sum(float(t.get("hold_s") or 0) for t in holds), 1),
        "accent_share": round(sum(1 for t in log if is_accent(t.get("recipe"))) / n, 3) if n else 0.0,
        "early_ends": sum(1 for t in holds if t.get("early")),
    }
